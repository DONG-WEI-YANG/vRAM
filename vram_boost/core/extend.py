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


def _get_filesystem(drive_letter: str) -> str:
    """Get filesystem type for a Windows drive."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Volume -DriveLetter {drive_letter}).FileSystem"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip().upper() if r.returncode == 0 else ""
    except Exception:
        return ""


def _extend_windows(plan: ExpansionPlan) -> ExtendResult:
    """
    Windows memory extension — auto-selects method by filesystem:
      NTFS → Windows pagefile (OS-level, all programs benefit)
      FAT32/exFAT → mmap swap files (application-level, <4GB chunks for FAT32)
    """
    path = plan.device.path.rstrip("\\")
    letter = path[0].upper()
    fs = _get_filesystem(letter)
    logger.info("Drive %s: filesystem=%s", letter, fs)

    if fs == "NTFS":
        return _extend_windows_pagefile(plan, letter)
    else:
        return _extend_windows_swapfile(plan, letter, fs)


def _extend_windows_pagefile(plan: ExpansionPlan, letter: str) -> ExtendResult:
    """NTFS: create Windows pagefile (best — OS-level)."""
    swap_mb = plan.swap_bytes // MB
    pagefile = f"{letter}:\\pagefile.sys"

    # Check existing
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_PageFileSetting | "
             f"Where-Object {{$_.Name -like '{letter}:*'}} | "
             f"Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=10,
        )
        if r.stdout.strip() not in ("", "0"):
            return ExtendResult(
                success=True, method="windows_pagefile", path=pagefile,
                size_gb=0, error="already exists",
            )
    except Exception:
        pass

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


def _extend_windows_swapfile(plan: ExpansionPlan, letter: str, fs: str) -> ExtendResult:
    """
    FAT32/exFAT: create swap files directly (no pagefile support).

    FAT32 has 4GB file limit, so we create multiple chunks.
    These are mmap-backed files that our system uses directly.
    OS doesn't auto-manage these — our daemon/monitor handles them.
    """
    swap_dir = f"{letter}:\\.vram"
    os.makedirs(swap_dir, exist_ok=True)

    # FAT32: max 4GB per file. Use 2GB chunks for safety.
    # exFAT: no limit, but keep chunks for consistency.
    if fs == "FAT32":
        chunk_bytes = 2 * GB
    else:
        chunk_bytes = min(plan.swap_bytes, 4 * GB)

    total_bytes = plan.swap_bytes
    created_files = []
    total_created = 0
    chunk_idx = 0

    logger.info("Creating swap files on %s (%s), total %.1f GB in %d MB chunks",
                letter, fs, total_bytes / GB, chunk_bytes // MB)

    while total_created < total_bytes:
        this_chunk = min(chunk_bytes, total_bytes - total_created)
        chunk_path = os.path.join(swap_dir, f"swap_{chunk_idx:03d}.bin")

        try:
            # Pre-allocate with zero-fill
            fd = os.open(chunk_path, os.O_CREAT | os.O_RDWR | os.O_BINARY)
            try:
                fill_block = b"\x00" * (4 * MB)
                written = 0
                while written < this_chunk:
                    to_write = min(len(fill_block), this_chunk - written)
                    os.write(fd, fill_block[:to_write])
                    written += to_write
                os.fsync(fd)
            finally:
                os.close(fd)

            created_files.append(chunk_path)
            total_created += this_chunk
            chunk_idx += 1
            logger.info("  Created %s (%.1f GB)", chunk_path, this_chunk / GB)

        except OSError as e:
            logger.error("Failed to create chunk %s: %s", chunk_path, e)
            break

    if not created_files:
        return ExtendResult(
            success=False, method="swap_file",
            error=f"Failed to create swap files on {fs} drive",
        )

    # Write manifest for cleanup
    import json
    manifest = {
        "method": "swap_file",
        "filesystem": fs,
        "files": created_files,
        "total_bytes": total_created,
        "created": __import__("time").time(),
    }
    manifest_path = os.path.join(swap_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return ExtendResult(
        success=True,
        method="swap_file",
        path=swap_dir,
        size_gb=round(total_created / GB, 1),
        needs_reboot=False,
    )


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
    """Remove memory extension from device — handles all methods."""
    system = platform.system()

    if system == "Windows":
        device_path = device_path.rstrip("\\").rstrip("/")
        if len(device_path) == 1:
            device_path = device_path + ":"
        letter = device_path[0].upper()

        # Try removing pagefile first (NTFS)
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-CimInstance Win32_PageFileSetting | "
                 f"Where-Object {{$_.Name -like '{letter}:*'}} | "
                 f"Remove-CimInstance"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0 and "Remove-CimInstance" not in r.stderr:
                return ExtendResult(success=True, method="windows_pagefile", needs_reboot=True)
        except Exception:
            pass

        # Try removing swap files (FAT32/exFAT)
        swap_dir = f"{letter}:\\.vram"
        if os.path.isdir(swap_dir):
            import shutil, json
            # Read manifest
            manifest_path = os.path.join(swap_dir, "manifest.json")
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                file_count = len(manifest.get("files", []))
            except Exception:
                file_count = len([f for f in os.listdir(swap_dir) if f.endswith(".bin")])

            shutil.rmtree(swap_dir, ignore_errors=True)
            return ExtendResult(
                success=True, method="swap_file",
                path=swap_dir,
            )

        # Try removing single swap file
        swap_file = f"{letter}:\\vram_boost.swap"
        if os.path.exists(swap_file):
            os.unlink(swap_file)
            return ExtendResult(success=True, method="swap_file")

        return ExtendResult(success=True, method="none", error="nothing to remove")

    else:
        # Linux
        swap_file = os.path.join(device_path, "vram_boost.swap")
        try:
            subprocess.run(["swapoff", swap_file], check=False, timeout=30)
            if os.path.exists(swap_file):
                os.unlink(swap_file)
            return ExtendResult(success=True, method="linux_swap")
        except Exception as e:
            return ExtendResult(success=False, method="linux_swap", error=str(e))
