"""
USB-VRAM Booster — Demo 模擬模組
製作者：Peter Yang

提供虛擬 GPU、USB 裝置數據，用於在無硬體環境中展示完整功能。
"""

from .detectors.os_detector import OSInfo
from .detectors.gpu_detector import GPUInfo
from .detectors.usb_detector import USBDeviceInfo, USBProtocol, USB_BANDWIDTH
from .detectors.system_scanner import SystemReport
from .memory.tier_manager import TierConfig


class DemoScenarios:
    """預設 Demo 場景"""

    @staticmethod
    def scenario_usb4_nvme_rtx4070() -> SystemReport:
        """場景 1: USB4 NVMe 外接盒 + RTX 4070"""
        report = SystemReport()
        report.os_info = OSInfo(
            os_type="Windows", os_name="Windows 11 Pro 24H2",
            os_version="10.0.26100", kernel_version="NT 10.0",
            architecture="AMD64", hostname="GAMING-PC",
            usb4_support=True, thunderbolt_support=True,
            pcie_tunneling_capable=True
        )
        report.gpus = [GPUInfo(
            index=0, name="NVIDIA GeForce RTX 4070", vendor="NVIDIA",
            vram_total_mb=12288, vram_used_mb=1024, vram_free_mb=11264,
            driver_version="560.94", pcie_gen="Gen4", pcie_width="x16",
            temperature=42, gpu_utilization=5, memory_utilization=8
        )]
        usb = USBDeviceInfo(
            device_path="\\\\.\\PhysicalDrive2",
            mount_point="E:\\",
            capacity_gb=1024, used_gb=50, free_gb=974,
            protocol=USBProtocol.USB4_V1,
            max_bandwidth_mbs=USB_BANDWIDTH[USBProtocol.USB4_V1],
            is_nvme=True, has_pcie_tunnel=True,
            model_name="Samsung 990 Pro 1TB (USB4 外接盒)",
            bus_type="USB4", controller_type="PCIe Router",
            vram_expandable=True
        )
        report.usb_devices = [usb]
        report.best_usb_device = usb
        report.vram_expandable = True
        report.max_expand_gb = usb.usable_for_vram_gb
        report.recommendation = (
            f"可以擴展！RTX 4070 的 VRAM 可透過 {usb.model_name} "
            f"擴展 +{usb.usable_for_vram_gb:.1f}GB（PCIe Tunneling 模式）"
        )
        return report

    @staticmethod
    def scenario_usb4v2_rtx4090() -> SystemReport:
        """場景 2: USB4 V2 NVMe + RTX 4090"""
        report = SystemReport()
        report.os_info = OSInfo(
            os_type="Linux", os_name="Ubuntu 24.04 LTS",
            os_version="6.8.0-45-generic", kernel_version="6.8.0-45-generic",
            architecture="x86_64", hostname="ai-workstation",
            usb4_support=True, thunderbolt_support=True,
            pcie_tunneling_capable=True
        )
        report.gpus = [GPUInfo(
            index=0, name="NVIDIA GeForce RTX 4090", vendor="NVIDIA",
            vram_total_mb=24576, vram_used_mb=512, vram_free_mb=24064,
            driver_version="555.42.06", pcie_gen="Gen4", pcie_width="x16",
            temperature=35, gpu_utilization=0, memory_utilization=2
        )]
        usb = USBDeviceInfo(
            device_path="/dev/nvme1n1",
            mount_point="/mnt/usb4-ssd",
            capacity_gb=2048, used_gb=100, free_gb=1948,
            protocol=USBProtocol.USB4_V2,
            max_bandwidth_mbs=USB_BANDWIDTH[USBProtocol.USB4_V2],
            is_nvme=True, has_pcie_tunnel=True,
            model_name="WD Black SN850X 2TB (USB4 V2 外接盒)",
            bus_type="USB4", controller_type="PCIe Router",
            vram_expandable=True
        )
        report.usb_devices = [usb]
        report.best_usb_device = usb
        report.vram_expandable = True
        report.max_expand_gb = usb.usable_for_vram_gb
        report.recommendation = (
            f"可以擴展！RTX 4090 的 VRAM 可透過 {usb.model_name} "
            f"擴展 +{usb.usable_for_vram_gb:.1f}GB（PCIe Tunneling 模式，80Gbps）"
        )
        return report

    @staticmethod
    def scenario_usb32_flash_drive() -> SystemReport:
        """場景 3: USB 3.2 Gen2 隨身碟 + RTX 3060"""
        report = SystemReport()
        report.os_info = OSInfo(
            os_type="Windows", os_name="Windows 10 22H2",
            os_version="10.0.19045", kernel_version="NT 10.0",
            architecture="AMD64", hostname="HOME-PC",
            usb4_support=False, thunderbolt_support=False,
            pcie_tunneling_capable=False
        )
        report.gpus = [GPUInfo(
            index=0, name="NVIDIA GeForce RTX 3060", vendor="NVIDIA",
            vram_total_mb=12288, vram_used_mb=2048, vram_free_mb=10240,
            driver_version="546.33", pcie_gen="Gen4", pcie_width="x16",
            temperature=38, gpu_utilization=3, memory_utilization=16
        )]
        usb = USBDeviceInfo(
            device_path="\\\\.\\PhysicalDrive3",
            mount_point="F:\\",
            capacity_gb=256, used_gb=20, free_gb=236,
            protocol=USBProtocol.USB_3_1,
            max_bandwidth_mbs=USB_BANDWIDTH[USBProtocol.USB_3_1],
            is_nvme=False, has_pcie_tunnel=False,
            model_name="SanDisk Extreme Pro 256GB",
            bus_type="USB", controller_type="xHCI",
            vram_expandable=True
        )
        report.usb_devices = [usb]
        report.best_usb_device = usb
        report.vram_expandable = True
        report.max_expand_gb = usb.usable_for_vram_gb
        report.recommendation = (
            f"可以擴展（有限）。RTX 3060 可透過 {usb.model_name} "
            f"擴展 +{usb.usable_for_vram_gb:.1f}GB，但頻寬受限於 USB 3.2 Gen2（無 PCIe Tunnel）"
        )
        return report

    @staticmethod
    def scenario_tb5_workstation() -> SystemReport:
        """場景 4: Thunderbolt 5 + RTX 5090"""
        report = SystemReport()
        report.os_info = OSInfo(
            os_type="Linux", os_name="Fedora 41 Workstation",
            os_version="6.12.0-200.fc41", kernel_version="6.12.0-200.fc41",
            architecture="x86_64", hostname="ml-server",
            usb4_support=True, thunderbolt_support=True,
            pcie_tunneling_capable=True
        )
        report.gpus = [GPUInfo(
            index=0, name="NVIDIA GeForce RTX 5090", vendor="NVIDIA",
            vram_total_mb=32768, vram_used_mb=256, vram_free_mb=32512,
            driver_version="570.86.16", pcie_gen="Gen5", pcie_width="x16",
            temperature=30, gpu_utilization=0, memory_utilization=1
        )]
        usb = USBDeviceInfo(
            device_path="/dev/nvme2n1",
            mount_point="/mnt/tb5-raid",
            capacity_gb=4096, used_gb=200, free_gb=3896,
            protocol=USBProtocol.TB5,
            max_bandwidth_mbs=USB_BANDWIDTH[USBProtocol.TB5],
            is_nvme=True, has_pcie_tunnel=True,
            model_name="Sabrent Rocket XTRM 4TB (TB5 外接盒)",
            bus_type="Thunderbolt", controller_type="PCIe Router",
            vram_expandable=True
        )
        report.usb_devices = [usb]
        report.best_usb_device = usb
        report.vram_expandable = True
        report.max_expand_gb = usb.usable_for_vram_gb
        report.recommendation = (
            f"極致擴展！RTX 5090 的 VRAM 可透過 {usb.model_name} "
            f"擴展 +{usb.usable_for_vram_gb:.1f}GB（Thunderbolt 5，120Gbps）"
        )
        return report

    @staticmethod
    def scenario_usb20_rejected() -> SystemReport:
        """場景 5: USB 2.0 隨身碟 — 應被拒絕"""
        report = SystemReport()
        report.os_info = OSInfo(
            os_type="Windows", os_name="Windows 11 Home",
            os_version="10.0.22631", kernel_version="NT 10.0",
            architecture="AMD64", hostname="LAPTOP",
            usb4_support=False, thunderbolt_support=False,
            pcie_tunneling_capable=False
        )
        report.gpus = [GPUInfo(
            index=0, name="NVIDIA GeForce RTX 4060 Laptop", vendor="NVIDIA",
            vram_total_mb=8192, vram_used_mb=1536, vram_free_mb=6656,
            driver_version="555.85", pcie_gen="Gen4", pcie_width="x8",
            temperature=45, gpu_utilization=10, memory_utilization=18
        )]
        usb = USBDeviceInfo(
            device_path="\\\\.\\PhysicalDrive4",
            mount_point="G:\\",
            capacity_gb=32, used_gb=5, free_gb=27,
            protocol=USBProtocol.USB_2_0,
            max_bandwidth_mbs=USB_BANDWIDTH[USBProtocol.USB_2_0],
            is_nvme=False, has_pcie_tunnel=False,
            model_name="Generic USB 2.0 Flash Drive 32GB",
            bus_type="USB", controller_type="xHCI",
            vram_expandable=False
        )
        report.usb_devices = [usb]
        report.best_usb_device = None
        report.vram_expandable = False
        report.max_expand_gb = 0
        report.recommendation = (
            "偵測到 USB 裝置，但頻寬不足（USB 2.0 僅 60 MB/s，需至少 200 MB/s）。"
            "建議使用 USB 3.2 Gen2 以上的裝置。"
        )
        return report


def get_demo_tier_config(report: SystemReport) -> TierConfig:
    """從 Demo 報告建立 TierConfig"""
    config = TierConfig()
    if report.gpus:
        config.vram_total_mb = report.gpus[0].vram_total_mb
    if report.best_usb_device:
        usb = report.best_usb_device
        config.usb_total_mb = int(usb.usable_for_vram_gb * 1024)
        config.usb_bandwidth_mbs = usb.max_bandwidth_mbs
        config.usb_device_path = usb.device_path
        config.usb_has_pcie_tunnel = usb.has_pcie_tunnel
    config.ram_total_mb = 32768  # 模擬 32GB RAM
    return config
