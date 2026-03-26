"""
Safety Policy Engine
=====================
Min/Max limits for pagefile sizing, device space reservation,
and system RAM protection.

Smart defaults are computed from device capacity and speed.
Global policy (host) and per-device overrides merge at load time.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Global policy default path
_APPDATA = os.environ.get("APPDATA", "")
GLOBAL_POLICY_DIR = Path(_APPDATA) / "vram_booster" if _APPDATA else Path.home() / ".vram_booster"
GLOBAL_POLICY_PATH = GLOBAL_POLICY_DIR / "safety_policy.json"


@dataclass
class PolicyLimits:
    """Concrete safety limits after merging all config layers."""
    device_reserved_gb: float     # Min free space to keep on device
    pagefile_min_gb: float        # Below this, don't bother activating
    pagefile_max_gb: float        # Single-device pagefile cap
    system_ram_reserve_gb: float  # RAM to keep free during drain


def _get_system_ram_gb() -> float:
    """Return total physical RAM in GB."""
    try:
        if platform.system().lower() == "windows":
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return mem.ullTotalPhys / (1024 ** 3)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        pass
    return 16.0  # safe fallback


class SafetyPolicy:
    """Min/Max safety policy engine with smart defaults and config merging."""

    @staticmethod
    def compute_smart_defaults(capacity_gb: float, speed_mbs: float) -> PolicyLimits:
        """
        Compute safety limits from device capacity and speed.

        Tier thresholds:
          < 32 GB:   reserved=2, pf_min=0.5, pf_max=60%
          32-128 GB: reserved=4, pf_min=1.0, pf_max=70%
          128+ GB:   reserved=8, pf_min=2.0, pf_max=80%
        """
        if capacity_gb < 32:
            reserved, pf_min, pf_pct = 2.0, 0.5, 0.6
        elif capacity_gb < 128:
            reserved, pf_min, pf_pct = 4.0, 1.0, 0.7
        else:
            reserved, pf_min, pf_pct = 8.0, 2.0, 0.8

        pf_max = round(capacity_gb * pf_pct, 2)
        ram_reserve = round(_get_system_ram_gb() * 0.2, 2)

        return PolicyLimits(
            device_reserved_gb=reserved,
            pagefile_min_gb=pf_min,
            pagefile_max_gb=pf_max,
            system_ram_reserve_gb=ram_reserve,
        )
