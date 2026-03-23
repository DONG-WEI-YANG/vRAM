"""
Legacy USB Flash Drive / Portable SSD Driver
===============================================
普通 USB 隨身碟與 USB 外接 SSD 驅動。
使用檔案系統 I/O（不需 admin 權限，不需要 PCIe Tunneling）。

涵蓋：
  - USB 2.0 隨身碟 (~35 MB/s)
  - USB 3.0 隨身碟 (~150-300 MB/s)
  - USB 3.x 外接 SSD (~500-900 MB/s)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Optional, Any

from .base_device import BaseDevice, DeviceInfo, DeviceCapability, ConnectionProtocol
from ..core.health_monitor import DeviceMetrics

logger = logging.getLogger(__name__)


class USBLegacyDevice(BaseDevice):
    """
    普通 USB 隨身碟 / 外接 SSD 驅動。
    透過檔案系統 I/O 運作，任何 USB 儲存裝置都能用。
    """

    POOL_DIR = ""

    def __init__(self):
        super().__init__()
        self._mount_point: Optional[Path] = None
        self._pool_dir: Optional[Path] = None
        self._is_windows = platform.system().lower() == "windows"
        self._measured_read_mbs: float = 0
        self._measured_write_mbs: float = 0
        self._is_ssd: bool = False

    def detect(self) -> List[DeviceInfo]:
        if self._is_windows:
            return self._detect_windows()
        return self._detect_linux()

    def initialize(self, device_info: DeviceInfo) -> bool:
        self._info = device_info
        self._mount_point = Path(device_info.device_path)

        if not self._mount_point.exists():
            logger.error("USB mount point not found: %s", self._mount_point)
            return False

        self._pool_dir = self._mount_point

        # 速度測試
        self._measured_read_mbs, self._measured_write_mbs = self._benchmark_quick()
        self._is_ssd = self._measured_read_mbs > 200  # 簡化判斷

        self._capability = DeviceCapability(
            max_bandwidth_mbs=self._measured_read_mbs * 1.2,
            typical_bandwidth_mbs=self._measured_read_mbs,
            min_latency_us=200 if self._is_ssd else 1000,
            typical_latency_us=500 if self._is_ssd else 2000,
            max_capacity_bytes=device_info.capacity_bytes,
        )

        self._is_initialized = True
        free = shutil.disk_usage(self._mount_point).free
        logger.info(
            "USB Legacy initialized: %s (R=%.0f/W=%.0f MB/s, SSD=%s, %.1fGB free)",
            device_info.device_name,
            self._measured_read_mbs, self._measured_write_mbs,
            self._is_ssd, free / (1024**3),
        )
        return True

    def read_block(self, block_id: str, offset: int, size: int) -> bytes:
        if not self._pool_dir:
            raise RuntimeError("Not initialized")
        block_file = self._pool_dir / f"{block_id}.bin"
        if not block_file.exists():
            raise KeyError(f"Block '{block_id}' not found")
        with open(block_file, "rb") as f:
            f.seek(offset)
            data = f.read(size)
        return data if len(data) >= size else data + b"\x00" * (size - len(data))

    def write_block(self, block_id: str, offset: int, data: bytes) -> bool:
        if not self._pool_dir:
            raise RuntimeError("Not initialized")
        block_file = self._pool_dir / f"{block_id}.bin"
        try:
            mode = "r+b" if block_file.exists() else "wb"
            with open(block_file, mode) as f:
                f.seek(offset)
                f.write(data)
            return True
        except OSError as e:
            logger.error("USB write error: %s", e)
            return False

    def allocate_block(self, block_id: str, size: int) -> bool:
        if block_id in self._allocated_blocks:
            return False
        free = shutil.disk_usage(self._mount_point).free if self._mount_point else 0
        if size > free:
            return False
        block_file = self._pool_dir / f"{block_id}.bin"
        try:
            with open(block_file, "wb") as f:
                f.truncate(size)
            self._allocated_blocks[block_id] = 0
            self._total_allocated += size
            return True
        except OSError:
            return False

    def free_block(self, block_id: str) -> bool:
        if block_id not in self._allocated_blocks:
            return False
        block_file = self._pool_dir / f"{block_id}.bin"
        try:
            block_file.unlink(missing_ok=True)
        except OSError:
            pass
        del self._allocated_blocks[block_id]
        return True

    def get_metrics(self) -> DeviceMetrics:
        connected = self._mount_point.exists() if self._mount_point else False
        return DeviceMetrics(
            device_id=self._info.device_id if self._info else "unknown",
            device_type="usb_legacy",
            timestamp=time.time(),
            is_connected=connected,
            read_bandwidth_mbs=self._measured_read_mbs,
            write_bandwidth_mbs=self._measured_write_mbs,
        )

    def shutdown(self) -> None:
        self._allocated_blocks.clear()
        self._total_allocated = 0
        self._is_initialized = False

    def _benchmark_quick(self) -> tuple:
        """快速 benchmark (4MB)"""
        if not self._pool_dir:
            return (100.0, 50.0)
        test_file = self._pool_dir / ".bench"
        data = os.urandom(4 * 1024 * 1024)
        try:
            start = time.perf_counter()
            with open(test_file, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            w_mbs = 4 / (time.perf_counter() - start)

            start = time.perf_counter()
            with open(test_file, "rb") as f:
                f.read()
            r_mbs = 4 / (time.perf_counter() - start)

            test_file.unlink(missing_ok=True)
            return (r_mbs, w_mbs)
        except OSError:
            return (100.0, 50.0)

    def _detect_windows(self) -> List[DeviceInfo]:
        devices = []
        try:
            ps_cmd = (
                "Get-Volume | Where-Object {"
                "$_.DriveType -eq 'Removable' -or $_.DriveType -eq 'Fixed'"
                "} | Select-Object DriveLetter,FileSystemLabel,Size,SizeRemaining,DriveType,FileSystem | ConvertTo-Json"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]

                # 取得 USB 磁碟清單
                usb_disks = set()
                try:
                    ps2 = "Get-Disk | Where-Object {$_.BusType -eq 'USB'} | Select-Object Number | ConvertTo-Json"
                    r2 = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", ps2],
                        capture_output=True, text=True, timeout=10,
                    )
                    if r2.returncode == 0 and r2.stdout.strip():
                        d2 = json.loads(r2.stdout)
                        if isinstance(d2, dict):
                            d2 = [d2]
                        for disk in d2:
                            usb_disks.add(disk.get("Number"))
                except Exception:
                    pass

                # 取得每個 volume 對應的磁碟
                for vol in data:
                    letter = vol.get("DriveLetter")
                    size = vol.get("Size", 0)
                    dtype = vol.get("DriveType", "")
                    label = vol.get("FileSystemLabel", "")

                    if not letter or size <= 0:
                        continue

                    # 只選 USB 裝置（Removable 通常是 USB）
                    if dtype not in ("Removable",):
                        continue

                    mount = f"{letter}:\\"
                    devices.append(DeviceInfo(
                        device_id=f"usb_legacy_{letter}",
                        device_name=f"USB Drive ({label or 'USB'}) [{letter}:]",
                        model=label or "USB Storage",
                        protocol=ConnectionProtocol.USB3_GEN1,
                        capacity_bytes=int(size),
                        device_path=mount,
                        is_removable=True,
                    ))

        except Exception as e:
            logger.warning("Windows USB legacy detection failed: %s", e)
        return devices

    def _detect_linux(self) -> List[DeviceInfo]:
        devices = []
        try:
            result = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,RM,TRAN,MODEL"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for dev in data.get("blockdevices", []):
                    if dev.get("tran") == "usb" and dev.get("rm"):
                        for part in dev.get("children", []):
                            mount = part.get("mountpoint")
                            if mount:
                                model = dev.get("model", "USB Drive")
                                devices.append(DeviceInfo(
                                    device_id=f"usb_legacy_{part['name']}",
                                    device_name=f"USB Drive ({model})",
                                    model=model,
                                    protocol=ConnectionProtocol.USB3_GEN1,
                                    capacity_bytes=0,  # lsblk size 需要 parse
                                    device_path=mount,
                                    is_removable=True,
                                ))
        except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
            pass
        return devices
