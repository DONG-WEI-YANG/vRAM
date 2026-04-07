"""
OS-Level Memory Extension
==========================
讓外接儲存裝置被作業系統識別為額外記憶體，
任何程式（不只 Python）自動受益。

Windows: 在 SD 卡上建立 pagefile.sys
Linux:   在 SD 卡上建立 swap file + swapon

用法::

    from microsystems.core.os_memory_extend import extend_memory
    result = extend_memory("E:")  # Windows
    result = extend_memory("/media/sdcard")  # Linux
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import shutil
import time
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

MB = 1024 * 1024
GB = 1024 * MB


def _is_admin() -> bool:
    """Check if running with elevated privileges."""
    if platform.system() == "Windows":
        try:
            return os.getuid() == 0
        except AttributeError:
            try:
                import ctypes
                return ctypes.windll.shell32.IsUserAnAdmin() != 0
            except Exception:
                return False
    else:
        return os.getuid() == 0


def _measure_speed(path: str) -> float:
    """Quick sequential write speed test (MB/s)."""
    test_file = os.path.join(path, ".vram_speed_test")
    data = os.urandom(4 * MB)
    try:
        t0 = time.perf_counter()
        fd = os.open(test_file, os.O_CREAT | os.O_RDWR | os.O_BINARY | os.O_TRUNC)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        t1 = time.perf_counter()
        return 4.0 / max(t1 - t0, 0.001)
    except Exception as e:
        logger.warning("Speed test failed: %s", e)
        return 25.0
    finally:
        try:
            os.unlink(test_file)
        except OSError:
            pass


def _calc_swap_size(device_path: str, speed_mbs: float, max_percent: float = 0.8) -> int:
    """Calculate optimal swap size based on device speed and capacity."""
    total, used, free = shutil.disk_usage(device_path)
    capacity_limit = int(free * max_percent)
    speed_limit = int(speed_mbs * 600 * MB)  # 10 min fill time
    return min(capacity_limit, speed_limit)


def extend_memory(
    device_path: str,
    size_gb: Optional[float] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    將外接裝置設定為 OS 記憶體擴展。

    Args:
        device_path: 裝置路徑 (Windows: "E:", Linux: "/media/sdcard")
        size_gb: 指定大小 (None = 自動計算)
        force: 跳過確認

    Returns:
        dict with success, method, size_gb, speed_mbs, etc.
    """
    system = platform.system()

    # Normalize path
    if system == "Windows":
        device_path = device_path.rstrip("\\").rstrip("/")
        if len(device_path) == 1:
            device_path = device_path + ":"
        check_path = device_path + "\\"
    else:
        check_path = device_path

    # Validate
    if not os.path.isdir(check_path):
        return {"success": False, "error": f"Path not found: {check_path}"}

    if not _is_admin():
        return {
            "success": False,
            "error": "Requires administrator/root privileges",
            "hint_windows": f"Run: powershell Start-Process python -Verb RunAs -ArgumentList '-c \"from microsystems.core.os_memory_extend import extend_memory; extend_memory(\\\"{device_path}\\\")\"'",
            "hint_linux": f"Run: sudo python3 -c 'from microsystems.core.os_memory_extend import extend_memory; extend_memory(\"{device_path}\")'",
        }

    # Measure speed
    speed = _measure_speed(check_path)
    logger.info("Device speed: %.1f MB/s", speed)

    # Calculate size
    if size_gb is None:
        swap_bytes = _calc_swap_size(check_path, speed)
    else:
        swap_bytes = int(size_gb * GB)

    swap_gb = swap_bytes / GB
    logger.info("Swap size: %.1f GB", swap_gb)

    if system == "Windows":
        return _extend_windows(device_path, swap_bytes, speed)
    else:
        return _extend_linux(device_path, swap_bytes, speed)


def _extend_windows(drive: str, swap_bytes: int, speed_mbs: float) -> Dict[str, Any]:
    """Windows: create pagefile on external drive."""
    letter = drive.rstrip(":")[0].upper()
    swap_mb = swap_bytes // MB
    pagefile_path = f"{letter}:\\pagefile.sys"

    logger.info("Creating Windows pagefile: %s (%d MB)", pagefile_path, swap_mb)

    # Check if pagefile already exists on this drive
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PageFileSetting | Select-Object Name | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            import json
            existing = json.loads(r.stdout)
            if isinstance(existing, dict):
                existing = [existing]
            for pf in existing:
                if pf.get("Name", "").upper().startswith(letter + ":"):
                    return {
                        "success": True,
                        "method": "windows_pagefile",
                        "status": "already_exists",
                        "path": pf["Name"],
                        "speed_mbs": round(speed_mbs, 1),
                    }
    except Exception:
        pass

    # Create pagefile via WMI
    ps_cmd = (
        f"$pf = [wmiclass]'Win32_PageFileSetting'; "
        f"$new = $pf.CreateInstance(); "
        f"$new.Name = '{pagefile_path}'; "
        f"$new.InitialSize = {swap_mb}; "
        f"$new.MaximumSize = {swap_mb}; "
        f"$new.Put()"
    )

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            logger.info("Pagefile created: %s", pagefile_path)
            return {
                "success": True,
                "method": "windows_pagefile",
                "path": pagefile_path,
                "size_gb": round(swap_bytes / GB, 1),
                "speed_mbs": round(speed_mbs, 1),
                "needs_reboot": True,
                "note": "Pagefile active after reboot. All programs benefit automatically.",
            }
        else:
            return {
                "success": False,
                "method": "windows_pagefile",
                "error": r.stderr.strip() or "WMI CreateInstance failed",
            }
    except Exception as e:
        return {"success": False, "method": "windows_pagefile", "error": str(e)}


