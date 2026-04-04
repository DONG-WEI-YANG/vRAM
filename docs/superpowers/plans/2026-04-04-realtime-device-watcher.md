# Real-Time Device Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 5-10 second polling with < 1 second event-driven device detection on Windows, with automatic fallback to 3-second polling.

**Architecture:** New `DeviceWatcher` module owns a long-running PowerShell subprocess that subscribes to WMI `Win32_VolumeChangeEvent`. On event, it diffs the current drive snapshot against the previous one and fires ARRIVED/REMOVED callbacks. Three consumers (HealthMonitor, hotplug_launcher, *_vram_system) subscribe. A smart expansion policy auto-joins high-speed devices and prompts for medium-speed ones.

**Tech Stack:** Python stdlib (subprocess, threading, json, enum, dataclasses), PowerShell WMI, existing `device_query.py` functions.

**Spec:** `docs/superpowers/specs/2026-04-04-realtime-device-watcher-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `microsystems/core/device_watcher.py` | **CREATE** | DeviceWatcher class: WMI subprocess, polling fallback, diff logic, callback dispatch |
| `tests/test_device_watcher.py` | **CREATE** | All unit tests for DeviceWatcher |
| `microsystems/core/health_monitor.py` | MODIFY | Add `attach_watcher()`, skip connection polling when watcher active |
| `microsystems/hotplug_launcher.py` | MODIFY | Subscribe to watcher, add smart expansion UI + removal notifications |
| `microsystems/systems/sd_vram_system.py` | MODIFY | Accept watcher, wire ARRIVED → expand, REMOVED → disconnect |
| `microsystems/systems/enc_vram_system.py` | MODIFY | Accept watcher, wire ARRIVED → expand, REMOVED → disconnect |
| `microsystems/systems/usb_vram_system.py` | MODIFY | Accept watcher, wire ARRIVED → expand, REMOVED → disconnect |

All paths below are relative to `提升VRAM與GPA效能設計方案/`.

---

### Task 1: DeviceWatcher — Data types and diff logic

**Files:**
- Create: `microsystems/core/device_watcher.py`
- Create: `tests/test_device_watcher.py`

This task builds the pure-logic core: enums, dataclasses, and the snapshot diff function. No I/O, no threads.

- [ ] **Step 1: Write failing tests for diff logic**

```python
# tests/test_device_watcher.py
"""Tests for DeviceWatcher — real-time device detection."""

import unittest
from microsystems.core.device_watcher import (
    DeviceEvent, DeviceChangeInfo, diff_snapshots,
)


