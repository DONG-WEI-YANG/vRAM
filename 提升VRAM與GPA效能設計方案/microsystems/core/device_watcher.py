"""
Real-Time Device Watcher
=========================
即時偵測 Windows 外接裝置的插入/拔除。

Primary:  PowerShell WMI Event Subscription (< 1s latency)
Fallback: 3-second polling via get_external_drive_letters()

消費者透過 on_change() 訂閱 DeviceChangeInfo callback。
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, Dict, List, Any

from .device_query import get_external_drive_letters, classify_device

logger = logging.getLogger(__name__)

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

_NO_WINDOW = 0
if platform.system().lower() == "windows":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class DeviceEvent(Enum):
    ARRIVED = "arrived"
    REMOVED = "removed"


@dataclass
class DeviceChangeInfo:
    event: DeviceEvent
    drive_letter: str
    device_info: Optional[Dict[str, Any]]
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
        self._active = False
        self._ps_proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._event_driven = False
        self._last_heartbeat = 0.0

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
