"""
USB-VRAM Booster — USB 裝置偵測器
製作者：DONG. WEI YANG

偵測 USB 儲存裝置，判斷其協定版本、是否支援 PCIe Tunneling、
以及作為 VRAM 擴展的可行性。
"""

import subprocess
import platform
import os
import json
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum


class USBProtocol(Enum):
    USB_2_0 = "USB 2.0 (最高 60 MB/s)"
    USB_3_0 = "USB 3.2 Gen1 (最高 625 MB/s)"
    USB_3_1 = "USB 3.2 Gen2 (最高 1,250 MB/s)"
    USB_3_2 = "USB 3.2 Gen2x2 (最高 2,500 MB/s)"
    USB4_V1 = "USB4 v1 (最高 5,000 MB/s)"
    USB4_V2 = "USB4 v2 (最高 10,000 MB/s)"
    TB3 = "Thunderbolt 3 (最高 5,000 MB/s)"
    TB4 = "Thunderbolt 4 (最高 5,000 MB/s)"
    TB5 = "Thunderbolt 5 (最高 10,000 MB/s)"
    UNKNOWN = "未知"


# 各協定的理論最大頻寬 (MB/s)
USB_BANDWIDTH = {
    USBProtocol.USB_2_0: 60,
    USBProtocol.USB_3_0: 625,
    USBProtocol.USB_3_1: 1250,
    USBProtocol.USB_3_2: 2500,
    USBProtocol.USB4_V1: 5000,
    USBProtocol.USB4_V2: 10000,
    USBProtocol.TB3: 5000,
    USBProtocol.TB4: 5000,
    USBProtocol.TB5: 10000,
    USBProtocol.UNKNOWN: 0,
}

# 最低可用頻寬門檻 (MB/s)
MIN_USABLE_BANDWIDTH = 200


@dataclass
class USBDeviceInfo:
    device_path: str = ""           # /dev/sda, /dev/nvme0n1
    mount_point: str = ""
    capacity_gb: float = 0
    used_gb: float = 0
    free_gb: float = 0
    protocol: USBProtocol = USBProtocol.UNKNOWN
    max_bandwidth_mbs: int = 0
    measured_read_mbs: int = 0
    measured_write_mbs: int = 0
    is_nvme: bool = False           # NVMe 模式 (USB4 PCIe Tunnel)
    has_pcie_tunnel: bool = False   # 是否透過 PCIe Tunneling
    model_name: str = ""
    serial: str = ""
    bus_type: str = ""              # "USB", "Thunderbolt"
    controller_type: str = ""       # "xHCI", "PCIe Router"
    vram_expandable: bool = False

    @property
    def usable_for_vram_gb(self) -> float:
        """可用於 VRAM 擴展的空間（保留 10% 給系統）"""
        return self.free_gb * 0.9

    @property
    def summary(self) -> str:
        status = "可擴展 VRAM" if self.vram_expandable else "頻寬不足"
        tunnel = " [PCIe Tunnel]" if self.has_pcie_tunnel else ""
        return (f"{self.model_name} | {self.capacity_gb:.0f}GB | "
                f"{self.protocol.value}{tunnel} | {'✓ ' + status if self.vram_expandable else '✗ ' + status}")


