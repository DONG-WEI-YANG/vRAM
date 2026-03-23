"""
記憶體分層管理器 — 管理 VRAM → RAM → SD 卡的三層記憶體架構
製作者：Peter Yang
"""

import os
import time
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List


class TierStatus(Enum):
    IDLE = "閒置"
    ACTIVE = "運作中"
    FULL = "已滿"
    ERROR = "錯誤"
    DISABLED = "停用"


@dataclass
class MemoryTier:
    """單一記憶體層級"""
    name: str
    tier_level: int          # 0=VRAM, 1=RAM, 2=SD Card
    capacity_mb: int
    used_mb: int
    bandwidth_mbs: int       # MB/s
    latency_us: int          # 微秒
    status: TierStatus
    device_path: str         # 裝置路徑

    @property
    def free_mb(self) -> int:
        return max(0, self.capacity_mb - self.used_mb)

    @property
    def usage_percent(self) -> float:
        if self.capacity_mb == 0:
            return 0.0
        return round(self.used_mb / self.capacity_mb * 100, 1)

    @property
    def capacity_gb(self) -> float:
        return round(self.capacity_mb / 1024, 1)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tier_level": self.tier_level,
            "capacity_mb": self.capacity_mb,
            "used_mb": self.used_mb,
            "free_mb": self.free_mb,
            "bandwidth_mbs": self.bandwidth_mbs,
            "latency_us": self.latency_us,
            "status": self.status.value,
            "usage_percent": self.usage_percent,
        }


