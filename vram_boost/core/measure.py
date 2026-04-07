"""Cross-platform device speed measurement."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MB = 1024 * 1024


@dataclass
class SpeedResult:
    write_mbs: float
    device_path: str
    test_size_mb: int = 4

    @property
    def is_usable(self) -> bool:
        """Minimum 1 MB/s to be useful."""
        return self.write_mbs >= 1.0


def measure_write_speed(device_path: str, test_size_mb: int = 4) -> SpeedResult:
    """
    Measure sequential write speed.
    Writes test_size_mb of random data, syncs, measures wall time.
    Runs twice and takes the average.
    """
    test_file = os.path.join(device_path, ".vram_speed_test")
    data = os.urandom(test_size_mb * MB)
    speeds = []

    try:
        for _ in range(2):
            flags = os.O_CREAT | os.O_RDWR | os.O_TRUNC
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY

            fd = os.open(test_file, flags)
            t0 = time.perf_counter()
            os.write(fd, data)
            os.fsync(fd)
            t1 = time.perf_counter()
            os.close(fd)
            speeds.append(test_size_mb / max(t1 - t0, 0.001))
    except Exception as e:
        logger.warning("Speed test failed on %s: %s", device_path, e)
        return SpeedResult(write_mbs=0.0, device_path=device_path)
    finally:
        try:
            os.unlink(test_file)
        except OSError:
            pass

    avg = sum(speeds) / len(speeds) if speeds else 0.0
    logger.info("Speed test %s: %.1f MB/s", device_path, avg)
    return SpeedResult(write_mbs=round(avg, 1), device_path=device_path)
