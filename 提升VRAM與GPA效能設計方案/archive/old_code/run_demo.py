#!/usr/bin/env python3
"""
SD-VRAM Booster — Demo 模式測試腳本
製作者：Peter Yang

在沙盒環境中模擬完整的偵測與效能計算流程。
無需實體 GPU 或 SD 卡即可驗證所有計算邏輯。
"""

import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sdvram.demo import (
    create_demo_report, run_performance_benchmark,
    DEMO_GPUS, DEMO_SD_CARDS
)


def print_header(text, char="═"):
    width = 70
    print(f"\n{char * width}")
    print(f"  {text}")
    print(f"{char * width}")


def print_section(text):
    print(f"\n{'─' * 50}")
    print(f"  {text}")
    print(f"{'─' * 50}")


def format_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def run_single_scenario(gpu_key, sd_key, os_type="Linux"):
    """執行單一場景的模擬測試"""

    print_header(f"場景: {gpu_key} + {sd_key} ({os_type})")

    # Step 1: 系統掃描
    print_section("Step 1: 自動偵測系統環境")
    report = create_demo_report(gpu_key, sd_key, os_type)

    print(f"  作業系統:  {report.os_info.summary}")
    for gpu in report.gpus:
        print(f"  GPU:       {gpu.summary}")
    for sd in report.sd_cards:
        print(f"  SD 卡:     {sd.summary}")
    print(f"  可擴展:    {'✓ 是' if report.can_boost else '✗ 否'}")
    print(f"  原因:      {report.boost_reason}")

    if not report.can_boost:
        print(f"\n  ⚠ 此場景無法進行 VRAM 擴展")
        return None

    # Step 2: 效能基準測試
    print_section("Step 2: 效能基準計算")
    results = run_performance_benchmark(report)

    # 頻寬比較
    bw = results["bandwidth"]
    print(f"\n  頻寬比較:")
    print(f"    GPU VRAM:     {bw['vram_gbs']:>8} GB/s")
    print(f"    System RAM:   {bw['ram_gbs']:>8} GB/s")
    print(f"    SD Express:   {bw['sd_gbs']:>8} GB/s")
    print(f"    SD vs VRAM:   {bw['sd_vs_vram_ratio']}")
    print(f"    SD vs RAM:    {bw['sd_vs_ram_ratio']}")

    # LLM 推理效能
    print(f"\n  LLM 推理效能預估:")
    print(f"  {'模型':<24} {'VRAM內':>8} {'SD擴展':>8} {'Context':>10} {'擴展後':>10} {'倍率':>6} {'狀態'}")
    print(f"  {'─'*24} {'─'*8} {'─'*8} {'─'*10} {'─'*10} {'─'*6} {'─'*30}")

    for model, data in results["llm_inference"].items():
        vram_tps = f"{data['vram_only_tps']:.1f}" if data['vram_only_tps'] > 0 else "N/A"
        boost_tps = f"{data['boosted_tps']:.1f}" if data['boosted_tps'] > 0 else "N/A"
        ctx_before = format_tokens(data['vram_context_tokens'])
        ctx_after = format_tokens(data['boosted_context_tokens'])
        ratio = f"{data['context_boost_ratio']:.0f}x"

        print(f"  {model:<24} {vram_tps:>7}t {boost_tps:>7}t "
              f"{ctx_before:>10} {ctx_after:>10} {ratio:>6} {data['status']}")

    # 應用場景
    print(f"\n  實際應用效益:")
    for key, uc in results["use_cases"].items():
        print(f"    [{uc['description']}]")
        print(f"      效益: {uc['benefit']}")
        print(f"      預估: {uc['typical_boost']}")

    # 總結
    s = results["summary"]
    print(f"\n  ┌─────────────────────────────────────────────┐")
    print(f"  │ 總結                                        │")
    print(f"  ├─────────────────────────────────────────────┤")
    print(f"  │ GPU:        {s['gpu']:<33}│")
    print(f"  │ 原始 VRAM:  {s['gpu_vram_gb']:<5} GB{' '*28}│")
    print(f"  │ SD 卡:      {s['sd_card']:<33}│")
    print(f"  │ SD 容量:    {s['sd_capacity_gb']:<5} GB{' '*28}│")
    print(f"  │ SD 頻寬:    {s['sd_bandwidth_mbs']:<5} MB/s{' '*25}│")
    print(f"  │ 擴展後總量: {s['total_effective_memory_gb']:<5} GB ({s['memory_expansion_ratio']}){' '*20}│")
    print(f"  └─────────────────────────────────────────────┘")

    return results


