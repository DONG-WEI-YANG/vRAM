"""
Tests for MmapSwapEngine (mmap_engine.py)
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "提升VRAM與GPA效能設計方案"))

from microsystems.core.mmap_engine import MmapSwapEngine, _MONITOR_INTERVAL
from microsystems.core.mmap_swap import SWAP_FILENAME


# Small block size for fast tests
import mmap as _mmap_mod
# On Windows, mmap offsets must be aligned to ALLOCATIONGRANULARITY (64 KB).
_TEST_BLOCK_SIZE = _mmap_mod.ALLOCATIONGRANULARITY  # typically 64 KB


class TestMmapSwapEngine:
    """Unit tests for MmapSwapEngine."""

    # ── test_activate_creates_swap_on_device ───────────────────────────

    def test_activate_creates_swap_on_device(self, tmp_path):
        """activate() should create the swap file, report success,
        needs_reboot=False, and status should show active."""
        engine = MmapSwapEngine()
        device_dir = str(tmp_path)
        size = _TEST_BLOCK_SIZE * 4  # 4 blocks

        try:
            result = engine.activate(
                device_path=device_dir,
                size_bytes=size,
                block_size=_TEST_BLOCK_SIZE,
            )

            assert result["success"] is True
            assert result["needs_reboot"] is False
            assert result["method"] == "mmap_swap"
            assert result["total_blocks"] == 4

            # Swap file must exist on disk
            swap_file = os.path.join(device_dir, SWAP_FILENAME)
            assert os.path.isfile(swap_file)

            # Status should report active
            st = engine.status()
            assert st["active"] is True
            assert st["total_blocks"] == 4
            assert st["mapped_blocks"] == 4
            assert st["degraded"] is False
        finally:
            engine.deactivate()

    # ── test_disconnect_triggers_degradation ───────────────────────────

    def test_disconnect_triggers_degradation(self, tmp_path):
        """After deleting the swap file (simulating disconnect) and
        waiting for the monitor, status.degraded should become True."""
        engine = MmapSwapEngine()
        device_dir = str(tmp_path)
        size = _TEST_BLOCK_SIZE * 2

        state_changes: list = []

        def on_state(new_state: str) -> None:
            state_changes.append(new_state)

        try:
            result = engine.activate(
                device_path=device_dir,
                size_bytes=size,
                block_size=_TEST_BLOCK_SIZE,
                on_state_change=on_state,
            )
            assert result["success"] is True

            # Simulate device disconnect by removing the swap file.
            # First close the mmap objects so we can delete the file on Windows.
            swap_file = os.path.join(device_dir, SWAP_FILENAME)
            if engine._swap is not None:
                for bid in list(engine._swap.blocks):
                    blk = engine._swap.blocks[bid]
                    if blk.mmap_obj is not None:
                        try:
                            blk.mmap_obj.close()
                        except Exception:
                            pass
                        blk.mmap_obj = None
                # Also close the fd so the file can be deleted on Windows
                if engine._swap._fd is not None:
                    try:
                        os.close(engine._swap._fd)
                    except OSError:
                        pass
                    engine._swap._fd = None

            os.remove(swap_file)

            # Wait long enough for the monitor thread to detect the loss.
            # The breaker needs 2 consecutive failures to trip (failure_threshold=2),
            # each detected at _MONITOR_INTERVAL intervals.
            # We wait for 3 cycles to be safe.
            time.sleep(_MONITOR_INTERVAL * 3 + 1)

            st = engine.status()
            assert st["degraded"] is True, (
                f"Expected degraded=True, got {st}"
            )
            assert "degraded" in state_changes
        finally:
            engine.deactivate()