class TierManager:
    """
    三層記憶體管理器
    Tier 0: GPU VRAM (最快，容量最小)
    Tier 1: System RAM (中等)
    Tier 2: SD Express Card (最慢，容量最大)
    """

    def __init__(self):
        self.tiers: List[MemoryTier] = []
        self._swap_file_path: Optional[str] = None
        self._is_active = False

    def initialize(self, gpus, sd_cards) -> bool:
        """根據偵測結果初始化記憶體層級"""
        self.tiers = []

        # Tier 0: VRAM
        if gpus:
            gpu = gpus[0]
            self.tiers.append(MemoryTier(
                name=f"GPU VRAM ({gpu.name})",
                tier_level=0,
                capacity_mb=gpu.vram_total_mb,
                used_mb=gpu.vram_used_mb,
                bandwidth_mbs=900_000,  # ~900 GB/s for modern GPU
                latency_us=1,
                status=TierStatus.ACTIVE,
                device_path=f"gpu:{gpu.index}",
            ))

        # Tier 1: System RAM
        ram_mb = self._get_system_ram_mb()
        ram_available = self._get_available_ram_mb()
        self.tiers.append(MemoryTier(
            name="System RAM",
            tier_level=1,
            capacity_mb=ram_mb,
            used_mb=ram_mb - ram_available,
            bandwidth_mbs=50_000,  # ~50 GB/s DDR5
            latency_us=100,
            status=TierStatus.IDLE,
            device_path="ram",
        ))

        # Tier 2: SD Card(s)
        for card in sd_cards:
            if card.vram_expandable:
                self.tiers.append(MemoryTier(
                    name=f"SD Card ({card.model_name})",
                    tier_level=2,
                    capacity_mb=int(card.usable_for_vram_gb * 1024),
                    used_mb=0,
                    bandwidth_mbs=card.max_bandwidth_mbs,
                    latency_us=10_000,  # ~10ms for NVMe SD
                    status=TierStatus.IDLE,
                    device_path=card.device_path,
                ))

        return len(self.tiers) >= 2

    def activate_sd_expansion(self, sd_card_path: str, size_gb: float) -> dict:
        """
        啟動 SD 卡 VRAM 擴展
        在 SD 卡上建立 swap 空間，並設定為 GPU 可用的記憶體池
        """
        result = {
            "success": False,
            "message": "",
            "swap_path": "",
            "size_gb": size_gb,
        }

        os_type = platform.system()

        if os_type == "Linux":
            return self._activate_linux(sd_card_path, size_gb, result)
        elif os_type == "Windows":
            return self._activate_windows(sd_card_path, size_gb, result)
        else:
            result["message"] = f"不支援的作業系統: {os_type}"
            return result

    def _activate_linux(self, mount_path: str, size_gb: float, result: dict) -> dict:
        """Linux: 在 SD 卡上建立 swap 檔案並啟用"""
        swap_path = os.path.join(mount_path, ".sdvram_swap")
        size_mb = int(size_gb * 1024)

        try:
            # 1. 建立 swap 檔案
            result["message"] = "正在建立記憶體擴展檔案..."
            subprocess.run(
                ["sudo", "dd", "if=/dev/zero", f"of={swap_path}",
                 "bs=1M", f"count={size_mb}"],
                capture_output=True, timeout=300
            )

            # 2. 設定 swap 格式
            subprocess.run(
                ["sudo", "mkswap", swap_path],
                capture_output=True, timeout=30
            )

            # 3. 設定權限
            subprocess.run(
                ["sudo", "chmod", "600", swap_path],
                capture_output=True, timeout=5
            )

            # 4. 啟用 swap (高優先級讓 GPU 優先使用)
            subprocess.run(
                ["sudo", "swapon", "-p", "10", swap_path],
                capture_output=True, timeout=10
            )

            self._swap_file_path = swap_path
            self._is_active = True

            # 更新 Tier 狀態
            for tier in self.tiers:
                if tier.tier_level == 2:
                    tier.status = TierStatus.ACTIVE
                    tier.used_mb = 0

            result["success"] = True
            result["message"] = f"VRAM 擴展已啟用！擴展空間: {size_gb:.1f} GB"
            result["swap_path"] = swap_path

        except subprocess.TimeoutExpired:
            result["message"] = "操作逾時，SD 卡可能速度過慢"
        except Exception as e:
            result["message"] = f"啟用失敗: {str(e)}"

        return result

    def _activate_windows(self, drive_path: str, size_gb: float, result: dict) -> dict:
        """Windows: 在 SD 卡上建立 pagefile"""
        size_mb = int(size_gb * 1024)
        pagefile_path = os.path.join(drive_path, "sdvram_pagefile.sys")

        try:
            # Windows 使用 wmic 設定 pagefile
            ps_cmd = (
                f"$drive = '{drive_path}';"
                f"$size = {size_mb};"
                f"$pf = [wmiclass]'Win32_PageFileSetting';"
                f"$newPF = $pf.CreateInstance();"
                f"$newPF.Name = '{pagefile_path}';"
                f"$newPF.InitialSize = $size;"
                f"$newPF.MaximumSize = $size;"
                f"$newPF.Put()"
            )
            r = subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=30
            )

            if r.returncode == 0:
                self._swap_file_path = pagefile_path
                self._is_active = True

                for tier in self.tiers:
                    if tier.tier_level == 2:
                        tier.status = TierStatus.ACTIVE

                result["success"] = True
                result["message"] = f"VRAM 擴展已啟用！擴展空間: {size_gb:.1f} GB"
                result["swap_path"] = pagefile_path
            else:
                result["message"] = f"設定 pagefile 失敗: {r.stderr}"

        except Exception as e:
            result["message"] = f"啟用失敗: {str(e)}"

        return result

    def deactivate(self) -> dict:
        """停用 VRAM 擴展"""
        result = {"success": False, "message": ""}

        if not self._is_active or not self._swap_file_path:
            result["message"] = "VRAM 擴展未啟用"
            return result

        os_type = platform.system()

        try:
            if os_type == "Linux":
                subprocess.run(
                    ["sudo", "swapoff", self._swap_file_path],
                    capture_output=True, timeout=30
                )
                os.remove(self._swap_file_path)
            elif os_type == "Windows":
                ps_cmd = (
                    f"$pf = Get-WmiObject Win32_PageFileSetting | "
                    f"Where-Object {{ $_.Name -eq '{self._swap_file_path}' }};"
                    f"if ($pf) {{ $pf.Delete() }}"
                )
                subprocess.run(
                    ["powershell", "-Command", ps_cmd],
                    capture_output=True, timeout=30
                )

            self._is_active = False
            for tier in self.tiers:
                if tier.tier_level == 2:
                    tier.status = TierStatus.IDLE
                    tier.used_mb = 0

            result["success"] = True
            result["message"] = "VRAM 擴展已停用"

        except Exception as e:
            result["message"] = f"停用失敗: {str(e)}"

        return result

    def get_status(self) -> dict:
        """取得當前記憶體分層狀態"""
        return {
            "is_active": self._is_active,
            "tiers": [t.to_dict() for t in self.tiers],
            "total_capacity_gb": round(sum(t.capacity_mb for t in self.tiers) / 1024, 1),
            "total_used_gb": round(sum(t.used_mb for t in self.tiers) / 1024, 1),
        }

    # ─── 系統資訊工具 ────────────────────────────────────────

    @staticmethod
    def _get_system_ram_mb() -> int:
        os_type = platform.system()
        if os_type == "Linux":
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            return int(line.split()[1]) // 1024
            except Exception:
                pass
        elif os_type == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    return int(r.stdout.strip()) // (1024 * 1024)
            except Exception:
                pass
        return 8192  # 預設 8GB

    @staticmethod
    def _get_available_ram_mb() -> int:
        os_type = platform.system()
        if os_type == "Linux":
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemAvailable:"):
                            return int(line.split()[1]) // 1024
            except Exception:
                pass
        elif os_type == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    return int(r.stdout.strip()) // 1024
            except Exception:
                pass
        return 4096
