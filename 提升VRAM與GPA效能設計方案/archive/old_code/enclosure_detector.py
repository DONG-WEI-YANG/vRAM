"""
Enclosure-VRAM Booster — 外接硬碟盒偵測器
製作者：DONG. WEI YANG

偵測 USB4/Thunderbolt 外接 NVMe 硬碟盒，判斷 PCIe Tunneling 支援度、
協定版本、頻寬等級，以及作為 VRAM 擴展的可行性。

與 USB 儲存裝置的關鍵差異：
- 外接硬碟盒走 PCIe Tunneling，協定轉換僅 1 次（封裝）
- USB 儲存裝置走 xHCI + Bridge，協定轉換 2 次
- 外接硬碟盒延遲 10-15μs，USB 儲存 80-200μs
"""

import subprocess
import platform
import os
import json
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum


class EnclosureProtocol(Enum):
    """外接硬碟盒連接協定"""
    USB4_V1 = "USB4 v1 PCIe Tunnel (40Gbps → 3,800 MB/s)"
    USB4_V2 = "USB4 v2 PCIe Tunnel (80Gbps → 7,500 MB/s)"
    TB3 = "Thunderbolt 3 PCIe Tunnel (40Gbps → 2,800 MB/s)"
    TB4 = "Thunderbolt 4 PCIe Tunnel (40Gbps → 3,000 MB/s)"
    TB5 = "Thunderbolt 5 PCIe Tunnel (120Gbps → 10,000 MB/s)"
    USB_BRIDGE = "USB xHCI Bridge (非 PCIe Tunnel，效能受限)"
    UNKNOWN = "未知"


# 各協定的實際可用頻寬 (MB/s)
# 理論值扣除協定開銷後的真實頻寬
ENCLOSURE_BANDWIDTH = {
    EnclosureProtocol.USB4_V1: 3800,
    EnclosureProtocol.USB4_V2: 7500,
    EnclosureProtocol.TB3: 2800,
    EnclosureProtocol.TB4: 3000,
    EnclosureProtocol.TB5: 10000,
    EnclosureProtocol.USB_BRIDGE: 1000,
    EnclosureProtocol.UNKNOWN: 0,
}

# 各協定的典型延遲 (μs)
ENCLOSURE_LATENCY = {
    EnclosureProtocol.USB4_V1: 15,
    EnclosureProtocol.USB4_V2: 12,
    EnclosureProtocol.TB3: 18,
    EnclosureProtocol.TB4: 15,
    EnclosureProtocol.TB5: 10,
    EnclosureProtocol.USB_BRIDGE: 100,
    EnclosureProtocol.UNKNOWN: 999,
}

# 最低可用頻寬門檻 (MB/s) — 外接硬碟盒門檻較高
MIN_ENCLOSURE_BANDWIDTH = 2000


@dataclass
class EnclosureInfo:
    """外接硬碟盒裝置資訊"""
    device_path: str = ""           # /dev/nvme0n1
    mount_point: str = ""
    capacity_gb: float = 0
    used_gb: float = 0
    free_gb: float = 0
    protocol: EnclosureProtocol = EnclosureProtocol.UNKNOWN
    max_bandwidth_mbs: int = 0
    typical_latency_us: int = 0
    measured_read_mbs: int = 0
    measured_write_mbs: int = 0
    is_nvme: bool = False
    has_pcie_tunnel: bool = False
    pcie_lanes: int = 0             # PCIe 通道數
    pcie_gen: str = ""              # PCIe 世代
    model_name: str = ""            # NVMe SSD 型號
    enclosure_model: str = ""       # 硬碟盒外殼型號
    serial: str = ""
    firmware: str = ""
    vram_expandable: bool = False

    @property
    def usable_for_vram_gb(self) -> float:
        """可用於 VRAM 擴展的空間（保留 10% 給系統 + 1GB swap）"""
        return max(0, self.free_gb * 0.9 - 1.0)

    @property
    def summary(self) -> str:
        status = "可擴展 VRAM" if self.vram_expandable else "不符合要求"
        tunnel = " [PCIe Tunnel]" if self.has_pcie_tunnel else " [USB Bridge]"
        return (f"{self.enclosure_model or self.model_name} | {self.capacity_gb:.0f}GB | "
                f"{self.protocol.value}{tunnel} | "
                f"{'✓ ' + status if self.vram_expandable else '✗ ' + status}")


