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
import platform
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, Dict, List, Any

from .device_query import get_external_drive_letters, classify_device

logger = logging.getLogger(__name__)


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
