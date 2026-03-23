"""
GPU 偵測器 — 自動偵測 NVIDIA / AMD GPU
製作者：Peter Yang | 純標準庫，零依賴
"""

import platform
import subprocess
import re
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class GPUInfo:
    index: int
    name: str
    vendor: str               # "NVIDIA" or "AMD" or "Unknown"
    vram_total_mb: int
    vram_used_mb: int
    vram_free_mb: int
    driver_version: str
    pcie_gen: str
    pcie_width: str
    temperature: int
    gpu_utilization: int
    memory_utilization: int

    @property
    def vram_total_gb(self) -> float:
        return round(self.vram_total_mb / 1024, 1)

    @property
    def vram_free_gb(self) -> float:
        return round(self.vram_free_mb / 1024, 1)

    @property
    def vram_used_gb(self) -> float:
        return round(self.vram_used_mb / 1024, 1)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["vram_total_gb"] = self.vram_total_gb
        d["vram_free_gb"] = self.vram_free_gb
        return d

    @property
    def summary(self) -> str:
        return (f"{self.name} | VRAM: {self.vram_used_mb}MB / "
                f"{self.vram_total_mb}MB | {self.pcie_gen} {self.pcie_width}")


class GPUDetector:
    """GPU 偵測器：支援 NVIDIA (nvidia-smi) / AMD (rocm-smi, sysfs) / 通用 (lspci, wmic)"""

    def __init__(self):
        self._gpus: List[GPUInfo] = []

    def detect(self) -> List[GPUInfo]:
        self._gpus = []
        self._gpus.extend(self._detect_nvidia())
        self._gpus.extend(self._detect_amd())
        if not self._gpus:
            self._gpus.extend(self._detect_fallback())
        return self._gpus

    @property
    def gpus(self) -> List[GPUInfo]:
        return self._gpus

    @property
    def total_vram_mb(self) -> int:
        return sum(g.vram_total_mb for g in self._gpus)

    @property
    def total_free_vram_mb(self) -> int:
        return sum(g.vram_free_mb for g in self._gpus)

    def _detect_nvidia(self) -> List[GPUInfo]:
        gpus = []
        try:
            cmd = [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free,"
                "driver_version,pcie.link.gen.current,pcie.link.width.current,"
                "temperature.gpu,utilization.gpu,utilization.memory",
                "--format=csv,noheader,nounits"
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    p = [x.strip() for x in line.split(",")]
                    if len(p) >= 11:
                        gpus.append(GPUInfo(
                            index=int(p[0]), name=p[1], vendor="NVIDIA",
                            vram_total_mb=int(p[2]), vram_used_mb=int(p[3]),
                            vram_free_mb=int(p[4]), driver_version=p[5],
                            pcie_gen=f"Gen{p[6]}", pcie_width=f"x{p[7]}",
                            temperature=self._safe_int(p[8]),
                            gpu_utilization=self._safe_int(p[9]),
                            memory_utilization=self._safe_int(p[10]),
                        ))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return gpus

    def _detect_amd(self) -> List[GPUInfo]:
        gpus = []
        if platform.system() != "Linux":
            return gpus
        drm = "/sys/class/drm"
        if not os.path.exists(drm):
            return gpus
        idx = 0
        for card in sorted(os.listdir(drm)):
            if not re.match(r"card\d+$", card):
                continue
            dev = os.path.join(drm, card, "device")
            vid = self._read_file(os.path.join(dev, "vendor"), "").strip()
            if vid != "0x1002":
                continue
            total = self._read_file_int(os.path.join(dev, "mem_info_vram_total"))
            used = self._read_file_int(os.path.join(dev, "mem_info_vram_used"))
            name = self._read_file(os.path.join(dev, "product_name"), f"AMD GPU {idx}").strip()
            gpus.append(GPUInfo(
                index=idx, name=name, vendor="AMD",
                vram_total_mb=total // (1024*1024),
                vram_used_mb=used // (1024*1024),
                vram_free_mb=(total - used) // (1024*1024),
                driver_version="amdgpu", pcie_gen="Unknown",
                pcie_width="Unknown", temperature=0,
                gpu_utilization=0, memory_utilization=0,
            ))
            idx += 1
        return gpus

    def _detect_fallback(self) -> List[GPUInfo]:
        gpus = []
        if platform.system() == "Linux":
            try:
                r = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    for m in re.finditer(r"(?:VGA|3D|Display).*?:\s*(.+?)(?:\n|$)", r.stdout):
                        n = m.group(1).strip()
                        v = "NVIDIA" if "NVIDIA" in n.upper() else (
                            "AMD" if "AMD" in n.upper() else "Unknown")
                        gpus.append(GPUInfo(
                            index=len(gpus), name=n, vendor=v,
                            vram_total_mb=0, vram_used_mb=0, vram_free_mb=0,
                            driver_version="Unknown", pcie_gen="Unknown",
                            pcie_width="Unknown", temperature=0,
                            gpu_utilization=0, memory_utilization=0,
                        ))
            except Exception:
                pass
        elif platform.system() == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Csv -NoTypeInformation"],
                    capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    lines = r.stdout.strip().split("\n")
                    for line in lines[1:]:
                        parts = [p.strip('"') for p in line.split('","')]
                        if len(parts) >= 3:
                            n = parts[0]
                            ram = self._safe_int(parts[1])
                            v = "NVIDIA" if "NVIDIA" in n.upper() else (
                                "AMD" if "AMD" in n.upper() else "Unknown")
                            gpus.append(GPUInfo(
                                index=len(gpus), name=n, vendor=v,
                                vram_total_mb=ram // (1024*1024),
                                vram_used_mb=0, vram_free_mb=ram // (1024*1024),
                                driver_version=parts[2] if len(parts) > 2 else "Unknown",
                                pcie_gen="Unknown", pcie_width="Unknown",
                                temperature=0, gpu_utilization=0, memory_utilization=0,
                            ))
            except Exception:
                pass
        return gpus

    @staticmethod
    def _safe_int(s, default=0) -> int:
        try:
            return int(s)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _read_file(path, default="") -> str:
        try:
            with open(path) as f:
                return f.read()
        except (FileNotFoundError, PermissionError):
            return default

    @staticmethod
    def _read_file_int(path, default=0) -> int:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (FileNotFoundError, PermissionError, ValueError):
            return default
