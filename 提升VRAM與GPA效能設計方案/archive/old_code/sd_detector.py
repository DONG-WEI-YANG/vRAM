"""
SD 卡偵測器 — 自動偵測 SD 卡規格與模式
製作者：Peter Yang | 純標準庫，零依賴
"""

import platform
import subprocess
import os
import re
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum


class SDCardMode(Enum):
    """SD 卡運作模式"""
    UNKNOWN = "Unknown"
    LEGACY_UHS_I = "UHS-I (最高 104 MB/s)"
    LEGACY_UHS_II = "UHS-II (最高 312 MB/s)"
    SD_EXPRESS_GEN3_X1 = "SD Express Gen3 x1 (最高 985 MB/s)"
    SD_EXPRESS_GEN3_X2 = "SD Express Gen3 x2 (最高 1,969 MB/s)"
    SD_EXPRESS_GEN4_X1 = "SD Express Gen4 x1 (最高 1,969 MB/s)"
    SD_EXPRESS_GEN4_X2 = "SD Express Gen4 x2 (最高 3,940 MB/s)"


# SD 卡模式對應的理論最大頻寬 (MB/s)
SD_BANDWIDTH = {
    SDCardMode.UNKNOWN: 0,
    SDCardMode.LEGACY_UHS_I: 104,
    SDCardMode.LEGACY_UHS_II: 312,
    SDCardMode.SD_EXPRESS_GEN3_X1: 985,
    SDCardMode.SD_EXPRESS_GEN3_X2: 1969,
    SDCardMode.SD_EXPRESS_GEN4_X1: 1969,
    SDCardMode.SD_EXPRESS_GEN4_X2: 3940,
}


@dataclass
class SDCardInfo:
    """SD 卡裝置資訊"""
    device_path: str          # e.g., "/dev/mmcblk0" or "E:\\"
    mount_point: str          # e.g., "/mnt/sdcard" or "E:\\"
    capacity_gb: float
    used_gb: float
    free_gb: float
    mode: SDCardMode
    max_bandwidth_mbs: int    # 理論最大頻寬 MB/s
    measured_read_mbs: int    # 實測讀取速度 MB/s (0 = 未測試)
    measured_write_mbs: int   # 實測寫入速度 MB/s (0 = 未測試)
    is_nvme_mode: bool        # 是否以 NVMe 模式運行
    model_name: str
    serial: str
    filesystem: str           # e.g., "ext4", "NTFS", "exFAT"
    vram_expandable: bool     # 是否可用於 VRAM 擴展

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["mode"] = self.mode.value
        return d

    @property
    def summary(self) -> str:
        status = "✓ 可擴展 VRAM" if self.vram_expandable else "✗ 頻寬不足"
        return (f"{self.model_name} | {self.capacity_gb:.0f}GB | "
                f"{self.mode.value} | {status}")

    @property
    def usable_for_vram_gb(self) -> float:
        """可用於 VRAM 擴展的空間 (保留 10% 給系統)"""
        return max(0, self.free_gb * 0.9)


