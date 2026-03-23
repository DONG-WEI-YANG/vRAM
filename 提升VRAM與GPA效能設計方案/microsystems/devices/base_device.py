"""
Base Device Interface
======================
所有外部儲存裝置的抽象基底類別。
定義統一的裝置生命週期與 I/O 介面。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any

from ..core.health_monitor import DeviceMetrics

logger = logging.getLogger(__name__)


class ConnectionProtocol(Enum):
    """連接協定"""
    # SD Express
    PCIE_GEN3_X1 = "pcie_gen3_x1"
    PCIE_GEN3_X2 = "pcie_gen3_x2"
    PCIE_GEN4_X1 = "pcie_gen4_x1"
    PCIE_GEN4_X2 = "pcie_gen4_x2"

    # USB
    USB3_GEN1 = "usb3_gen1"        # 5 Gbps
    USB3_GEN2 = "usb3_gen2"        # 10 Gbps
    USB3_GEN2X2 = "usb3_gen2x2"    # 20 Gbps
    USB4_V1 = "usb4_v1"            # 40 Gbps
    USB4_V2 = "usb4_v2"            # 80 Gbps

    # Thunderbolt
    TB3 = "thunderbolt_3"          # 40 Gbps
    TB4 = "thunderbolt_4"          # 40 Gbps
    TB5 = "thunderbolt_5"          # 80/120 Gbps

    UNKNOWN = "unknown"


@dataclass
class DeviceCapability:
    """裝置能力描述"""
    max_bandwidth_mbs: float
    typical_bandwidth_mbs: float
    min_latency_us: float
    typical_latency_us: float
    max_capacity_bytes: int
    supports_pcie_tunneling: bool = False
    supports_nvme: bool = False
    supports_hotplug: bool = True
    supports_trim: bool = False
    protocol_conversions: int = 0    # 協定轉換次數


@dataclass
class DeviceInfo:
    """裝置辨識資訊"""
    device_id: str
    device_name: str
    vendor: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    protocol: ConnectionProtocol = ConnectionProtocol.UNKNOWN
    capacity_bytes: int = 0
    device_path: str = ""            # e.g., /dev/nvme0n1, \\.\PhysicalDrive2
    is_removable: bool = True

    @property
    def capacity_gb(self) -> float:
        return self.capacity_bytes / (1024 ** 3)


class BaseDevice(ABC):
    """
    外部儲存裝置抽象基底。

    生命週期：
    1. detect()     — 掃描系統中可用的裝置
    2. initialize() — 初始化選定的裝置（格式化記憶池區域）
    3. allocate()   — 在裝置上分配記憶體區塊
    4. read/write() — 執行 I/O 操作
    5. free()       — 釋放記憶體區塊
    6. shutdown()   — 安全關閉裝置
    """

    def __init__(self):
        self._info: Optional[DeviceInfo] = None
        self._capability: Optional[DeviceCapability] = None
        self._is_initialized = False
        self._allocated_blocks: Dict[str, int] = {}  # block_id → offset
        self._total_allocated: int = 0

    @property
    def info(self) -> Optional[DeviceInfo]:
        return self._info

    @property
    def capability(self) -> Optional[DeviceCapability]:
        return self._capability

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def available_bytes(self) -> int:
        if not self._info:
            return 0
        return self._info.capacity_bytes - self._total_allocated

    # ── Abstract Methods (每種裝置必須實作) ──

    @abstractmethod
    def detect(self) -> List[DeviceInfo]:
        """
        掃描系統中所有可用的該類型裝置。
        Returns: 找到的裝置列表
        """
        ...

    @abstractmethod
    def initialize(self, device_info: DeviceInfo) -> bool:
        """
        初始化指定裝置作為 VRAM 擴展用途。
        包含：驗證協定、分配記憶池區域、設定 DMA 映射。
        Returns: 是否成功
        """
        ...

    @abstractmethod
    def read_block(self, block_id: str, offset: int, size: int) -> bytes:
        """
        從裝置讀取資料。
        Args:
            block_id: 區塊識別碼
            offset: 區塊內偏移量
            size: 讀取大小 (bytes)
        Returns: 讀取到的資料
        """
        ...

    @abstractmethod
    def write_block(self, block_id: str, offset: int, data: bytes) -> bool:
        """
        寫入資料到裝置。
        Returns: 是否成功
        """
        ...

    @abstractmethod
    def allocate_block(self, block_id: str, size: int) -> bool:
        """
        在裝置上分配一個記憶體區塊。
        Returns: 是否成功
        """
        ...

    @abstractmethod
    def free_block(self, block_id: str) -> bool:
        """釋放裝置上的記憶體區塊"""
        ...

    @abstractmethod
    def get_metrics(self) -> DeviceMetrics:
        """取得裝置當前的健康與效能指標"""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """安全關閉裝置，釋放所有資源"""
        ...

    # ── 共用方法 ──

    def benchmark(self, block_size_kb: int = 4096, iterations: int = 10) -> Dict[str, float]:
        """
        對裝置進行簡單的讀寫速度測試。
        Returns: {"read_mbs": ..., "write_mbs": ..., "latency_us": ...}
        """
        import time

        if not self._is_initialized:
            raise RuntimeError("Device not initialized")

        test_block = "benchmark_block"
        test_size = block_size_kb * 1024
        test_data = b"\x00" * test_size

        try:
            self.allocate_block(test_block, test_size)

            # 寫入測試
            write_start = time.perf_counter()
            for _ in range(iterations):
                self.write_block(test_block, 0, test_data)
            write_elapsed = time.perf_counter() - write_start
            write_mbs = (test_size * iterations / (1024 * 1024)) / write_elapsed

            # 讀取測試
            read_start = time.perf_counter()
            for _ in range(iterations):
                self.read_block(test_block, 0, test_size)
            read_elapsed = time.perf_counter() - read_start
            read_mbs = (test_size * iterations / (1024 * 1024)) / read_elapsed

            # 延遲測試 (4KB random read)
            small_data = b"\x00" * 4096
            self.write_block(test_block, 0, small_data)
            lat_start = time.perf_counter()
            for _ in range(100):
                self.read_block(test_block, 0, 4096)
            lat_elapsed = time.perf_counter() - lat_start
            latency_us = (lat_elapsed / 100) * 1_000_000

            return {
                "read_mbs": round(read_mbs, 1),
                "write_mbs": round(write_mbs, 1),
                "latency_us": round(latency_us, 1),
            }

        finally:
            self.free_block(test_block)

    def get_summary(self) -> Dict[str, Any]:
        """取得裝置摘要資訊"""
        return {
            "device_type": self.__class__.__name__,
            "device_id": self._info.device_id if self._info else None,
            "device_name": self._info.device_name if self._info else None,
            "protocol": self._info.protocol.value if self._info else None,
            "capacity_gb": self._info.capacity_gb if self._info else 0,
            "available_gb": self.available_bytes / (1024**3),
            "is_initialized": self._is_initialized,
            "allocated_blocks": len(self._allocated_blocks),
            "capability": {
                "max_bandwidth_mbs": self._capability.max_bandwidth_mbs,
                "typical_latency_us": self._capability.typical_latency_us,
                "pcie_tunneling": self._capability.supports_pcie_tunneling,
                "protocol_conversions": self._capability.protocol_conversions,
            } if self._capability else None,
        }
