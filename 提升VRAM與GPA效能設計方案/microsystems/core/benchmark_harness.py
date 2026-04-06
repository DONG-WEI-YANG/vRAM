"""
P0 Benchmark Harness — Scaled Trace-Driven LLM Inference Simulation
====================================================================
用 1/16 (8B) 或 1/32 (70B) 縮比模擬 LLM 推理的記憶體存取模式，
驅動真實的 MemoryPool、TransferEngine、PredictivePrefetcher 程式碼路徑。

目標指標（供論文使用）：
  - tokens/sec (模擬)
  - time-to-first-token (ms)
  - prefetch hit rate (%)
  - per-layer compression ratio
  - effective / physical bandwidth (MB/s)
  - eviction count

用法::

    python -m microsystems.core.benchmark_harness [--model 8b] [--config caap]

或全部跑::

    python -m microsystems.core.benchmark_harness --all
"""

from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .memory_pool import Block, MemoryPool, MemoryTier
from .transfer_engine import TransferEngine
from .prefetcher import (
    LayerProfile,
    PredictivePrefetcher,
    PrefetchStrategy,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Layer 1: Model Profile Generator
# ═══════════════════════════════════════════════════════════════

MB = 1024 * 1024
GB = 1024 * MB


class ModelScale(Enum):
    LLAMA3_8B = "llama3_8b"
    LLAMA3_70B = "llama3_70b"
    MIXTRAL_8X7B = "mixtral_8x7b"


@dataclass
class ModelSpec:
    """模型規格 — 真實值 + 縮比後的值"""
    name: str
    num_layers: int
    real_layer_size_bytes: int      # 真實每層大小
    scale_factor: int               # 縮比倍率
    scaled_layer_size_bytes: int    # 縮比後每層大小

    # 不同層類型的壓縮率（基於 transformer 結構相似性��
    attention_compression: float    # Q/K/V/O projections — 結構高度重複
    ffn_compression: float          # FFN up/down — 較獨特
    embedding_compression: float    # Embedding / LM head — vocabulary 隨機性高

    # 模擬 GPU compute time (ms per layer)
    compute_time_range: Tuple[float, float]  # (min, max)

    # 記憶體容量設定 (縮比後)
    vram_capacity_bytes: int        # 模擬 GPU VRAM (只能放幾層)
    ram_capacity_bytes: int
    external_capacity_bytes: int

    # MoE 參數
    is_moe: bool = False
    num_experts: int = 1            # 每層的 expert 數量
    active_experts: int = 1         # 每個 token 啟動的 expert 數量
    # MoE 層中每個 expert 的大小 = scaled_layer_size_bytes / num_experts

    # 頻寬模擬
    bandwidth_mbs: float = 200.0    # 外部裝置頻寬 (MB/s)

    @property
    def total_model_bytes(self) -> int:
        return self.num_layers * self.scaled_layer_size_bytes

    @property
    def total_real_bytes(self) -> int:
        return self.num_layers * self.real_layer_size_bytes


# 兩個模型的完整規格
MODEL_SPECS: Dict[ModelScale, ModelSpec] = {
    ModelScale.LLAMA3_8B: ModelSpec(
        name="Llama-3-8B",
        num_layers=32,
        real_layer_size_bytes=500 * MB,     # ~16GB / 32 layers
        scale_factor=16,
        scaled_layer_size_bytes=31 * MB,    # 500MB / 16

        attention_compression=4.2,
        ffn_compression=2.3,
        embedding_compression=1.5,

        compute_time_range=(5.0, 15.0),     # ms

        # 模擬 8GB VRAM: 縮比後只能放 ~8 層
        vram_capacity_bytes=256 * MB,
        ram_capacity_bytes=512 * MB,
        external_capacity_bytes=2 * GB,
    ),
    ModelScale.LLAMA3_70B: ModelSpec(
        name="Llama-3-70B",
        num_layers=80,
        real_layer_size_bytes=int(1.75 * GB),  # ~140GB / 80 layers
        scale_factor=32,
        scaled_layer_size_bytes=55 * MB,       # 1.75GB / 32

        attention_compression=4.5,
        ffn_compression=2.5,
        embedding_compression=1.5,

        compute_time_range=(20.0, 50.0),    # ms

        # 模擬 8GB VRAM: 縮比後只能放 ~4 層
        vram_capacity_bytes=256 * MB,
        ram_capacity_bytes=1 * GB,
        external_capacity_bytes=8 * GB,
    ),
    ModelScale.MIXTRAL_8X7B: ModelSpec(
        name="Mixtral-8x7B",
        num_layers=32,                          # 32 transformer blocks
        real_layer_size_bytes=int(1.4 * GB),    # 每層含 8 experts ~1.4GB
        scale_factor=16,
        scaled_layer_size_bytes=88 * MB,        # 1.4GB / 16

        # MoE: Attention 壓縮率高 (共享)，Expert FFN 壓縮率變異大
        attention_compression=4.0,
        ffn_compression=2.0,                    # Expert FFN 較獨特，壓縮率較低
        embedding_compression=1.5,

        compute_time_range=(8.0, 20.0),         # ms

        vram_capacity_bytes=256 * MB,
        ram_capacity_bytes=1 * GB,
        external_capacity_bytes=4 * GB,

        # MoE 專有：每層 8 experts，每 token 啟動 2 個
        is_moe=True,
        num_experts=8,
        active_experts=2,
        bandwidth_mbs=200.0,
    ),
}


def _layer_compression_ratio(layer_idx: int, num_layers: int, spec: ModelSpec) -> float:
    """
    根據層的位置決定壓縮率。

    Transformer 結構:
      - Layer 0: Embedding (壓縮率低)
      - Layer 1..N-2: Alternating Attention / FFN blocks
      - Layer N-1: LM Head / Final Norm (壓縮率低)
    """
    if layer_idx == 0 or layer_idx == num_layers - 1:
        return spec.embedding_compression
    # 偶數層 = Attention, 奇數層 = FFN（簡化模擬）
    if layer_idx % 2 == 0:
        return spec.attention_compression
    return spec.ffn_compression


def _layer_compute_time_ms(layer_idx: int, num_layers: int, spec: ModelSpec) -> float:
    """
    模擬每層的 GPU compute time。

    Attention 層通常比 FFN 層快（矩陣較小），
    這裡用線性插值模擬從淺到深的計算量遞增。
    """
    lo, hi = spec.compute_time_range
    # 淺層較快，深層較慢
    ratio = layer_idx / max(1, num_layers - 1)
    base = lo + (hi - lo) * ratio
    # Attention 層 -20%, FFN +10%
    if layer_idx % 2 == 0:
        base *= 0.8
    else:
        base *= 1.1
    return round(base, 2)


def generate_layer_profiles(spec: ModelSpec) -> List[LayerProfile]:
    """
    產生模型的 LayerProfile 列表。

    對於 MoE 模型，每個 transformer block 拆成：
      - 1 個 attention block (共享)
      - N 個 expert FFN blocks (各自獨立)
    """
    profiles = []
    if not spec.is_moe:
        for i in range(spec.num_layers):
            cr = _layer_compression_ratio(i, spec.num_layers, spec)
            ct = _layer_compute_time_ms(i, spec.num_layers, spec)
            lp = LayerProfile(
                layer_id=f"layer_{i}",
                block_id=f"block_{i}",
                size_bytes=spec.scaled_layer_size_bytes,
                compute_time_ms=ct,
                compression_ratio=cr,
            )
            lp._ratio_samples = 1
            profiles.append(lp)
    else:
        # MoE: 每個 block = attention (共享) + N experts (各自獨立)
        attn_size = spec.scaled_layer_size_bytes // 4         # attention ~25%
        expert_size = (spec.scaled_layer_size_bytes * 3 // 4) // spec.num_experts  # FFN 75% / N

        for i in range(spec.num_layers):
            if i == 0 or i == spec.num_layers - 1:
                # Embedding / LM Head: 單一 block
                lp = LayerProfile(
                    layer_id=f"layer_{i}",
                    block_id=f"block_{i}_embed",
                    size_bytes=spec.scaled_layer_size_bytes,
                    compute_time_ms=_layer_compute_time_ms(i, spec.num_layers, spec),
                    compression_ratio=spec.embedding_compression,
                )
                lp._ratio_samples = 1
                profiles.append(lp)
                continue

            # Attention block (每個 token 都用)
            lp_attn = LayerProfile(
                layer_id=f"layer_{i}_attn",
                block_id=f"block_{i}_attn",
                size_bytes=attn_size,
                compute_time_ms=_layer_compute_time_ms(i, spec.num_layers, spec) * 0.3,
                compression_ratio=spec.attention_compression,
            )
            lp_attn._ratio_samples = 1
            profiles.append(lp_attn)

            # Expert FFN blocks (每個 token 只用 active_experts 個)
            for e in range(spec.num_experts):
                # 不同 expert 壓縮率不同：模擬真實的 expert 特化
                # expert 0-1: 通用 expert (壓縮率較高，被路由較多)
                # expert 6-7: 特化 expert (壓縮率較低，被路由較少)
                cr_variation = spec.ffn_compression * (1.0 + 0.15 * (e - spec.num_experts / 2))
                cr_variation = max(1.2, cr_variation)

                lp_exp = LayerProfile(
                    layer_id=f"layer_{i}_expert_{e}",
                    block_id=f"block_{i}_expert_{e}",
                    size_bytes=expert_size,
                    compute_time_ms=_layer_compute_time_ms(i, spec.num_layers, spec) * 0.7 / spec.active_experts,
                    compression_ratio=cr_variation,
                )
                lp_exp._ratio_samples = 1
                profiles.append(lp_exp)

    return profiles


# ═══════════════════════════════════════════════════════════════
# Layer 2: Inference Simulator
# ═══════════════════════════════════════════════════════════════

class BenchmarkConfig(Enum):
    """5 種對比組態"""
    NO_PREFETCH = "no_prefetch"
    FIXED_LOOKAHEAD_4 = "fixed_lookahead_4"
    CAAP = "caap"
    CAAP_NO_COMPRESS = "caap_no_compress"
    UNIFIED_MEMORY = "unified_memory"


@dataclass
class BenchmarkMetrics:
    """單次 benchmark 的完整結果"""
    model_name: str = ""
    config_name: str = ""
    scale_factor: int = 1

    # 核心指標
    tokens_per_sec: float = 0.0
    time_to_first_token_ms: float = 0.0
    total_inference_time_ms: float = 0.0
    num_tokens_simulated: int = 0

    # CAAP 特有指標
    prefetch_hit_rate_pct: float = 0.0
    avg_lookahead: float = 0.0
    per_layer_compression_ratios: List[float] = field(default_factory=list)

    # I/O 指標
    effective_bandwidth_mbs: float = 0.0
    physical_bandwidth_mbs: float = 0.0
    total_logical_io_bytes: int = 0
    total_physical_io_bytes: int = 0
    total_io_wait_ms: float = 0.0   # GPU 等待 I/O 的總時間 (miss penalty)
    eviction_count: int = 0

    # 記憶體指標
    peak_vram_used_bytes: int = 0
    peak_ram_used_bytes: int = 0
    peak_external_used_bytes: int = 0


def _available_ram_gb() -> float:
    """取得系統可用記憶體 (GB)"""
    try:
        if platform.system() == "Windows":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            mem_status = ctypes.c_ulonglong()
            kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(mem_status))
            total = mem_status.value * 1024  # KB → bytes
            # 粗略估算可用 = 總量的 50%
            return (total / GB) * 0.5
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / (1024 * 1024)  # KB → GB
    except Exception:
        pass
    return 8.0  # 保守預設


def run_single_benchmark(
    model: ModelScale,
    config: BenchmarkConfig,
    num_tokens: int = 32,
    work_dir: Optional[str] = None,
) -> BenchmarkMetrics:
    """
    執行單一 benchmark 配��。

    模擬 `num_tokens` 個 token 的推理過程：
    每個 token = 一次完整的 forward pass (遍歷所有層)。

    Args:
        model: 模型規格
        config: 測試配置
        num_tokens: 模擬的 token 數量
        work_dir: mmap swap file 的工作目錄 (None = tempdir)
    """
    spec = MODEL_SPECS[model]
    metrics = BenchmarkMetrics(
        model_name=spec.name,
        config_name=config.value,
        scale_factor=spec.scale_factor,
        num_tokens_simulated=num_tokens,
    )

    logger.info("═══ Benchmark: %s × %s ═══", spec.name, config.value)

    # ── 建立記憶體池 ──
    external_bw = 200.0  # 模擬 SD Express / USB SSD: 200 MB/s
    pool = MemoryPool(
        vram_capacity_bytes=spec.vram_capacity_bytes,
        ram_capacity_bytes=spec.ram_capacity_bytes,
        external_capacity_bytes=spec.external_capacity_bytes,
        vram_bandwidth_mbs=300_000.0,   # GPU VRAM
        ram_bandwidth_mbs=25_000.0,     # DDR4
        external_bandwidth_mbs=external_bw,
    )

    # ── 建立 Transfer Engine (僅用於 prefetcher 初始化，不實際傳輸) ──
    enable_compression = config not in (
        BenchmarkConfig.CAAP_NO_COMPRESS,
        BenchmarkConfig.NO_PREFETCH,
    )
    transfer = TransferEngine(
        max_workers=1,
        bandwidth_limit_mbs=external_bw,
        enable_compression=enable_compression,
    )
    # 不啟動 worker threads — benchmark 用同步模擬

    # ── 產生 Layer Profiles ──
    profiles = generate_layer_profiles(spec)

    # ── 建立 Prefetcher ──
    prefetcher: Optional[PredictivePrefetcher] = None

    if config == BenchmarkConfig.NO_PREFETCH:
        pass  # 無 prefetch

    elif config == BenchmarkConfig.FIXED_LOOKAHEAD_4:
        prefetcher = PredictivePrefetcher(
            pool=pool, transfer=transfer,
            lookahead=4,
            strategy=PrefetchStrategy.SEQUENTIAL,
        )

    elif config in (BenchmarkConfig.CAAP, BenchmarkConfig.CAAP_NO_COMPRESS):
        prefetcher = PredictivePrefetcher(
            pool=pool, transfer=transfer,
            lookahead=2,
            strategy=PrefetchStrategy.ADAPTIVE,
        )

    elif config == BenchmarkConfig.UNIFIED_MEMORY:
        # Unified Memory 模擬：全部放 RAM，按需 fault（高延遲）
        pool = MemoryPool(
            vram_capacity_bytes=0,
            ram_capacity_bytes=spec.total_model_bytes + 256 * MB,
            external_capacity_bytes=0,
            ram_bandwidth_mbs=10_000.0,  # Unified Memory 有效頻寬比 native 低
        )

    if prefetcher:
        prefetcher.register_model_layers(profiles)
        # 不啟動背景 thread — 我們用同步 create_prefetch_plan() 驅動

    # ── 配置模型區塊到記憶體池 ──
    for i, lp in enumerate(profiles):
        # 初始：所有層在 EXTERNAL（模擬 "模型在 SD 卡上"）
        if config == BenchmarkConfig.UNIFIED_MEMORY:
            target_tier = MemoryTier.RAM
        else:
            target_tier = MemoryTier.EXTERNAL
        pool.allocate(
            block_id=lp.block_id,
            size_bytes=lp.size_bytes,
            preferred_tier=target_tier,
            tag=lp.layer_id,
        )

    # ── 模擬推理 ──
    import random as _rng
    total_logical_io = 0
    total_physical_io = 0
    total_io_wait_ms = 0.0   # GPU 等待 I/O 的總時間 (miss penalty)
    evictions = 0
    layer_hit_count = 0
    layer_miss_count = 0
    peak_vram = 0
    peak_ram = 0

    # 建立 block_id → profile index 查詢表
    _bid_to_idx: Dict[str, int] = {p.block_id: i for i, p in enumerate(profiles)}

    overall_start = time.perf_counter()
    first_token_done = False
    first_token_time = 0.0

    def _ensure_vram_space(needed: int) -> int:
        """驅逐 VRAM 中最冷的 block 以騰出空間，回傳驅逐次數"""
        count = 0
        max_evictions = 16
        while pool.get_tier_state(MemoryTier.VRAM).free_bytes < needed and count < max_evictions:
            vram_blocks = pool.get_blocks_in_tier(MemoryTier.VRAM)
            unpinned = [b for b in vram_blocks if not b.is_pinned]
            if not unpinned:
                break
            victim = min(unpinned, key=lambda b: b.last_access_ts)
            if not pool.demote(victim.block_id, MemoryTier.RAM):
                break
            count += 1
        return count

    def _io_wait_ms(physical_bytes: int) -> float:
        """計算 demand fetch 的 I/O 等待時間 (ms)"""
        bw = spec.bandwidth_mbs if hasattr(spec, 'bandwidth_mbs') else 200.0
        return (physical_bytes / MB / bw) * 1000.0

    def _access_block(bid: str, layer_profile_idx: int) -> None:
        """存取一個 block: 檢查 hit/miss，執行 demand fetch 並計入 I/O penalty"""
        nonlocal layer_hit_count, layer_miss_count, evictions
        nonlocal total_logical_io, total_physical_io, total_io_wait_ms

        lp = profiles[layer_profile_idx]
        blk = pool.get_block(bid)
        if not blk:
            return

        is_fast = blk.tier <= MemoryTier.RAM

        if is_fast:
            layer_hit_count += 1
            pool.access(bid)
            # RAM→VRAM promotion: skip for MoE experts (they change every token)
            if blk.tier == MemoryTier.RAM and config != BenchmarkConfig.UNIFIED_MEMORY:
                if "expert_" not in bid:
                    evictions += _ensure_vram_space(lp.size_bytes)
                    pool.promote(bid, MemoryTier.VRAM)
        else:
            layer_miss_count += 1
            if config != BenchmarkConfig.UNIFIED_MEMORY:
                evictions += _ensure_vram_space(lp.size_bytes)
                promoted = pool.promote(bid, MemoryTier.VRAM)
                if promoted:
                    physical = int(lp.size_bytes / lp.compression_ratio) if enable_compression else lp.size_bytes
                    total_logical_io += lp.size_bytes
                    total_physical_io += physical
                    # ★ I/O penalty: GPU 必須等這次 demand fetch 完成
                    total_io_wait_ms += _io_wait_ms(physical)
                    if prefetcher:
                        prefetcher.report_transfer_stats(layer_profile_idx, lp.size_bytes, physical)
                pool.access(bid)

    # ── MoE expert routing: 預先決定每個 token 每層啟動哪些 expert ──
    def _select_experts(token_idx: int, layer_idx: int) -> List[int]:
        """模擬 MoE router: 選擇 active_experts 個 expert"""
        if not spec.is_moe:
            return []
        # 使用確定性 seed 以保證可重現
        _rng.seed(token_idx * 1000 + layer_idx)
        # 真實 MoE 有 load balancing loss，分佈接近均勻但有偏好
        # expert 0-1 被選中的機率較高 (模擬 "通用 expert")
        weights = [1.5 if e < 2 else 1.0 for e in range(spec.num_experts)]
        total_w = sum(weights)
        probs = [w / total_w for w in weights]
        # 加權抽樣（不重複）
        selected = []
        remaining = list(range(spec.num_experts))
        rem_probs = list(probs)
        for _ in range(spec.active_experts):
            if not remaining:
                break
            r = _rng.random() * sum(rem_probs)
            cumulative = 0.0
            for j, (idx, p) in enumerate(zip(remaining, rem_probs)):
                cumulative += p
                if r <= cumulative:
                    selected.append(idx)
                    remaining.pop(j)
                    rem_probs.pop(j)
                    break
        return selected

    for token_idx in range(num_tokens):
        token_start = time.perf_counter()

        # 決定每層要存取哪些 profile
        if spec.is_moe:
            # MoE: profiles 是 [embed, attn, expert0..7, attn, expert0..7, ..., embed]
            # 需要遍歷每個 transformer block，只存取被路由到的 expert
            profile_cursor = 0
            transformer_block = 0

            while profile_cursor < len(profiles):
                lp = profiles[profile_cursor]

                if "embed" in lp.block_id:
                    # Embedding / LM Head: 全部存取
                    if prefetcher:
                        prefetcher.notify_layer_start(profile_cursor)
                        plan = prefetcher.create_prefetch_plan(profile_cursor)
                        for tbid in plan.prefetch_targets:
                            tblk = pool.get_block(tbid)
                            if tblk and tblk.tier == MemoryTier.EXTERNAL:
                                if pool.promote(tbid, MemoryTier.RAM):
                                    ti = _bid_to_idx.get(tbid, -1)
                                    if ti >= 0:
                                        tlp = profiles[ti]
                                        phys = int(tlp.size_bytes / tlp.compression_ratio) if enable_compression else tlp.size_bytes
                                        total_logical_io += tlp.size_bytes
                                        total_physical_io += phys
                                        prefetcher.report_transfer_stats(ti, tlp.size_bytes, phys)

                    _access_block(lp.block_id, profile_cursor)
                    time.sleep(lp.compute_time_ms / 10000.0)
                    if prefetcher:
                        prefetcher.notify_layer_complete(profile_cursor, lp.compute_time_ms)
                    profile_cursor += 1

                elif "attn" in lp.block_id:
                    # Attention block: 全部存取
                    if prefetcher:
                        prefetcher.notify_layer_start(profile_cursor)

                    _access_block(lp.block_id, profile_cursor)
                    time.sleep(lp.compute_time_ms / 10000.0)
                    if prefetcher:
                        prefetcher.notify_layer_complete(profile_cursor, lp.compute_time_ms)
                    profile_cursor += 1

                    # Expert routing: 決定此 token 此層啟動哪些 expert
                    selected = _select_experts(token_idx, transformer_block)

                    # ★ MoE-Aware Prefetch (CAAP 專屬)
                    # 三層策略：
                    #   1. 當前層的 predicted experts (routing temporal locality)
                    #   2. 下一層的 attention block (必定需要)
                    #   3. 下一層的 predicted experts (speculative)
                    if prefetcher:
                        # 從 attention block ID 提取層號
                        layer_num = int(lp.block_id.split("_")[1])

                        def _prefetch_bid(bid: str) -> None:
                            nonlocal total_logical_io, total_physical_io
                            tblk = pool.get_block(bid)
                            if tblk and tblk.tier == MemoryTier.EXTERNAL:
                                if pool.promote(bid, MemoryTier.RAM):
                                    ti = _bid_to_idx.get(bid, -1)
                                    if ti >= 0:
                                        tlp = profiles[ti]
                                        phys = int(tlp.size_bytes / tlp.compression_ratio) if enable_compression else tlp.size_bytes
                                        total_logical_io += tlp.size_bytes
                                        total_physical_io += phys
                                        prefetcher.report_transfer_stats(ti, tlp.size_bytes, phys)

                        # 1. 當前層 expert: 用前 token 路由預測
                        if token_idx > 0:
                            predicted = _select_experts(token_idx - 1, transformer_block)
                            for pe in predicted:
                                _prefetch_bid(f"block_{layer_num}_expert_{pe}")

                        # 2. 下一層 attention (必定需要，成本低)
                        next_layer = layer_num + 1
                        if next_layer < spec.num_layers - 1:
                            _prefetch_bid(f"block_{next_layer}_attn")

                    # Expert blocks: 只存取被路由的
                    for e in range(spec.num_experts):
                        expert_profile_idx = profile_cursor + e
                        if expert_profile_idx < len(profiles) and e in selected:
                            _access_block(profiles[expert_profile_idx].block_id, expert_profile_idx)
                            time.sleep(profiles[expert_profile_idx].compute_time_ms / 10000.0)

                    profile_cursor += spec.num_experts
                    transformer_block += 1
                else:
                    profile_cursor += 1

        else:
            # 標準 dense model: 順序存取所有層
            for layer_idx in range(len(profiles)):
                lp = profiles[layer_idx]

                # ── 同步 Prefetch ──
                if prefetcher:
                    prefetcher.notify_layer_start(layer_idx)
                    plan = prefetcher.create_prefetch_plan(layer_idx)
                    for target_bid in plan.prefetch_targets:
                        tblk = pool.get_block(target_bid)
                        if tblk and tblk.tier == MemoryTier.EXTERNAL:
                            if pool.promote(target_bid, MemoryTier.RAM):
                                tidx = _bid_to_idx.get(target_bid, -1)
                                if tidx >= 0:
                                    tlp = profiles[tidx]
                                    phys = int(tlp.size_bytes / tlp.compression_ratio) if enable_compression else tlp.size_bytes
                                    total_logical_io += tlp.size_bytes
                                    total_physical_io += phys
                                    prefetcher.report_transfer_stats(tidx, tlp.size_bytes, phys)

                _access_block(lp.block_id, layer_idx)

                compute_ms = lp.compute_time_ms
                time.sleep(compute_ms / 10000.0)

                if prefetcher:
                    prefetcher.notify_layer_complete(layer_idx, compute_ms)

        # 追蹤峰值
        vs = pool.get_tier_state(MemoryTier.VRAM)
        rs = pool.get_tier_state(MemoryTier.RAM)
        if vs:
            peak_vram = max(peak_vram, vs.used_bytes)
        if rs:
            peak_ram = max(peak_ram, rs.used_bytes)

        # Token 完成
        token_end = time.perf_counter()
        if not first_token_done:
            first_token_time = (token_end - overall_start) * 1000
            first_token_done = True

    overall_end = time.perf_counter()
    total_time_ms = (overall_end - overall_start) * 1000

    # ── 清理 ──

    # ── 收集指標 ──
    # I/O-aware inference time: 真實時間 + I/O 等待懲罰
    # (真實 sleep 用 1/10 速度，但 I/O wait 是精確模擬的)
    io_aware_time_ms = total_time_ms + total_io_wait_ms
    metrics.total_inference_time_ms = round(io_aware_time_ms, 1)
    metrics.time_to_first_token_ms = round(first_token_time, 1)
    metrics.tokens_per_sec = round(num_tokens / (io_aware_time_ms / 1000), 2) if io_aware_time_ms > 0 else 0
    metrics.total_io_wait_ms = round(total_io_wait_ms, 1)

    total_layer_accesses = layer_hit_count + layer_miss_count
    metrics.prefetch_hit_rate_pct = round(
        (layer_hit_count / max(1, total_layer_accesses)) * 100, 1,
    )

    if prefetcher:
        pstats = prefetcher.stats
        metrics.avg_lookahead = round(pstats.avg_lookahead, 1)

    metrics.per_layer_compression_ratios = [
        round(lp.compression_ratio, 2) for lp in profiles
    ]

    metrics.total_logical_io_bytes = total_logical_io
    metrics.total_physical_io_bytes = total_physical_io
    if total_time_ms > 0:
        metrics.effective_bandwidth_mbs = round(
            (total_logical_io / MB) / (total_time_ms / 1000), 1,
        )
        metrics.physical_bandwidth_mbs = round(
            (total_physical_io / MB) / (total_time_ms / 1000), 1,
        )

    metrics.eviction_count = evictions
    metrics.peak_vram_used_bytes = peak_vram
    metrics.peak_ram_used_bytes = peak_ram

    logger.info(
        "  Result: %.1f tok/s, TTFT=%.0fms, hit=%.1f%%, evict=%d",
        metrics.tokens_per_sec, metrics.time_to_first_token_ms,
        metrics.prefetch_hit_rate_pct, evictions,
    )

    return metrics


# ═══════════════════════════════════════════════════════════════
# Layer 3: Metrics Collector + Reporter
# ═══════════════════════════════════════════════════════════════

def run_full_benchmark(
    num_tokens: int = 32,
    output_dir: Optional[str] = None,
) -> List[BenchmarkMetrics]:
    """
    執行全部 10 組 benchmark (5 configs × 2 models)。

    自動跳過 70B 如果可用 RAM 不足。
    結果寫入 JSON + CSV。
    """
    if output_dir is None:
        output_dir = os.path.join(
            os.path.dirname(__file__), "..", "benchmark_results",
        )
    os.makedirs(output_dir, exist_ok=True)

    all_results: List[BenchmarkMetrics] = []
    avail_gb = _available_ram_gb()
    logger.info("Available RAM: ~%.1f GB", avail_gb)

    models_to_run = [ModelScale.LLAMA3_8B, ModelScale.MIXTRAL_8X7B]
    if avail_gb >= 6.0:
        models_to_run.append(ModelScale.LLAMA3_70B)
    else:
        logger.warning("Skipping 70B: need ~6GB free RAM, have %.1fGB", avail_gb)

    configs = list(BenchmarkConfig)

    for model in models_to_run:
        for config in configs:
            try:
                result = run_single_benchmark(
                    model=model,
                    config=config,
                    num_tokens=num_tokens,
                )
                all_results.append(result)
            except Exception as e:
                logger.error("FAILED: %s × %s: %s", model.value, config.value, e)

    # ── 寫入 JSON ──
    ts = time.strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(output_dir, f"benchmark_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            [asdict(r) for r in all_results],
            f, indent=2, ensure_ascii=False,
        )
    logger.info("Results written to %s", json_path)

    # ── 寫入 CSV (方便畫圖) ──
    csv_path = os.path.join(output_dir, f"benchmark_{ts}.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        headers = [
            "model", "config", "tokens_per_sec", "ttft_ms",
            "hit_rate_pct", "avg_lookahead", "eff_bw_mbs",
            "phys_bw_mbs", "evictions", "logical_io_mb", "physical_io_mb",
        ]
        f.write(",".join(headers) + "\n")
        for r in all_results:
            row = [
                r.model_name, r.config_name,
                f"{r.tokens_per_sec:.2f}", f"{r.time_to_first_token_ms:.1f}",
                f"{r.prefetch_hit_rate_pct:.1f}", f"{r.avg_lookahead:.1f}",
                f"{r.effective_bandwidth_mbs:.1f}", f"{r.physical_bandwidth_mbs:.1f}",
                str(r.eviction_count),
                f"{r.total_logical_io_bytes / MB:.1f}",
                f"{r.total_physical_io_bytes / MB:.1f}",
            ]
            f.write(",".join(row) + "\n")
    logger.info("CSV written to %s", csv_path)

    # ── 印出摘要表 ──
    _print_summary(all_results)

    return all_results


def _print_summary(results: List[BenchmarkMetrics]) -> None:
    """印出易讀的摘要表"""
    if not results:
        return

    print("\n" + "═" * 80)
    print("  BENCHMARK RESULTS")
    print("═" * 80)

    # 按模型分組
    models = {}
    for r in results:
        models.setdefault(r.model_name, []).append(r)

    for model_name, runs in models.items():
        print(f"\n  Model: {model_name} (scale 1/{runs[0].scale_factor})")
        print(f"  {'Config':<22} {'tok/s':>8} {'TTFT':>8} {'Hit%':>7} "
              f"{'LA':>4} {'IO Wait':>9} {'Evict':>6}")
        print("  " + "─" * 68)

        for r in runs:
            print(f"  {r.config_name:<22} {r.tokens_per_sec:>8.2f} "
                  f"{r.time_to_first_token_ms:>7.0f}ms "
                  f"{r.prefetch_hit_rate_pct:>6.1f}% "
                  f"{r.avg_lookahead:>4.1f} "
                  f"{r.total_io_wait_ms:>7.1f}ms "
                  f"{r.eviction_count:>6d}")

    print("\n" + "═" * 80)


# ═══════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="vRAM Benchmark Harness")
    parser.add_argument("--model", choices=["8b", "70b", "all"], default="all")
    parser.add_argument("--config", choices=[c.value for c in BenchmarkConfig] + ["all"], default="all")
    parser.add_argument("--tokens", type=int, default=32, help="Number of tokens to simulate")
    parser.add_argument("--output-dir", type=str, default=None)
    args = parser.parse_args()

    if args.model == "all" and args.config == "all":
        run_full_benchmark(num_tokens=args.tokens, output_dir=args.output_dir)
    else:
        model = {
            "8b": ModelScale.LLAMA3_8B,
            "70b": ModelScale.LLAMA3_70B,
        }.get(args.model)
        config = None
        for c in BenchmarkConfig:
            if c.value == args.config:
                config = c
                break

        if model and config:
            result = run_single_benchmark(model=model, config=config, num_tokens=args.tokens)
            print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
        elif args.model == "all" and config:
            for m in ModelScale:
                r = run_single_benchmark(model=m, config=config, num_tokens=args.tokens)
                print(json.dumps(asdict(r), indent=2, ensure_ascii=False))
        elif model and args.config == "all":
            results = []
            for c in BenchmarkConfig:
                results.append(run_single_benchmark(model=model, config=c, num_tokens=args.tokens))
            _print_summary(results)


if __name__ == "__main__":
    main()
