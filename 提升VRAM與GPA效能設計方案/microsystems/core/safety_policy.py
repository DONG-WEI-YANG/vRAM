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

    @staticmethod
    def load_merged_policy(global_path: Path, device_path: Path,
                           capacity_gb: float, speed_mbs: float) -> PolicyLimits:
        """
        Merge config layers: smart_default <- global <- device_override.

        Fields set to "auto" or missing use the smart default value.
        """
        defaults = SafetyPolicy.compute_smart_defaults(capacity_gb, speed_mbs)
        result = {
            "device_reserved_gb": defaults.device_reserved_gb,
            "pagefile_min_gb": defaults.pagefile_min_gb,
            "pagefile_max_gb": defaults.pagefile_max_gb,
            "system_ram_reserve_gb": defaults.system_ram_reserve_gb,
        }

        # Layer 1: Global config
        try:
            if global_path.exists():
                data = json.loads(global_path.read_text(encoding="utf-8"))
                gd = data.get("global_defaults", {})
                for key in ("device_reserved_gb", "pagefile_min_gb", "pagefile_max_gb"):
                    val = gd.get(key)
                    if val is not None and val != "auto":
                        result[key] = float(val)
                ram_pct = gd.get("system_ram_reserve_pct")
                if ram_pct is not None and ram_pct != "auto":
                    result["system_ram_reserve_gb"] = round(
                        _get_system_ram_gb() * float(ram_pct) / 100, 2)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("Failed to load global policy: %s", e)

        # Layer 2: Device override
        try:
            if device_path.exists():
                data = json.loads(device_path.read_text(encoding="utf-8"))
                so = data.get("safety_override", {})
                for key in ("device_reserved_gb", "pagefile_min_gb",
                            "pagefile_max_gb", "system_ram_reserve_gb"):
                    val = so.get(key)
                    if val is not None:
                        result[key] = float(val)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("Failed to load device override: %s", e)

        return PolicyLimits(**result)

    @staticmethod
    def save_global_policy(path: Path, overrides: dict) -> None:
        """Save global policy to host filesystem."""
        path.parent.mkdir(parents=True, exist_ok=True)

        existing = {"version": 1, "global_defaults": {}}
        try:
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

        existing.setdefault("version", 1)
        existing.setdefault("global_defaults", {})
        existing["global_defaults"].update(overrides)

        path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Global policy saved to %s", path)

    @staticmethod
    def save_device_override(device_config_path: Path, overrides: dict) -> None:
        """Merge safety overrides into existing device config."""
        existing = {}
        try:
            if device_config_path.exists():
                existing = json.loads(
                    device_config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

        so = existing.get("safety_override", {})
        so.update(overrides)
        existing["safety_override"] = so

        device_config_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Device override saved to %s", device_config_path)

    @staticmethod
    def validate_activation(capacity_gb: float, requested_gb: float,
                            policy: PolicyLimits) -> tuple:
        """
        Check if a pagefile activation request is within policy limits.

        Returns (ok: bool, reason: str). reason is "" if ok.
        """
        if requested_gb < policy.pagefile_min_gb:
            return (False,
                    f"Requested {requested_gb:.1f} GB is below min "
                    f"({policy.pagefile_min_gb:.1f} GB)")

        if requested_gb > policy.pagefile_max_gb:
            return (False,
                    f"Requested {requested_gb:.1f} GB exceeds max "
                    f"({policy.pagefile_max_gb:.1f} GB)")

        remaining = capacity_gb - requested_gb
        if remaining < policy.device_reserved_gb:
            return (False,
                    f"Only {remaining:.1f} GB would remain on device, "
                    f"but {policy.device_reserved_gb:.1f} GB reserved")

        return (True, "")
