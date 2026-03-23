"""
Legacy SD Card Device Driver (UHS-I / UHS-II / UHS-III)
=========================================================
普通 SD 卡驅動 — 使用檔案系統 I/O 而非 NVMe 直接存取。

與 sd_express.py 的差異：
  SD Express → OS 辨識為 NVMe SSD → 區塊裝置直接 I/O
  SD Legacy  → OS 辨識為卸除式磁碟 → 檔案系統 I/O（掛載點 + 檔案讀寫）

你的 500GB SDXC 卡就是這種 — Realtek RTS525A 讀卡機 + UHS-I 模式。
雖然慢（~100 MB/s），但搭配壓縮 + buffer 後可用於 KV Cache 卸載。
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


class SDLegacyProtocol:
    """SD Legacy 的連線協定（非 PCIe）"""
    UHS_I = "sd_uhs_i"       # max 104 MB/s
    UHS_II = "sd_uhs_ii"     # max 312 MB/s
    UHS_III = "sd_uhs_iii"   # max 624 MB/s
    DEFAULT = "sd_default"    # max 25 MB/s


class SDLegacyDevice(BaseDevice):
    """
    普通 SD / SDXC / SDHC 卡驅動。

    使用檔案系統層 I/O（不需要 admin 權限）：
    - 在 SD 卡上建立 .vram_pool/ 目錄
    - 每個 memory block 存為一個檔案
    - 搭配 SlowDeviceOptimizer 做壓縮 + buffer
    """

    POOL_DIR = ""  # 直接使用根目錄，不建子目錄

    def __init__(self):
        super().__init__()
        self._mount_point: Optional[Path] = None
        self._pool_dir: Optional[Path] = None
        self._is_windows = platform.system().lower() == "windows"
        self._sd_protocol = SDLegacyProtocol.UHS_I

    def detect(self) -> List[DeviceInfo]:
        """偵測系統中的可移除式 SD/SDXC 卡"""
        if self._is_windows:
            return self._detect_windows()
        return self._detect_linux()

    def initialize(self, device_info: DeviceInfo) -> bool:
        """初始化 SD 卡為 VRAM pool 儲存"""
        self._info = device_info

        # 取得掛載點
        self._mount_point = Path(device_info.device_path)
        if not self._mount_point.exists():
            logger.error("SD card mount point not found: %s", self._mount_point)
            return False

        # 直接使用 SD 卡根目錄
        self._pool_dir = self._mount_point

        # 偵測速度等級
        self._sd_protocol = self._detect_speed_class()

        # 設定 capability
        self._capability = self._resolve_capability(self._sd_protocol)

        self._is_initialized = True
        free_space = shutil.disk_usage(self._mount_point).free

        logger.info(
            "SD Legacy initialized: %s (%s, %.1fGB free, mount=%s)",
            device_info.device_name,
            self._sd_protocol,
            free_space / (1024**3),
            self._mount_point,
        )
        return True

    def read_block(self, block_id: str, offset: int, size: int) -> bytes:
        if not self._is_initialized or not self._pool_dir:
            raise RuntimeError("Device not initialized")

        block_file = self._pool_dir / f"{block_id}.bin"
        if not block_file.exists():
            raise KeyError(f"Block file not found: {block_id}")

        try:
            with open(block_file, "rb") as f:
                f.seek(offset)
                data = f.read(size)
            return data if len(data) >= size else data + b"\x00" * (size - len(data))
        except OSError as e:
            logger.error("SD read error: %s", e)
            raise

    def write_block(self, block_id: str, offset: int, data: bytes) -> bool:
        if not self._is_initialized or not self._pool_dir:
            raise RuntimeError("Device not initialized")

        block_file = self._pool_dir / f"{block_id}.bin"
        try:
            mode = "r+b" if block_file.exists() else "wb"
            with open(block_file, mode) as f:
                f.seek(offset)
                f.write(data)
            return True
        except OSError as e:
            logger.error("SD write error: %s", e)
            return False

    def allocate_block(self, block_id: str, size: int) -> bool:
        if block_id in self._allocated_blocks:
            return False

        free_space = shutil.disk_usage(self._mount_point).free if self._mount_point else 0
        if size > free_space:
            logger.error("SD card full: need %dMB, free %dMB",
                         size // (1024**2), free_space // (1024**2))
            return False

        # 預分配檔案
        block_file = self._pool_dir / f"{block_id}.bin"
        try:
            with open(block_file, "wb") as f:
                f.truncate(size)
            self._allocated_blocks[block_id] = 0
            self._total_allocated += size
            return True
        except OSError as e:
            logger.error("SD allocate failed: %s", e)
            return False

    def free_block(self, block_id: str) -> bool:
        if block_id not in self._allocated_blocks:
            return False

        block_file = self._pool_dir / f"{block_id}.bin"
        try:
            if block_file.exists():
                block_file.unlink()
        except OSError:
            pass

        del self._allocated_blocks[block_id]
        return True

    def get_metrics(self) -> DeviceMetrics:
        connected = self._mount_point.exists() if self._mount_point else False
        return DeviceMetrics(
            device_id=self._info.device_id if self._info else "unknown",
            device_type="sd_legacy",
            timestamp=time.time(),
            is_connected=connected,
            connection_protocol=self._sd_protocol,
        )

    def shutdown(self) -> None:
        self._allocated_blocks.clear()
        self._total_allocated = 0
        self._is_initialized = False
        logger.info("SD Legacy device shut down")

    @property
    def free_space_bytes(self) -> int:
        if self._mount_point and self._mount_point.exists():
            return shutil.disk_usage(self._mount_point).free
        return 0

    # ── Detection ──

    def _detect_windows(self) -> List[DeviceInfo]:
        devices = []
        try:
            ps_cmd = (
                "Get-Volume | Where-Object {$_.DriveType -eq 'Removable'} | "
                "Select-Object DriveLetter,FileSystemLabel,Size,SizeRemaining,FileSystem | ConvertTo-Json"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]

                for vol in data:
                    letter = vol.get("DriveLetter")
                    size = vol.get("Size", 0)
                    remaining = vol.get("SizeRemaining", 0)
                    label = vol.get("FileSystemLabel", "")
                    fs = vol.get("FileSystem", "")

                    if not letter or size <= 0:
                        continue

                    mount = f"{letter}:\\"
                    devices.append(DeviceInfo(
                        device_id=f"sd_legacy_{letter}",
                        device_name=f"SD Card ({label or 'SDXC'}) [{letter}:]",
                        vendor="",
                        model=label or "SDXC Card",
                        protocol=ConnectionProtocol.UNKNOWN,
                        capacity_bytes=int(size),
                        device_path=mount,
                        is_removable=True,
                    ))

        except Exception as e:
            logger.warning("Windows SD legacy detection failed: %s", e)

        return devices

    def _detect_linux(self) -> List[DeviceInfo]:
        devices = []
        try:
            result = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,RM,FSTYPE,MODEL"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for dev in data.get("blockdevices", []):
                    if dev.get("rm") and dev.get("type") == "disk":
                        for part in dev.get("children", []):
                            mount = part.get("mountpoint")
                            if mount:
                                size_str = part.get("size", "0")
                                model = dev.get("model", "SD Card")
                                devices.append(DeviceInfo(
                                    device_id=f"sd_legacy_{part['name']}",
                                    device_name=f"SD Card ({model})",
                                    model=model,
                                    protocol=ConnectionProtocol.UNKNOWN,
                                    capacity_bytes=self._parse_size(size_str),
                                    device_path=mount,
                                    is_removable=True,
                                ))
        except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
            pass
        return devices

    def _detect_speed_class(self) -> str:
        """透過簡單 benchmark 推斷 SD 卡速度等級"""
        if not self._pool_dir:
            return SDLegacyProtocol.DEFAULT

        test_file = self._pool_dir / ".speed_test"
        try:
            data = os.urandom(4 * 1024 * 1024)  # 4MB
            start = time.perf_counter()
            with open(test_file, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            write_time = time.perf_counter() - start
            write_mbs = (len(data) / (1024 * 1024)) / write_time

            start = time.perf_counter()
            with open(test_file, "rb") as f:
                f.read()
            read_time = time.perf_counter() - start
            read_mbs = (len(data) / (1024 * 1024)) / read_time

            test_file.unlink(missing_ok=True)

            logger.info("SD speed test: read=%.0f MB/s, write=%.0f MB/s", read_mbs, write_mbs)

            if read_mbs >= 400:
                return SDLegacyProtocol.UHS_III
            elif read_mbs >= 150:
                return SDLegacyProtocol.UHS_II
            elif read_mbs >= 40:
                return SDLegacyProtocol.UHS_I
            return SDLegacyProtocol.DEFAULT

        except OSError:
            return SDLegacyProtocol.DEFAULT

    @staticmethod
    def _resolve_capability(protocol: str) -> DeviceCapability:
        caps = {
            SDLegacyProtocol.UHS_I: DeviceCapability(
                max_bandwidth_mbs=104, typical_bandwidth_mbs=80,
                min_latency_us=500, typical_latency_us=1000,
                max_capacity_bytes=2 * (1024**4),
            ),
            SDLegacyProtocol.UHS_II: DeviceCapability(
                max_bandwidth_mbs=312, typical_bandwidth_mbs=250,
                min_latency_us=200, typical_latency_us=500,
                max_capacity_bytes=2 * (1024**4),
            ),
            SDLegacyProtocol.UHS_III: DeviceCapability(
                max_bandwidth_mbs=624, typical_bandwidth_mbs=500,
                min_latency_us=100, typical_latency_us=300,
                max_capacity_bytes=2 * (1024**4),
            ),
        }
        return caps.get(protocol, DeviceCapability(
            max_bandwidth_mbs=25, typical_bandwidth_mbs=20,
            min_latency_us=2000, typical_latency_us=5000,
            max_capacity_bytes=256 * (1024**3),
        ))

    @staticmethod
    def _parse_size(size_str: str) -> int:
        try:
            s = size_str.upper().strip()
            if s.endswith("T"):
                return int(float(s[:-1]) * (1024**4))
            if s.endswith("G"):
                return int(float(s[:-1]) * (1024**3))
            if s.endswith("M"):
                return int(float(s[:-1]) * (1024**2))
            return int(s)
        except (ValueError, IndexError):
            return 0
