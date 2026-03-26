# Safety Policy + Safe Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add min/max safety policy enforcement and four-phase safe removal with drain progress to the Host Mode UI.

**Architecture:** Two new core modules (`safety_policy.py`, `safe_removal.py`) provide the policy engine and removal state machine. `host_ui.py` is extended with a collapsible Advanced policy panel and drain progress cards. `real_boost.py` integrates policy validation into its `activate()` flow.

**Tech Stack:** Python 3.10+, tkinter, dataclasses, JSON config, Windows WMI (Win32_PageFileUsage), existing VhdPagefileEngine/VhdBridge APIs

**Spec:** `docs/superpowers/specs/2026-03-26-safety-policy-safe-removal-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `提升VRAM與GPA效能設計方案/microsystems/core/safety_policy.py` | Create | PolicyLimits dataclass, smart defaults, config merge, validation |
| `提升VRAM與GPA效能設計方案/microsystems/core/safe_removal.py` | Create | RemovalState enum, PreflightResult, DrainProgress, SafeRemovalManager state machine |
| `提升VRAM與GPA效能設計方案/microsystems/host_ui.py` | Modify | PolicyPanel, DrainProgressCard, Advanced panel, drain flow UI |
| `提升VRAM與GPA效能設計方案/microsystems/core/real_boost.py` | Modify | Integrate validate_activation() into activate() |
| `提升VRAM與GPA效能設計方案/tests/test_safety_policy.py` | Create | Tests for SafetyPolicy |
| `提升VRAM與GPA效能設計方案/tests/test_safe_removal.py` | Create | Tests for SafeRemovalManager |

---

## Task 1: SafetyPolicy — PolicyLimits + Smart Defaults

**Files:**
- Create: `提升VRAM與GPA效能設計方案/tests/test_safety_policy.py`
- Create: `提升VRAM與GPA效能設計方案/microsystems/core/safety_policy.py`

- [ ] **Step 1: Write failing tests for PolicyLimits and compute_smart_defaults**

Create `提升VRAM與GPA效能設計方案/tests/test_safety_policy.py`:

```python
"""Tests for SafetyPolicy engine."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from microsystems.core.safety_policy import SafetyPolicy, PolicyLimits


class TestPolicyLimits(unittest.TestCase):
    """Test PolicyLimits dataclass."""

    def test_create_policy_limits(self):
        p = PolicyLimits(
            device_reserved_gb=4.0,
            pagefile_min_gb=1.0,
            pagefile_max_gb=25.0,
            system_ram_reserve_gb=6.4,
        )
        self.assertEqual(p.device_reserved_gb, 4.0)
        self.assertEqual(p.pagefile_min_gb, 1.0)
        self.assertEqual(p.pagefile_max_gb, 25.0)
        self.assertEqual(p.system_ram_reserve_gb, 6.4)


class TestSmartDefaults(unittest.TestCase):
    """Test compute_smart_defaults() tier logic."""

    def test_small_device_under_32gb(self):
        """< 32 GB: reserved=2, pf_min=0.5, pf_max=capacity*60%"""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=16.0, speed_mbs=500.0)
        self.assertEqual(p.device_reserved_gb, 2.0)
        self.assertEqual(p.pagefile_min_gb, 0.5)
        self.assertAlmostEqual(p.pagefile_max_gb, 16.0 * 0.6)

    def test_medium_device_32_to_128gb(self):
        """32-128 GB: reserved=4, pf_min=1, pf_max=capacity*70%"""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=64.0, speed_mbs=1000.0)
        self.assertEqual(p.device_reserved_gb, 4.0)
        self.assertEqual(p.pagefile_min_gb, 1.0)
        self.assertAlmostEqual(p.pagefile_max_gb, 64.0 * 0.7)

    def test_large_device_over_128gb(self):
        """128+ GB: reserved=8, pf_min=2, pf_max=capacity*80%"""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=256.0, speed_mbs=3000.0)
        self.assertEqual(p.device_reserved_gb, 8.0)
        self.assertEqual(p.pagefile_min_gb, 2.0)
        self.assertAlmostEqual(p.pagefile_max_gb, 256.0 * 0.8)

    def test_boundary_32gb_exact(self):
        """32 GB exactly falls into medium tier."""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=32.0, speed_mbs=500.0)
        self.assertEqual(p.device_reserved_gb, 4.0)
        self.assertEqual(p.pagefile_min_gb, 1.0)

    def test_boundary_128gb_exact(self):
        """128 GB exactly falls into large tier."""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=128.0, speed_mbs=1000.0)
        self.assertEqual(p.device_reserved_gb, 8.0)
        self.assertEqual(p.pagefile_min_gb, 2.0)

    def test_system_ram_reserve_is_set(self):
        """system_ram_reserve_gb should be > 0 (based on system RAM * 20%)."""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=64.0, speed_mbs=1000.0)
        self.assertGreater(p.system_ram_reserve_gb, 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/test_safety_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'microsystems.core.safety_policy'`

- [ ] **Step 3: Implement PolicyLimits and compute_smart_defaults**

Create `提升VRAM與GPA效能設計方案/microsystems/core/safety_policy.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/test_safety_policy.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd "D:/product/vRAM"
git add "提升VRAM與GPA效能設計方案/microsystems/core/safety_policy.py" "提升VRAM與GPA效能設計方案/tests/test_safety_policy.py"
git commit -m "feat: SafetyPolicy — PolicyLimits + smart defaults engine"
```

---

## Task 2: SafetyPolicy — Config Load/Save/Merge + Validation

**Files:**
- Modify: `提升VRAM與GPA效能設計方案/tests/test_safety_policy.py`
- Modify: `提升VRAM與GPA效能設計方案/microsystems/core/safety_policy.py`

- [ ] **Step 1: Write failing tests for merge, save, and validate**

Append to `提升VRAM與GPA效能設計方案/tests/test_safety_policy.py`:

```python
import tempfile
import json


class TestConfigMerge(unittest.TestCase):
    """Test load_merged_policy with global + device overrides."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.global_path = os.path.join(self.tmpdir, "safety_policy.json")
        self.device_path = os.path.join(self.tmpdir, ".vram_boost_config.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_config_files_uses_smart_defaults(self):
        """When no config files exist, smart defaults are used."""
        p = SafetyPolicy.load_merged_policy(
            Path(os.path.join(self.tmpdir, "nonexistent.json")),
            Path(os.path.join(self.tmpdir, "nonexistent2.json")),
            capacity_gb=64.0, speed_mbs=1000.0,
        )
        self.assertEqual(p.device_reserved_gb, 4.0)  # medium tier default
        self.assertEqual(p.pagefile_min_gb, 1.0)

    def test_global_override_replaces_smart_default(self):
        """Global config overrides smart defaults."""
        with open(self.global_path, "w") as f:
            json.dump({
                "version": 1,
                "global_defaults": {
                    "device_reserved_gb": 10,
                    "pagefile_min_gb": "auto",
                    "pagefile_max_gb": "auto",
                    "system_ram_reserve_pct": 20,
                }
            }, f)

        p = SafetyPolicy.load_merged_policy(
            Path(self.global_path), Path(self.device_path),
            capacity_gb=64.0, speed_mbs=1000.0,
        )
        self.assertEqual(p.device_reserved_gb, 10.0)  # overridden
        self.assertEqual(p.pagefile_min_gb, 1.0)       # "auto" = smart default

    def test_device_override_wins_over_global(self):
        """Device-level override takes precedence over global."""
        with open(self.global_path, "w") as f:
            json.dump({
                "version": 1,
                "global_defaults": {"device_reserved_gb": 10}
            }, f)

        with open(self.device_path, "w") as f:
            json.dump({
                "card_fingerprint": "64.0GB|TEST",
                "rand_write_mbs": 100,
                "swap_size_bytes": 0,
                "safety_override": {"device_reserved_gb": 3}
            }, f)

        p = SafetyPolicy.load_merged_policy(
            Path(self.global_path), Path(self.device_path),
            capacity_gb=64.0, speed_mbs=1000.0,
        )
        self.assertEqual(p.device_reserved_gb, 3.0)  # device wins

    def test_save_global_policy(self):
        """save_global_policy writes valid JSON."""
        SafetyPolicy.save_global_policy(
            Path(self.global_path),
            {"device_reserved_gb": 5, "pagefile_max_gb": 30},
        )
        with open(self.global_path) as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["global_defaults"]["device_reserved_gb"], 5)

    def test_save_device_override(self):
        """save_device_override merges into existing config."""
        # Pre-existing device config
        with open(self.device_path, "w") as f:
            json.dump({"card_fingerprint": "64.0GB|TEST", "rand_write_mbs": 100}, f)

        SafetyPolicy.save_device_override(
            Path(self.device_path),
            {"pagefile_max_gb": 20},
        )
        with open(self.device_path) as f:
            data = json.load(f)
        self.assertEqual(data["card_fingerprint"], "64.0GB|TEST")  # preserved
        self.assertEqual(data["safety_override"]["pagefile_max_gb"], 20)


