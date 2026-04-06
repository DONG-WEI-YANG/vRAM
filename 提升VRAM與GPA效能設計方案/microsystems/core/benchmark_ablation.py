"""
P3 Ablation Study + Paper Figure Data
======================================
拆解 CAAP 各組件的貢獻，產出論文用的 CSV 數據。

Figure 1: Per-layer compression ratio heterogeneity
Figure 2: Ablation — tokens/sec breakdown by component
Figure 3: I/O wait time comparison across configs
Table 1:  Full benchmark results (all models × all configs)

用法::

    python -m microsystems.core.benchmark_ablation
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from dataclasses import asdict
from typing import List

from .benchmark_harness import (
    BenchmarkConfig,
    BenchmarkMetrics,
    ModelScale,
    ModelSpec,
    MODEL_SPECS,
    generate_layer_profiles,
    run_single_benchmark,
    _print_summary,
    MB,
    GB,
)

logger = logging.getLogger(__name__)


def generate_figure1_compression_ratios(output_dir: str) -> None:
    """
    Figure 1: Per-layer compression ratio heterogeneity.

    每個模型一個 CSV, 列出每層的:
      - layer_id
      - layer_type (embedding / attention / ffn / expert)
      - compression_ratio
      - physical_size_bytes (scaled)
      - logical_size_bytes (scaled)

    這張圖是論文的核心論據: 證明 LLM 不同層的壓縮率異質性
    """
    for model_scale, spec in MODEL_SPECS.items():
        profiles = generate_layer_profiles(spec)

        csv_path = os.path.join(output_dir, f"fig1_compression_{model_scale.value}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "layer_id", "block_id", "layer_type",
                "compression_ratio", "logical_size_mb", "physical_size_mb",
            ])
            for lp in profiles:
                # 判斷層類型
                if "embed" in lp.block_id:
                    layer_type = "embedding"
                elif "attn" in lp.block_id:
                    layer_type = "attention"
                elif "expert" in lp.block_id:
                    layer_type = "expert_ffn"
                elif lp.layer_id.startswith("layer_") and int(lp.layer_id.split("_")[1]) % 2 == 0:
                    layer_type = "attention"
                else:
                    layer_type = "ffn"

                writer.writerow([
                    lp.layer_id,
                    lp.block_id,
                    layer_type,
                    round(lp.compression_ratio, 3),
                    round(lp.size_bytes / MB, 2),
                    round(lp.physical_size_bytes / MB, 2),
                ])

        logger.info("Figure 1 data: %s (%d layers)", csv_path, len(profiles))


def generate_figure2_ablation(output_dir: str, num_tokens: int = 16) -> List[BenchmarkMetrics]:
    """
    Figure 2: Ablation study — 每個組件的個別貢獻。

    拆解方式:
      baseline     = no_prefetch (無優化)
      + prefetch   = fixed_lookahead_4 (只加 prefetch)
      + adaptive   = caap_no_compress (加 CAAP adaptive 但不壓縮)
      + compress   = caap (加壓縮 = 完整系統)
      ceiling      = unified_memory (理論上限)

    每個組件的貢獻 = (有它的 tok/s) - (沒它的 tok/s)
    """
    ablation_configs = [
        ("baseline",    BenchmarkConfig.NO_PREFETCH),
        ("+ prefetch",  BenchmarkConfig.FIXED_LOOKAHEAD_4),
        ("+ adaptive",  BenchmarkConfig.CAAP_NO_COMPRESS),
        ("+ compress",  BenchmarkConfig.CAAP),
        ("ceiling",     BenchmarkConfig.UNIFIED_MEMORY),
    ]

    all_results = []

    for model_scale in ModelScale:
        spec = MODEL_SPECS[model_scale]
        # 跳過 70B 如果記憶體不夠
        if model_scale == ModelScale.LLAMA3_70B:
            from .benchmark_harness import _available_ram_gb
            if _available_ram_gb() < 6.0:
                logger.warning("Skipping 70B ablation (insufficient RAM)")
                continue

        model_results = []
        for label, config in ablation_configs:
            r = run_single_benchmark(model_scale, config, num_tokens=num_tokens)
            model_results.append((label, r))
            all_results.append(r)

        # 寫入 ablation CSV
        csv_path = os.path.join(output_dir, f"fig2_ablation_{model_scale.value}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "component", "config", "model",
                "tokens_per_sec", "hit_rate_pct", "io_wait_ms",
                "evictions", "logical_io_mb", "physical_io_mb",
            ])

            prev_tps = 0.0
            for label, r in model_results:
                delta_tps = r.tokens_per_sec - prev_tps
                writer.writerow([
                    label, r.config_name, r.model_name,
                    round(r.tokens_per_sec, 2),
                    round(r.prefetch_hit_rate_pct, 1),
                    round(r.total_io_wait_ms, 1),
                    r.eviction_count,
                    round(r.total_logical_io_bytes / MB, 1),
                    round(r.total_physical_io_bytes / MB, 1),
                ])
                prev_tps = r.tokens_per_sec

        logger.info("Figure 2 data: %s", csv_path)

        # 印出 ablation 摘要
        print(f"\n  Ablation: {spec.name}")
        print(f"  {'Component':<14} {'tok/s':>8} {'delta':>8} {'Hit%':>7} {'IO Wait':>9}")
        print("  " + "─" * 50)
        prev = 0.0
        for label, r in model_results:
            delta = r.tokens_per_sec - prev
            print(f"  {label:<14} {r.tokens_per_sec:>8.2f} {delta:>+7.2f} "
                  f"{r.prefetch_hit_rate_pct:>6.1f}% {r.total_io_wait_ms:>7.1f}ms")
            prev = r.tokens_per_sec

    return all_results


def generate_figure3_io_comparison(output_dir: str, num_tokens: int = 16) -> None:
    """
    Figure 3: I/O wait time breakdown across configs.

    顯示每個 config 的:
      - demand fetch wait (miss penalty)
      - prefetch I/O (background, not stalling GPU)
      - total logical vs physical I/O (compression effect)
    """
    csv_path = os.path.join(output_dir, "fig3_io_comparison.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "model", "config", "io_wait_ms",
            "logical_io_mb", "physical_io_mb", "compression_ratio",
            "tokens_per_sec",
        ])

        for model_scale in [ModelScale.LLAMA3_8B, ModelScale.MIXTRAL_8X7B]:
            for config in BenchmarkConfig:
                r = run_single_benchmark(model_scale, config, num_tokens=num_tokens)
                cr = (r.total_logical_io_bytes / max(1, r.total_physical_io_bytes))
                writer.writerow([
                    r.model_name, r.config_name,
                    round(r.total_io_wait_ms, 1),
                    round(r.total_logical_io_bytes / MB, 1),
                    round(r.total_physical_io_bytes / MB, 1),
                    round(cr, 2),
                    round(r.tokens_per_sec, 2),
                ])

    logger.info("Figure 3 data: %s", csv_path)


def generate_table1_full(output_dir: str, num_tokens: int = 32) -> None:
    """
    Table 1: Full benchmark results (all models × all configs).

    LaTeX-ready format for the paper.
    """
    csv_path = os.path.join(output_dir, "table1_full.csv")

    results = []
    for model_scale in [ModelScale.LLAMA3_8B, ModelScale.MIXTRAL_8X7B]:
        for config in BenchmarkConfig:
            r = run_single_benchmark(model_scale, config, num_tokens=num_tokens)
            results.append(r)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "model", "config", "scale", "tokens_per_sec", "ttft_ms",
            "hit_rate_pct", "io_wait_ms", "evictions",
            "logical_io_mb", "physical_io_mb", "compression_ratio",
        ])
        for r in results:
            cr = r.total_logical_io_bytes / max(1, r.total_physical_io_bytes)
            writer.writerow([
                r.model_name, r.config_name, f"1/{r.scale_factor}",
                round(r.tokens_per_sec, 2),
                round(r.time_to_first_token_ms, 1),
                round(r.prefetch_hit_rate_pct, 1),
                round(r.total_io_wait_ms, 1),
                r.eviction_count,
                round(r.total_logical_io_bytes / MB, 1),
                round(r.total_physical_io_bytes / MB, 1),
                round(cr, 2),
            ])

    logger.info("Table 1 data: %s", csv_path)
    _print_summary(results)


def main():
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="P3 Ablation Study")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--tokens", type=int, default=16)
    parser.add_argument("--figure", choices=["1", "2", "3", "table1", "all"], default="all")
    args = parser.parse_args()

    output_dir = args.output_dir or os.path.join(
        os.path.dirname(__file__), "..", "benchmark_results",
    )
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n  Output directory: {output_dir}\n")

    if args.figure in ("1", "all"):
        print("  === Figure 1: Compression Ratio Heterogeneity ===")
        generate_figure1_compression_ratios(output_dir)

    if args.figure in ("2", "all"):
        print("\n  === Figure 2: Ablation Study ===")
        generate_figure2_ablation(output_dir, num_tokens=args.tokens)

    if args.figure in ("3", "all"):
        print("\n  === Figure 3: I/O Comparison ===")
        generate_figure3_io_comparison(output_dir, num_tokens=args.tokens)

    if args.figure in ("table1", "all"):
        print("\n  === Table 1: Full Results ===")
        generate_table1_full(output_dir, num_tokens=args.tokens)

    print(f"\n  All data written to: {output_dir}")


if __name__ == "__main__":
    main()
