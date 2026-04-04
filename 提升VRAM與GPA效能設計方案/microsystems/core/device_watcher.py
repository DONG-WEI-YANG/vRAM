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