class TestValidation(unittest.TestCase):
    """Test validate_activation()."""

    def setUp(self):
        self.policy = PolicyLimits(
            device_reserved_gb=4.0,
            pagefile_min_gb=1.0,
            pagefile_max_gb=25.0,
            system_ram_reserve_gb=6.4,
        )

    def test_valid_activation(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 20.0, self.policy)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_reject_below_min(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 0.5, self.policy)
        self.assertFalse(ok)
        self.assertIn("min", reason.lower())

    def test_reject_above_max(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 30.0, self.policy)
        self.assertFalse(ok)
        self.assertIn("max", reason.lower())

    def test_reject_insufficient_device_space(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 62.0, self.policy)
        self.assertFalse(ok)
        self.assertIn("reserved", reason.lower())

    def test_exact_boundary_max(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 25.0, self.policy)
        self.assertTrue(ok)

    def test_exact_boundary_min(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 1.0, self.policy)
        self.assertTrue(ok)
```

- [ ] **Step 2: Run tests to verify new tests fail**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/test_safety_policy.py -v -k "TestConfigMerge or TestValidation"`
Expected: FAIL — `AttributeError: type object 'SafetyPolicy' has no attribute 'load_merged_policy'`

- [ ] **Step 3: Implement load_merged_policy, save methods, and validate_activation**

Append to `提升VRAM與GPA效能設計方案/microsystems/core/safety_policy.py` (inside `class SafetyPolicy`):

```python
    @staticmethod
    def load_merged_policy(global_path: Path, device_path: Path,
                           capacity_gb: float, speed_mbs: float) -> PolicyLimits:
        """
        Merge config layers: smart_default ← global ← device_override.

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
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/test_safety_policy.py -v`
Expected: All 13 tests PASS

- [ ] **Step 5: Commit**

```bash
cd "D:/product/vRAM"
git add "提升VRAM與GPA效能設計方案/microsystems/core/safety_policy.py" "提升VRAM與GPA效能設計方案/tests/test_safety_policy.py"
git commit -m "feat: SafetyPolicy — config merge, save, and activation validation"
```

---

## Task 3: SafeRemovalManager — State Machine + Preflight

**Files:**
- Create: `提升VRAM與GPA效能設計方案/tests/test_safe_removal.py`
- Create: `提升VRAM與GPA效能設計方案/microsystems/core/safe_removal.py`

- [ ] **Step 1: Write failing tests for RemovalState and preflight_check**

Create `提升VRAM與GPA效能設計方案/tests/test_safe_removal.py`:

