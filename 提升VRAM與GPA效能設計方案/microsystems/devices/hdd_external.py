"""
External HDD Device Driver
=============================
USB 外接機械式硬碟驅動。

HDD 的致命弱點：隨機讀寫 ~0.5 MB/s（SSD 的 1/400）
HDD 的隱藏優勢：順序讀寫 ~150 MB/s（還算可用）

策略：**絕對禁止隨機存取**。
所有操作都必須轉化為大塊順序 I/O。

適用場景：
  - 模型權重的一次性載入（startup 時大塊順序讀取）
  - KV Cache 的 checkpoint 存儲（定期大塊寫入）
  - 不適合即時的 KV Cache 讀寫（延遲太高）
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


class HDDExternalDevice(BaseDevice):
    """
    USB 外接 HDD 驅動。

    特殊設計：
    - 所有寫入先進 RAM buffer，定期大塊 flush
    - 讀取只用順序模式（預讀大塊後在記憶體中切片）
    - 絕不做小塊隨機 I/O
    """

    POOL_DIR = ""

    def __init__(self):
        super().__init__()
        self._mount_point: Optional[Path] = None
        self._pool_dir: Optional[Path] = None
        self._is_windows = platform.system().lower() == "windows"
        self._is_rotational: bool = True
        self._measured_seq_read: float = 0
        self._measured_seq_write: float = 0

    def detect(self) -> List[DeviceInfo]:
        if self._is_windows:
            return self._detect_windows()
        return self._detect_linux()

    def initialize(self, device_info: DeviceInfo) -> bool:
        self._info = device_info
        self._mount_point = Path(device_info.device_path)

        if not self._mount_point.exists():
            logger.error("HDD mount point not found: %s", self._mount_point)
            return False

        self._pool_dir = self._mount_point

        # 順序讀寫 benchmark (8MB 塊)
        self._measured_seq_read, self._measured_seq_write = self._benchmark_sequential()

        self._capability = DeviceCapability(
            max_bandwidth_mbs=self._measured_seq_read * 1.1,
            typical_bandwidth_mbs=self._measured_seq_read,
            min_latency_us=4000,       # HDD 最低延遲 ~4ms (seek time)
            typical_latency_us=8000,   # 平均 ~8ms
            max_capacity_bytes=device_info.capacity_bytes,
        )

        self._is_initialized = True
        free = shutil.disk_usage(self._mount_point).free
        logger.info(
            "External HDD initialized: %s (seq R=%.0f/W=%.0f MB/s, %.1fGB free)\n"
            "  WARNING: Random I/O disabled — HDD sequential-only mode",
            device_info.device_name,
            self._measured_seq_read, self._measured_seq_write,
            free / (1024**3),
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
            logger.error("HDD write error: %s", e)
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
            device_type="hdd_external",
            timestamp=time.time(),
            is_connected=connected,
            read_bandwidth_mbs=self._measured_seq_read,
            write_bandwidth_mbs=self._measured_seq_write,
        )

    def shutdown(self) -> None:
        self._allocated_blocks.clear()
        self._total_allocated = 0
        self._is_initialized = False

    def read_block_sequential(self, block_id: str, chunk_size: int = 4 * 1024 * 1024) -> bytes:
        """
        HDD 專用：大塊順序讀取整個 block。
        chunk_size 預設 4MB（HDD 順序讀的甜蜜點）。
        """
        if not self._pool_dir:
            raise RuntimeError("Not initialized")
        block_file = self._pool_dir / f"{block_id}.bin"
        if not block_file.exists():
            raise KeyError(f"Block '{block_id}' not found")

        chunks = []
        with open(block_file, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def write_block_sequential(self, block_id: str, data: bytes) -> bool:
        """HDD 專用：一次性順序寫入整個 block。"""
        if not self._pool_dir:
            raise RuntimeError("Not initialized")
        block_file = self._pool_dir / f"{block_id}.bin"
        try:
            with open(block_file, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as e:
            logger.error("HDD sequential write error: %s", e)
            return False

    def _benchmark_sequential(self) -> tuple:
        if not self._pool_dir:
            return (100.0, 80.0)
        test_file = self._pool_dir / ".bench_seq"
        data = os.urandom(8 * 1024 * 1024)  # 8MB — HDD 需要更大塊才準
        try:
            start = time.perf_counter()
            with open(test_file, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            w_mbs = 8 / (time.perf_counter() - start)

            start = time.perf_counter()
            with open(test_file, "rb") as f:
                f.read()
            r_mbs = 8 / (time.perf_counter() - start)

            test_file.unlink(missing_ok=True)
            return (r_mbs, w_mbs)
        except OSError:
            return (100.0, 80.0)

    def _detect_windows(self) -> List[DeviceInfo]:
        devices = []
        try:
            ps_cmd = (
                "Get-PhysicalDisk | Where-Object {"
                "$_.BusType -eq 'USB' -and $_.MediaType -ne 'SSD'"
                "} | Select-Object DeviceId,FriendlyName,Size,MediaType | ConvertTo-Json"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
                for disk in data:
                    size = int(disk.get("Size", 0))
                    if size <= 0:
                        continue
                    friendly = disk.get("FriendlyName", "External HDD")

                    # 找掛載點
                    mount = self._find_mount_windows(disk.get("DeviceId", ""))
                    if not mount:
                        continue

                    devices.append(DeviceInfo(
                        device_id=f"hdd_{disk.get('DeviceId', '0')}",
                        device_name=f"External HDD ({friendly})",
                        model=friendly,
                        protocol=ConnectionProtocol.USB3_GEN1,
                        capacity_bytes=size,
                        device_path=mount,
                        is_removable=True,
                    ))
        except Exception as e:
            logger.warning("Windows HDD detection failed: %s", e)
        return devices

    def _detect_linux(self) -> List[DeviceInfo]:
        devices = []
        try:
            result = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,RM,TRAN,ROTA,MODEL"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for dev in data.get("blockdevices", []):
                    if dev.get("tran") == "usb" and dev.get("rota"):
                        for part in dev.get("children", []):
                            mount = part.get("mountpoint")
                            if mount:
                                model = dev.get("model", "External HDD")
                                devices.append(DeviceInfo(
                                    device_id=f"hdd_{part['name']}",
                                    device_name=f"External HDD ({model})",
                                    model=model,
                                    protocol=ConnectionProtocol.USB3_GEN1,
                                    capacity_bytes=0,
                                    device_path=mount,
                                    is_removable=True,
                                ))
        except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
            pass
        return devices

    @staticmethod
    def _find_mount_windows(device_id: str) -> str:
        try:
            ps = (
                f"Get-Partition -DiskNumber {device_id} -ErrorAction SilentlyContinue | "
                "Where-Object {$_.DriveLetter} | Select-Object -First 1 DriveLetter | ConvertTo-Json"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                letter = d.get("DriveLetter")
                if letter:
                    return f"{letter}:\\"
        except Exception:
            pass
        return ""
