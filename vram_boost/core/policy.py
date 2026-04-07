"""
Expansion policy engine.
Decides whether and how to use a detected device.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .detect import ExternalDevice
from .measure import SpeedResult

logger = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2


@dataclass
class ExpansionPlan:
    device: ExternalDevice
    speed: SpeedResult
    action: str           # "expand", "skip", "too_slow", "too_small"
    swap_bytes: int = 0
    block_size: int = 0
    reason: str = ""

    @property
    def swap_gb(self) -> float:
        return self.swap_bytes / GB


def evaluate(device: ExternalDevice, speed: SpeedResult) -> ExpansionPlan:
    """
    Decide whether to expand memory using this device.

    Rules:
      - speed < 1 MB/s → skip (unusable)
      - free < 512 MB → skip (too small)
      - otherwise → expand with auto-tuned params
    """
    # Too slow
    if speed.write_mbs < 1.0:
        return ExpansionPlan(
            device=device, speed=speed, action="too_slow",
            reason=f"Write speed {speed.write_mbs:.1f} MB/s below minimum 1 MB/s",
        )

    # Too small
    if device.free_bytes < 512 * MB:
        return ExpansionPlan(
            device=device, speed=speed, action="too_small",
            reason=f"Free space {device.free_gb:.1f} GB below minimum 0.5 GB",
        )

    # Calculate swap size
    # Cap at 8GB: even Llama-405B INT4+compression = 6.8GB
    # Larger swap wastes creation time on slow devices
    MAX_SWAP = 8 * GB
    speed_limit = int(speed.write_mbs * 120 * MB)    # 2 min fill time
    capacity_limit = int(device.free_bytes * 0.5)     # 50% of free
    swap_bytes = min(speed_limit, capacity_limit, MAX_SWAP)

    # Block size based on speed
    if speed.write_mbs < 30:
        block_size = 16 * MB
    elif speed.write_mbs < 100:
        block_size = 32 * MB
    elif speed.write_mbs < 500:
        block_size = 64 * MB
    else:
        block_size = 64 * MB

    logger.info(
        "Policy: expand %s — %.1f GB swap, %d MB blocks (%.1f MB/s)",
        device.name, swap_bytes / GB, block_size // MB, speed.write_mbs,
    )

    return ExpansionPlan(
        device=device,
        speed=speed,
        action="expand",
        swap_bytes=swap_bytes,
        block_size=block_size,
        reason=f"Speed {speed.write_mbs:.1f} MB/s, allocating {swap_bytes/GB:.1f} GB",
    )
