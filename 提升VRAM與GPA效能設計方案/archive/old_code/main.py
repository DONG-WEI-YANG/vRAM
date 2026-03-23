"""
USB-VRAM Booster — 主程式進入點
製作者：Peter Yang

隨插即用自動啟動流程：
1. 自動偵測 OS / GPU / USB 裝置
2. 彈出確認視窗
3. 啟動 VRAM 擴展
4. 顯示儀表板
"""

import sys
import argparse
from .engine.booster import USBVRAMBooster


def main():
    parser = argparse.ArgumentParser(
        description="USB-VRAM Booster — 透過 USB 裝置擴展 GPU VRAM (by Peter Yang)"
    )
    parser.add_argument("--demo", type=int, choices=[1, 2, 3, 4, 5],
                        help="Demo 模式 (1-5)")
    parser.add_argument("--no-gui", action="store_true",
                        help="不啟動 GUI，僅輸出 CLI 報告")
    parser.add_argument("--scan-only", action="store_true",
                        help="僅掃描系統，不啟動擴展")
    args = parser.parse_args()

    print("=" * 60)
    print("  USB-VRAM Booster v0.1.0")
    print("  製作者：Peter Yang")
    print("=" * 60)

    if args.demo:
        _run_demo(args.demo, args.no_gui)
    elif args.scan_only:
        _run_scan()
    else:
        _run_normal(args.no_gui)


def _run_demo(scenario_id: int, no_gui: bool):
    """Demo 模式"""
    from .demo import DemoScenarios, get_demo_tier_config
    from .memory.tier_manager import TierManager

    scenarios = {
        1: ("USB4 NVMe + RTX 4070", DemoScenarios.scenario_usb4_nvme_rtx4070),
        2: ("USB4 V2 NVMe + RTX 4090", DemoScenarios.scenario_usb4v2_rtx4090),
        3: ("USB 3.2 隨身碟 + RTX 3060", DemoScenarios.scenario_usb32_flash_drive),
        4: ("TB5 + RTX 5090", DemoScenarios.scenario_tb5_workstation),
        5: ("USB 2.0 (被拒絕)", DemoScenarios.scenario_usb20_rejected),
    }

    name, func = scenarios[scenario_id]
    print(f"\n[Demo 場景 {scenario_id}] {name}")
    print("-" * 60)

    report = func()
    report.print_report()

    if report.vram_expandable:
        config = get_demo_tier_config(report)
        tm = TierManager(config)

        print("\n[效能預估]")
        print("-" * 60)

        models = [
            ("Llama-3 8B (Q4)", 4500, "llama-3-8b"),
            ("Llama-3 70B (Q4)", 40000, "llama-3-70b"),
            ("Llama-3 70B (FP16)", 140000, "llama-3-70b"),
        ]

        for model_name, size_mb, arch in models:
            est = tm.estimate_performance(model_name, size_mb, arch)
            print(f"\n  模型: {model_name} ({size_mb}MB)")
            print(f"  分配: VRAM {est.vram_portion_mb}MB + RAM {est.ram_portion_mb}MB + USB {est.usb_portion_mb}MB")
            print(f"  推理速度: {est.estimated_tps:.2f} tokens/s")
            print(f"  Context Window: {est.max_context_tokens:,} tokens")
            if est.context_boost_ratio < float('inf'):
                print(f"  Context 提升: {est.context_boost_ratio:.0f}x")

    if not no_gui and report.vram_expandable:
        try:
            from .gui.popup import PlugAndPlayPopup
            popup = PlugAndPlayPopup(report)
            if popup.show():
                from .engine.booster import USBVRAMBooster, BoostResult
                booster = USBVRAMBooster()
                boost_result = BoostResult(
                    success=True,
                    system_report=report,
                    tier_config=get_demo_tier_config(report),
                    message="Demo 模式 — VRAM 擴展已啟動",
                    elapsed_seconds=0.5
                )
                booster.tier_manager = TierManager(boost_result.tier_config)
                from .gui.dashboard import Dashboard
                dashboard = Dashboard(booster, boost_result)
                dashboard.show()
        except Exception as e:
            print(f"\n[注意] GUI 不可用 ({e})，已輸出 CLI 報告")


def _run_scan():
    """僅掃描系統"""
    booster = USBVRAMBooster()
    report = booster.scan_system()
    report.print_report()


def _run_normal(no_gui: bool):
    """正常運行模式"""
    booster = USBVRAMBooster()
    result = booster.initialize()

    if not result.success:
        result.system_report.print_report()
        return

    result.system_report.print_report()

    if no_gui:
        status = booster.activate()
        print(f"\n[狀態] {status['message']}")
        print(f"  VRAM: {status['vram_total']}")
        print(f"  USB 擴展: {status['usb_expand']}")
        print(f"  頻寬: {status['bandwidth']}")
        print(f"  PCIe Tunnel: {status['pcie_tunnel']}")
    else:
        try:
            from .gui.popup import PlugAndPlayPopup
            popup = PlugAndPlayPopup(result.system_report)
            if popup.show():
                booster.activate()
                from .gui.dashboard import Dashboard
                dashboard = Dashboard(booster, result)
                dashboard.show()
        except Exception as e:
            print(f"\n[注意] GUI 不可用 ({e})，使用 CLI 模式")
            status = booster.activate()
            print(f"\n[狀態] {status['message']}")


if __name__ == "__main__":
    main()