class USBDetector:
    """偵測 USB 儲存裝置"""

    @staticmethod
    def detect() -> List[USBDeviceInfo]:
        os_type = platform.system()
        if os_type == "Linux":
            return USBDetector._detect_linux()
        elif os_type == "Windows":
            return USBDetector._detect_windows()
        return []

    @staticmethod
    def _detect_linux() -> List[USBDeviceInfo]:
        devices = []

        # 方法 1: 使用 lsblk 偵測 USB 儲存裝置
        try:
            result = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,SIZE,FSTYPE,MOUNTPOINT,TRAN,MODEL,SERIAL,TYPE"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for dev in data.get("blockdevices", []):
                    tran = dev.get("tran", "") or ""
                    if tran.lower() in ("usb", "thunderbolt") and dev.get("type") == "disk":
                        device = USBDeviceInfo()
                        device.device_path = f"/dev/{dev['name']}"
                        device.model_name = dev.get("model", "Unknown USB Device") or "Unknown"
                        device.serial = dev.get("serial", "") or ""
                        device.bus_type = tran.upper()

                        # 取得容量
                        size_str = dev.get("size", "0")
                        device.capacity_gb = USBDetector._parse_size(size_str)

                        # 取得掛載點
                        children = dev.get("children", [])
                        for child in children:
                            mp = child.get("mountpoint")
                            if mp:
                                device.mount_point = mp
                                break

                        # 判斷是否為 NVMe (USB4 PCIe Tunnel)
                        if dev["name"].startswith("nvme"):
                            device.is_nvme = True
                            device.has_pcie_tunnel = True

                        # 偵測 USB 版本
                        device.protocol = USBDetector._detect_usb_version_linux(dev["name"])
                        device.max_bandwidth_mbs = USB_BANDWIDTH.get(device.protocol, 0)

                        # 判斷是否可用於 VRAM 擴展
                        device.vram_expandable = device.max_bandwidth_mbs >= MIN_USABLE_BANDWIDTH

                        # 偵測控制器類型
                        if device.has_pcie_tunnel:
                            device.controller_type = "PCIe Router"
                        else:
                            device.controller_type = "xHCI"

                        # 取得可用空間
                        if device.mount_point:
                            try:
                                stat = os.statvfs(device.mount_point)
                                device.free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
                                total = (stat.f_blocks * stat.f_frsize) / (1024**3)
                                device.used_gb = total - device.free_gb
                            except Exception:
                                device.free_gb = device.capacity_gb * 0.8

                        devices.append(device)

        except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
            pass

        return devices

    @staticmethod
    def _detect_usb_version_linux(dev_name: str) -> USBProtocol:
        """偵測 Linux USB 裝置的協定版本"""
        try:
            # 檢查 sysfs USB 速度
            for usb_dev in os.listdir("/sys/bus/usb/devices/"):
                speed_file = f"/sys/bus/usb/devices/{usb_dev}/speed"
                if os.path.exists(speed_file):
                    with open(speed_file) as f:
                        speed = f.read().strip()
                    if speed == "480":
                        return USBProtocol.USB_2_0
                    elif speed == "5000":
                        return USBProtocol.USB_3_0
                    elif speed == "10000":
                        return USBProtocol.USB_3_1
                    elif speed == "20000":
                        return USBProtocol.USB_3_2
                    elif speed == "40000":
                        return USBProtocol.USB4_V1
                    elif speed == "80000":
                        return USBProtocol.USB4_V2

            # 檢查 Thunderbolt
            if os.path.exists("/sys/bus/thunderbolt"):
                for tb_dev in os.listdir("/sys/bus/thunderbolt/devices/"):
                    gen_file = f"/sys/bus/thunderbolt/devices/{tb_dev}/generation"
                    if os.path.exists(gen_file):
                        with open(gen_file) as f:
                            gen = f.read().strip()
                        if gen == "4":
                            return USBProtocol.TB4
                        elif gen == "3":
                            return USBProtocol.TB3

        except Exception:
            pass

        return USBProtocol.UNKNOWN

    @staticmethod
    def _detect_windows() -> List[USBDeviceInfo]:
        """偵測 Windows USB 儲存裝置"""
        devices = []
        try:
            # 使用 PowerShell 偵測 USB 磁碟
            ps_cmd = """
            Get-WmiObject Win32_DiskDrive | Where-Object { $_.InterfaceType -eq 'USB' } |
            Select-Object Model, SerialNumber, Size, MediaType, InterfaceType |
            ConvertTo-Json
            """
            result = subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
                for disk in data:
                    device = USBDeviceInfo()
                    device.model_name = disk.get("Model", "Unknown")
                    device.serial = disk.get("SerialNumber", "")
                    device.capacity_gb = int(disk.get("Size", 0)) / (1024**3)
                    device.bus_type = "USB"
                    device.protocol = USBProtocol.USB_3_0  # 預設
                    device.max_bandwidth_mbs = USB_BANDWIDTH[device.protocol]
                    device.vram_expandable = device.max_bandwidth_mbs >= MIN_USABLE_BANDWIDTH
                    device.controller_type = "xHCI"
                    device.free_gb = device.capacity_gb * 0.8
                    devices.append(device)
        except Exception:
            pass
        return devices

    @staticmethod
    def _parse_size(size_str: str) -> float:
        """解析 lsblk 的大小字串"""
        try:
            size_str = size_str.strip()
            if size_str.endswith("T"):
                return float(size_str[:-1]) * 1024
            elif size_str.endswith("G"):
                return float(size_str[:-1])
            elif size_str.endswith("M"):
                return float(size_str[:-1]) / 1024
            return float(size_str) / (1024**3)
        except (ValueError, IndexError):
            return 0
