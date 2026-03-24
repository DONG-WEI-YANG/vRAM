"""
Tests for SwapFileManager (mmap_swap.py)
"""

import os
import sys
import tempfile

import pytest

# Ensure the package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "提升VRAM與GPA效能設計方案"))

from microsystems.core.mmap_swap import (
    SwapFileManager,
    DEFAULT_BLOCK_SIZE,
    SWAP_FILENAME,
)


# Use a small block size for tests so we don't allocate 64 MB per block.
import mmap as _mmap_mod
# On Windows, mmap offsets must be aligned to ALLOCATIONGRANULARITY (64 KB).
# Use that as the minimum test block size.
_TEST_BLOCK_SIZE = _mmap_mod.ALLOCATIONGRANULARITY  # typically 64 KB


class TestSwapFileManager:
    """Unit tests for SwapFileManager."""

    # ── test_create_swap_file ──────────────────────────────────────────

    def test_create_swap_file(self, tmp_path):
        """create() produces a file whose size equals the requested size
        (rounded up to block_size)."""
        mgr = SwapFileManager(block_size=_TEST_BLOCK_SIZE)
        swap_path = str(tmp_path / SWAP_FILENAME)

        requested = _TEST_BLOCK_SIZE * 3  # exactly 3 blocks
        mgr.create(swap_path, requested)

        actual_size = os.path.getsize(swap_path)
        assert actual_size == requested, (
            f"Expected {requested} bytes, got {actual_size}"
        )

    def test_create_swap_file_rounds_up(self, tmp_path):
        """create() rounds up to the nearest block boundary."""
        mgr = SwapFileManager(block_size=_TEST_BLOCK_SIZE)
        swap_path = str(tmp_path / SWAP_FILENAME)

        requested = _TEST_BLOCK_SIZE * 2 + 100  # not a multiple
        mgr.create(swap_path, requested)

        expected = _TEST_BLOCK_SIZE * 3
        actual_size = os.path.getsize(swap_path)
        assert actual_size == expected

    # ── test_map_and_write_block ───────────────────────────────────────

    def test_map_and_write_block(self, tmp_path):
        """Map a block, write data, read it back, and verify access_count
        is incremented."""
        mgr = SwapFileManager(block_size=_TEST_BLOCK_SIZE)
        swap_path = str(tmp_path / SWAP_FILENAME)
        mgr.create(swap_path, _TEST_BLOCK_SIZE * 2)
        mgr.open()

        try:
            mgr.map_block(0)

            payload = b"hello mmap"
            assert mgr.write_block(0, payload, 0) is True

            data = mgr.read_block(0, 0, len(payload))
            assert data == payload

            # write + read = 2 accesses
            stats = mgr.get_block_stats()
            block0 = next(b for b in stats if b.block_id == 0)
            assert block0.access_count == 2
        finally:
            mgr.close()

    # ── test_device_presence_check ─────────────────────────────────────

    def test_device_presence_check(self, tmp_path):
        """is_device_present() returns False after the swap file is deleted."""
        mgr = SwapFileManager(block_size=_TEST_BLOCK_SIZE)
        swap_path = str(tmp_path / SWAP_FILENAME)
        mgr.create(swap_path, _TEST_BLOCK_SIZE)

        assert mgr.is_device_present() is True

        os.remove(swap_path)

        assert mgr.is_device_present() is False

    # ── test_read_after_unmap_returns_none ──────────────────────────────

    def test_read_after_unmap_returns_none(self, tmp_path):
        """Reading from an unmapped block returns None."""
        mgr = SwapFileManager(block_size=_TEST_BLOCK_SIZE)
        swap_path = str(tmp_path / SWAP_FILENAME)
        mgr.create(swap_path, _TEST_BLOCK_SIZE * 2)
        mgr.open()

        try:
            mgr.map_block(0)
            mgr.write_block(0, b"data", 0)

            mgr.unmap_block(0)

            result = mgr.read_block(0, 0, 4)
            assert result is None
        finally:
            mgr.close()