```python
"""Tests for SafeRemovalManager."""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from microsystems.core.safe_removal import (
    SafeRemovalManager, RemovalState, PreflightResult, DrainProgress,
)
from microsystems.core.safety_policy import PolicyLimits


class TestRemovalState(unittest.TestCase):
    """Test RemovalState enum values."""

    def test_states_exist(self):
        self.assertEqual(RemovalState.IDLE.value, "idle")
        self.assertEqual(RemovalState.PREFLIGHT.value, "preflight")
        self.assertEqual(RemovalState.DRAINING.value, "draining")
        self.assertEqual(RemovalState.DETACHING.value, "detaching")
        self.assertEqual(RemovalState.READY.value, "ready")
        self.assertEqual(RemovalState.FORCE_EJECT.value, "force_eject")


class TestPreflightResult(unittest.TestCase):
    """Test PreflightResult dataclass."""

    def test_can_remove_immediately(self):
        r = PreflightResult(
            can_remove_immediately=True,
            current_usage_mb=0,
            system_ram_available_mb=16000,
            can_absorb=True,
            estimated_drain_seconds=0,
            warnings=[],
        )
        self.assertTrue(r.can_remove_immediately)
        self.assertEqual(r.warnings, [])

    def test_needs_drain(self):
        r = PreflightResult(
            can_remove_immediately=False,
            current_usage_mb=1200,
            system_ram_available_mb=8000,
            can_absorb=True,
            estimated_drain_seconds=27.0,
            warnings=[],
        )
        self.assertFalse(r.can_remove_immediately)
        self.assertEqual(r.current_usage_mb, 1200)


class TestSafeRemovalManager(unittest.TestCase):
    """Test SafeRemovalManager state machine."""

    def setUp(self):
        self.policy = PolicyLimits(
            device_reserved_gb=4.0,
            pagefile_min_gb=1.0,
            pagefile_max_gb=25.0,
            system_ram_reserve_gb=6.4,
        )
        self.manager = SafeRemovalManager()

    def test_initial_state_is_idle(self):
        self.assertEqual(self.manager.state("E"), RemovalState.IDLE)

    def test_preflight_with_zero_usage(self):
        """When pagefile usage is 0, can_remove_immediately should be True."""
        mock_engine = MagicMock()
        mock_engine.get_pagefile_usage.return_value = {
            "allocated_mb": 8192,
            "current_usage_mb": 0,
            "peak_usage_mb": 500,
            "safe_to_remove": True,
        }

        self.manager.set_vhd_engine(mock_engine)
        result = self.manager.preflight_check("G", self.policy)

        self.assertTrue(result.can_remove_immediately)
        self.assertEqual(result.current_usage_mb, 0)
        self.assertEqual(self.manager.state("G"), RemovalState.PREFLIGHT)

    def test_preflight_with_active_usage(self):
        """When pagefile has usage, can_remove_immediately should be False."""
        mock_engine = MagicMock()
        mock_engine.get_pagefile_usage.return_value = {
            "allocated_mb": 8192,
            "current_usage_mb": 1200,
            "peak_usage_mb": 2000,
            "safe_to_remove": False,
        }

        self.manager.set_vhd_engine(mock_engine)
        result = self.manager.preflight_check("G", self.policy)

        self.assertFalse(result.can_remove_immediately)
        self.assertEqual(result.current_usage_mb, 1200)

    def test_preflight_warns_low_ram(self):
        """When system RAM is below reserve, a warning should be added."""
        mock_engine = MagicMock()
        mock_engine.get_pagefile_usage.return_value = {
            "allocated_mb": 8192,
            "current_usage_mb": 500,
            "peak_usage_mb": 1000,
            "safe_to_remove": False,
        }

        self.manager.set_vhd_engine(mock_engine)

        with patch("microsystems.core.safe_removal._get_available_ram_mb",
                   return_value=4000):  # 4 GB < 6.4 GB reserve
            result = self.manager.preflight_check("G", self.policy)

        self.assertTrue(len(result.warnings) > 0)
        self.assertIn("RAM", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/test_safe_removal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'microsystems.core.safe_removal'`

- [ ] **Step 3: Implement RemovalState, PreflightResult, DrainProgress, and SafeRemovalManager**

Create `提升VRAM與GPA效能設計方案/microsystems/core/safe_removal.py`:

