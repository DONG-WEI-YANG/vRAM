"""
Cross-platform external device detection.
Finds USB drives, SD cards, and NVMe enclosures.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExternalDevice:
    path: str           # "E:\\" or "/media/sdcard"
    name: str           # "SD Card 500GB"
    bus: str            # "USB", "SD", "NVMe"
    size_bytes: int     # total capacity
    free_bytes: int     # available space
    disk_number: int = -1  # Windows disk number
    removable: bool = True

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024 ** 3)


def detect_devices() -> List[ExternalDevice]:
    """Detect all external storage devices."""
    system = platform.system()
    if system == "Windows":
        return _detect_windows()
    elif system == "Linux":
        return _detect_linux()
    else:
        logger.warning("Unsupported OS: %s", system)
        return []


def _detect_windows() -> List[ExternalDevice]:
    """Windows: Get-Disk + Get-Partition + Get-Volume cross-join by BusType."""
    devices = []
    try:
        # Step 1: Find external disks (USB, SD via SCSI card reader)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Disk | Where-Object {$_.BusType -eq 'USB' -or "
             "($_.BusType -eq 'SCSI' -and $_.FriendlyName -match 'Card|SD|Realtek|Reader')} "
             "| Select-Object Number, FriendlyName, BusType, Size | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return devices

        disks = json.loads(r.stdout)
        if isinstance(disks, dict):
            disks = [disks]

        external_nums = {}
        for d in disks:
            num = d.get("Number")
            if num is not None:
                bus = d.get("BusType", "USB")
                if "Card" in d.get("FriendlyName", "") or "SD" in d.get("FriendlyName", "") or "Realtek" in d.get("FriendlyName", ""):
                    bus = "SD"
                external_nums[int(num)] = (d.get("FriendlyName", ""), bus, int(d.get("Size", 0)))

        if not external_nums:
            return devices

        # Step 2: Map disk number → drive letter
        r2 = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Partition | Where-Object {$_.DriveLetter} "
             "| Select-Object DiskNumber, DriveLetter | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10,
        )
        if r2.returncode != 0:
            return devices

        parts = json.loads(r2.stdout)
        if isinstance(parts, dict):
            parts = [parts]

        for p in parts:
            disk_num = int(p.get("DiskNumber", -1))
            letter = p.get("DriveLetter", "")
            if disk_num in external_nums and letter:
                name, bus, total_size = external_nums[disk_num]
                path = f"{letter}:\\"

                # Get free space
                import shutil
                try:
                    total, used, free = shutil.disk_usage(path)
                except OSError:
                    continue

                devices.append(ExternalDevice(
                    path=path,
                    name=f"{name} ({letter}:)",
                    bus=bus,
                    size_bytes=total,
                    free_bytes=free,
                    disk_number=disk_num,
                ))

    except Exception as e:
        logger.warning("Windows detection failed: %s", e)

    return devices


def _detect_linux() -> List[ExternalDevice]:
    """Linux: lsblk to find removable block devices with mount points."""
    devices = []
    try:
        r = subprocess.run(
            ["lsblk", "-Jno", "NAME,MOUNTPOINT,SIZE,RM,TRAN,MODEL,FSAVAIL"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return devices

        data = json.loads(r.stdout)
        for dev in data.get("blockdevices", []):
            # Check children (partitions)
            for child in dev.get("children", [dev]):
                mount = child.get("mountpoint")
                rm = child.get("rm")
                tran = dev.get("tran", "")

                if not mount or not rm:
                    continue
                if mount in ("/", "/boot", "/boot/efi"):
                    continue

                bus = "USB" if tran == "usb" else ("SD" if "mmc" in dev.get("name", "") else "other")

                import shutil
                try:
                    total, used, free = shutil.disk_usage(mount)
                except OSError:
                    continue

                devices.append(ExternalDevice(
                    path=mount,
                    name=f"{dev.get('model', 'External')} ({mount})",
                    bus=bus,
                    size_bytes=total,
                    free_bytes=free,
                ))

    except Exception as e:
        logger.warning("Linux detection failed: %s", e)

    return devices
