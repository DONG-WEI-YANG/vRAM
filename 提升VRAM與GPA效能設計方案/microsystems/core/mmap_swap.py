"""
SwapFileManager -- Low-level mmap operations for external storage
=================================================================
Manages a swap file on an external storage device (SD card / USB SSD).

Creates a pre-allocated file, maps fixed-size blocks (default 64 MB) using
Python's ``mmap`` module, and provides safe read/write with access counting
for LRU tracking.
"""

from __future__ import annotations

import logging
import mmap
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

DEFAULT_BLOCK_SIZE: int = 64 * 1024 * 1024  # 64 MB
SWAP_FILENAME: str = "vram_boost.swap"
_ALLOC_CHUNK: int = 1 * 1024 * 1024  # 1 MB write-chunk for pre-allocation
# On Windows mmap offsets must be aligned to ALLOCATIONGRANULARITY (64 KB).
_GRANULARITY: int = mmap.ALLOCATIONGRANULARITY


@dataclass
class MappedBlock:
    """Metadata for a single memory-mapped block inside the swap file."""

    block_id: int
    offset: int
    size: int
    mmap_obj: Optional[mmap.mmap] = field(default=None, repr=False)
    label: str = ""
    access_count: int = 0
    last_access: float = 0.0
    state: str = "free"  # "free" | "mapped" | "evicted"


