"""
Cross-platform memory extension.
Creates pagefile (Windows) or swap file (Linux) on external device.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Dict, Any

from .policy import ExpansionPlan

logger = logging.getLogger(__name__)

MB = 1024 ** 2
GB = 1024 ** 3


@dataclass
class ExtendResult:
    success: bool
    method: str          # "windows_pagefile" or "linux_swap"
    path: str = ""
    size_gb: float = 0.0
    needs_reboot: bool = False
    error: str = ""


def is_admin() -> bool:
    """Check for elevated privileges."""
    if platform.system() == "Windows":
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return os.getuid() == 0


def execute(plan: ExpansionPlan) -> ExtendResult:
    """Execute an expansion plan."""
    if plan.action != "expand":
        return ExtendResult(success=False, method="none", error=plan.reason)

    if not is_admin():
        return ExtendResult(
            success=False, method="none",
            error="Requires administrator/root privileges",
        )

    system = platform.system()
    if system == "Windows":
        return _extend_windows(plan)
    elif system == "Linux":
        return _extend_linux(plan)
    else:
        return ExtendResult(success=False, method="none", error=f"Unsupported OS: {system}")


def _extend_windows(plan: ExpansionPlan) -> ExtendResult:
    """Create pagefile on external drive."""
    path = plan.device.path.rstrip("\\")
    letter = path[0].upper()
    swap_mb = plan.swap_bytes // MB
    pagefile = f"{letter}:\\pagefile.sys"

    # Check existing
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_PageFileSetting | Where-Object {{$_.Name -like '{letter}:*'}} | Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=10,
        )
        if r.stdout.strip() not in ("", "0"):
            return ExtendResult(
                success=True, method="windows_pagefile", path=pagefile,
                size_gb=0, error="already exists",
            )
    except Exception:
        pass

    # Create via WMI
    ps = (
        f"$pf = [wmiclass]'Win32_PageFileSetting'; "
        f"$n = $pf.CreateInstance(); "
        f"$n.Name = '{pagefile}'; "
        f"$n.InitialSize = {swap_mb}; "
        f"$n.MaximumSize = {swap_mb}; "
        f"$n.Put()"
    )

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            logger.info("Created pagefile: %s (%d MB)", pagefile, swap_mb)
            return ExtendResult(
                success=True, method="windows_pagefile",
                path=pagefile, size_gb=round(plan.swap_bytes / GB, 1),
                needs_reboot=True,
            )
        return ExtendResult(
            success=False, method="windows_pagefile",
            error=r.stderr.strip() or "WMI failed",
        )
    except Exception as e:
        return ExtendResult(success=False, method="windows_pagefile", error=str(e))


def _extend_linux(plan: ExpansionPlan) -> ExtendResult:
    """Create and activate swap file."""
    swap_file = os.path.join(plan.device.path, "vram_boost.swap")

    # Check active
    try:
        r = subprocess.run(
            ["swapon", "--show=NAME", "--noheadings"],
            capture_output=True, text=True, timeout=5,
        )
        if swap_file in (r.stdout or ""):
            return ExtendResult(
                success=True, method="linux_swap", path=swap_file,
                error="already active",
            )
    except Exception:
        pass

    try:
        # Allocate
        if hasattr(os, "posix_fallocate"):
            fd = os.open(swap_file, os.O_CREAT | os.O_RDWR)
            try:
                os.posix_fallocate(fd, 0, plan.swap_bytes)
            finally:
                os.close(fd)
        else:
            swap_mb = plan.swap_bytes // MB
            subprocess.run(
                ["dd", "if=/dev/zero", f"of={swap_file}",
                 "bs=1M", f"count={swap_mb}", "status=progress"],
                check=True, timeout=600,
            )

        os.chmod(swap_file, 0o600)
        subprocess.run(["mkswap", swap_file], check=True, timeout=30)
        subprocess.run(["swapon", "-d", "-p", "10", swap_file], check=True, timeout=30)

        logger.info("Swap active: %s", swap_file)
        return ExtendResult(
            success=True, method="linux_swap",
            path=swap_file, size_gb=round(plan.swap_bytes / GB, 1),
            needs_reboot=False,
        )
    except Exception as e:
        return ExtendResult(success=False, method="linux_swap", error=str(e))


def remove(device_path: str) -> ExtendResult:
    """Remove memory extension from device."""
    system = platform.system()
    if not is_admin():
        return ExtendResult(success=False, method="none", error="Need admin")

    if system == "Windows":
        letter = device_path.rstrip(":\\")[0].upper()
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-CimInstance Win32_PageFileSetting | Where-Object {{$_.Name -like '{letter}:*'}} | Remove-CimInstance"],
                check=True, capture_output=True, timeout=15,
            )
            return ExtendResult(success=True, method="windows_pagefile", needs_reboot=True)
        except Exception as e:
            return ExtendResult(success=False, method="windows_pagefile", error=str(e))
    else:
        swap_file = os.path.join(device_path, "vram_boost.swap")
        try:
            subprocess.run(["swapoff", swap_file], check=True, timeout=30)
            os.unlink(swap_file)
            return ExtendResult(success=True, method="linux_swap")
        except Exception as e:
            return ExtendResult(success=False, method="linux_swap", error=str(e))