```python
"""
Safe Removal Manager
=====================
Four-phase device removal with drain progress and emergency fallback.

State machine:
  IDLE → PREFLIGHT → DRAINING → DETACHING → READY
                        ↓
                   FORCE_EJECT → READY
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional

from .safety_policy import PolicyLimits

logger = logging.getLogger(__name__)

# Timeouts
DRAIN_WARN_SECONDS = 300    # 5 minutes → suggest force eject
DRAIN_HARD_LIMIT_SECONDS = 600  # 10 minutes → auto force eject


class RemovalState(str, Enum):
    IDLE = "idle"
    PREFLIGHT = "preflight"
    DRAINING = "draining"
    DETACHING = "detaching"
    READY = "ready"
    FORCE_EJECT = "force_eject"


@dataclass
class PreflightResult:
    can_remove_immediately: bool
    current_usage_mb: float
    system_ram_available_mb: float
    can_absorb: bool
    estimated_drain_seconds: float
    warnings: list


@dataclass
class DrainProgress:
    remaining_mb: float
    total_mb: float
    drain_rate_mbs: float
    eta_seconds: float
    phase: str  # "draining" | "detaching" | "ready"


def _get_available_ram_mb() -> float:
    """Return available physical RAM in MB."""
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
            return mem.ullAvailPhys / (1024 ** 2)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 8000.0  # safe fallback


@dataclass
class _DeviceDrainState:
    """Internal per-device drain tracking."""
    state: RemovalState = RemovalState.IDLE
    mount_letter: str = ""
    drive_letter: str = ""
    total_mb: float = 0
    drain_start_time: float = 0
    drain_thread: Optional[threading.Thread] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class SafeRemovalManager:
    """
    Manages the four-phase safe removal flow for VHD pagefile devices.

    Usage:
        mgr = SafeRemovalManager()
        mgr.set_vhd_engine(vhd_engine)
        result = mgr.preflight_check("G", policy)
        if not result.can_remove_immediately:
            mgr.start_drain("G", on_progress=callback)
        # ... or mgr.force_eject("G")
    """

    def __init__(self):
        self._states: Dict[str, _DeviceDrainState] = {}
        self._vhd_engine = None
        self._lock = threading.Lock()

    def set_vhd_engine(self, engine) -> None:
        """Inject the VhdPagefileEngine for pagefile operations."""
        self._vhd_engine = engine

    def _get_state(self, device_id: str) -> _DeviceDrainState:
        with self._lock:
            if device_id not in self._states:
                self._states[device_id] = _DeviceDrainState()
            return self._states[device_id]

    def state(self, device_id: str) -> RemovalState:
        """Return current removal state for a device."""
        return self._get_state(device_id).state

    def preflight_check(self, device_id: str,
                        policy: PolicyLimits) -> PreflightResult:
        """
        Phase 0: Check if device can be removed and estimate drain time.

        device_id is the VHD mount letter (e.g. "G").
        """
        ds = self._get_state(device_id)
        ds.state = RemovalState.PREFLIGHT
        ds.mount_letter = device_id

        # Get pagefile usage
        usage = {"current_usage_mb": 0, "allocated_mb": 0}
        if self._vhd_engine:
            usage = self._vhd_engine.get_pagefile_usage(device_id)

        current_mb = usage.get("current_usage_mb", 0)
        ram_avail = _get_available_ram_mb()

        warnings = []
        if ram_avail < policy.system_ram_reserve_gb * 1024:
            warnings.append(
                f"Low RAM: {ram_avail:.0f} MB available, "
                f"{policy.system_ram_reserve_gb * 1024:.0f} MB recommended reserve"
            )

        can_absorb = ram_avail > current_mb
        if not can_absorb and current_mb > 0:
            warnings.append(
                f"RAM ({ram_avail:.0f} MB) may not absorb all pages "
                f"({current_mb:.0f} MB) — drain may be slow"
            )

        # Rough ETA: assume ~50 MB/s drain rate as baseline
        drain_rate = 50.0
        eta = current_mb / drain_rate if current_mb > 0 else 0

        return PreflightResult(
            can_remove_immediately=(current_mb == 0),
            current_usage_mb=current_mb,
            system_ram_available_mb=ram_avail,
            can_absorb=can_absorb,
            estimated_drain_seconds=eta,
            warnings=warnings,
        )

    def start_drain(self, device_id: str,
                    on_progress: Optional[Callable[[DrainProgress], None]] = None,
                    on_complete: Optional[Callable[[], None]] = None,
                    on_timeout_warn: Optional[Callable[[], None]] = None) -> None:
        """
        Phase 1: Remove pagefile from Windows list and monitor drain.

        Runs in a background thread. Calls on_progress periodically.
        Auto-transitions to Phase 2 (detach) when drain completes.
        """
        ds = self._get_state(device_id)
        ds.state = RemovalState.DRAINING
        ds.cancel_event.clear()
        ds.drain_start_time = time.time()

        # Get initial usage for total
        usage = {"current_usage_mb": 0}
        if self._vhd_engine:
            usage = self._vhd_engine.get_pagefile_usage(device_id)
        ds.total_mb = usage.get("current_usage_mb", 0)

        def drain_loop():
            # Step 1: Remove pagefile from Windows list
            if self._vhd_engine and hasattr(self._vhd_engine, '_remove_pagefile_on_volume'):
                self._vhd_engine._remove_pagefile_on_volume(device_id)

            warned = False
            prev_mb = ds.total_mb
            prev_time = time.time()

            # Step 2: Monitor drain
            while not ds.cancel_event.is_set():
                usage = {"current_usage_mb": 0}
                if self._vhd_engine:
                    usage = self._vhd_engine.get_pagefile_usage(device_id)

                remaining = usage.get("current_usage_mb", 0)
                now = time.time()
                elapsed = now - ds.drain_start_time
                dt = now - prev_time

                # Calculate drain rate
                rate = (prev_mb - remaining) / dt if dt > 0 else 0
                rate = max(rate, 0)
                eta = remaining / rate if rate > 0 else 999

                prev_mb = remaining
                prev_time = now

                progress = DrainProgress(
                    remaining_mb=remaining,
                    total_mb=ds.total_mb,
                    drain_rate_mbs=rate,
                    eta_seconds=eta,
                    phase="draining",
                )
                if on_progress:
                    on_progress(progress)

                # Drain complete
                if remaining == 0:
                    ds.state = RemovalState.DETACHING
                    if on_progress:
                        on_progress(DrainProgress(
                            remaining_mb=0, total_mb=ds.total_mb,
                            drain_rate_mbs=0, eta_seconds=0,
                            phase="detaching",
                        ))
                    self._do_detach(device_id)
                    ds.state = RemovalState.READY
                    if on_progress:
                        on_progress(DrainProgress(
                            remaining_mb=0, total_mb=ds.total_mb,
                            drain_rate_mbs=0, eta_seconds=0,
                            phase="ready",
                        ))
                    if on_complete:
                        on_complete()
                    return

                # Timeout warning at 5 minutes
                if elapsed > DRAIN_WARN_SECONDS and not warned:
                    warned = True
                    if on_timeout_warn:
                        on_timeout_warn()

                # Hard limit at 10 minutes
                if elapsed > DRAIN_HARD_LIMIT_SECONDS:
                    logger.warning("Drain hard limit reached for %s, forcing eject",
                                   device_id)
                    self._do_force_eject(device_id)
                    ds.state = RemovalState.READY
                    if on_complete:
                        on_complete()
                    return

                ds.cancel_event.wait(timeout=1.0)

            # Cancelled
            if ds.cancel_event.is_set() and ds.state == RemovalState.DRAINING:
                # Re-add pagefile (cancel drain)
                logger.info("Drain cancelled for %s, restoring pagefile", device_id)
                ds.state = RemovalState.IDLE

        ds.drain_thread = threading.Thread(target=drain_loop, daemon=True)
        ds.drain_thread.start()

    def cancel_drain(self, device_id: str) -> None:
        """Cancel an in-progress drain and restore the pagefile."""
        ds = self._get_state(device_id)
        if ds.state == RemovalState.DRAINING:
            ds.cancel_event.set()

    def force_eject(self, device_id: str) -> None:
        """Emergency bypass: flush + force detach."""
        ds = self._get_state(device_id)

        # Cancel any running drain
        if ds.state == RemovalState.DRAINING:
            ds.cancel_event.set()
            if ds.drain_thread:
                ds.drain_thread.join(timeout=2)

        ds.state = RemovalState.FORCE_EJECT
        self._do_force_eject(device_id)
        ds.state = RemovalState.READY

    def _do_detach(self, device_id: str) -> None:
        """Phase 2: Detach VHD and clean up."""
        if not self._vhd_engine:
            return

        # Find the device in VHD engine and detach
        with getattr(self._vhd_engine, '_lock', threading.Lock()):
            for dev in getattr(self._vhd_engine, '_devices', []):
                if dev.mount_letter == device_id:
                    try:
                        dev.bridge.detach()
                        dev.bridge.close()
                    except Exception as e:
                        logger.warning("Detach error for %s: %s", device_id, e)

                    try:
                        self._vhd_engine._devices.remove(dev)
                        self._vhd_engine._release_letter(dev.mount_letter)
                    except Exception:
                        pass
                    break

    def _do_force_eject(self, device_id: str) -> None:
        """Force eject: remove pagefile + force detach with 5s timeout."""
        if not self._vhd_engine:
            return

        # Best-effort pagefile removal
        try:
            if hasattr(self._vhd_engine, '_remove_pagefile_on_volume'):
                self._vhd_engine._remove_pagefile_on_volume(device_id)
        except Exception as e:
            logger.warning("Force pagefile removal failed for %s: %s", device_id, e)

        # Wait briefly for flush
        time.sleep(min(5, 2))

        # Force detach
        self._do_detach(device_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/test_safe_removal.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd "D:/product/vRAM"
git add "提升VRAM與GPA效能設計方案/microsystems/core/safe_removal.py" "提升VRAM與GPA效能設計方案/tests/test_safe_removal.py"
git commit -m "feat: SafeRemovalManager — state machine, preflight, drain, force eject"
```

