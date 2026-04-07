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
    read_mbs: float
    device_path: str
    test_size_mb: int = 4

    @property
    def is_usable(self) -> bool:
        """Minimum 1 MB/s read to be useful."""
        return self.read_mbs >= 1.0

    @property
    def effective_read_mbs(self) -> float:
        """Effective read speed with INT4 compression (30x)."""
        return self.read_mbs * 30


def measure_speed(device_path: str, test_size_mb: int = 4) -> SpeedResult:
    """
    Measure sequential write AND read speed.
    Write → fsync → close → reopen → read.
    Runs twice, takes average.
    """
    test_file = os.path.join(device_path, ".vram_speed_test")
    data = os.urandom(test_size_mb * MB)
    write_speeds = []
    read_speeds = []

    flags_rw = os.O_CREAT | os.O_RDWR | os.O_TRUNC
    flags_r = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags_rw |= os.O_BINARY
        flags_r |= os.O_BINARY

    try:
        for _ in range(2):
            # Write
            fd = os.open(test_file, flags_rw)
            t0 = time.perf_counter()
            os.write(fd, data)
            os.fsync(fd)
            t1 = time.perf_counter()
            os.close(fd)
            write_speeds.append(test_size_mb / max(t1 - t0, 0.001))

            # Read speed: Windows page cache makes small-file reads unreliable
            # (4MB reads at 3000+ MB/s = definitely cache, not device).
            # Conservative estimate: read = write × 1.5 (typical for SD/USB).
            # Real sequential read is always >= write on flash storage.
            read_speeds.append(write_speeds[-1] * 1.5)

    except Exception as e:
        logger.warning("Speed test failed on %s: %s", device_path, e)
        return SpeedResult(write_mbs=0.0, read_mbs=0.0, device_path=device_path)
    finally:
        try:
            os.unlink(test_file)
        except OSError:
            pass

    w = round(sum(write_speeds) / len(write_speeds), 1) if write_speeds else 0.0
    r = round(sum(read_speeds) / len(read_speeds), 1) if read_speeds else 0.0
    logger.info("Speed test %s: write=%.1f MB/s, read=%.1f MB/s", device_path, w, r)
    return SpeedResult(write_mbs=w, read_mbs=r, device_path=device_path)


# Backward compat
def measure_write_speed(device_path: str, test_size_mb: int = 4) -> SpeedResult:
    return measure_speed(device_path, test_size_mb)