def _extend_linux(mount_path: str, swap_bytes: int, speed_mbs: float) -> Dict[str, Any]:
    """Linux: create and activate swap file on external device."""
    swap_file = os.path.join(mount_path, "vram_boost.swap")
    swap_mb = swap_bytes // MB

    logger.info("Creating Linux swap: %s (%d MB)", swap_file, swap_mb)

    try:
        # Check if already active
        r = subprocess.run(["swapon", "--show=NAME", "--noheadings"],
                           capture_output=True, text=True, timeout=5)
        if swap_file in (r.stdout or ""):
            return {
                "success": True,
                "method": "linux_swap",
                "status": "already_active",
                "path": swap_file,
                "speed_mbs": round(speed_mbs, 1),
            }

        # Create swap file
        if hasattr(os, 'posix_fallocate'):
            fd = os.open(swap_file, os.O_CREAT | os.O_RDWR)
            try:
                os.posix_fallocate(fd, 0, swap_bytes)
            finally:
                os.close(fd)
        else:
            subprocess.run(
                ["dd", "if=/dev/zero", f"of={swap_file}",
                 "bs=1M", f"count={swap_mb}", "status=progress"],
                check=True, timeout=600,
            )

        os.chmod(swap_file, 0o600)

        # Format
        subprocess.run(["mkswap", swap_file], check=True, timeout=30)

        # Activate with TRIM support (for SD Express/NVMe)
        subprocess.run(
            ["swapon", "-d", "-p", "10", swap_file],
            check=True, timeout=30,
        )

        logger.info("Swap active: %s", swap_file)
        return {
            "success": True,
            "method": "linux_swap",
            "path": swap_file,
            "size_gb": round(swap_bytes / GB, 1),
            "speed_mbs": round(speed_mbs, 1),
            "needs_reboot": False,
            "note": "Swap active NOW. All programs benefit automatically.",
        }

    except Exception as e:
        return {"success": False, "method": "linux_swap", "error": str(e)}


def status() -> Dict[str, Any]:
    """Query current OS memory extension status."""
    system = platform.system()
    result = {"os": system, "pagefiles": []}

    if system == "Windows":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_PageFileUsage | "
                 "Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage | "
                 "ConvertTo-Json"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                import json
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                for pf in data:
                    is_external = not pf["Name"].upper().startswith("C:")
                    result["pagefiles"].append({
                        "path": pf["Name"],
                        "allocated_mb": pf["AllocatedBaseSize"],
                        "used_mb": pf["CurrentUsage"],
                        "peak_mb": pf["PeakUsage"],
                        "is_external": is_external,
                    })
        except Exception as e:
            result["error"] = str(e)

    else:  # Linux
        try:
            r = subprocess.run(
                ["swapon", "--show=NAME,SIZE,USED,PRIO", "--bytes", "--noheadings"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (r.stdout or "").strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    name = parts[0]
                    is_external = "vram" in name.lower() or "/media" in name or "/mnt" in name
                    result["pagefiles"].append({
                        "path": name,
                        "is_external": is_external,
                    })
        except Exception as e:
            result["error"] = str(e)

    return result


def remove(device_path: str) -> Dict[str, Any]:
    """Remove memory extension from device."""
    system = platform.system()

    if not _is_admin():
        return {"success": False, "error": "Requires administrator/root"}

    if system == "Windows":
        letter = device_path.rstrip(":").rstrip("\\")[0].upper()
        ps_cmd = f"wmic pagefileset where name='{letter}:\\\\pagefile.sys' delete"
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15,
            )
            return {
                "success": r.returncode == 0,
                "method": "windows_pagefile",
                "needs_reboot": True,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    else:
        swap_file = os.path.join(device_path, "vram_boost.swap")
        try:
            subprocess.run(["swapoff", swap_file], check=True, timeout=30)
            os.unlink(swap_file)
            return {"success": True, "method": "linux_swap"}
        except Exception as e:
            return {"success": False, "error": str(e)}