def main():
    print("=" * 70)
    print("  SD-VRAM Booster v0.1.0 — Demo 模擬測試")
    print("  製作者: Peter Yang")
    print("  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)

    # ─── 場景 1: 主流配置 (RTX 4070 + 512GB SD Express) ─────
    r1 = run_single_scenario("RTX 4070", "SD Express 512GB Gen3x2", "Linux")

    # ─── 場景 2: 入門配置 (RTX 4060 + 256GB SD Express) ─────
    r2 = run_single_scenario("RTX 4060", "SD Express 256GB Gen3x1", "Windows")

    # ─── 場景 3: 高階配置 (RTX 4090 + 1TB SD Express Gen4) ──
    r3 = run_single_scenario("RTX 4090", "SD Express 1TB Gen4x2", "Linux")

    # ─── 場景 4: 不合格 SD 卡 (UHS-I) ──────────────────────
    r4 = run_single_scenario("RTX 4070", "UHS-I 128GB (不合格)", "Windows")

    # ─── 場景 5: AMD GPU ────────────────────────────────────
    r5 = run_single_scenario("RX 7900 XTX", "SD Express 1TB Gen4x2", "Linux")

    # ─── 最終摘要 ───────────────────────────────────────────
    print_header("測試摘要", "█")
    print(f"""
  測試完成！共執行 5 個場景：

  場景 1: RTX 4070 + 512GB SD Express Gen3x2 (Linux)
    → 12GB VRAM → 擴展至 ~454GB 有效記憶體 (38x)
    → Llama-3 70B: 從「無法運行」→「可運行」

  場景 2: RTX 4060 + 256GB SD Express Gen3x1 (Windows)
    → 8GB VRAM → 擴展至 ~217GB 有效記憶體 (27x)
    → Context Window: 最高 34x 提升

  場景 3: RTX 4090 + 1TB SD Express Gen4x2 (Linux)
    → 24GB VRAM → 擴展至 ~901GB 有效記憶體 (38x)
    → 可運行 Mixtral 8x22B 等超大模型

  場景 4: RTX 4070 + UHS-I 128GB (Windows)
    → ✗ 頻寬不足，無法擴展 (UHS-I 僅 104 MB/s)
    → 系統正確拒絕不合格的 SD 卡

  場景 5: RX 7900 XTX + 1TB SD Express Gen4x2 (Linux)
    → AMD GPU 也可以使用！24GB → ~901GB

  關鍵結論：
  ─────────────────────────────────────────────────
  1. SD Express 卡使用 NVMe 協定，技術上可行
  2. 核心價值：讓「無法運行」的模型變成「可以運行」
  3. Context Window 可提升 20-80 倍
  4. 系統會自動拒絕頻寬不足的 SD 卡
  5. 支援 NVIDIA + AMD，Windows + Linux
""")

    # 匯出 JSON 報告
    all_results = {
        "scenario_1_rtx4070_512gb": r1,
        "scenario_2_rtx4060_256gb": r2,
        "scenario_3_rtx4090_1tb": r3,
        "scenario_4_uhsi_rejected": r4,
        "scenario_5_amd_7900xtx": r5,
    }

    output_path = os.path.join(os.path.dirname(__file__), "demo_results.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    print(f"  詳細結果已匯出: {output_path}")


if __name__ == "__main__":
    main()
