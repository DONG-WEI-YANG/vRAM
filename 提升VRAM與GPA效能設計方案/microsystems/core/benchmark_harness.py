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
    """產生模型的 LayerProfile 列表"""
    profiles = []
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
        # 預填壓縮率 (模擬已經觀察到真實值)
        lp._ratio_samples = 1
        profiles.append(lp)
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
    total_logical_io = 0
    total_physical_io = 0
    evictions = 0
    layer_hit_count = 0
    layer_miss_count = 0
    peak_vram = 0
    peak_ram = 0

    overall_start = time.perf_counter()
    first_token_done = False
    first_token_time = 0.0

    def _ensure_vram_space(needed: int) -> int:
        """驅逐 VRAM 中最冷的 block 以騰出空間，回傳驅逐次數"""
        count = 0
        max_evictions = 16  # 安全上限，避免無限迴圈
        while pool.get_tier_state(MemoryTier.VRAM).free_bytes < needed and count < max_evictions:
            vram_blocks = pool.get_blocks_in_tier(MemoryTier.VRAM)
            unpinned = [b for b in vram_blocks if not b.is_pinned]
            if not unpinned:
                break
            victim = min(unpinned, key=lambda b: b.last_access_ts)
            if not pool.demote(victim.block_id, MemoryTier.RAM):
                break  # demote 失敗 (e.g., RAM 也滿了)
            count += 1
        return count

    for token_idx in range(num_tokens):
        token_start = time.perf_counter()

        for layer_idx in range(spec.num_layers):
            lp = profiles[layer_idx]

            # ── 同步 Prefetch: 在處理當前層之前，執行 prefetch plan ──
            # Prefetch 將 EXTERNAL → RAM（預熱），demand fetch 將 RAM → VRAM
            if prefetcher:
                prefetcher.notify_layer_start(layer_idx)
                plan = prefetcher.create_prefetch_plan(layer_idx)
                for target_bid in plan.prefetch_targets:
                    tblk = pool.get_block(target_bid)
                    if tblk and tblk.tier == MemoryTier.EXTERNAL:
                        if pool.promote(target_bid, MemoryTier.RAM):
                            tidx = next(
                                (j for j, p in enumerate(profiles) if p.block_id == target_bid),
                                -1,
                            )
                            if tidx >= 0:
                                tlp = profiles[tidx]
                                phys = int(tlp.size_bytes / tlp.compression_ratio) if enable_compression else tlp.size_bytes
                                total_logical_io += tlp.size_bytes
                                total_physical_io += phys
                                prefetcher.report_transfer_stats(tidx, tlp.size_bytes, phys)

            # ── 檢查此層是否已在快速層 (VRAM 或 RAM) ──
            blk = pool.get_block(lp.block_id)
            is_fast = blk and blk.tier <= MemoryTier.RAM  # VRAM=0, RAM=1

            if is_fast:
                layer_hit_count += 1
                pool.access(lp.block_id)
                # 如果在 RAM 但不在 VRAM，promote 到 VRAM (快速，幾乎不計 I/O)
                if blk.tier == MemoryTier.RAM and config != BenchmarkConfig.UNIFIED_MEMORY:
                    evictions += _ensure_vram_space(lp.size_bytes)
                    pool.promote(lp.block_id, MemoryTier.VRAM)
            else:
                layer_miss_count += 1
                # Demand fetch: EXTERNAL → VRAM (慢，計入 I/O)
                if blk and config != BenchmarkConfig.UNIFIED_MEMORY:
                    evictions += _ensure_vram_space(lp.size_bytes)
                    promoted = pool.promote(lp.block_id, MemoryTier.VRAM)
                    if promoted:
                        physical = int(lp.size_bytes / lp.compression_ratio) if enable_compression else lp.size_bytes
                        total_logical_io += lp.size_bytes
                        total_physical_io += physical
                        if prefetcher:
                            prefetcher.report_transfer_stats(layer_idx, lp.size_bytes, physical)
                    pool.access(lp.block_id)

            # 模擬 GPU compute
            # 用 1/10 的實際 sleep 以加速 benchmark，
            # 但記錄完整的 compute_time_ms 以正確計算 CAAP lookahead
            compute_ms = lp.compute_time_ms
            time.sleep(compute_ms / 10000.0)  # 1/10 speed sleep

            # 通知 prefetcher 完成
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
    metrics.total_inference_time_ms = round(total_time_ms, 1)
    metrics.time_to_first_token_ms = round(first_token_time, 1)
    metrics.tokens_per_sec = round(num_tokens / (total_time_ms / 1000), 2) if total_time_ms > 0 else 0

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

    models_to_run = [ModelScale.LLAMA3_8B]
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
              f"{'LA':>4} {'Eff BW':>9} {'Evict':>6}")
        print("  " + "─" * 68)

        for r in runs:
            print(f"  {r.config_name:<22} {r.tokens_per_sec:>8.2f} "
                  f"{r.time_to_first_token_ms:>7.0f}ms "
                  f"{r.prefetch_hit_rate_pct:>6.1f}% "
                  f"{r.avg_lookahead:>4.1f} "
                  f"{r.effective_bandwidth_mbs:>7.1f}MB "
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