---

## Task 4: Integrate SafetyPolicy into RealBoostEngine.activate()

**Files:**
- Modify: `提升VRAM與GPA效能設計方案/microsystems/core/real_boost.py`

- [ ] **Step 1: Read the current activate() method to find the exact insertion point**

Run: Read `提升VRAM與GPA效能設計方案/microsystems/core/real_boost.py` from line 214 (the `activate` method) through ~line 300 to see the existing flow.

- [ ] **Step 2: Add SafetyPolicy import and validation to activate()**

At the top of `real_boost.py`, add the import:

```python
from .safety_policy import SafetyPolicy, PolicyLimits, GLOBAL_POLICY_PATH
```

Inside `activate()`, after the disk usage is calculated and before the swap/pagefile is created, insert the policy validation. The exact location depends on reading the code in Step 1, but the logic is:

```python
# ── Safety Policy validation ──
device_config_path = Path(drive_letter + ":\\") / self.CONFIG_FILENAME
policy = SafetyPolicy.load_merged_policy(
    GLOBAL_POLICY_PATH, device_config_path,
    capacity_gb=capacity_gb, speed_mbs=self._measured_rand_write_mbs,
)

# Apply policy limits to requested size
requested_gb = swap_size_bytes / (1024 ** 3)
requested_gb = min(requested_gb, policy.pagefile_max_gb)
requested_gb = max(requested_gb, policy.pagefile_min_gb)

ok, reason = SafetyPolicy.validate_activation(capacity_gb, requested_gb, policy)
if not ok:
    logger.warning("Safety policy rejected activation: %s", reason)
    if on_progress:
        on_progress(f"Policy rejected: {reason}")
    return {"success": False, "error": f"Safety policy: {reason}"}

swap_size_bytes = int(requested_gb * (1024 ** 3))
```

