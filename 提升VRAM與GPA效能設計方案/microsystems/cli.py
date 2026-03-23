"""
VRAM Booster Unified CLI
=========================
統一入口點，自動偵測最佳裝置並啟動對應的微系統。

Usage:
  python -m microsystems.cli scan              # 掃描所有裝置
  python -m microsystems.cli activate           # 自動選擇最佳裝置並啟動
  python -m microsystems.cli activate --device sd    # 指定裝置類型
  python -m microsystems.cli activate --device usb
  python -m microsystems.cli activate --device enc
  python -m microsystems.cli estimate           # 效能預估
  python -m microsystems.cli benchmark          # 裝置速度測試
  python -m microsystems.cli status             # 系統狀態
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from typing import Optional

from .config import SystemConfig, DeviceType
from .systems.sd_vram_system import SDVRAMSystem
from .systems.usb_vram_system import USBVRAMSystem
from .systems.enc_vram_system import EnclosureVRAMSystem


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def print_header() -> None:
    print("""
╔══════════════════════════════════════════════════╗
║        VRAM Booster Micro-Systems v0.2.0         ║
║   SD Express | USB Storage | NVMe Enclosure      ║
║                                                  ║
║   Author: DONG. WEI YANG                         ║
╚══════════════════════════════════════════════════╝
""")


def cmd_scan(args: argparse.Namespace) -> None:
    """掃描所有可用裝置"""
    print_header()
    print("[Scanning all device types...]\n")

    results = {}

    # SD Express
    print("--- SD Express ---")
    sd_sys = SDVRAMSystem()
    sd_result = sd_sys.scan()
    results["sd"] = sd_result
    print(f"  GPU VRAM: {sd_result['gpu_vram_gb']:.1f} GB")
    print(f"  System RAM: {sd_result['system_ram_gb']:.1f} GB")
    print(f"  SD Express devices: {sd_result['sd_devices_found']}")
    for d in sd_result.get("sd_devices", []):
        print(f"    - {d['name']} ({d['protocol']}, {d['capacity_gb']:.0f}GB)")
    print()

    # USB Storage
    print("--- USB Storage ---")
    usb_sys = USBVRAMSystem()
    usb_result = usb_sys.scan()
    results["usb"] = usb_result
    print(f"  USB devices: {usb_result['usb_devices_found']}")
    for d in usb_result.get("usb_devices", []):
        tunnel = " [PCIe Tunnel]" if d.get("pcie_tunneling") else ""
        print(f"    - {d['name']} ({d['protocol']}{tunnel}, "
              f"{d['capacity_gb']:.0f}GB, {d['bandwidth_mbs']:.0f} MB/s) "
              f"[{d['product_tier']}]")
    print()

    # Enclosure NVMe
    print("--- Enclosure NVMe ---")
    enc_sys = EnclosureVRAMSystem()
    enc_result = enc_sys.scan()
    results["enc"] = enc_result
    print(f"  Thunderbolt: {enc_result.get('thunderbolt_version', 'not detected')}")
    print(f"  Enclosure devices: {enc_result['enclosure_devices_found']}")
    for d in enc_result.get("enclosure_devices", []):
        print(f"    - {d['name']} ({d['protocol']}, "
              f"{d['capacity_gb']:.0f}GB, {d['bandwidth_mbs']:.0f} MB/s)")
    print()

    # 總結建議
    total_devices = (sd_result["sd_devices_found"]
                     + usb_result["usb_devices_found"]
                     + enc_result["enclosure_devices_found"])

    print(f"=== Total: {total_devices} device(s) found ===")

    if total_devices == 0:
        print("  No compatible devices detected.")
        print("  Please connect an SD Express card, USB storage, or NVMe enclosure.")
    else:
        # 推薦最佳方案
        best_system = None
        best_bw = 0

        for d in enc_result.get("enclosure_devices", []):
            if d["bandwidth_mbs"] > best_bw:
                best_bw = d["bandwidth_mbs"]
                best_system = f"Enclosure: {d['name']}"

        for d in sd_result.get("sd_devices", []):
            # SD Express 用固定頻寬估算
            sd_bw = 3500
            if sd_bw > best_bw:
                best_bw = sd_bw
                best_system = f"SD Express: {d['name']}"

        for d in usb_result.get("usb_devices", []):
            if d["bandwidth_mbs"] > best_bw:
                best_bw = d["bandwidth_mbs"]
                best_system = f"USB: {d['name']}"

        if best_system:
            print(f"  Recommended: {best_system} ({best_bw:.0f} MB/s)")

    if args.json:
        print("\n" + json.dumps(results, indent=2, ensure_ascii=False))


def cmd_activate(args: argparse.Namespace) -> None:
    """啟動 VRAM 擴展"""
    print_header()

    config = SystemConfig()

    # 選擇系統
    system = None
    device_type = args.device

    if device_type == "auto":
        # 自動偵測最佳裝置
        print("[Auto-detecting best device...]\n")

        enc_sys = EnclosureVRAMSystem(config)
        enc_result = enc_sys.scan()
        if enc_result["enclosure_devices_found"] > 0:
            system = enc_sys
            print("Selected: Enclosure-VRAM Booster (best performance)")
        else:
            sd_sys = SDVRAMSystem(config)
            sd_result = sd_sys.scan()
            if sd_result["sd_devices_found"] > 0:
                system = sd_sys
                print("Selected: SD-VRAM Booster (lowest latency)")
            else:
                usb_sys = USBVRAMSystem(config)
                usb_result = usb_sys.scan()
                if usb_result["usb_devices_found"] > 0:
                    system = usb_sys
                    print("Selected: USB-VRAM Booster")

    elif device_type == "sd":
        system = SDVRAMSystem(config)
        system.scan()
    elif device_type == "usb":
        system = USBVRAMSystem(config)
        system.scan()
    elif device_type == "enc":
        system = EnclosureVRAMSystem(config)
        system.scan()

    if system is None:
        print("ERROR: No compatible devices found.")
        sys.exit(1)

    # 啟動
    print()
    if not system.activate():
        print("ERROR: Failed to activate VRAM expansion.")
        sys.exit(1)

    print("\nVRAM expansion is ACTIVE. Press Ctrl+C to stop.\n")

    # 等待中斷
    def signal_handler(sig, frame):
        print("\nReceived shutdown signal...")
        system.deactivate()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # 定期印出狀態
    try:
        while True:
            time.sleep(10)
            status = system.status()
            pool = status.get("memory_pool", {})
            print(f"[{time.strftime('%H:%M:%S')}] "
                  f"Pool: {pool.get('total_used_gb', 0):.1f}/{pool.get('total_capacity_gb', 0):.1f} GB | "
                  f"Health: {status.get('health', '?')}")
    except KeyboardInterrupt:
        system.deactivate()


def cmd_estimate(args: argparse.Namespace) -> None:
    """效能預估"""
    print_header()
    model = args.model
    print(f"[Performance Estimate: {model}]\n")

    from .config import MODEL_PROFILES
    if model not in MODEL_PROFILES:
        print(f"Available models: {', '.join(MODEL_PROFILES.keys())}")
        sys.exit(1)

    # 對三種架構都做預估
    systems = [
        ("SD-VRAM Booster", SDVRAMSystem()),
        ("USB-VRAM Booster", USBVRAMSystem()),
        ("Enclosure-VRAM Booster", EnclosureVRAMSystem()),
    ]

    for name, sys_instance in systems:
        sys_instance.scan()
        try:
            est = sys_instance.estimate_performance(model)
            print(f"--- {name} ---")
            print(f"  Model: {est.model_name} ({est.model_size_gb}GB)")
            print(f"  Overflow: {est.overflow_gb:.1f}GB")
            print(f"  Estimated Speed: {est.estimated_tps:.2f} tokens/s")
            print(f"  Context Window: {est.context_window_tokens:,} tokens")
            print(f"  Context Boost: {est.context_boost_factor:.1f}x")
            print(f"  Feasibility: {est.feasibility}")
            print()
        except Exception as e:
            print(f"--- {name} ---")
            print(f"  Error: {e}\n")


def cmd_status(args: argparse.Namespace) -> None:
    """顯示系統狀態"""
    print("Status check requires an active system. Use 'activate' first.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vram-booster",
        description="VRAM Booster Micro-Systems — GPU Memory Expansion via External Storage",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    sub = parser.add_subparsers(dest="command", help="Command")

    # scan
    scan_p = sub.add_parser("scan", help="Scan all devices")
    scan_p.add_argument("--json", action="store_true", help="Output JSON")

    # activate
    act_p = sub.add_parser("activate", help="Activate VRAM expansion")
    act_p.add_argument("--device", choices=["auto", "sd", "usb", "enc"],
                       default="auto", help="Device type")

    # estimate
    est_p = sub.add_parser("estimate", help="Performance estimation")
    est_p.add_argument("--model", default="llama3_70b_q4",
                       help="Model key (e.g., llama3_8b_q4, llama3_70b_q4)")

    # status
    sub.add_parser("status", help="System status")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "activate":
        cmd_activate(args)
    elif args.command == "estimate":
        cmd_estimate(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