class TestDiffSnapshots(unittest.TestCase):
    """Test the pure snapshot diff logic."""

    def test_no_change(self):
        prev = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        curr = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertEqual(arrived, [])
        self.assertEqual(removed, [])

    def test_single_arrival(self):
        prev = {"E": {"bus_type": "USB"}}
        curr = {"E": {"bus_type": "USB"}, "G": {"bus_type": "NVMe"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertEqual(arrived, ["G"])
        self.assertEqual(removed, [])

    def test_single_removal(self):
        prev = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        curr = {"E": {"bus_type": "USB"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertEqual(arrived, [])
        self.assertEqual(removed, ["F"])

    def test_simultaneous_arrival_and_removal(self):
        prev = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        curr = {"E": {"bus_type": "USB"}, "G": {"bus_type": "NVMe"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertIn("G", arrived)
        self.assertIn("F", removed)

    def test_empty_prev(self):
        arrived, removed = diff_snapshots({}, {"E": {"bus_type": "USB"}})
        self.assertEqual(arrived, ["E"])
        self.assertEqual(removed, [])

    def test_empty_both(self):
        arrived, removed = diff_snapshots({}, {})
        self.assertEqual(arrived, [])
        self.assertEqual(removed, [])


class TestDeviceChangeInfo(unittest.TestCase):
    def test_fields(self):
        info = DeviceChangeInfo(
            event=DeviceEvent.ARRIVED,
            drive_letter="G",
            device_info=None,
            timestamp=1000.0,
        )
        self.assertEqual(info.event, DeviceEvent.ARRIVED)
        self.assertEqual(info.drive_letter, "G")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py" -v`
Expected: `ModuleNotFoundError` — `device_watcher` doesn't exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# microsystems/core/device_watcher.py
"""
Real-Time Device Watcher
=========================
即時偵測 Windows 外接裝置的插入/拔除。

Primary:  PowerShell WMI Event Subscription (< 1s latency)
Fallback: 3-second polling via get_external_drive_letters()

消費者透過 on_change() 訂閱 DeviceChangeInfo callback。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, Dict, List, Any

logger = logging.getLogger(__name__)


class DeviceEvent(Enum):
    ARRIVED = "arrived"
    REMOVED = "removed"


@dataclass
class DeviceChangeInfo:
    event: DeviceEvent
    drive_letter: str
    device_info: Optional[Dict[str, Any]]  # from classify_device / get_external_drive_letters
    timestamp: float


def diff_snapshots(
    prev: Dict[str, Dict],
    curr: Dict[str, Dict],
) -> tuple:
    """
    Compare two drive-letter snapshots, return (arrived, removed) letter lists.

    Each snapshot is {letter: info_dict} from get_external_drive_letters().
    """
    prev_set = set(prev.keys())
    curr_set = set(curr.keys())
    arrived = sorted(curr_set - prev_set)
    removed = sorted(prev_set - curr_set)
    return arrived, removed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py" -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/microsystems/core/device_watcher.py" \
       "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py"
git commit -m "feat(device_watcher): data types and snapshot diff logic"
```

---

### Task 2: DeviceWatcher — Bandwidth estimation and smart policy

**Files:**
- Modify: `microsystems/core/device_watcher.py`
- Modify: `tests/test_device_watcher.py`

Add the function that estimates bandwidth from device classification and returns the expansion decision.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_device_watcher.py`:

```python
from microsystems.core.device_watcher import ExpansionAction, evaluate_expansion_policy


class TestExpansionPolicy(unittest.TestCase):
    """Test smart expansion: auto / prompt / ignore."""

    def test_nvme_enclosure_auto(self):
        action = evaluate_expansion_policy("nvme_enclosure", "NVMe")
        self.assertEqual(action, ExpansionAction.AUTO_EXPAND)

    def test_sd_express_auto(self):
        action = evaluate_expansion_policy("sd_express", "NVMe")
        self.assertEqual(action, ExpansionAction.AUTO_EXPAND)

    def test_usb_ssd_prompt(self):
        action = evaluate_expansion_policy("usb_ssd", "USB")
        self.assertEqual(action, ExpansionAction.PROMPT_USER)

    def test_usb_drive_prompt(self):
        action = evaluate_expansion_policy("usb_drive", "USB")
        self.assertEqual(action, ExpansionAction.PROMPT_USER)

    def test_sd_card_prompt(self):
        action = evaluate_expansion_policy("sd_card", "SD")
        self.assertEqual(action, ExpansionAction.PROMPT_USER)

    def test_hdd_ignore(self):
        action = evaluate_expansion_policy("hdd", "USB")
        self.assertEqual(action, ExpansionAction.IGNORE)

    def test_unknown_type_ignore(self):
        action = evaluate_expansion_policy("unknown", "")
        self.assertEqual(action, ExpansionAction.IGNORE)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py::TestExpansionPolicy" -v`
Expected: `ImportError` — `ExpansionAction` not defined yet.

- [ ] **Step 3: Write implementation**

Add to `microsystems/core/device_watcher.py`:

```python
class ExpansionAction(Enum):
    AUTO_EXPAND = "auto_expand"    # >= 500 MB/s: NVMe enclosure, SD Express
    PROMPT_USER = "prompt_user"    # 50-500 MB/s: USB SSD, USB drive, SD card
    IGNORE = "ignore"              # < 50 MB/s: HDD, unknown


# Device type -> expansion action mapping
_EXPANSION_MAP: Dict[str, ExpansionAction] = {
    "nvme_enclosure": ExpansionAction.AUTO_EXPAND,
    "sd_express": ExpansionAction.AUTO_EXPAND,
    "usb_ssd": ExpansionAction.PROMPT_USER,
    "usb_drive": ExpansionAction.PROMPT_USER,
    "sd_card": ExpansionAction.PROMPT_USER,
    "hdd": ExpansionAction.IGNORE,
}


def evaluate_expansion_policy(
    device_type: str,
    bus_type: str,
) -> ExpansionAction:
    """
    Determine expansion action based on device classification.

    device_type: output of classify_device() (e.g. "nvme_enclosure", "usb_ssd")
    bus_type: BusType string (e.g. "USB", "NVMe", "Thunderbolt")
    """
    # Thunderbolt/USB4 always high-speed
    if bus_type.lower() in ("thunderbolt", "usb4"):
        return ExpansionAction.AUTO_EXPAND

    return _EXPANSION_MAP.get(device_type, ExpansionAction.IGNORE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py" -v`
Expected: 13 passed (6 diff + 1 dataclass + 6 policy).

- [ ] **Step 5: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/microsystems/core/device_watcher.py" \
       "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py"
git commit -m "feat(device_watcher): expansion policy — auto/prompt/ignore"
```

---

### Task 3: DeviceWatcher — Callback registry and snapshot management

**Files:**
- Modify: `microsystems/core/device_watcher.py`
- Modify: `tests/test_device_watcher.py`

Build the `DeviceWatcher` class with callback registration, manual snapshot taking, and event dispatch. No PowerShell yet — test with direct `_process_snapshot()` calls.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_device_watcher.py`:

```python
from unittest.mock import patch, MagicMock
from microsystems.core.device_watcher import DeviceWatcher


class TestDeviceWatcherCallbacks(unittest.TestCase):
    """Test callback registration and dispatch via manual snapshot injection."""

    def setUp(self):
        self.watcher = DeviceWatcher()
        self.events: List[DeviceChangeInfo] = []
        self.watcher.on_change(self.events.append)

    def test_arrival_fires_callback(self):
        self.watcher._snapshot = {"E": {"bus_type": "USB", "friendly_name": "T5",
                                         "media_type": "SSD", "spindle_speed": 0,
                                         "size_bytes": 500_000_000_000}}
        new_snap = dict(self.watcher._snapshot)
        new_snap["G"] = {"bus_type": "NVMe", "friendly_name": "NVMe SSD",
                         "media_type": "SSD", "spindle_speed": 0,
                         "size_bytes": 1_000_000_000_000}
        self.watcher._process_snapshot(new_snap)

        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].event, DeviceEvent.ARRIVED)
        self.assertEqual(self.events[0].drive_letter, "G")

    def test_removal_fires_callback(self):
        self.watcher._snapshot = {
            "E": {"bus_type": "USB", "friendly_name": "T5",
                  "media_type": "SSD", "spindle_speed": 0,
                  "size_bytes": 500_000_000_000},
            "F": {"bus_type": "SD", "friendly_name": "SD Card",
                  "media_type": "SSD", "spindle_speed": 0,
                  "size_bytes": 128_000_000_000},
        }
        new_snap = {"E": self.watcher._snapshot["E"]}
        self.watcher._process_snapshot(new_snap)

        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].event, DeviceEvent.REMOVED)
        self.assertEqual(self.events[0].drive_letter, "F")

    def test_no_change_no_callback(self):
        self.watcher._snapshot = {"E": {"bus_type": "USB", "friendly_name": "T5",
                                         "media_type": "SSD", "spindle_speed": 0,
                                         "size_bytes": 500_000_000_000}}
        self.watcher._process_snapshot(dict(self.watcher._snapshot))
        self.assertEqual(len(self.events), 0)

    def test_multiple_callbacks(self):
        second: List[DeviceChangeInfo] = []
        self.watcher.on_change(second.append)

        self.watcher._snapshot = {}
        self.watcher._process_snapshot({"E": {"bus_type": "USB", "friendly_name": "T5",
                                               "media_type": "SSD", "spindle_speed": 0,
                                               "size_bytes": 500_000_000_000}})
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(second), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py::TestDeviceWatcherCallbacks" -v`
Expected: `AttributeError` — DeviceWatcher has no `_process_snapshot`.

- [ ] **Step 3: Write implementation**

Add the `DeviceWatcher` class to `microsystems/core/device_watcher.py`:

```python
import platform
from .device_query import get_external_drive_letters, classify_device


class DeviceWatcher:
    """
    Real-time device insertion/removal watcher.

    Primary:  PowerShell WMI event subscription (< 1s)
    Fallback: 3-second polling

    Usage:
        watcher = DeviceWatcher()
        watcher.on_change(my_callback)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(self) -> None:
        self._callbacks: List[Callable[[DeviceChangeInfo], None]] = []
        self._snapshot: Dict[str, Dict] = {}
        self._is_windows = platform.system().lower() == "windows"

    def on_change(self, callback: Callable[[DeviceChangeInfo], None]) -> None:
        """Register a callback for device arrival/removal events."""
        self._callbacks.append(callback)

    def take_snapshot(self) -> Dict[str, Dict]:
        """Take a current snapshot of external drives. Returns {letter: info_dict}."""
        drives = get_external_drive_letters()
        return {d["letter"].upper(): d for d in drives}

    def _process_snapshot(self, new_snapshot: Dict[str, Dict]) -> None:
        """Compare new snapshot with stored one, fire callbacks for changes."""
        arrived, removed = diff_snapshots(self._snapshot, new_snapshot)

        for letter in removed:
            old_info = self._snapshot.get(letter)
            change = DeviceChangeInfo(
                event=DeviceEvent.REMOVED,
                drive_letter=letter,
                device_info=old_info,
                timestamp=time.time(),
            )
            self._fire(change)

        for letter in arrived:
            info = new_snapshot.get(letter, {})
            # Enrich with device classification
            device_type = classify_device(
                bus_type=info.get("bus_type", ""),
                media_type=info.get("media_type", ""),
                friendly_name=info.get("friendly_name", ""),
                spindle_speed=info.get("spindle_speed", 0),
                capacity_gb=info.get("size_bytes", 0) / (1024 ** 3),
            )
            info["device_type"] = device_type
            info["expansion_action"] = evaluate_expansion_policy(
                device_type, info.get("bus_type", ""),
            ).value
            change = DeviceChangeInfo(
                event=DeviceEvent.ARRIVED,
                drive_letter=letter,
                device_info=info,
                timestamp=time.time(),
            )
            self._fire(change)

        self._snapshot = new_snapshot

    def _fire(self, change: DeviceChangeInfo) -> None:
        for cb in self._callbacks:
            try:
                cb(change)
            except Exception as e:
                logger.error("DeviceWatcher callback error: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py" -v`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/microsystems/core/device_watcher.py" \
       "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py"
git commit -m "feat(device_watcher): callback registry and snapshot processing"
```

---

### Task 4: DeviceWatcher — PowerShell WMI subprocess + polling fallback

**Files:**
- Modify: `microsystems/core/device_watcher.py`
- Modify: `tests/test_device_watcher.py`

Add `start()` / `stop()` with the PowerShell long-running subprocess and the 3-second polling fallback.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_device_watcher.py`:

```python
import threading


class TestDeviceWatcherLifecycle(unittest.TestCase):
    """Test start/stop and fallback behavior."""

    def test_start_stop(self):
        watcher = DeviceWatcher()
        watcher.start()
        self.assertTrue(watcher._active)
        watcher.stop()
        self.assertFalse(watcher._active)

    def test_double_start_is_safe(self):
        watcher = DeviceWatcher()
        watcher.start()
        watcher.start()  # should not raise
        watcher.stop()

    def test_stop_without_start_is_safe(self):
        watcher = DeviceWatcher()
        watcher.stop()  # should not raise

    @patch("microsystems.core.device_watcher.subprocess.Popen",
           side_effect=FileNotFoundError("no powershell"))
    def test_fallback_to_polling_when_ps_fails(self, mock_popen):
        watcher = DeviceWatcher()
        watcher.start()
        self.assertFalse(watcher.is_event_driven)
        self.assertTrue(watcher._active)
        watcher.stop()

    def test_is_event_driven_property(self):
        watcher = DeviceWatcher()
        self.assertFalse(watcher.is_event_driven)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py::TestDeviceWatcherLifecycle" -v`
Expected: `AttributeError` — no `start`, `stop`, `_active`, `is_event_driven`.

- [ ] **Step 3: Write implementation**

Add to `DeviceWatcher` in `microsystems/core/device_watcher.py`:

```python
import json
import subprocess
import threading

# PowerShell WMI event subscription script
_PS_WATCHER_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
Register-WmiEvent -Class Win32_VolumeChangeEvent -SourceIdentifier VolChange
while ($true) {
    $evt = Wait-Event -SourceIdentifier VolChange -Timeout 30
    if ($evt) {
        Remove-Event -SourceIdentifier VolChange
        $ts = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        Write-Output "{`"event`":`"volume_change`",`"ts`":$ts}"
    } else {
        Write-Output "{`"heartbeat`":true}"
    }
    [Console]::Out.Flush()
}
"""

# Add to __init__:
#   self._active = False
#   self._ps_proc: Optional[subprocess.Popen] = None
#   self._reader_thread: Optional[threading.Thread] = None
#   self._poll_thread: Optional[threading.Thread] = None
#   self._event_driven = False
#   self._last_heartbeat = 0.0

_NO_WINDOW = 0
if platform.system().lower() == "windows":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
```

Update `__init__` to include these fields. Then add:

```python
    @property
    def is_event_driven(self) -> bool:
        """True if PowerShell WMI subscription is active; False if polling fallback."""
        return self._event_driven

    def start(self) -> None:
        """Start watching. Tries PS WMI first, falls back to polling."""
        if self._active:
            return
        self._active = True

        # Take initial snapshot
        try:
            self._snapshot = self.take_snapshot()
        except Exception as e:
            logger.warning("Initial snapshot failed: %s", e)
            self._snapshot = {}

        # Try PowerShell WMI event subscription
        if self._is_windows:
            try:
                self._start_ps_watcher()
                return
            except (FileNotFoundError, OSError) as e:
                logger.warning("PowerShell WMI watcher failed, using polling: %s", e)

        # Fallback to polling
        self._start_polling()

    def stop(self) -> None:
        """Stop watching and clean up resources."""
        self._active = False

        if self._ps_proc and self._ps_proc.poll() is None:
            try:
                self._ps_proc.terminate()
                self._ps_proc.wait(timeout=5)
            except Exception:
                try:
                    self._ps_proc.kill()
                except Exception:
                    pass
            self._ps_proc = None

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=5)
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)

        self._event_driven = False
        logger.info("DeviceWatcher stopped")

    def _start_ps_watcher(self) -> None:
        """Launch PowerShell subprocess with WMI event subscription."""
        self._ps_proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NoLogo", "-Command", _PS_WATCHER_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            creationflags=_NO_WINDOW if _NO_WINDOW else 0,
        )
        self._event_driven = True
        self._last_heartbeat = time.time()

        self._reader_thread = threading.Thread(
            target=self._ps_reader_loop,
            name="DeviceWatcher-PS",
            daemon=True,
        )
        self._reader_thread.start()
        logger.info("DeviceWatcher started (event-driven: PowerShell WMI)")

    def _ps_reader_loop(self) -> None:
        """Read lines from PowerShell stdout, trigger snapshot on volume_change."""
        while self._active and self._ps_proc and self._ps_proc.poll() is None:
            try:
                line = self._ps_proc.stdout.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if msg.get("heartbeat"):
                    self._last_heartbeat = time.time()
                    continue

                if msg.get("event") == "volume_change":
                    self._last_heartbeat = time.time()
                    try:
                        new_snap = self.take_snapshot()
                        self._process_snapshot(new_snap)
                    except Exception as e:
                        logger.error("Snapshot after WMI event failed: %s", e)

            except Exception as e:
                logger.error("PS reader error: %s", e)
                break

        # PS process died — degrade to polling
        if self._active:
            logger.warning("PowerShell WMI subprocess exited, degrading to polling")
            self._event_driven = False
            self._start_polling()

    def _start_polling(self) -> None:
        """Start 3-second polling fallback."""
        self._event_driven = False
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="DeviceWatcher-Poll",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info("DeviceWatcher started (fallback: 3s polling)")

    def _poll_loop(self) -> None:
        """Poll for device changes every 3 seconds."""
        while self._active:
            try:
                new_snap = self.take_snapshot()
                self._process_snapshot(new_snap)
            except Exception as e:
                logger.debug("Poll snapshot failed: %s", e)

            # Sleep in small increments so stop() doesn't block for 3 seconds
            for _ in range(30):
                if not self._active:
                    return
                time.sleep(0.1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py" -v`
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/microsystems/core/device_watcher.py" \
       "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py"
git commit -m "feat(device_watcher): PowerShell WMI subprocess + polling fallback"
```

---

### Task 5: Integrate DeviceWatcher into HealthMonitor

**Files:**
- Modify: `microsystems/core/health_monitor.py:102-270`
- Modify: `tests/test_device_watcher.py`

Add `attach_watcher()` method. When watcher fires REMOVED, immediately call `_on_disconnect`. When watcher fires ARRIVED, immediately call `_on_reconnect`. Connection status polling skipped when watcher is active.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_device_watcher.py`:

```python
from microsystems.core.health_monitor import HealthMonitor


class TestHealthMonitorWatcherIntegration(unittest.TestCase):
    """Test that HealthMonitor receives instant events from DeviceWatcher."""

    def test_attach_watcher(self):
        monitor = HealthMonitor(check_interval_s=60)
        watcher = DeviceWatcher()
        monitor.attach_watcher(watcher)
        self.assertIs(monitor._watcher, watcher)

    def test_disconnect_via_watcher(self):
        monitor = HealthMonitor(check_interval_s=60)
        watcher = DeviceWatcher()
        disconnected = []
        monitor.on_disconnect(lambda did: disconnected.append(did))
        monitor.attach_watcher(watcher)

        # Simulate device removal
        watcher._snapshot = {"E": {"bus_type": "USB", "friendly_name": "T5",
                                    "media_type": "SSD", "spindle_speed": 0,
                                    "size_bytes": 500_000_000_000,
                                    "disk_number": 2}}
        watcher._process_snapshot({})

        self.assertEqual(len(disconnected), 1)
        # device_id format matches what systems use
        self.assertIn("E", disconnected[0])

    def test_reconnect_via_watcher(self):
        monitor = HealthMonitor(check_interval_s=60)
        watcher = DeviceWatcher()
        reconnected = []
        monitor.on_reconnect(lambda did: reconnected.append(did))
        monitor.attach_watcher(watcher)

        # Simulate device arrival
        watcher._snapshot = {}
        watcher._process_snapshot({"E": {"bus_type": "USB", "friendly_name": "T5",
                                          "media_type": "SSD", "spindle_speed": 0,
                                          "size_bytes": 500_000_000_000,
                                          "disk_number": 2}})
        self.assertEqual(len(reconnected), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py::TestHealthMonitorWatcherIntegration" -v`
Expected: `AttributeError` — `attach_watcher` not defined.

- [ ] **Step 3: Write implementation**

Add to `HealthMonitor.__init__` in `microsystems/core/health_monitor.py` (after line 139):

```python
        self._watcher = None  # Optional[DeviceWatcher]
```

Add method after `on_reconnect` (after line 167):

```python
    def attach_watcher(self, watcher) -> None:
        """
        Attach a DeviceWatcher for instant connection/disconnection detection.

        When attached, connection status is event-driven (< 1s).
        Temperature/wear/error polling continues at the configured interval.
        """
        from .device_watcher import DeviceEvent
        self._watcher = watcher

        def _on_watcher_event(change):
            if change.event == DeviceEvent.REMOVED:
                device_id = f"drive_{change.drive_letter}"
                logger.info("Watcher: instant disconnect for %s", device_id)
                if self._on_disconnect:
                    self._on_disconnect(device_id)
            elif change.event == DeviceEvent.ARRIVED:
                device_id = f"drive_{change.drive_letter}"
                logger.info("Watcher: instant reconnect for %s", device_id)
                if self._on_reconnect:
                    self._on_reconnect(device_id)

        watcher.on_change(_on_watcher_event)
        logger.info("HealthMonitor: attached DeviceWatcher (event-driven connection detection)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py" -v`
Expected: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/microsystems/core/health_monitor.py" \
       "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py"
git commit -m "feat(health_monitor): attach_watcher for instant disconnect/reconnect"
```

---

### Task 6: Integrate DeviceWatcher into hotplug_launcher

**Files:**
- Modify: `microsystems/hotplug_launcher.py:459-597`

Wire DeviceWatcher into the GUI. REMOVED → immediate quit (for own drive) or notification. ARRIVED → smart policy notification.

- [ ] **Step 1: Add watcher initialization in `_show_active`**

In `microsystems/hotplug_launcher.py`, add after `self._poll_monitor()` (line 537):

```python
        # 即時裝置監聽
        try:
            from .core.device_watcher import DeviceWatcher, DeviceEvent, ExpansionAction
            self._device_watcher = DeviceWatcher()
            self._device_watcher.on_change(self._on_device_event)
            self._device_watcher.start()
            logger.info("hotplug_launcher: DeviceWatcher attached")
        except Exception as e:
            logger.warning("DeviceWatcher unavailable: %s", e)
            self._device_watcher = None
```

- [ ] **Step 2: Add device event handler**

Add method to the launcher class (after `_poll_monitor`):

```python
    def _on_device_event(self, change):
        """Handle real-time device arrival/removal from DeviceWatcher."""
        from .core.device_watcher import DeviceEvent, ExpansionAction

        if change.event == DeviceEvent.REMOVED:
            if change.drive_letter == self._my_drive:
                # Our drive was removed — emergency quit
                logger.critical("Own drive %s removed — immediate exit", change.drive_letter)
                try:
                    self._root.after(0, self._quit)
                except Exception:
                    pass
                return

            # Another drive removed — show notification if in active phase
            if self._phase == "active":
                msg = f"{change.drive_letter}:\\ removed"
                logger.warning("Device removed: %s", msg)
                try:
                    self._root.after(0, lambda: self._show_notification(msg, self.ORANGE))
                except Exception:
                    pass

        elif change.event == DeviceEvent.ARRIVED:
            if self._phase != "active":
                return

            info = change.device_info or {}
            action = info.get("expansion_action", "ignore")
            name = info.get("friendly_name", "Unknown")
            letter = change.drive_letter

            if action == ExpansionAction.AUTO_EXPAND.value:
                msg = f"Auto-joined {letter}:\\ ({name})"
                logger.info("Auto-expand: %s", msg)
                try:
                    self._root.after(0, lambda: self._show_notification(msg, self.GREEN))
                except Exception:
                    pass

            elif action == ExpansionAction.PROMPT_USER.value:
                msg = f"Found {name} ({letter}:\\). Add to expansion?"
                logger.info("Prompt: %s", msg)
                try:
                    self._root.after(0, lambda: self._show_expansion_prompt(letter, name))
                except Exception:
                    pass
```

- [ ] **Step 3: Add notification and prompt UI helpers**

```python
    def _show_notification(self, message: str, color: str):
        """Show a temporary notification bar at the top of the active view."""
        if not hasattr(self, '_notif_lbl'):
            self._notif_lbl = tk.Label(
                self._frame, text="", font=("Segoe UI", 9),
                fg="white", bg=self.BG2, anchor="w", padx=10, pady=4,
            )
        self._notif_lbl.configure(text=message, bg=color)
        self._notif_lbl.pack(fill="x", before=self._frame.winfo_children()[0])
        # Auto-hide after 8 seconds
        self._root.after(8000, lambda: self._notif_lbl.pack_forget())

    def _show_expansion_prompt(self, letter: str, name: str):
        """Show a prompt asking user whether to add a device for expansion."""
        prompt_f = tk.Frame(self._frame, bg="#1a237e", padx=8, pady=6)
        prompt_f.pack(fill="x", before=self._frame.winfo_children()[0])

        tk.Label(prompt_f, text=f"Found: {name} ({letter}:\\)",
                 font=("Segoe UI", 9, "bold"), fg="white", bg="#1a237e").pack(anchor="w")

        btn_f = tk.Frame(prompt_f, bg="#1a237e")
        btn_f.pack(anchor="e", pady=(4, 0))

        def accept():
            prompt_f.destroy()
            self._show_notification(f"Added {letter}:\\ to expansion", self.GREEN)
            # TODO: wire to RealBoostEngine.expand_to_device in a future task

        def decline():
            prompt_f.destroy()

        tk.Button(btn_f, text="Add", font=("Segoe UI", 8), fg="white",
                  bg="#00c853", relief="flat", padx=8, command=accept).pack(side="left", padx=4)
        tk.Button(btn_f, text="Skip", font=("Segoe UI", 8), fg=self.GRAY,
                  bg="#333333", relief="flat", padx=8, command=decline).pack(side="left")
```

- [ ] **Step 4: Add watcher cleanup in `_quit`**

Find `_quit` method in `hotplug_launcher.py` and add before `self._root.destroy()`:

```python
        if hasattr(self, '_device_watcher') and self._device_watcher:
            self._device_watcher.stop()
```

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/" -x -q --tb=short`
Expected: 183+ passed (existing tests unbroken).

- [ ] **Step 6: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/microsystems/hotplug_launcher.py"
git commit -m "feat(hotplug_launcher): real-time device notifications via DeviceWatcher"
```

---

### Task 7: Integrate DeviceWatcher into *_vram_system modules

**Files:**
- Modify: `microsystems/systems/sd_vram_system.py`
- Modify: `microsystems/systems/enc_vram_system.py`
- Modify: `microsystems/systems/usb_vram_system.py`

Each system creates a shared DeviceWatcher, passes it to HealthMonitor via `attach_watcher`, and subscribes for ARRIVED (auto-expand) / REMOVED (instant disconnect).

- [ ] **Step 1: Modify sd_vram_system.py**

After the HealthMonitor setup block (around line 217), add:

```python
        # 7. 即時裝置監聽（取代 HealthMonitor 的連線 polling）
        try:
            from ..core.device_watcher import DeviceWatcher, DeviceEvent, ExpansionAction
            self._device_watcher = DeviceWatcher()
            self._device_watcher.on_change(self._on_device_change)
            self._monitor.attach_watcher(self._device_watcher)
            self._device_watcher.start()
        except Exception as e:
            logger.warning("DeviceWatcher not available: %s", e)
            self._device_watcher = None
```

Add handler method before `_handle_disconnect`:

```python
    def _on_device_change(self, change) -> None:
        """Handle real-time device events from DeviceWatcher."""
        from ..core.device_watcher import DeviceEvent, ExpansionAction

        if change.event == DeviceEvent.ARRIVED:
            info = change.device_info or {}
            action = info.get("expansion_action", "ignore")
            if action == ExpansionAction.AUTO_EXPAND.value:
                logger.info(
                    "Auto-expanding to %s:\\ (%s)",
                    change.drive_letter, info.get("friendly_name", ""),
                )
                # Dynamic expansion will be wired in a future task
                # via RealBoostEngine.expand_to_device()
```

Add watcher cleanup in the `deactivate()` method:

```python
        if hasattr(self, '_device_watcher') and self._device_watcher:
            self._device_watcher.stop()
```

- [ ] **Step 2: Modify enc_vram_system.py**

Same pattern as sd_vram_system. After HealthMonitor setup (around line 203):

```python
        # 7. 即時裝置監聽
        try:
            from ..core.device_watcher import DeviceWatcher, DeviceEvent, ExpansionAction
            self._device_watcher = DeviceWatcher()
            self._device_watcher.on_change(self._on_device_change)
            self._monitor.attach_watcher(self._device_watcher)
            self._device_watcher.start()
        except Exception as e:
            logger.warning("DeviceWatcher not available: %s", e)
            self._device_watcher = None
```

Add handler:

```python
    def _on_device_change(self, change) -> None:
        from ..core.device_watcher import DeviceEvent, ExpansionAction
        if change.event == DeviceEvent.ARRIVED:
            info = change.device_info or {}
            action = info.get("expansion_action", "ignore")
            if action == ExpansionAction.AUTO_EXPAND.value:
                logger.info(
                    "Auto-expanding to %s:\\ (%s)",
                    change.drive_letter, info.get("friendly_name", ""),
                )
```

Add watcher cleanup in `deactivate()`.

- [ ] **Step 3: Modify usb_vram_system.py**

Same pattern. After HealthMonitor setup (around line 226):

```python
        # 7. 即時裝置監聽
        try:
            from ..core.device_watcher import DeviceWatcher, DeviceEvent, ExpansionAction
            self._device_watcher = DeviceWatcher()
            self._device_watcher.on_change(self._on_device_change)
            self._monitor.attach_watcher(self._device_watcher)
            self._device_watcher.start()
        except Exception as e:
            logger.warning("DeviceWatcher not available: %s", e)
            self._device_watcher = None
```

Add handler:

```python
    def _on_device_change(self, change) -> None:
        from ..core.device_watcher import DeviceEvent, ExpansionAction
        if change.event == DeviceEvent.ARRIVED:
            info = change.device_info or {}
            action = info.get("expansion_action", "ignore")
            if action == ExpansionAction.AUTO_EXPAND.value:
                logger.info(
                    "Auto-expanding to %s:\\ (%s)",
                    change.drive_letter, info.get("friendly_name", ""),
                )
```

Add watcher cleanup in `deactivate()`.

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/" -x -q --tb=short`
Expected: 183+ passed.

- [ ] **Step 5: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/microsystems/systems/sd_vram_system.py" \
       "提升VRAM與GPA效能設計方案/microsystems/systems/enc_vram_system.py" \
       "提升VRAM與GPA效能設計方案/microsystems/systems/usb_vram_system.py"
git commit -m "feat(systems): wire DeviceWatcher into sd/enc/usb vram systems"
```

---

### Task 8: Final integration test — full event flow

**Files:**
- Modify: `tests/test_device_watcher.py`

End-to-end test: DeviceWatcher → HealthMonitor → system disconnect callback, all in one flow.

- [ ] **Step 1: Write integration test**

Append to `tests/test_device_watcher.py`:

```python
class TestEndToEndEventFlow(unittest.TestCase):
    """Integration: watcher -> health_monitor -> system handler."""

    def test_removal_reaches_system_handler(self):
        """Simulate: device removed -> watcher -> monitor -> disconnect callback."""
        watcher = DeviceWatcher()
        monitor = HealthMonitor(check_interval_s=60)

        disconnect_log = []
        monitor.on_disconnect(lambda did: disconnect_log.append(did))
        monitor.attach_watcher(watcher)

        # Also track raw watcher events
        watcher_log = []
        watcher.on_change(lambda c: watcher_log.append(c))

        # Set initial state: drive E exists
        watcher._snapshot = {"E": {"bus_type": "USB", "friendly_name": "T5",
                                    "media_type": "SSD", "spindle_speed": 0,
                                    "size_bytes": 500_000_000_000}}

        # Drive E disappears
        watcher._process_snapshot({})

        # Watcher saw the event
        self.assertEqual(len(watcher_log), 1)
        self.assertEqual(watcher_log[0].event, DeviceEvent.REMOVED)

        # Monitor received instant disconnect
        self.assertEqual(len(disconnect_log), 1)
        self.assertIn("E", disconnect_log[0])

    def test_arrival_with_expansion_policy(self):
        """Simulate: NVMe device plugged in -> auto-expand classification."""
        watcher = DeviceWatcher()
        events = []
        watcher.on_change(events.append)

        watcher._snapshot = {}
        watcher._process_snapshot({
            "G": {
                "bus_type": "NVMe",
                "friendly_name": "Samsung 990 Pro",
                "media_type": "SSD",
                "spindle_speed": 0,
                "size_bytes": 1_000_000_000_000,
            },
        })

        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.event, DeviceEvent.ARRIVED)
        self.assertEqual(evt.drive_letter, "G")
        self.assertEqual(evt.device_info["device_type"], "nvme_enclosure")
        self.assertEqual(evt.device_info["expansion_action"], "auto_expand")

    def test_arrival_usb_drive_prompts(self):
        """Simulate: USB drive plugged in -> prompt classification."""
        watcher = DeviceWatcher()
        events = []
        watcher.on_change(events.append)

        watcher._snapshot = {}
        watcher._process_snapshot({
            "H": {
                "bus_type": "USB",
                "friendly_name": "Generic USB Flash",
                "media_type": "",
                "spindle_speed": 0,
                "size_bytes": 32_000_000_000,
            },
        })

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].device_info["expansion_action"], "prompt_user")
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py" -v`
Expected: 28+ passed.

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest "提升VRAM與GPA效能設計方案/tests/" -x -q --tb=short`
Expected: 186+ passed (183 original + 3 new integration).

- [ ] **Step 4: Commit**

```bash
git add "提升VRAM與GPA效能設計方案/tests/test_device_watcher.py"
git commit -m "test(device_watcher): end-to-end event flow integration tests"
```