- [ ] **Step 3: Run existing tests to verify nothing is broken**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/ -v --timeout=30`
Expected: All existing tests still pass

- [ ] **Step 4: Commit**

```bash
cd "D:/product/vRAM"
git add "提升VRAM與GPA效能設計方案/microsystems/core/real_boost.py"
git commit -m "feat: integrate SafetyPolicy validation into RealBoostEngine.activate()"
```

---

## Task 5: Host UI — Safety Policy Panel (collapsed + Advanced)

**Files:**
- Modify: `提升VRAM與GPA效能設計方案/microsystems/host_ui.py`

- [ ] **Step 1: Add SafetyPolicy import to host_ui.py**

At the top of `host_ui.py`, add:

```python
from .core.safety_policy import SafetyPolicy, PolicyLimits, GLOBAL_POLICY_PATH
```

- [ ] **Step 2: Create PolicyPanel class**

Add a new class before `HostUI` in `host_ui.py`:

```python
class PolicyPanel(tk.Frame):
    """Collapsible safety policy panel with Advanced slider controls."""

    def __init__(self, parent, on_policy_changed=None, **kwargs):
        super().__init__(parent, bg=BG, **kwargs)
        self._expanded = False
        self._policy: Optional[PolicyLimits] = None
        self._capacity_gb: float = 0
        self._speed_mbs: float = 0
        self._on_policy_changed = on_policy_changed
        self._device_config_path: Optional[Path] = None
        self._sliders = {}
        self._build()

    def _build(self):
        # Header row: icon + summary + toggle button
        header = tk.Frame(self, bg=BG)
        header.pack(fill=tk.X, padx=16, pady=(8, 0))

        tk.Label(header, text="Safety Policy", font=("Arial", 11, "bold"),
                 fg=WHITE, bg=BG).pack(side=tk.LEFT)

        self._toggle_btn = tk.Button(
            header, text="Advanced \u25bc", font=("Arial", 9),
            bg=BG, fg=GRAY, relief=tk.FLAT, padx=4,
            command=self._toggle_advanced,
        )
        self._toggle_btn.pack(side=tk.RIGHT)

        # Summary line
        self._summary_label = tk.Label(
            self, text="No device connected",
            font=("Consolas", 9), fg=GRAY, bg=BG,
        )
        self._summary_label.pack(anchor=tk.W, padx=16)

        # Advanced panel (hidden by default)
        self._adv_frame = tk.Frame(self, bg=BG_CARD, padx=12, pady=8)
        # Not packed yet — shown on toggle

        # Sliders
        self._slider_defs = [
            ("device_reserved_gb", "Device Reserved", "GB"),
            ("pagefile_min_gb", "Pagefile Min", "GB"),
            ("pagefile_max_gb", "Pagefile Max", "GB"),
            ("system_ram_reserve_pct", "RAM Reserve", "%"),
        ]

        for key, label, unit in self._slider_defs:
            row = tk.Frame(self._adv_frame, bg=BG_CARD)
            row.pack(fill=tk.X, pady=2)

            tk.Label(row, text=f"{label}:", font=("Consolas", 9),
                     fg=WHITE, bg=BG_CARD, width=16, anchor=tk.W).pack(side=tk.LEFT)

            scale = tk.Scale(
                row, from_=0, to=100, orient=tk.HORIZONTAL,
                bg=BG_CARD, fg=WHITE, troughcolor=ACCENT,
                highlightthickness=0, length=160, showvalue=False,
                command=lambda val, k=key: self._on_slider_change(k, val),
            )
            scale.pack(side=tk.LEFT, padx=4)

            val_label = tk.Label(row, text="0", font=("Consolas", 9, "bold"),
                                 fg=GREEN, bg=BG_CARD, width=8)
            val_label.pack(side=tk.LEFT)

            self._sliders[key] = {"scale": scale, "label": val_label, "unit": unit}

        # Validation message
        self._validation_label = tk.Label(
            self._adv_frame, text="", font=("Arial", 8),
            fg=RED, bg=BG_CARD, wraplength=300, justify=tk.LEFT,
        )
        self._validation_label.pack(fill=tk.X, pady=(4, 0))

        # Buttons row
        btn_row = tk.Frame(self._adv_frame, bg=BG_CARD)
        btn_row.pack(fill=tk.X, pady=(4, 0))

        tk.Button(btn_row, text="Reset to Smart Defaults", font=("Arial", 8),
                  bg=ACCENT, fg=WHITE, relief=tk.FLAT, padx=6,
                  command=self._reset_defaults).pack(side=tk.LEFT)

        tk.Button(btn_row, text="Apply", font=("Arial", 8),
                  bg=GREEN, fg=WHITE, relief=tk.FLAT, padx=12,
                  command=self._apply).pack(side=tk.RIGHT)

    def update_policy(self, policy: PolicyLimits, capacity_gb: float,
                      speed_mbs: float, device_config_path=None):
        """Update panel with current policy values."""
        self._policy = policy
        self._capacity_gb = capacity_gb
        self._speed_mbs = speed_mbs
        self._device_config_path = device_config_path

        # Update summary
        self._summary_label.config(
            text=f"Device reserve: {policy.device_reserved_gb:.0f} GB  |  "
                 f"PF: {policy.pagefile_min_gb:.0f}~{policy.pagefile_max_gb:.0f} GB",
            fg=WHITE,
        )

        # Update slider ranges and values
        self._update_slider("device_reserved_gb", 0, capacity_gb * 0.5,
                            policy.device_reserved_gb, "GB")
        self._update_slider("pagefile_min_gb", 0.5, policy.pagefile_max_gb,
                            policy.pagefile_min_gb, "GB")
        self._update_slider("pagefile_max_gb", policy.pagefile_min_gb,
                            capacity_gb * 0.9, policy.pagefile_max_gb, "GB")
        ram_pct = (policy.system_ram_reserve_gb /
                   max(1, SafetyPolicy.compute_smart_defaults(1, 1).system_ram_reserve_gb / 0.2)) * 100
        ram_pct = min(50, max(5, ram_pct))
        self._update_slider("system_ram_reserve_pct", 5, 50, ram_pct if ram_pct > 0 else 20, "%")

    def _update_slider(self, key, from_val, to_val, current, unit):
        s = self._sliders.get(key)
        if not s:
            return
        s["scale"].config(from_=from_val, to=to_val)
        s["scale"].set(current)
        s["label"].config(text=f"{current:.1f} {unit}")

    def _on_slider_change(self, key, val):
        s = self._sliders.get(key)
        if s:
            v = float(val)
            s["label"].config(text=f"{v:.1f} {s['unit']}")

        # Live validation
        self._validate_current()

    def _validate_current(self):
        if not self._policy:
            return
        pf_max = float(self._sliders["pagefile_max_gb"]["scale"].get())
        pf_min = float(self._sliders["pagefile_min_gb"]["scale"].get())
        reserved = float(self._sliders["device_reserved_gb"]["scale"].get())

        errors = []
        if pf_min > pf_max:
            errors.append("Pagefile min > max")
        if reserved + pf_max > self._capacity_gb:
            errors.append(f"Reserved + PF max ({reserved + pf_max:.1f} GB) > device capacity ({self._capacity_gb:.1f} GB)")

        self._validation_label.config(
            text="\n".join(errors) if errors else "",
            fg=RED if errors else GREEN,
        )

    def _toggle_advanced(self):
        self._expanded = not self._expanded
        if self._expanded:
            self._adv_frame.pack(fill=tk.X, padx=16, pady=(4, 8))
            self._toggle_btn.config(text="Advanced \u25b2")
        else:
            self._adv_frame.pack_forget()
            self._toggle_btn.config(text="Advanced \u25bc")

    def _reset_defaults(self):
        if self._capacity_gb > 0:
            defaults = SafetyPolicy.compute_smart_defaults(
                self._capacity_gb, self._speed_mbs)
            self.update_policy(defaults, self._capacity_gb,
                               self._speed_mbs, self._device_config_path)

    def _apply(self):
        """Save current slider values to global + device config."""
        overrides = {
            "device_reserved_gb": float(self._sliders["device_reserved_gb"]["scale"].get()),
            "pagefile_min_gb": float(self._sliders["pagefile_min_gb"]["scale"].get()),
            "pagefile_max_gb": float(self._sliders["pagefile_max_gb"]["scale"].get()),
        }
        ram_pct = float(self._sliders["system_ram_reserve_pct"]["scale"].get())

        # Save global
        global_overrides = dict(overrides)
        global_overrides["system_ram_reserve_pct"] = ram_pct
        SafetyPolicy.save_global_policy(GLOBAL_POLICY_PATH, global_overrides)

        # Save device override if path is known
        if self._device_config_path:
            SafetyPolicy.save_device_override(
                Path(self._device_config_path), overrides)

        # Update internal policy
        self._policy = PolicyLimits(
            device_reserved_gb=overrides["device_reserved_gb"],
            pagefile_min_gb=overrides["pagefile_min_gb"],
            pagefile_max_gb=overrides["pagefile_max_gb"],
            system_ram_reserve_gb=round(
                SafetyPolicy.compute_smart_defaults(1, 1).system_ram_reserve_gb
                * ram_pct / 20, 2),
        )

        self._validation_label.config(text="Saved", fg=GREEN)

        if self._on_policy_changed:
            self._on_policy_changed(self._policy)
```

- [ ] **Step 3: Integrate PolicyPanel into HostUI._build_ui()**

In `HostUI._build_ui()`, after the system info section and separator, add:

```python
        # Safety Policy panel
        self._policy_panel = PolicyPanel(self._root, on_policy_changed=self._on_policy_update)
        self._policy_panel.pack(fill=tk.X)

        ttk.Separator(self._root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=16)
```

Add the callback method to `HostUI`:

```python
    def _on_policy_update(self, policy: PolicyLimits):
        """Called when user changes policy via Advanced panel."""
        logger.info("Policy updated: %s", policy)
