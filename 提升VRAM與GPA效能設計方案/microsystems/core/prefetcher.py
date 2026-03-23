"""
Predictive Prefetch Engine
===========================
針對 LLM 推理的逐層（layer-by-layer）執行模式，
在 GPU 處理第 N 層時，提前將第 N+1、N+2 層的權重
從外部儲存非同步傳輸至 VRAM 或 RAM。

管線化預取（Pipelined Prefetching）策略：

  GPU 運算:  [Layer N    ] [Layer N+1  ] [Layer N+2  ] ...
  預取 I/O:  [Load N+1   ] [Load N+2   ] [Load N+3   ] ...
                ^-- 與 GPU 運算重疊，掩蓋 I/O 延遲

對於 MoE (Mixture of Experts) 模型，預取引擎還支援
根據 router 的預測結果，提前載入可能被激活的 expert 權重。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Set, Callable, Any

from .memory_pool import MemoryPool, MemoryTier
from .transfer_engine import TransferEngine, TransferPriority

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


class PredictivePrefetcher:
    """
    預測性預取引擎。

    監控 GPU 的層級執行進度，提前將即將被需要的資料
    從慢速儲存搬移到快速層級。
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

        # 模型層級註冊表
        self._layers: List[LayerProfile] = []
        self._layer_index: Dict[str, int] = {}  # layer_id → index

        # 運行狀態
        self._current_layer_idx: int = -1
        self._prefetched: Set[str] = set()      # 已預取的 block_id
        self._in_flight: Set[str] = set()        # 正在傳輸中的

        # 執行歷史（用於自適應策略）
        self._compute_history: deque = deque(maxlen=200)
        self._io_history: deque = deque(maxlen=200)

        # 統計
        self._stats = PrefetchStats()

        # 背景預取執行緒
        self._prefetch_thread: Optional[threading.Thread] = None
        self._event = threading.Event()

    def register_model_layers(self, layers: List[LayerProfile]) -> None:
        """
        註冊模型的層級結構。
        預取引擎需要知道模型有哪些層，以及每層對應的記憶體區塊。
        """
        with self._lock:
            self._layers = layers
            self._layer_index = {lp.layer_id: i for i, lp in enumerate(layers)}
            logger.info("Registered %d model layers for prefetching", len(layers))

    def start(self) -> None:
        """啟動背景預取執行緒"""
        if self._active:
            return
        self._active = True
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_loop,
            name="Prefetcher",
            daemon=True,
        )
        self._prefetch_thread.start()
        logger.info("Prefetcher started: strategy=%s, lookahead=%d",
                     self._strategy.value, self._lookahead)

    def stop(self) -> None:
        """停止預取引擎"""
        self._active = False
        self._event.set()
        if self._prefetch_thread:
            self._prefetch_thread.join(timeout=5.0)
        logger.info("Prefetcher stopped")

    def notify_layer_start(self, layer_idx: int) -> None:
        """
        通知預取引擎：GPU 開始計算第 layer_idx 層。
        這會觸發對後續層的預取。
        """
        with self._lock:
            self._current_layer_idx = layer_idx
        self._event.set()

    def notify_layer_complete(self, layer_idx: int, compute_time_ms: float) -> None:
        """通知預取引擎：GPU 完成第 layer_idx 層的計算"""
        if layer_idx < len(self._layers):
            self._layers[layer_idx].compute_time_ms = compute_time_ms
            self._compute_history.append(compute_time_ms)

        # 檢查下一層是否已在快速層級
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

    def create_prefetch_plan(self, current_layer: int) -> PrefetchPlan:
        """
        根據當前執行位置建立預取計畫。
        """
        targets = []
        estimated_save = 0.0

        if self._strategy == PrefetchStrategy.SEQUENTIAL:
            targets, estimated_save = self._plan_sequential(current_layer)
        elif self._strategy == PrefetchStrategy.MOE_ROUTER:
            targets, estimated_save = self._plan_moe(current_layer)
        elif self._strategy == PrefetchStrategy.KV_CACHE:
            targets, estimated_save = self._plan_kv_cache(current_layer)
        elif self._strategy == PrefetchStrategy.ADAPTIVE:
            targets, estimated_save = self._plan_adaptive(current_layer)

        return PrefetchPlan(
            current_layer=current_layer,
            prefetch_targets=targets,
            estimated_save_ms=estimated_save,
        )

    @property
    def stats(self) -> PrefetchStats:
        return self._stats

    # ── Strategy Implementations ──

    def _plan_sequential(self, current: int) -> tuple:
        """順序預取：載入接下來 N 層"""
        targets = []
        save_ms = 0.0

        for offset in range(1, self._lookahead + 1):
            idx = current + offset
            if idx >= len(self._layers):
                break

            layer = self._layers[idx]
            blk = self._pool.get_block(layer.block_id)

            # 只預取不在 VRAM/RAM 中的區塊
            if blk and blk.tier == MemoryTier.EXTERNAL:
                if layer.block_id not in self._in_flight:
                    targets.append(layer.block_id)
                    save_ms += layer.load_time_ms

        return targets, save_ms

    def _plan_moe(self, current: int) -> tuple:
        """
        MoE 預取：預取最可能被激活的 expert 層。
        在真實場景中，會根據 router logits 預測。
        此處簡化為預取所有可能的 expert。
        """
        # 簡化實作：與 sequential 相同，但標記 MoE expert 層
        return self._plan_sequential(current)

    def _plan_kv_cache(self, current: int) -> tuple:
        """
        KV Cache 預取：載入即將被 attention 層存取的 KV 頁面。
        KV Cache 的存取模式是「寫入一次，讀取多次」，
        因此預取策略著重在熱門 token 範圍的 KV 頁面。
        """
        return self._plan_sequential(current)

    def _plan_adaptive(self, current: int) -> tuple:
        """
        自適應策略：根據歷史 compute_time 與 io_time 的比率
        動態調整 lookahead 深度。

        若 GPU 計算很快（compute < io），增加 lookahead
        若 GPU 計算較慢（compute > io），減少 lookahead
        """
        if len(self._compute_history) > 5:
            avg_compute = sum(self._compute_history) / len(self._compute_history)
            avg_io = sum(self._io_history) / len(self._io_history) if self._io_history else avg_compute

            if avg_io > 0:
                ratio = avg_compute / avg_io
                if ratio < 0.5:
                    self._lookahead = min(self._lookahead + 1, 8)
                elif ratio > 2.0:
                    self._lookahead = max(self._lookahead - 1, 1)

            self._stats.avg_lookahead = self._lookahead

        return self._plan_sequential(current)

    # ── Background Loop ──

    def _prefetch_loop(self) -> None:
        """背景預取執行緒主迴圈"""
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

                # 提交預取傳輸請求
                self._in_flight.add(block_id)
                self._stats.total_prefetch_requests += 1

                def on_complete(rid: str, success: bool, bid=block_id):
                    self._in_flight.discard(bid)
                    if success:
                        self._prefetched.add(bid)
                        # 在 pool 中執行 promotion
                        self._pool.promote(bid, MemoryTier.RAM)

                self._transfer.submit_migration(
                    block_id=block_id,
                    src=blk.tier,
                    dst=MemoryTier.RAM,
                    size_bytes=blk.size_bytes,
                    priority=TransferPriority.HIGH,
                    callback=on_complete,
                )

    def reset_for_new_inference(self) -> None:
        """重置預取狀態（新的推理回合開始時呼叫）"""
        with self._lock:
            self._current_layer_idx = -1
            self._prefetched.clear()
            self._in_flight.clear()
