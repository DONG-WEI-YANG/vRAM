"""
USB Storage Device Driver
==========================
USB 3.x / USB4 / Thunderbolt 外接儲存裝置驅動。

架構差異：
  - USB 3.x: xHCI + Bridge 晶片 → 2 次協定轉換 → 高延遲 (80-200μs)
  - USB4:    PCIe Tunneling      → 1 次封裝      → 低延遲 (12-15μs)

自動偵測裝置的 USB 版本與傳輸模式，選擇最佳 I/O 路徑。
包含 QoS 管理（避免 USB 匯流排上其他裝置干擾）。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Optional, Any

from .base_device import (
    BaseDevice, DeviceInfo, DeviceCapability, ConnectionProtocol,
)
from ..core.health_monitor import DeviceMetrics

logger = logging.getLogger(__name__)


class USBStorageDevice(BaseDevice):
    """
    USB 外接儲存裝置驅動。

    核心特性：
    - 自動偵測 USB 版本 (3.0/3.1/3.2/USB4)
    - PCIe Tunneling 偵測（USB4/TB4+）
    - QoS 優先級管理
    - 熱插拔防護（Memory Pinning + Fallback）
    """

    # USB 版本 → 協定映射
    USB_SPEED_MAP = {
        "5000": (ConnectionProtocol.USB3_GEN1, "USB 3.2 Gen1 (5Gbps)"),
        "10000": (ConnectionProtocol.USB3_GEN2, "USB 3.2 Gen2 (10Gbps)"),
        "20000": (ConnectionProtocol.USB3_GEN2X2, "USB 3.2 Gen2x2 (20Gbps)"),
        "40000": (ConnectionProtocol.USB4_V1, "USB4 v1 (40Gbps)"),
        "80000": (ConnectionProtocol.USB4_V2, "USB4 v2 (80Gbps)"),
    }

    MIN_BANDWIDTH_MBS = 200  # 低於此頻寬的 USB 裝置不適合 VRAM 擴展

    def __init__(self):
        super().__init__()
        self._device_fd: Optional[Any] = None
        self._pool_offset: int = 0
        self._pool_size: int = 0
        self._is_windows = platform.system().lower() == "windows"
        self._has_pcie_tunnel: bool = False
        self._qos_priority: int = 0  # 0=normal, 1=high

    def detect(self) -> List[DeviceInfo]:
        """掃描系統中的 USB 儲存裝置"""
        if self._is_windows:
            devices = self._detect_windows()
        else:
            devices = self._detect_linux()

        # 過濾掉頻寬過低的裝置
        capable = []
        for dev in devices:
            cap = self._resolve_capability(dev.protocol)
            if cap.typical_bandwidth_mbs >= self.MIN_BANDWIDTH_MBS:
                capable.append(dev)
            else:
                logger.info("Skipping %s: bandwidth %.0f MB/s below minimum %d",
                            dev.device_name, cap.typical_bandwidth_mbs, self.MIN_BANDWIDTH_MBS)

        logger.info("USB storage scan: found %d usable device(s) (of %d total)",
                     len(capable), len(devices))
        return capable

    def initialize(self, device_info: DeviceInfo) -> bool:
        """初始化 USB 儲存裝置"""
        self._info = device_info
        self._capability = self._resolve_capability(device_info.protocol)
        self._has_pcie_tunnel = self._capability.supports_pcie_tunneling

        try:
            if self._is_windows:
                self._init_windows(device_info)
            else:
                self._init_linux(device_info)
        except Exception as e:
            logger.error("Failed to initialize USB storage: %s", e)
            return False

        # 保留 8% 空間
        usable = int(device_info.capacity_bytes * 0.92)
        self._pool_offset = device_info.capacity_bytes - usable
        self._pool_size = usable

        self._is_initialized = True
        tunnel_str = " [PCIe Tunnel]" if self._has_pcie_tunnel else ""
        logger.info(
            "USB storage initialized: %s (%s%s, %.1fGB usable)",
            device_info.device_name,
            device_info.protocol.value,
            tunnel_str,
            usable / (1024**3),
        )
        return True

    def read_block(self, block_id: str, offset: int, size: int) -> bytes:
        if not self._is_initialized:
            raise RuntimeError("Device not initialized")

        blk_offset = self._allocated_blocks.get(block_id)
        if blk_offset is None:
            raise KeyError(f"Block '{block_id}' not found")

        abs_offset = self._pool_offset + blk_offset + offset

        try:
            if self._device_fd:
                self._device_fd.seek(abs_offset)
                return self._device_fd.read(size)
            return b"\x00" * size
        except Exception as e:
            logger.error("USB read error: %s", e)
            raise

    def write_block(self, block_id: str, offset: int, data: bytes) -> bool:
        if not self._is_initialized:
            raise RuntimeError("Device not initialized")

        blk_offset = self._allocated_blocks.get(block_id)
        if blk_offset is None:
            raise KeyError(f"Block '{block_id}' not found")

        abs_offset = self._pool_offset + blk_offset + offset

        try:
            if self._device_fd:
                self._device_fd.seek(abs_offset)
                self._device_fd.write(data)
                self._device_fd.flush()
                return True
            return True
        except Exception as e:
            logger.error("USB write error: %s", e)
            return False

    def allocate_block(self, block_id: str, size: int) -> bool:
        if block_id in self._allocated_blocks:
            return False
        if self._total_allocated + size > self._pool_size:
            return False

        self._allocated_blocks[block_id] = self._total_allocated
        self._total_allocated += size
        return True

    def free_block(self, block_id: str) -> bool:
        if block_id not in self._allocated_blocks:
            return False
        del self._allocated_blocks[block_id]
        return True

    def get_metrics(self) -> DeviceMetrics:
        metrics = DeviceMetrics(
            device_id=self._info.device_id if self._info else "unknown",
            device_type="usb_storage",
            timestamp=time.time(),
            is_connected=self._check_connected(),
            connection_protocol=self._info.protocol.value if self._info else "",
        )

        if self._capability:
            metrics.read_bandwidth_mbs = self._capability.typical_bandwidth_mbs
            metrics.read_latency_us = self._capability.typical_latency_us

        return metrics

    def shutdown(self) -> None:
        if self._device_fd:
            try:
                self._device_fd.flush()
                self._device_fd.close()
            except Exception:
                pass
            self._device_fd = None

        self._allocated_blocks.clear()
        self._total_allocated = 0
        self._is_initialized = False
        logger.info("USB storage device shut down")

    # ── USB 專屬功能 ──

    @property
    def has_pcie_tunneling(self) -> bool:
        return self._has_pcie_tunnel

    def set_qos_priority(self, high: bool = True) -> None:
        """
        設定 USB I/O 的 QoS 優先級。
        在 Linux 上透過 ionice/cgroup 實作。
        """
        self._qos_priority = 1 if high else 0
        if not self._is_windows:
            try:
                pid = os.getpid()
                # ionice -c 1 -n 0 = real-time priority
                cls = "1" if high else "2"
                subprocess.run(
                    ["ionice", "-c", cls, "-n", "0", "-p", str(pid)],
                    timeout=5, check=False,
                )
                logger.info("Set I/O QoS priority: %s", "HIGH" if high else "NORMAL")
            except FileNotFoundError:
                pass

    def get_usb_topology(self) -> Dict[str, Any]:
        """取得 USB 匯流排拓撲資訊（用於 debug）"""
        if not self._info:
            return {}

        return {
            "device": self._info.device_name,
            "protocol": self._info.protocol.value,
            "pcie_tunneling": self._has_pcie_tunnel,
            "protocol_conversions": self._capability.protocol_conversions if self._capability else 0,
            "qos_priority": "HIGH" if self._qos_priority else "NORMAL",
        }

    # ── Internal Detection ──

    def _detect_linux(self) -> List[DeviceInfo]:
        devices = []

        # 掃描 /sys/block/ 中的 USB 裝置
        block_path = Path("/sys/block")
        if not block_path.exists():
            return devices

        for dev in block_path.iterdir():
            try:
                # 檢查是否為 USB 裝置
                device_link = (dev / "device").resolve()
                device_str = str(device_link)
                if "usb" not in device_str.lower():
                    continue

                # 取得容量
                size_file = dev / "size"
                if not size_file.exists():
                    continue
                sectors = int(size_file.read_text().strip())
                capacity = sectors * 512
                if capacity < 1024 * 1024 * 1024:  # 跳過 < 1GB
                    continue

                # 取得型號
                model_file = dev / "device" / "model"
                model = model_file.read_text().strip() if model_file.exists() else dev.name

                # 推測 USB 版本
                protocol = self._guess_usb_protocol_linux(device_link)

                dev_name = dev.name
                devices.append(DeviceInfo(
                    device_id=f"usb_{dev_name}",
                    device_name=f"USB Storage ({model})",
                    model=model,
                    protocol=protocol,
                    capacity_bytes=capacity,
                    device_path=f"/dev/{dev_name}",
                    is_removable=True,
                ))

            except (OSError, ValueError) as e:
                logger.debug("Skip block device %s: %s", dev.name, e)

        return devices

    def _detect_windows(self) -> List[DeviceInfo]:
        devices = []

        try:
            ps_cmd = (
                "Get-PhysicalDisk | Where-Object {$_.BusType -eq 'USB'} | "
                "Select-Object DeviceId,FriendlyName,Size,SerialNumber,MediaType | ConvertTo-Json"
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
                    if size < 1024 * 1024 * 1024:  # 跳過 < 1GB
                        continue

                    friendly = disk.get("FriendlyName", "")
                    dev_id = disk.get("DeviceId", "0")

                    devices.append(DeviceInfo(
                        device_id=f"usb_drive_{dev_id}",
                        device_name=f"USB Storage ({friendly})",
                        model=friendly,
                        protocol=ConnectionProtocol.USB3_GEN2,  # 預設
                        capacity_bytes=size,
                        device_path=f"\\\\.\\PhysicalDrive{dev_id}",
                        is_removable=True,
                    ))

        except Exception as e:
            logger.warning("Windows USB detection failed: %s", e)

        return devices

    def _init_linux(self, info: DeviceInfo) -> None:
        try:
            self._device_fd = open(info.device_path, "r+b", buffering=0)
        except PermissionError:
            self._device_fd = None

    def _init_windows(self, info: DeviceInfo) -> None:
        try:
            self._device_fd = open(info.device_path, "r+b", buffering=0)
        except (PermissionError, OSError):
            self._device_fd = None

    def _check_connected(self) -> bool:
        if not self._info:
            return False
        if self._is_windows:
            return True  # Windows 無法簡單檢查
        return os.path.exists(self._info.device_path)

    @staticmethod
    def _guess_usb_protocol_linux(device_path: Path) -> ConnectionProtocol:
        """從 sysfs 推斷 USB 版本"""
        try:
            current = device_path
            for _ in range(10):
                speed_file = current / "speed"
                if speed_file.exists():
                    speed = speed_file.read_text().strip()
                    for key, (proto, _) in USBStorageDevice.USB_SPEED_MAP.items():
                        if speed == key:
                            return proto
                    # Fallback by range
                    speed_int = int(speed)
                    if speed_int >= 40000:
                        return ConnectionProtocol.USB4_V1
                    elif speed_int >= 10000:
                        return ConnectionProtocol.USB3_GEN2
                    elif speed_int >= 5000:
                        return ConnectionProtocol.USB3_GEN1
                current = current.parent
        except (OSError, ValueError):
            pass
        return ConnectionProtocol.USB3_GEN2  # 預設

    @staticmethod
    def _resolve_capability(protocol: ConnectionProtocol) -> DeviceCapability:
        caps = {
            ConnectionProtocol.USB3_GEN1: DeviceCapability(
                max_bandwidth_mbs=500, typical_bandwidth_mbs=400,
                min_latency_us=150, typical_latency_us=200,
                max_capacity_bytes=4 * (1024**4),
                supports_pcie_tunneling=False, protocol_conversions=2,
            ),
            ConnectionProtocol.USB3_GEN2: DeviceCapability(
                max_bandwidth_mbs=1000, typical_bandwidth_mbs=800,
                min_latency_us=100, typical_latency_us=150,
                max_capacity_bytes=4 * (1024**4),
                supports_pcie_tunneling=False, protocol_conversions=2,
            ),
            ConnectionProtocol.USB3_GEN2X2: DeviceCapability(
                max_bandwidth_mbs=1800, typical_bandwidth_mbs=1500,
                min_latency_us=80, typical_latency_us=120,
                max_capacity_bytes=4 * (1024**4),
                supports_pcie_tunneling=False, protocol_conversions=2,
            ),
            ConnectionProtocol.USB4_V1: DeviceCapability(
                max_bandwidth_mbs=3800, typical_bandwidth_mbs=3200,
                min_latency_us=12, typical_latency_us=15,
                max_capacity_bytes=8 * (1024**4),
                supports_pcie_tunneling=True, protocol_conversions=1,
            ),
            ConnectionProtocol.USB4_V2: DeviceCapability(
                max_bandwidth_mbs=7500, typical_bandwidth_mbs=6500,
                min_latency_us=10, typical_latency_us=12,
                max_capacity_bytes=8 * (1024**4),
                supports_pcie_tunneling=True, protocol_conversions=1,
            ),
        }
        return caps.get(protocol, DeviceCapability(
            max_bandwidth_mbs=500, typical_bandwidth_mbs=400,
            min_latency_us=150, typical_latency_us=200,
            max_capacity_bytes=4 * (1024**4),
            protocol_conversions=2,
        ))
