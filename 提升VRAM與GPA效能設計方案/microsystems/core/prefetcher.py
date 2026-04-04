"""
Predictive Prefetch Engine -- Optimized v2
===========================================
針對 LLM 推理的逐層（layer-by-layer）執行模式優化。
整合「壓縮感知頻寬估算」與「Pipeline Overlap」機制。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Set, Callable, Any

from .memory_pool import MemoryPool, MemoryTier
from .transfer_engine import TransferEngine, TransferPriority
from .llm_optimizations import SpeculativePrefetchEngine

logger = logging.getLogger(__name__)


class PrefetchStrategy(Enum):
    SEQUENTIAL = "sequential"    # 順序預取（標準 Transformer）
    MOE_ROUTER = "moe_router"   # MoE 路由預測預取
    KV_CACHE = "kv_cache"       # KV Cache 分頁預取
    ADAPTIVE = "adaptive"       # 根據執行歷史自適應


@dataclass
class LayerProfile:
    """單一模型層的記憶資料"""
    layer_id: str
    block_id: str       # 對應 MemoryPool 中的 block_id
    size_bytes: int
    compute_time_ms: float = 0.0   # GPU 運算此層所需的時間
    load_time_ms: float = 0.0      # 從外部載入此層所需的時間
    # ── Compression-Aware Prefetch fields ──
    compression_ratio: float = 1.0  # 觀測到的壓縮率 (logical / physical)
    _ratio_samples: int = 0         # 壓縮率觀測樣本數

    def update_compression_ratio(self, logical: int, physical: int) -> None:
        """用 EWMA 更新此層的壓縮率估計。"""
        if physical <= 0:
            return
        observed = logical / physical
        alpha = 0.3  # EWMA 平滑係數
        if self._ratio_samples == 0:
            self.compression_ratio = observed
        else:
            self.compression_ratio = alpha * observed + (1 - alpha) * self.compression_ratio
        self._ratio_samples += 1

    @property
    def physical_size_bytes(self) -> int:
        """壓縮後的預估物理大小。"""
        if self.compression_ratio <= 0:
            return self.size_bytes
        return max(1, int(self.size_bytes / self.compression_ratio))


@dataclass
class PrefetchPlan:
    """預取計劃"""
    current_layer: int
    prefetch_targets: List[str]    # 要預取的 block_id 列表
    estimated_save_ms: float = 0.0  # 預估可節省的等待時間


@dataclass
class PrefetchStats:
    """預取引擎統計"""
    total_prefetch_requests: int = 0
    prefetch_hits: int = 0        # 預取成功命中
    prefetch_misses: int = 0      # 預取未命中（GPU 等待 I/O）
    total_saved_ms: float = 0.0   # 預取節省的等待時間
    avg_lookahead: float = 0.0
    hit_rate_pct: float = 0.0
    # ── Compression-Aware stats ──
    avg_compression_ratio: float = 1.0  # 全模型平均壓縮率
    bandwidth_utilization_pct: float = 0.0  # I/O 管道利用率


class PredictivePrefetcher:
    """
    優化後的預測性預取引擎。
    """

    def __init__(
        self,
        pool: MemoryPool,
        transfer: TransferEngine,
        lookahead: int = 2,
        strategy: PrefetchStrategy = PrefetchStrategy.SEQUENTIAL,
    ):
        self._pool = pool
        self._transfer = transfer
        self._lookahead = lookahead
        self._strategy = strategy

        self._lock = threading.Lock()
        self._active = False

        self._layers: List[LayerProfile] = []
        self._layer_index: Dict[str, int] = {}
        self._current_layer_idx: int = -1
        self._prefetched: Set[str] = set()
        self._in_flight: Set[str] = set()

        self._compute_history: deque = deque(maxlen=200)
        self._io_history: deque = deque(maxlen=200)

        self._stats = PrefetchStats()
        self._speculative = SpeculativePrefetchEngine(
            base_lookahead=lookahead,
            max_lookahead=8,
        )

        self._prefetch_thread: Optional[threading.Thread] = None
        self._event = threading.Event()

    def register_model_layers(self, layers: List[LayerProfile]) -> None:
        with self._lock:
            self._layers = layers
            self._layer_index = {lp.layer_id: i for i, lp in enumerate(layers)}
            logger.info("Registered %d model layers for prefetching", len(layers))

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_loop,
            name="Prefetcher",
            daemon=True,
        )
        self._prefetch_thread.start()
        logger.info("Prefetcher v2 started: strategy=%s, lookahead=%d",
                     self._strategy.value, self._lookahead)

    def stop(self) -> None:
        self._active = False
        self._event.set()
        if self._prefetch_thread:
            self._prefetch_thread.join(timeout=5.0)

    def notify_layer_start(self, layer_idx: int) -> None:
        with self._lock:
            self._current_layer_idx = layer_idx
        self._event.set()

    def report_transfer_stats(self, layer_idx: int,
                              logical_bytes: int, physical_bytes: int) -> None:
        """
        回報一次實際傳輸的壓縮比。由 TransferEngine 完成傳輸後呼叫。

        這是 Compression-Aware Adaptive Prefetch 的核心 feedback 迴路：
        實測壓縮率 → 更新 LayerProfile → 影響下一輪 lookahead 計算。
        """
        if 0 <= layer_idx < len(self._layers) and physical_bytes > 0:
            self._layers[layer_idx].update_compression_ratio(logical_bytes, physical_bytes)

    def notify_layer_complete(self, layer_idx: int, compute_time_ms: float) -> None:
        if layer_idx < len(self._layers):
            self._layers[layer_idx].compute_time_ms = compute_time_ms
            self._compute_history.append(compute_time_ms)

        # 檢查下一層是否已在快速層級（hit/miss 追蹤）
        next_idx = layer_idx + 1
        if next_idx < len(self._layers):
            block_id = self._layers[next_idx].block_id
            blk = self._pool.get_block(block_id)
            if blk and blk.tier <= MemoryTier.RAM:
                self._stats.prefetch_hits += 1
            else:
                self._stats.prefetch_misses += 1

            total = self._stats.prefetch_hits + self._stats.prefetch_misses
            if total > 0:
                self._stats.hit_rate_pct = (self._stats.prefetch_hits / total) * 100

            # Speculative verification: 從 hit/miss 學習以調整信心度與 lookahead
            self._speculative.verify_prediction(
                list(self._prefetched), block_id
            )

    def create_prefetch_plan(self, current_layer: int) -> PrefetchPlan:
        """根據當前策略建立預取計畫（策略分流）。"""
        effective_bw = self._transfer.stats.avg_throughput_mbs
        if effective_bw <= 0:
            effective_bw = 800.0

        lookahead = self._compute_effective_lookahead(effective_bw)

        if self._strategy == PrefetchStrategy.SEQUENTIAL:
            targets, save_ms = self._plan_sequential(current_layer, lookahead, effective_bw)
        elif self._strategy == PrefetchStrategy.MOE_ROUTER:
            targets, save_ms = self._plan_moe(current_layer, lookahead, effective_bw)
        elif self._strategy == PrefetchStrategy.KV_CACHE:
            targets, save_ms = self._plan_kv_cache(current_layer, lookahead, effective_bw)
        elif self._strategy == PrefetchStrategy.ADAPTIVE:
            targets, save_ms = self._plan_adaptive(current_layer, effective_bw)
        else:
            targets, save_ms = self._plan_sequential(current_layer, lookahead, effective_bw)

        return PrefetchPlan(current_layer, targets, save_ms)

    # ── Compression-Aware Adaptive Prefetch (CAAP) Algorithm ─────────
    #
    # 核心觀察：不同 layer 的壓縮率差異極大（Attention ~4-6x, FFN ~2-3x,
    # Embedding ~1.5x）。固定 lookahead 對高壓縮層浪費時間窗口，
    # 對低壓縮層又不足。
    #
    # 形式化：
    #   給定 bandwidth B (MB/s)，從 layer current+1 開始，
    #   尋找最大 L 使得：
    #
    #     Σ(i=1..L) T_load(i) ≤ Σ(i=1..L) T_compute(i)
    #
    #   其中 T_load(i) = physical_size(i) / B
    #               = (logical_size(i) / compression_ratio(i)) / B
    #         T_compute(i) = 觀測到的 GPU 計算時間
    #
    #   即：I/O 前綴和 ≤ Compute 前綴和 時，I/O 可被完全掩蓋。
    #
    # 與傳統 prefetch 的差異：
    #   - 傳統：L = ceil(avg_load / avg_compute) — 全局平均，忽略層間差異
    #   - CAAP：per-layer prefix sum — 精確考慮每層的壓縮率和計算時間
    #
    # Feedback loop：
    #   TransferEngine 完成傳輸 → report_transfer_stats(layer, logical, physical)
    #   → LayerProfile.update_compression_ratio (EWMA α=0.3)
    #   → 下一輪 _compute_effective_lookahead 使用更準的壓縮率
    #   → lookahead 自適應收斂

    def _compute_effective_lookahead(self, effective_bw: float,
                                     from_layer: int = -1) -> int:
        """
        Compression-Aware Adaptive Prefetch (CAAP) 核心演算法。

        使用 per-layer 壓縮率做前綴和比較，找到最大 lookahead L
        使得所有預取 I/O 可被 GPU compute 完全掩蓋。

        Args:
            effective_bw: 有效頻寬 (MB/s)，已含裝置實測
            from_layer: 起算位置，-1 表示用 _current_layer_idx

        Returns:
            lookahead depth (clamped to [base, 8])
        """
        if from_layer < 0:
            from_layer = self._current_layer_idx
        if from_layer < 0 or not self._layers:
            return self._lookahead

        # ── Phase 1: Per-layer prefix sum (CAAP core) ──
        cumulative_io_ms = 0.0
        cumulative_compute_ms = 0.0
        caap_lookahead = 0

        for offset in range(1, min(len(self._layers) - from_layer, 9)):
            idx = from_layer + offset
            if idx >= len(self._layers):
                break

            layer = self._layers[idx]

            # Physical load time（壓縮感知）
            physical_mb = layer.physical_size_bytes / (1024 * 1024)
            load_ms = (physical_mb / effective_bw) * 1000
            cumulative_io_ms += load_ms

            # Compute time（用觀測值或全局平均）
            if layer.compute_time_ms > 0:
                comp_ms = layer.compute_time_ms
            elif len(self._compute_history) > 0:
                comp_ms = sum(self._compute_history) / len(self._compute_history)
            else:
                comp_ms = load_ms  # 無資料時假設 compute = load（保守）
            cumulative_compute_ms += comp_ms

            # I/O 前綴和 ≤ Compute 前綴和 → 可以再多預取一層
            if cumulative_io_ms <= cumulative_compute_ms:
                caap_lookahead = offset
            else:
                # I/O 超過 compute → 再多看一層作為 buffer，然後停止
                caap_lookahead = offset
                break

        # ── Phase 2: 結合 Speculative 信心度 ──
        spec_lookahead = self._speculative.current_lookahead

        # ── Phase 3: 取各訊號的最大值 ──
        final = max(caap_lookahead, spec_lookahead, self._lookahead)
        final = min(final, 8)  # 硬上限

        # ── 更新統計 ──
        self._stats.avg_lookahead = float(final)
        if self._layers:
            ratios = [lp.compression_ratio for lp in self._layers if lp._ratio_samples > 0]
            if ratios:
                self._stats.avg_compression_ratio = sum(ratios) / len(ratios)
            if cumulative_compute_ms > 0:
                self._stats.bandwidth_utilization_pct = min(
                    100.0, (cumulative_io_ms / cumulative_compute_ms) * 100
                )

        return final

    # ── Strategy: Sequential ────────────────────────────────────────

    def _plan_sequential(self, current: int, lookahead: int,
                         effective_bw: float) -> tuple:
        """順序預取：載入接下來 N 層，使用 per-layer 壓縮率估算物理傳輸時間。"""
        targets = []
        save_ms = 0.0

        for offset in range(1, lookahead + 1):
            idx = current + offset
            if idx >= len(self._layers):
                break

            layer = self._layers[idx]
            blk = self._pool.get_block(layer.block_id)

            if blk and blk.tier == MemoryTier.EXTERNAL:
                if layer.block_id not in self._in_flight:
                    # 壓縮感知：用 physical_size_bytes 而非 size_bytes
                    physical_mb = layer.physical_size_bytes / (1024 * 1024)
                    load_time = (physical_mb / effective_bw) * 1000
                    targets.append(layer.block_id)
                    save_ms += load_time

        return targets, save_ms

    # ── Strategy: MoE Router ────────────────────────────────────────

    def _plan_moe(self, current: int, lookahead: int,
                  effective_bw: float) -> tuple:
        """
        MoE 預取：用 speculative engine 的 transition matrix 預測
        最可能被激活的 expert 分支，再以 sequential 補齊剩餘空位。
        """
        targets = []
        save_ms = 0.0

        if current < len(self._layers):
            current_block = self._layers[current].block_id
            available = [lp.block_id for lp in self._layers[current + 1:]]
            predicted = self._speculative.predict_next(current_block, available)

            for bid in predicted[:lookahead]:
                blk = self._pool.get_block(bid)
                if blk and blk.tier == MemoryTier.EXTERNAL and bid not in self._in_flight:
                    targets.append(bid)
                    save_ms += (blk.size_bytes / (1024 * 1024)) / effective_bw * 1000

        # Sequential 補齊：speculative 尚無足夠歷史時確保不空轉
        if len(targets) < lookahead:
            seq_targets, seq_save = self._plan_sequential(current, lookahead, effective_bw)
            for bid in seq_targets:
                if bid not in targets and len(targets) < lookahead:
                    targets.append(bid)
                    save_ms += (self._pool.get_block(bid).size_bytes / (1024 * 1024)) / effective_bw * 1000

        return targets, save_ms

    # ── Strategy: KV Cache ──────────────────────────────────────────

    def _plan_kv_cache(self, current: int, lookahead: int,
                       effective_bw: float) -> tuple:
        """
        KV Cache 預取：除了順序預取前方層，
        還回撈已被 evict 到 EXTERNAL 的歷史 KV 區塊。

        Attention 層的存取模式是「回頭讀先前層的 KV」，
        因此向前看 + 向後撈，雙向覆蓋。
        """
        # 前半 lookahead：順序預取前方層
        forward_slots = max(1, lookahead // 2)
        targets, save_ms = self._plan_sequential(current, forward_slots, effective_bw)

        # 後半 lookahead：掃描已通過的層中仍在 EXTERNAL 的區塊
        backward_slots = lookahead - len(targets)
        if backward_slots > 0:
            for idx in range(current - 1, -1, -1):
                if backward_slots <= 0:
                    break
                layer = self._layers[idx]
                blk = self._pool.get_block(layer.block_id)
                if (blk and blk.tier == MemoryTier.EXTERNAL
                        and layer.block_id not in self._in_flight
                        and layer.block_id not in targets):
                    physical_mb = layer.physical_size_bytes / (1024 * 1024)
                    load_time = (physical_mb / effective_bw) * 1000
                    targets.append(layer.block_id)
                    save_ms += load_time
                    backward_slots -= 1

        return targets, save_ms

    # ── Strategy: Adaptive（智慧分流）──────────────────────────────

    def _plan_adaptive(self, current: int, effective_bw: float) -> tuple:
        """
        自適應策略 — 智慧分流。

        1. record_access：持續餵入 speculative engine 建立 transition matrix
        2. predict_next：取得 speculative 預測（捕捉 MoE 分支/重複模式）
        3. sequential 補齊：確保線性路徑的基本覆蓋
        4. 合併去重：speculative ∪ sequential 取聯集
        """
        lookahead = self._compute_effective_lookahead(effective_bw)

        # 1. 記錄存取 — 讓 speculative engine 持續學習
        if current < len(self._layers):
            self._speculative.record_access(self._layers[current].block_id)

        # 2. Speculative 預測
        spec_targets = []
        if current < len(self._layers):
            current_block = self._layers[current].block_id
            available = [lp.block_id for lp in self._layers[current + 1:]]
            predicted = self._speculative.predict_next(current_block, available)

            for bid in predicted[:lookahead]:
                blk = self._pool.get_block(bid)
                if blk and blk.tier == MemoryTier.EXTERNAL and bid not in self._in_flight:
                    spec_targets.append(bid)

        # 3. Sequential 基底
        seq_targets, _ = self._plan_sequential(current, lookahead, effective_bw)

        # 4. 合併：speculative 優先（學到的模式更準），sequential 補齊
        merged = list(spec_targets)
        for bid in seq_targets:
            if bid not in merged and len(merged) < lookahead:
                merged.append(bid)

        save_ms = 0.0
        for bid in merged:
            # 用 LayerProfile 的壓縮率估算物理傳輸時間
            layer_match = next((lp for lp in self._layers if lp.block_id == bid), None)
            if layer_match:
                physical_mb = layer_match.physical_size_bytes / (1024 * 1024)
            else:
                blk = self._pool.get_block(bid)
                physical_mb = (blk.size_bytes / (1024 * 1024)) if blk else 0
            save_ms += (physical_mb / effective_bw) * 1000

        return merged, save_ms

    def _prefetch_loop(self) -> None:
        while self._active:
            self._event.wait(timeout=0.1)
            self._event.clear()

            if not self._active:
                break

            current = self._current_layer_idx
            if current < 0 or not self._layers:
                continue

            plan = self.create_prefetch_plan(current)

            for block_id in plan.prefetch_targets:
                if block_id in self._prefetched or block_id in self._in_flight:
                    continue

                blk = self._pool.get_block(block_id)
                if blk is None:
                    continue

                self._in_flight.add(block_id)
                self._stats.total_prefetch_requests += 1

                def on_complete(rid: str, success: bool, bid=block_id):
                    self._in_flight.discard(bid)
                    if success:
                        self._prefetched.add(bid)
                        # 在 Pool 中將層級提升至 RAM
                        self._pool.promote(bid, MemoryTier.RAM)

                self._transfer.submit_migration(
                    block_id=block_id,
                    src=blk.tier,
                    dst=MemoryTier.RAM,
                    size_bytes=blk.size_bytes,
                    priority=TransferPriority.HIGH,
                    callback=on_complete,
                )

    @property
    def stats(self) -> PrefetchStats:
        return self._stats

    def reset_for_new_inference(self) -> None:
        with self._lock:
            self._current_layer_idx = -1
            self._prefetched.clear()
            self._in_flight.clear()