class SDCardDetector:
    """
    SD 卡偵測器
    自動偵測插入的 SD 卡，判斷其規格與是否支援 VRAM 擴展。
    """

    # 最低可用頻寬門檻 (MB/s)，低於此值不建議用於 VRAM 擴展
    MIN_BANDWIDTH_FOR_VRAM = 200

    def __init__(self):
        self._cards: List[SDCardInfo] = []
        self._os_type = platform.system()

    def detect(self) -> List[SDCardInfo]:
        """執行 SD 卡偵測"""
        self._cards = []
        if self._os_type == "Linux":
            self._cards = self._detect_linux()
        elif self._os_type == "Windows":
            self._cards = self._detect_windows()
        return self._cards

    @property
    def cards(self) -> List[SDCardInfo]:
        return self._cards

    @property
    def vram_expandable_cards(self) -> List[SDCardInfo]:
        return [c for c in self._cards if c.vram_expandable]

    # ─── Linux 偵測 ──────────────────────────────────────────

    def _detect_linux(self) -> List[SDCardInfo]:
        cards = []

        # 偵測 mmcblk 裝置 (傳統 SD 模式)
        cards.extend(self._detect_linux_mmc())

        # 偵測 NVMe 裝置中的 SD Express 卡
        cards.extend(self._detect_linux_nvme_sd())

        return cards

    def _detect_linux_mmc(self) -> List[SDCardInfo]:
        """偵測 /dev/mmcblk* 裝置"""
        cards = []
        if not os.path.exists("/sys/class/mmc_host"):
            return cards

        for host in os.listdir("/sys/class/mmc_host"):
            host_path = os.path.join("/sys/class/mmc_host", host)
            for entry in os.listdir(host_path):
                if not entry.startswith("mmc"):
                    continue
                card_path = os.path.join(host_path, entry)
                if not os.path.isdir(card_path):
                    continue

                # 讀取卡片資訊
                card_type = self._read_sysfs(os.path.join(card_path, "type"))
                if card_type not in ("SD", "SDXC", "SDHC", ""):
                    continue

                name = self._read_sysfs(os.path.join(card_path, "name"), "Unknown SD")
                serial = self._read_sysfs(os.path.join(card_path, "serial"), "")

                # 找到對應的 block 裝置
                dev_path = ""
                mount = ""
                cap_gb = 0.0
                used_gb = 0.0
                free_gb = 0.0
                fs = ""

                for blk in os.listdir("/sys/block"):
                    if blk.startswith("mmcblk"):
                        dev_path = f"/dev/{blk}"
                        # 取得容量
                        size_sectors = self._read_sysfs_int(
                            f"/sys/block/{blk}/size")
                        cap_gb = (size_sectors * 512) / (1024**3)
                        # 取得掛載點
                        mount, fs, used_gb, free_gb = self._get_mount_info(dev_path)
                        break

                # 判斷模式
                speed_class = self._read_sysfs(os.path.join(card_path, "speed_class"), "")
                mode = self._classify_mmc_mode(card_path)
                bw = SD_BANDWIDTH.get(mode, 0)

                cards.append(SDCardInfo(
                    device_path=dev_path, mount_point=mount,
                    capacity_gb=round(cap_gb, 1),
                    used_gb=round(used_gb, 1), free_gb=round(free_gb, 1),
                    mode=mode, max_bandwidth_mbs=bw,
                    measured_read_mbs=0, measured_write_mbs=0,
                    is_nvme_mode=False, model_name=name, serial=serial,
                    filesystem=fs,
                    vram_expandable=bw >= self.MIN_BANDWIDTH_FOR_VRAM
                ))

        return cards

    def _detect_linux_nvme_sd(self) -> List[SDCardInfo]:
        """偵測以 NVMe 模式運行的 SD Express 卡"""
        cards = []
        try:
            r = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL,SERIAL,TRAN"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode != 0:
                return cards

            import json
            data = json.loads(r.stdout)
            for dev in data.get("blockdevices", []):
                tran = dev.get("tran", "") or ""
                model = dev.get("model", "") or ""
                # SD Express 卡在 NVMe 模式下會顯示為 nvme 傳輸
                # 需要進一步判斷是否為 SD 卡（透過 model 名稱或 PCI ID）
                if "nvme" in tran.lower() and self._is_sd_express_device(dev):
                    name = f"/dev/{dev['name']}"
                    size_str = dev.get("size", "0")
                    cap_gb = self._parse_size_to_gb(size_str)
                    mount = dev.get("mountpoint", "") or ""
                    fs = dev.get("fstype", "") or ""
                    serial = dev.get("serial", "") or ""

                    free_gb = 0.0
                    used_gb = 0.0
                    if mount:
                        st = os.statvfs(mount)
                        free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
                        total = (st.f_blocks * st.f_frsize) / (1024**3)
                        used_gb = total - free_gb

                    mode = SDCardMode.SD_EXPRESS_GEN3_X1  # 預設，需進一步偵測
                    bw = SD_BANDWIDTH[mode]

                    cards.append(SDCardInfo(
                        device_path=name, mount_point=mount,
                        capacity_gb=round(cap_gb, 1),
                        used_gb=round(used_gb, 1), free_gb=round(free_gb, 1),
                        mode=mode, max_bandwidth_mbs=bw,
                        measured_read_mbs=0, measured_write_mbs=0,
                        is_nvme_mode=True, model_name=model, serial=serial,
                        filesystem=fs, vram_expandable=True
                    ))
        except Exception:
            pass
        return cards

    def _classify_mmc_mode(self, card_sysfs_path: str) -> SDCardMode:
        """根據 sysfs 資訊判斷 SD 卡模式"""
        # 嘗試讀取 timing_spec
        timing = self._read_sysfs(os.path.join(card_sysfs_path, "speed_class"), "")
        # 簡化判斷邏輯
        if "express" in timing.lower():
            return SDCardMode.SD_EXPRESS_GEN3_X1
        return SDCardMode.LEGACY_UHS_I

    # ─── Windows 偵測 ────────────────────────────────────────

    def _detect_windows(self) -> List[SDCardInfo]:
        """透過 PowerShell 偵測 Windows 上的 SD 卡"""
        cards = []
        try:
            # 取得可移除磁碟
            ps_cmd = (
                "Get-WmiObject Win32_LogicalDisk | "
                "Where-Object { $_.DriveType -eq 2 } | "
                "Select-Object DeviceID, Size, FreeSpace, FileSystem, VolumeName | "
                "ConvertTo-Csv -NoTypeInformation"
            )
            r = subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                lines = r.stdout.strip().split("\n")
                for line in lines[1:]:
                    parts = [p.strip('"') for p in line.split('","')]
                    if len(parts) >= 5:
                        drive = parts[0]
                        total = int(parts[1]) if parts[1].isdigit() else 0
                        free = int(parts[2]) if parts[2].isdigit() else 0
                        fs = parts[3]
                        vol_name = parts[4]

                        cap_gb = total / (1024**3)
                        free_gb = free / (1024**3)
                        used_gb = cap_gb - free_gb

                        # 判斷是否為 SD 卡（透過 WMI 進一步查詢）
                        is_sd, model, mode = self._identify_windows_sd(drive)
                        if not is_sd:
                            continue

                        bw = SD_BANDWIDTH.get(mode, 0)
                        cards.append(SDCardInfo(
                            device_path=drive, mount_point=drive,
                            capacity_gb=round(cap_gb, 1),
                            used_gb=round(used_gb, 1), free_gb=round(free_gb, 1),
                            mode=mode, max_bandwidth_mbs=bw,
                            measured_read_mbs=0, measured_write_mbs=0,
                            is_nvme_mode=(mode in (
                                SDCardMode.SD_EXPRESS_GEN3_X1,
                                SDCardMode.SD_EXPRESS_GEN3_X2,
                                SDCardMode.SD_EXPRESS_GEN4_X1,
                                SDCardMode.SD_EXPRESS_GEN4_X2)),
                            model_name=model or vol_name or "SD Card",
                            serial="", filesystem=fs,
                            vram_expandable=bw >= self.MIN_BANDWIDTH_FOR_VRAM
                        ))
        except Exception:
            pass
        return cards

    def _identify_windows_sd(self, drive_letter: str) -> tuple:
        """進一步判斷 Windows 可移除磁碟是否為 SD 卡"""
        try:
            ps_cmd = (
                f"Get-PhysicalDisk | Where-Object {{ $_.BusType -match 'SD|MMC|NVMe' }} | "
                f"Select-Object FriendlyName, BusType, MediaType | "
                f"ConvertTo-Csv -NoTypeInformation"
            )
            r = subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                lines = r.stdout.strip().split("\n")
                for line in lines[1:]:
                    parts = [p.strip('"') for p in line.split('","')]
                    if len(parts) >= 3:
                        name = parts[0]
                        bus = parts[1]
                        if "SD" in bus.upper() or "MMC" in bus.upper():
                            mode = SDCardMode.LEGACY_UHS_I
                            return True, name, mode
                        elif "NVMe" in bus:
                            # 可能是 SD Express
                            mode = SDCardMode.SD_EXPRESS_GEN3_X1
                            return True, name, mode
        except Exception:
            pass
        # 預設假設可移除磁碟是 SD 卡
        return True, "SD Card", SDCardMode.LEGACY_UHS_I

    # ─── 工具方法 ────────────────────────────────────────────

    @staticmethod
    def _read_sysfs(path: str, default: str = "") -> str:
        try:
            with open(path) as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError):
            return default

    @staticmethod
    def _read_sysfs_int(path: str, default: int = 0) -> int:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (FileNotFoundError, PermissionError, ValueError):
            return default

    @staticmethod
    def _get_mount_info(dev_path: str) -> tuple:
        """取得裝置的掛載點、檔案系統、已用/可用空間"""
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if parts[0].startswith(dev_path):
                        mount = parts[1]
                        fs = parts[2]
                        st = os.statvfs(mount)
                        free = (st.f_bavail * st.f_frsize) / (1024**3)
                        total = (st.f_blocks * st.f_frsize) / (1024**3)
                        return mount, fs, total - free, free
        except Exception:
            pass
        return "", "", 0.0, 0.0

    @staticmethod
    def _is_sd_express_device(dev_info: dict) -> bool:
        """判斷 NVMe 裝置是否為 SD Express 卡"""
        model = (dev_info.get("model", "") or "").lower()
        return any(kw in model for kw in ("sd", "sdxc", "express", "mmc"))

    @staticmethod
    def _parse_size_to_gb(size_str: str) -> float:
        """解析 lsblk 的 size 字串"""
        try:
            if size_str.endswith("T"):
                return float(size_str[:-1]) * 1024
            elif size_str.endswith("G"):
                return float(size_str[:-1])
            elif size_str.endswith("M"):
                return float(size_str[:-1]) / 1024
            return float(size_str) / (1024**3)
        except ValueError:
            return 0.0
