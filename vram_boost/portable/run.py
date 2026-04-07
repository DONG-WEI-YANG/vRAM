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

    # Step 3: Speed test
    print(f"\n  Testing write speed...")
    speed = measure_write_speed(drive)
    print(f"  Write speed: {speed.write_mbs} MB/s")

    if not speed.is_usable:
        print(f"\n  Device too slow ({speed.write_mbs} MB/s). Exiting.")
        input("  Press Enter to close...")
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
