"""
Portable vram-boost — runs FROM the SD card.
Double-click vram-boost.exe to expand memory.

Flow:
  1. Detect which drive this program is running from
  2. Speed test the drive
  3. Evaluate: expand or skip?
  4. If expand: request admin → create pagefile/swap
  5. Monitor until user exits or card removed
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from vram_boost.core.detect import ExternalDevice
from vram_boost.core.measure import measure_write_speed
from vram_boost.core.policy import evaluate
from vram_boost.core.extend import execute, is_admin, remove
from vram_boost.core.monitor import Monitor, HealthStatus

LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
CONFIG_FILE = "vram-boost.conf"


def _get_own_drive() -> str:
    """Detect which drive this executable is running from."""
    exe_path = os.path.abspath(sys.argv[0])
    if platform.system() == "Windows":
        return exe_path[:3]  # "E:\\"
    else:
        # Find mount point
        path = Path(exe_path)
        while path != path.parent:
            if path.is_mount():
                return str(path)
            path = path.parent
        return str(Path(exe_path).parent)


def _load_config(drive: str) -> dict:
    """Load config from SD card, or create default."""
    config_path = os.path.join(drive, CONFIG_FILE)
    defaults = {
        "max_swap_percent": 80,
        "auto_expand": True,
        "min_speed_mbs": 1.0,
        "min_free_gb": 0.5,
    }
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                loaded = json.load(f)
                defaults.update(loaded)
        except Exception:
            pass
    else:
        try:
            with open(config_path, "w") as f:
                json.dump(defaults, f, indent=2)
        except Exception:
            pass
    return defaults


def _request_admin_and_relaunch():
    """Re-launch this script with admin privileges."""
    if platform.system() == "Windows":
        import ctypes
        script = os.path.abspath(sys.argv[0])
        params = " ".join(sys.argv[1:])
        print("\n  Requesting administrator privileges...")
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" {params}', None, 1
        )
        sys.exit(0)
    else:
        print("\n  Please run with sudo:")
        print(f"  sudo {sys.executable} {' '.join(sys.argv)}")
        sys.exit(1)


def _print_device_rating(write_mbs: float):
    """Show device tier, capabilities, and upgrade recommendations."""
    print()
    print("  ┌─────────────────────────────────────────────┐")

    if write_mbs < 1:
        tier = "UNUSABLE"
        bar = "X"
        print(f"  │  {write_mbs:>6.1f} MB/s — {tier:<14}  [{bar:<10}] │")
        print(f"  │                                             │")
        print(f"  │  Device too slow for any memory expansion.  │")
        print(f"  │  Minimum: 1 MB/s                            │")

    elif write_mbs < 30:
        tier = "BASIC"
        bar = "=" * 2
        print(f"  │  {write_mbs:>6.1f} MB/s — {tier:<14}  [{bar:<10}] │")
        print(f"  │                                             │")
        print(f"  │  Capability: Emergency overflow (anti-OOM)  │")
        print(f"  │  LLM inference: Very slow (~1-3 sec/token)  │")
        print(f"  │                                             │")
        print(f"  │  Upgrade suggestion:                        │")
        print(f"  │  > USB 3.2 SSD (~$30) = 10-20x faster      │")
        print(f"  │    Enables smooth LLM inference offload     │")

    elif write_mbs < 100:
        tier = "STANDARD"
        bar = "=" * 4
        print(f"  │  {write_mbs:>6.1f} MB/s — {tier:<14}  [{bar:<10}] │")
        print(f"  │                                             │")
        print(f"  │  Capability: Usable memory expansion        │")
        print(f"  │  LLM inference: Moderate (~5-10 tok/s)      │")
        print(f"  │  With INT4 compression: ~15 tok/s           │")
        print(f"  │                                             │")
        print(f"  │  Good for general use!                      │")

    elif write_mbs < 500:
        tier = "FAST"
        bar = "=" * 7
        print(f"  │  {write_mbs:>6.1f} MB/s — {tier:<14}  [{bar:<10}] │")
        print(f"  │                                             │")
        print(f"  │  Capability: Full LLM offload support       │")
        print(f"  │  LLM inference: Near-native speed           │")
        print(f"  │  With INT4 compression: compute-bound       │")
        print(f"  │                                             │")
        print(f"  │  Excellent device!                          │")

    else:
        tier = "ULTRA"
        bar = "=" * 10
        print(f"  │  {write_mbs:>6.1f} MB/s — {tier:<14}  [{bar:<10}] │")
        print(f"  │                                             │")
        print(f"  │  Capability: NVMe-class performance         │")
        print(f"  │  LLM inference: Full native speed           │")
        print(f"  │  Compression: Skipped (device fast enough)  │")
        print(f"  │                                             │")
        print(f"  │  Top tier — no upgrade needed!              │")

    print(f"  └─────────────────────────────────────────────┘")

    # Show what models can run
    print()
    if write_mbs >= 1:
        eff = write_mbs * 30  # QATC effective bandwidth
        print(f"  Effective bandwidth with INT4 compression: {eff:.0f} MB/s")
        print()
        print(f"  Model support at this speed:")
        models = [
            ("Llama-3-8B (INT4)",   4.5,  0.15),
            ("Llama-3-70B (INT4)", 35.0,  1.17),
            ("Mixtral 8x7B (INT4)", 24.0, 0.80),
        ]
        for name, raw_gb, comp_gb in models:
            load_time = comp_gb * 1024 / write_mbs
            if load_time < 60:
                time_str = f"{load_time:.0f} sec load"
            else:
                time_str = f"{load_time/60:.1f} min load"
            print(f"    {name:<24} {comp_gb:.2f} GB compressed  {time_str}")


def _print_banner():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║     vram-boost — Memory Expansion    ║")
    print("  ║     Insert SD card, run, expand.     ║")
    print("  ╚══════════════════════════════════════╝")
    print()


def _print_status(status: HealthStatus):
    sys.stdout.write(
        f"\r  [{time.strftime('%H:%M:%S')}] "
        f"Swap: {status.swap_used_pct:.0f}% used | "
        f"Device: {'OK' if status.device_present else 'DISCONNECTED'} | "
        f"Uptime: {status.uptime_s:.0f}s    "
    )
    sys.stdout.flush()


def main():
    logging.basicConfig(level=logging.INFO, format=LOG_FMT)
    _print_banner()

    # Step 1: Find own drive
    drive = _get_own_drive()
    print(f"  Running from: {drive}")

    import shutil
    total, used, free = shutil.disk_usage(drive)
    print(f"  Capacity: {total/1024**3:.1f} GB, Free: {free/1024**3:.1f} GB")

    # Step 2: Load config
    config = _load_config(drive)
    print(f"  Config: max_swap={config['max_swap_percent']}%, auto={config['auto_expand']}")

    # Step 3: Speed test + device rating
    print(f"\n  Testing write speed...")
    speed = measure_write_speed(drive)
    print(f"  Write speed: {speed.write_mbs} MB/s")
    _print_device_rating(speed.write_mbs)

    if not speed.is_usable:
        input("\n  Press Enter to close...")
        return

    # Step 4: Evaluate
    device = ExternalDevice(
        path=drive,
        name=f"SD Card ({drive})",
        bus="SD",
        size_bytes=total,
        free_bytes=free,
    )
    plan = evaluate(device, speed)
    print(f"\n  Decision: {plan.action}")
    print(f"  Reason: {plan.reason}")

    if plan.action != "expand":
        input("\n  Press Enter to close...")
        return

    print(f"  Swap size: {plan.swap_gb:.1f} GB")

    # Step 5: Check admin
    if not is_admin():
        print("\n  Administrator privileges required to create swap.")
        _request_admin_and_relaunch()
        return

    # Step 6: Create swap
    print(f"\n  Creating memory expansion...")
    result = execute(plan)

    if result.success:
        print(f"\n  SUCCESS: {result.method}")
        print(f"  Path: {result.path}")
        print(f"  Size: {result.size_gb:.1f} GB")
        if result.needs_reboot:
            print(f"\n  NOTE: Pagefile will be active after REBOOT.")
            print(f"  After reboot, all programs automatically benefit.")
        else:
            print(f"\n  Swap is ACTIVE NOW. All programs benefit.")
    else:
        print(f"\n  FAILED: {result.error}")
        input("\n  Press Enter to close...")
        return

    # Step 7: Monitor
    print(f"\n  Monitoring... (Press Ctrl+C to stop)")
    print()

    mon = Monitor(
        device_path=drive,
        swap_path=result.path,
        check_interval_s=5.0,
        on_status=_print_status,
        on_disconnect=lambda: print("\n\n  WARNING: Device disconnected!"),
    )
    mon.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n  Stopping...")
        mon.stop()

    # Ask about removal
    print()
    ans = input("  Remove swap before ejecting? [Y/n]: ").strip().lower()
    if ans != "n":
        print("  Removing swap...")
        r = remove(drive)
        if r.success:
            print("  Swap removed. Safe to eject.")
        else:
            print(f"  Warning: {r.error}")

    print("  Done.")


if __name__ == "__main__":
    main()