class EnclosureDetector:
    """偵測 USB4/Thunderbolt 外接 NVMe 硬碟盒"""

    @staticmethod
    def detect() -> List[EnclosureInfo]:
        os_type = platform.system()
        if os_type == "Linux":
            return EnclosureDetector._detect_linux()
        elif os_type == "Windows":
            return EnclosureDetector._detect_windows()
        return []

    @staticmethod
    def _detect_linux() -> List[EnclosureInfo]:
        """Linux 偵測流程"""
        devices = []

        # 方法 1: 偵測 Thunderbolt 裝置
        tb_devices = EnclosureDetector._scan_thunderbolt_linux()

        # 方法 2: 偵測 NVMe 裝置並判斷是否為外接
        nvme_devices = EnclosureDetector._scan_nvme_linux()

        # 合併：NVMe 裝置若掛在 Thunderbolt/USB4 控制器下，即為外接硬碟盒
        for nvme in nvme_devices:
            if nvme.has_pcie_tunnel:
                nvme.vram_expandable = nvme.max_bandwidth_mbs >= MIN_ENCLOSURE_BANDWIDTH
                devices.append(nvme)

        return devices

    @staticmethod
    def _scan_thunderbolt_linux() -> List[dict]:
        """掃描 Linux Thunderbolt 裝置"""
        tb_info = []
        tb_path = "/sys/bus/thunderbolt/devices/"
        if not os.path.exists(tb_path):
            return tb_info

        for dev_name in os.listdir(tb_path):
            dev_dir = os.path.join(tb_path, dev_name)
            info = {"name": dev_name, "path": dev_dir}

            # 讀取世代
            gen_file = os.path.join(dev_dir, "generation")
            if os.path.exists(gen_file):
                try:
                    with open(gen_file) as f:
                        info["generation"] = int(f.read().strip())
                except (ValueError, IOError):
                    info["generation"] = 0

            # 讀取裝置名稱
            name_file = os.path.join(dev_dir, "device_name")
            if os.path.exists(name_file):
                try:
                    with open(name_file) as f:
                        info["device_name"] = f.read().strip()
                except IOError:
                    pass

            tb_info.append(info)

        return tb_info

    @staticmethod
    def _scan_nvme_linux() -> List[EnclosureInfo]:
        """掃描 Linux NVMe 裝置，判斷是否為外接"""
        devices = []

        try:
            result = subprocess.run(
                ["nvme", "list", "-o", "json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for dev in data.get("Devices", []):
                    enc = EnclosureInfo()
                    enc.device_path = dev.get("DevicePath", "")
                    enc.model_name = dev.get("ModelNumber", "").strip()
                    enc.serial = dev.get("SerialNumber", "").strip()
                    enc.firmware = dev.get("Firmware", "").strip()
                    enc.is_nvme = True

                    # 取得容量
                    size_bytes = dev.get("PhysicalSize", 0)
                    enc.capacity_gb = size_bytes / (1024**3)

                    # 判斷是否為外接裝置（透過 sysfs 檢查 PCIe 拓撲）
                    enc.has_pcie_tunnel = EnclosureDetector._is_external_nvme_linux(enc.device_path)

                    if enc.has_pcie_tunnel:
                        enc.protocol = EnclosureDetector._detect_tunnel_protocol_linux(enc.device_path)
                        enc.max_bandwidth_mbs = ENCLOSURE_BANDWIDTH.get(enc.protocol, 0)
                        enc.typical_latency_us = ENCLOSURE_LATENCY.get(enc.protocol, 999)

                        # 取得可用空間
                        try:
                            lsblk = subprocess.run(
                                ["lsblk", "-J", "-o", "NAME,SIZE,MOUNTPOINT",
                                 enc.device_path.split("/")[-1]],
                                capture_output=True, text=True, timeout=5
                            )
                            if lsblk.returncode == 0:
                                blk_data = json.loads(lsblk.stdout)
                                for bd in blk_data.get("blockdevices", []):
                                    for child in bd.get("children", []):
                                        mp = child.get("mountpoint")
                                        if mp:
                                            enc.mount_point = mp
                                            stat = os.statvfs(mp)
                                            enc.free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
                                            total = (stat.f_blocks * stat.f_frsize) / (1024**3)
                                            enc.used_gb = total - enc.free_gb
                                            break
                        except Exception:
                            enc.free_gb = enc.capacity_gb * 0.85

                    devices.append(enc)

        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

        return devices

    @staticmethod
    def _is_external_nvme_linux(device_path: str) -> bool:
        """判斷 NVMe 裝置是否為外接（透過 Thunderbolt/USB4）"""
        dev_name = device_path.split("/")[-1]  # nvme0n1 -> nvme0
        if "n" in dev_name:
            ctrl_name = dev_name.rsplit("n", 1)[0]
        else:
            ctrl_name = dev_name

        # 檢查 sysfs 路徑中是否包含 thunderbolt 或 usb4
        sysfs_path = f"/sys/class/nvme/{ctrl_name}"
        if os.path.exists(sysfs_path):
            try:
                real_path = os.path.realpath(sysfs_path)
                return "thunderbolt" in real_path or "usb4" in real_path
            except Exception:
                pass

        return False

    @staticmethod
    def _detect_tunnel_protocol_linux(device_path: str) -> EnclosureProtocol:
        """偵測 PCIe Tunnel 的協定版本"""
        # 檢查 Thunderbolt 世代
        if os.path.exists("/sys/bus/thunderbolt"):
            for dev in os.listdir("/sys/bus/thunderbolt/devices/"):
                gen_file = f"/sys/bus/thunderbolt/devices/{dev}/generation"
                if os.path.exists(gen_file):
                    try:
                        with open(gen_file) as f:
                            gen = int(f.read().strip())
                        if gen >= 5:
                            return EnclosureProtocol.TB5
                        elif gen == 4:
                            return EnclosureProtocol.TB4
                        elif gen == 3:
                            return EnclosureProtocol.TB3
                    except (ValueError, IOError):
                        pass

        # 檢查 USB4 版本
        for usb_dev in os.listdir("/sys/bus/usb/devices/") if os.path.exists("/sys/bus/usb/devices/") else []:
            speed_file = f"/sys/bus/usb/devices/{usb_dev}/speed"
            if os.path.exists(speed_file):
                try:
                    with open(speed_file) as f:
                        speed = int(f.read().strip())
                    if speed >= 80000:
                        return EnclosureProtocol.USB4_V2
                    elif speed >= 40000:
                        return EnclosureProtocol.USB4_V1
                except (ValueError, IOError):
                    pass

        return EnclosureProtocol.USB4_V1  # 預設

    @staticmethod
    def _detect_windows() -> List[EnclosureInfo]:
        """Windows 偵測流程"""
        devices = []
        try:
            # 使用 PowerShell 偵測 Thunderbolt/USB4 NVMe 裝置
            ps_cmd = """
            $tbDevices = Get-PnpDevice | Where-Object {
                ($_.FriendlyName -match 'NVMe') -and
                ($_.InstanceId -match 'USB4|Thunderbolt|TBT')
            }
            $results = @()
            foreach ($dev in $tbDevices) {
                $disk = Get-PhysicalDisk | Where-Object { $_.FriendlyName -match $dev.FriendlyName }
                $results += @{
                    Name = $dev.FriendlyName
                    InstanceId = $dev.InstanceId
                    Size = if ($disk) { $disk.Size } else { 0 }
                    MediaType = if ($disk) { $disk.MediaType } else { "Unknown" }
                    BusType = if ($disk) { $disk.BusType } else { "Unknown" }
                }
            }
            $results | ConvertTo-Json
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
                    enc = EnclosureInfo()
                    enc.model_name = disk.get("Name", "Unknown NVMe")
                    enc.capacity_gb = int(disk.get("Size", 0)) / (1024**3)
                    enc.is_nvme = True
                    enc.has_pcie_tunnel = True
                    instance_id = disk.get("InstanceId", "").upper()
                    if "TBT" in instance_id or "THUNDERBOLT" in instance_id:
                        enc.protocol = EnclosureProtocol.TB4
                    else:
                        enc.protocol = EnclosureProtocol.USB4_V1
                    enc.max_bandwidth_mbs = ENCLOSURE_BANDWIDTH[enc.protocol]
                    enc.typical_latency_us = ENCLOSURE_LATENCY[enc.protocol]
                    enc.vram_expandable = enc.max_bandwidth_mbs >= MIN_ENCLOSURE_BANDWIDTH
                    enc.free_gb = enc.capacity_gb * 0.85
                    devices.append(enc)
        except Exception:
            pass

        return devices