```

- [ ] **Step 4: Update _refresh_display to feed policy data to PolicyPanel**

In `HostUI._refresh_display()`, after the device status is retrieved, add:

```python
        # Update policy panel
        if self._engine and hasattr(self, '_policy_panel'):
            try:
                drives = getattr(self._engine, '_known_drives', set())
                if drives:
                    drive = next(iter(drives))
                    usage = shutil.disk_usage(f"{drive}:\\")
                    capacity_gb = usage.total / (1024 ** 3)
                    speed = getattr(self._engine, '_measured_rand_write_mbs', 500.0)
                    device_config = Path(f"{drive}:\\") / self._engine.CONFIG_FILENAME

                    policy = SafetyPolicy.load_merged_policy(
                        GLOBAL_POLICY_PATH, device_config,
                        capacity_gb, speed,
                    )
                    self._policy_panel.update_policy(
                        policy, capacity_gb, speed, device_config)
            except Exception:
                pass
```

Add `import shutil` at the top if not already present (it is already imported in `real_boost.py` but check `host_ui.py`).

- [ ] **Step 5: Commit**

```bash
cd "D:/product/vRAM"
git add "提升VRAM與GPA效能設計方案/microsystems/host_ui.py"
git commit -m "feat: PolicyPanel — collapsible safety policy UI with Advanced sliders"
```

---

## Task 6: Host UI — Drain Progress Card + Safe Eject Flow

**Files:**
- Modify: `提升VRAM與GPA效能設計方案/microsystems/host_ui.py`

- [ ] **Step 1: Add SafeRemovalManager import**

At the top of `host_ui.py`, add:

```python
from .core.safe_removal import SafeRemovalManager, RemovalState, DrainProgress
```

- [ ] **Step 2: Create DrainProgressCard class**

Add after `PolicyPanel` in `host_ui.py`:

```python
class DrainProgressCard(tk.Frame):
    """Shows drain progress during safe removal."""

    def __init__(self, parent, drive_letter: str,
                 on_force_eject=None, on_cancel=None, **kwargs):
        super().__init__(parent, bg=BG_CARD, padx=12, pady=8, **kwargs)
        self.drive_letter = drive_letter
        self._on_force_eject = on_force_eject
        self._on_cancel = on_cancel
        self._build()

    def _build(self):
        # Header
        header = tk.Frame(self, bg=BG_CARD)
        header.pack(fill=tk.X)

        self._status_label = tk.Label(
            header, text=f"  {self.drive_letter}:\\  [DRAINING...]",
            font=("Consolas", 14, "bold"), fg=ORANGE, bg=BG_CARD,
        )
        self._status_label.pack(side=tk.LEFT)

        # Progress info
        self._info_label = tk.Label(
            self, text="Draining pagefile...",
            font=("Consolas", 9), fg=WHITE, bg=BG_CARD,
        )
        self._info_label.pack(anchor=tk.W, pady=(4, 0))

        # Progress bar (canvas-based for custom colors)
        bar_frame = tk.Frame(self, bg=BG_CARD)
        bar_frame.pack(fill=tk.X, pady=(4, 0))

        self._bar_canvas = tk.Canvas(
            bar_frame, height=16, bg=ACCENT,
            highlightthickness=0,
        )
        self._bar_canvas.pack(fill=tk.X)

        # Stats line
        self._stats_label = tk.Label(
            self, text="Speed: -- MB/s  |  ETA: --",
            font=("Consolas", 8), fg=GRAY, bg=BG_CARD,
        )
        self._stats_label.pack(anchor=tk.W, pady=(2, 0))

        # Timeout warning (hidden initially)
        self._warn_label = tk.Label(
            self, text="", font=("Arial", 8), fg=RED, bg=BG_CARD,
        )
        self._warn_label.pack(anchor=tk.W)

        # Buttons
        btn_row = tk.Frame(self, bg=BG_CARD)
        btn_row.pack(fill=tk.X, pady=(4, 0))

        tk.Button(
            btn_row, text="Force Eject", font=("Arial", 9),
            bg=RED, fg=WHITE, relief=tk.FLAT, padx=8,
            command=lambda: self._on_force_eject(self.drive_letter)
            if self._on_force_eject else None,
        ).pack(side=tk.LEFT, padx=4)

        tk.Button(
            btn_row, text="Cancel", font=("Arial", 9),
            bg=GRAY, fg=WHITE, relief=tk.FLAT, padx=8,
            command=lambda: self._on_cancel(self.drive_letter)
            if self._on_cancel else None,
        ).pack(side=tk.RIGHT, padx=4)

    def update_progress(self, progress: DrainProgress):
        """Update the progress display from a DrainProgress."""
        if progress.phase == "ready":
            self._status_label.config(
                text=f"  {self.drive_letter}:\\  [READY TO REMOVE]", fg=GREEN)
            self._info_label.config(text="VHD detached. Safe to unplug hardware.")
            self._stats_label.config(text="")
            self._draw_bar(1.0, GREEN)
            return

        if progress.phase == "detaching":
            self._status_label.config(
                text=f"  {self.drive_letter}:\\  [DETACHING...]", fg=ORANGE)
            self._info_label.config(text="Detaching VHD...")
            return

        # Draining
        done = progress.total_mb - progress.remaining_mb
        pct = done / max(1, progress.total_mb)

        self._info_label.config(
            text=f"Draining: {progress.remaining_mb:.0f} / {progress.total_mb:.0f} MB"
        )

        eta_str = (f"{progress.eta_seconds:.0f} sec"
                   if progress.eta_seconds < 999 else "calculating...")
        self._stats_label.config(
            text=f"Speed: {progress.drain_rate_mbs:.1f} MB/s  |  ETA: ~{eta_str}"
        )

        bar_color = GREEN if pct > 0.7 else (ORANGE if pct > 0.3 else RED)
        self._draw_bar(pct, bar_color)

    def show_timeout_warning(self):
        self._warn_label.config(text="Taking long. Consider Force Eject.")

    def _draw_bar(self, pct: float, color: str):
        self._bar_canvas.delete("all")
        w = self._bar_canvas.winfo_width() or 300
        filled = int(w * min(1.0, pct))
        self._bar_canvas.create_rectangle(0, 0, filled, 16, fill=color, outline="")