class SwapFileManager:
    """
    Manages a single swap file backed by *mmap* blocks.

    Lifecycle::

        mgr = SwapFileManager(block_size=DEFAULT_BLOCK_SIZE)
        mgr.create("/mnt/sd/vram_boost.swap", 512 * 1024**2)
        mgr.open()
        mm = mgr.map_block(0)
        mgr.write_block(0, b"hello", 0)
        data = mgr.read_block(0, 0, 5)
        mgr.close()
    """

    def __init__(self, block_size: int = DEFAULT_BLOCK_SIZE):
        # Ensure block_size is a multiple of the OS allocation granularity
        # so that every block offset is valid for mmap().
        if block_size % _GRANULARITY != 0:
            block_size = ((block_size + _GRANULARITY - 1) // _GRANULARITY) * _GRANULARITY
            logger.info("block_size rounded up to %d for alignment", block_size)
        self._block_size = block_size
        self._path: Optional[Path] = None
        self._fd: Optional[int] = None
        self._file_size: int = 0
        self._total_blocks: int = 0
        self._blocks: Dict[int, MappedBlock] = {}
        self._io_errors: int = 0  # I/O 失敗計數，供 circuit breaker 偵測

    # ── Public API ─────────────────────────────────────────────────────

    def create(self, path: str, size_bytes: int) -> None:
        """Pre-allocate a swap file filled with zeros.

        *size_bytes* is rounded **up** to the nearest multiple of
        ``block_size`` so that every block is fully addressable.
        """
        remainder = size_bytes % self._block_size
        if remainder:
            size_bytes += self._block_size - remainder

        self._path = Path(path)
        self._file_size = size_bytes
        self._total_blocks = size_bytes // self._block_size

        logger.info(
            "Creating swap file %s  (%.1f MB, %d blocks)",
            self._path,
            size_bytes / (1024 ** 2),
            self._total_blocks,
        )

        # 既有檔案大小正確 → 直接複用
        if self._path.exists() and self._path.stat().st_size == size_bytes:
            logger.info("Swap file reuse (size matches)")
            return

        # 快速預配置：seek 到尾端寫 1 byte，讓 OS 配置空間
        # 不實際寫滿（8GB 寫零在 USB 要好幾分鐘），mmap 會按需寫入
        with open(self._path, "wb") as f:
            f.seek(size_bytes - 1)
            f.write(b"\x00")
            f.flush()

        logger.info("Swap file created (sparse, %.1f MB)", size_bytes / (1024 ** 2))

    def open(self) -> None:
        """Open the swap file and build the block table."""
        if self._path is None:
            raise RuntimeError("create() must be called before open()")

        self._fd = os.open(str(self._path), os.O_RDWR | getattr(os, "O_BINARY", 0))
        self._file_size = os.fstat(self._fd).st_size
        self._total_blocks = self._file_size // self._block_size

        # Build block metadata (all start as "free")
        self._blocks = {}
        for bid in range(self._total_blocks):
            self._blocks[bid] = MappedBlock(
                block_id=bid,
                offset=bid * self._block_size,
                size=self._block_size,
                state="free",
            )

        logger.info(
            "Swap file opened: %d blocks of %d MB",
            self._total_blocks,
            self._block_size // (1024 ** 2),
        )

    def map_block(self, block_id: int) -> mmap.mmap:
        """Map one block and return the *mmap* object."""
        blk = self._get_block(block_id)
        if blk.state == "mapped" and blk.mmap_obj is not None:
            return blk.mmap_obj

        if self._fd is None:
            raise RuntimeError("File not open")

        mm = mmap.mmap(
            self._fd,
            length=blk.size,
            offset=blk.offset,
        )
        blk.mmap_obj = mm
        blk.state = "mapped"
        logger.debug("Mapped block %d (offset=%d)", block_id, blk.offset)
        return mm

    def unmap_block(self, block_id: int) -> None:
        """Close the *mmap* for one block."""
        blk = self._blocks.get(block_id)
        if blk is None:
            return
        if blk.mmap_obj is not None:
            try:
                blk.mmap_obj.close()
            except Exception:
                pass
            blk.mmap_obj = None
        blk.state = "free"
        logger.debug("Unmapped block %d", block_id)

    def read_block(
        self, block_id: int, offset: int, size: int
    ) -> Optional[bytes]:
        """Safe read -- returns *None* when the device is disconnected or
        the block is not mapped."""
        blk = self._blocks.get(block_id)
        if blk is None or blk.mmap_obj is None or blk.state != "mapped":
            return None
        if not self.is_device_present():
            return None
        try:
            mm = blk.mmap_obj
            mm.seek(offset)
            data = mm.read(size)
            blk.access_count += 1
            blk.last_access = time.time()
            return data
        except (OSError, ValueError):
            self._io_errors += 1
            return None

    def write_block(self, block_id: int, data: bytes, offset: int = 0) -> bool:
        """Safe write -- returns *False* on device disconnect."""
        blk = self._blocks.get(block_id)
        if blk is None or blk.mmap_obj is None or blk.state != "mapped":
            return False
        if not self.is_device_present():
            return False
        try:
            mm = blk.mmap_obj
            mm.seek(offset)
            mm.write(data)
            mm.flush()
            blk.access_count += 1
            blk.last_access = time.time()
            return True
        except (OSError, ValueError):
            self._io_errors += 1
            return False

    @property
    def io_errors(self) -> int:
        return self._io_errors

    def reset_io_errors(self) -> None:
        self._io_errors = 0

    def is_device_present(self) -> bool:
        """Quick check whether the swap file is still reachable."""
        if self._path is None:
            return False
        try:
            os.stat(str(self._path))
            return True
        except OSError:
            return False

    def get_block_stats(self) -> List[MappedBlock]:
        """Return block metadata sorted by *access_count* descending (hottest first)."""
        return sorted(
            self._blocks.values(),
            key=lambda b: b.access_count,
            reverse=True,
        )

    def close(self) -> None:
        """Unmap every block and close the underlying file descriptor."""
        for bid in list(self._blocks):
            self.unmap_block(bid)
        self._blocks.clear()

        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        logger.info("SwapFileManager closed")

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def path(self) -> Optional[Path]:
        return self._path

    @property
    def total_blocks(self) -> int:
        return self._total_blocks

    @property
    def block_size(self) -> int:
        return self._block_size

    @property
    def blocks(self) -> Dict[int, MappedBlock]:
        return self._blocks

    # ── Internal ───────────────────────────────────────────────────────

    def _get_block(self, block_id: int) -> MappedBlock:
        blk = self._blocks.get(block_id)
        if blk is None:
            raise KeyError(f"Block {block_id} does not exist")
        return blk
