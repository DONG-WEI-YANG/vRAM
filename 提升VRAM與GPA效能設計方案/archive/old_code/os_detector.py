"""
OS 偵測器 — 自動偵測 Windows / Linux
製作者：Peter Yang | 純標準庫，零依賴
"""

import platform
import os
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class OSInfo:
    os_type: str            # "Windows" or "Linux"
    os_name: str            # e.g., "Windows 11", "Ubuntu 22.04"
    os_version: str
    kernel_version: str
    architecture: str
    hostname: str
    pcie_support: bool
    nvme_support: bool
    sd_express_capable: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @property
    def summary(self) -> str:
        status = "✓ 支援 SD Express" if self.sd_express_capable else "✗ 不支援 SD Express"
        return f"{self.os_name} ({self.architecture}) — {status}"


class OSDetector:
    """雙系統 OS 偵測器"""

    def __init__(self):
        self._info: Optional[OSInfo] = None

    def detect(self) -> OSInfo:
        system = platform.system()
        if system == "Windows":
            self._info = self._detect_windows()
        elif system == "Linux":
            self._info = self._detect_linux()
        else:
            self._info = OSInfo(
                os_type=system, os_name=system, os_version="Unknown",
                kernel_version="Unknown", architecture=platform.machine(),
                hostname=platform.node(), pcie_support=False,
                nvme_support=False, sd_express_capable=False
            )
        return self._info

    @property
    def info(self) -> Optional[OSInfo]:
        return self._info

    def _detect_windows(self) -> OSInfo:
        version = platform.version()
        release = platform.release()
        build = int(version.split(".")[-1]) if version else 0
        win_name = "Windows 11" if build >= 22000 else f"Windows {release}"
        nvme = self._run_cmd_check(
            ["powershell", "-Command",
             "Get-PnpDevice -Class DiskDrive -ErrorAction SilentlyContinue"],
            "NVMe", default=True)
        pcie = True  # 現代 Windows 都有 PCIe
        return OSInfo(
            os_type="Windows", os_name=win_name, os_version=version,
            kernel_version=f"NT {release}", architecture=platform.machine(),
            hostname=platform.node(), pcie_support=pcie, nvme_support=nvme,
            sd_express_capable=(build >= 22000) and nvme
        )

    def _detect_linux(self) -> OSInfo:
        os_name = self._get_linux_distro()
        kver = platform.release()
        nvme = (any(f.startswith("nvme") for f in os.listdir("/dev"))
                if os.path.exists("/dev") else False)
        if not nvme:
            nvme = self._run_cmd_check(["modinfo", "nvme"], "filename", default=False)
        pcie = os.path.exists("/sys/bus/pci")
        major, minor = self._parse_kernel(kver)
        sd_express = (major > 5 or (major == 5 and minor >= 18)) and nvme
        return OSInfo(
            os_type="Linux", os_name=os_name, os_version=kver,
            kernel_version=kver, architecture=platform.machine(),
            hostname=platform.node(), pcie_support=pcie, nvme_support=nvme,
            sd_express_capable=sd_express
        )

    def _get_linux_distro(self) -> str:
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except FileNotFoundError:
            pass
        return f"Linux {platform.release()}"

    @staticmethod
    def _run_cmd_check(cmd, keyword, default=False) -> bool:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return keyword.lower() in r.stdout.lower()
        except Exception:
            return default

    @staticmethod
    def _parse_kernel(v: str) -> tuple:
        try:
            p = v.split(".")
            return int(p[0]), int(p[1])
        except (IndexError, ValueError):
            return 0, 0