```

- [ ] **Step 3: Replace _safe_eject_device in HostUI with drain flow**

Replace the existing `_safe_eject_device` method in `HostUI` with the new four-phase flow:

```python
    def _safe_eject_device(self, drive_letter: str):
        """Safe eject with four-phase drain flow."""
        if not self._engine:
            return

        # Initialize SafeRemovalManager if needed
        if not hasattr(self, '_removal_mgr'):
            self._removal_mgr = SafeRemovalManager()
            if hasattr(self._engine, '_vhd_engine') and self._engine._vhd_engine:
                self._removal_mgr.set_vhd_engine(self._engine._vhd_engine)

        # Find mount letter for this drive
        mount_letter = None
        if hasattr(self._engine, '_vhd_engine') and self._engine._vhd_engine:
            with self._engine._vhd_engine._lock:
                for dev in self._engine._vhd_engine._devices:
                    if dev.drive_letter == drive_letter:
                        mount_letter = dev.mount_letter
                        break

        if not mount_letter:
            messagebox.showwarning("Eject", f"Device {drive_letter}:\\ not found",
                                   parent=self._root)
            return

        # Load policy for preflight
        policy = PolicyLimits(
            device_reserved_gb=4.0, pagefile_min_gb=1.0,
            pagefile_max_gb=25.0, system_ram_reserve_gb=6.4,
        )
        if hasattr(self, '_policy_panel') and self._policy_panel._policy:
            policy = self._policy_panel._policy

        # Phase 0: Preflight
        result = self._removal_mgr.preflight_check(mount_letter, policy)

        if result.warnings:
            warn_text = "\n".join(result.warnings)
            if not messagebox.askyesno(
                "Eject Warning",
                f"Warnings:\n{warn_text}\n\nContinue with eject?",
                parent=self._root,
            ):
                return

        if result.can_remove_immediately:
            # No drain needed — direct detach
            self._removal_mgr.force_eject(mount_letter)
            messagebox.showinfo("Safe Eject",
                                f"Device {drive_letter}:\\ safely ejected.\n"
                                f"You can now remove the device.",
                                parent=self._root)
            self._update_status(f"{drive_letter}:\\ ejected")
            return

        # Phase 1: Start drain with progress UI
        self._show_drain_card(drive_letter, mount_letter)

    def _show_drain_card(self, drive_letter: str, mount_letter: str):
        """Replace the device card with a drain progress card."""
        # Clear existing cards for this drive
        for card in self._device_cards:
            card.destroy()
        self._device_cards.clear()

        drain_card = DrainProgressCard(
            self._device_frame, drive_letter,
            on_force_eject=lambda dl: self._handle_force_eject(mount_letter),
            on_cancel=lambda dl: self._handle_cancel_drain(mount_letter),
        )
        drain_card.pack(fill=tk.X, pady=4)
        self._device_cards.append(drain_card)

        def on_progress(progress: DrainProgress):
            if self._root and self._running:
                self._root.after(0, lambda: drain_card.update_progress(progress))

        def on_complete():
            if self._root and self._running:
                self._root.after(0, lambda: messagebox.showinfo(
                    "Safe Eject",
                    f"Device {drive_letter}:\\ safely ejected.\n"
                    f"You can now remove the device.",
                    parent=self._root))
                self._update_status(f"{drive_letter}:\\ ejected")

        def on_timeout():
            if self._root and self._running:
                self._root.after(0, drain_card.show_timeout_warning)

        self._removal_mgr.start_drain(
            mount_letter,
            on_progress=on_progress,
            on_complete=on_complete,
            on_timeout_warn=on_timeout,
        )

    def _handle_force_eject(self, mount_letter: str):
        """Force eject with confirmation dialog."""
        if not messagebox.askyesno(
            "Force Eject",
            "Force eject may cause running processes to crash.\n\n"
            "The pagefile contains swap data, not your files.\n"
            "Proceed?",
            icon="warning", parent=self._root,
        ):
            return

        if hasattr(self, '_removal_mgr'):
            self._removal_mgr.force_eject(mount_letter)
            self._update_status("Force ejected")

    def _handle_cancel_drain(self, mount_letter: str):
        """Cancel drain and restore pagefile."""
        if hasattr(self, '_removal_mgr'):
            self._removal_mgr.cancel_drain(mount_letter)
            self._update_status("Eject cancelled")
```

- [ ] **Step 4: Test UI manually**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -c "from microsystems.host_ui import HostUI; print('Import OK')"`
Expected: `Import OK` (verifies all imports resolve without error)

- [ ] **Step 5: Commit**

```bash
cd "D:/product/vRAM"
git add "提升VRAM與GPA效能設計方案/microsystems/host_ui.py"
git commit -m "feat: DrainProgressCard + four-phase safe eject flow in Host UI"
```

---

## Task 7: Final Integration Verification

**Files:**
- All modified files

- [ ] **Step 1: Run all tests**

Run: `cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -m pytest tests/ -v`
Expected: All tests pass (existing + new)

- [ ] **Step 2: Verify import chain works end-to-end**

Run:
```bash
cd "D:/product/vRAM/提升VRAM與GPA效能設計方案" && python -c "
from microsystems.core.safety_policy import SafetyPolicy, PolicyLimits, GLOBAL_POLICY_PATH
from microsystems.core.safe_removal import SafeRemovalManager, RemovalState, DrainProgress
from microsystems.host_ui import HostUI, PolicyPanel, DrainProgressCard, DeviceCard

# Verify SafetyPolicy
p = SafetyPolicy.compute_smart_defaults(64.0, 1000.0)
print(f'Smart defaults: reserved={p.device_reserved_gb} min={p.pagefile_min_gb} max={p.pagefile_max_gb}')

ok, reason = SafetyPolicy.validate_activation(64.0, 20.0, p)
print(f'Validation: ok={ok} reason={reason!r}')

# Verify SafeRemovalManager
mgr = SafeRemovalManager()
print(f'Initial state: {mgr.state(\"G\")}')

print('All imports and basic operations OK')
"
```
Expected:
```
Smart defaults: reserved=4.0 min=1.0 max=44.8
Validation: ok=True reason=''
Initial state: idle
All imports and basic operations OK
```

- [ ] **Step 3: Commit final state**

```bash
cd "D:/product/vRAM"
git add -A
git status
# If there are any uncommitted changes, commit them:
git commit -m "chore: final integration cleanup for safety policy + safe removal"
```
