"""
MmapSwapEngine -- Orchestrator for mmap-backed swap
====================================================
Wires :class:`SwapFileManager` together with the project's
:class:`CircuitBreaker` and a background device-monitoring thread.

Provides ``activate`` / ``deactivate`` / ``status`` as the main user
interface.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Dict, Any, Optional

from .circuit_breaker import CircuitBreaker, BreakerConfig, BreakerState
from .mmap_swap import SwapFileManager, DEFAULT_BLOCK_SIZE, SWAP_FILENAME

logger = logging.getLogger(__name__)

_MONITOR_INTERVAL: float = 5.0  # seconds between device-presence checks


class MmapSwapEngine:
    """
    High-level engine that manages a mmap-backed swap file on an
    external storage device.

    Usage::

        engine = MmapSwapEngine()
        result = engine.activate("E:\\\\", 2 * 1024**3)  # 2 GB on E:
        print(engine.status())
        engine.deactivate()
    """

    def __init__(self) -> None:
        self._swap: Optional[SwapFileManager] = None
        self._active: bool = False
        self._degraded: bool = False
        self._swap_path: Optional[str] = None
        self._block_size: int = DEFAULT_BLOCK_SIZE

        # Callbacks
        self._on_progress: Optional[Callable[[str], None]] = None
        self._on_state_change: Optional[Callable[[str], None]] = None

        # Circuit breaker for device health
        self._breaker = CircuitBreaker(
            name="device_swap",
            config=BreakerConfig(
                failure_threshold=2,
                cooldown_seconds=5.0,
                success_threshold=1,
            ),
        )

        # Monitor thread
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Public API ─────────────────────────────────────────────────────

    def activate(
        self,
        device_path: str,
        size_bytes: int,
        block_size: int = DEFAULT_BLOCK_SIZE,
        on_progress: Optional[Callable[[str], None]] = None,
        on_state_change: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Create + open swap, start monitoring.

        Returns a result dict compatible with the rest of the boost pipeline.
        """
        self._on_progress = on_progress
        self._on_state_change = on_state_change
        self._block_size = block_size

        swap_path = os.path.join(device_path, SWAP_FILENAME)
        self._swap_path = swap_path

        try:
            self._swap = SwapFileManager(block_size=block_size)
            self._swap.create(swap_path, size_bytes)

            size_mb = size_bytes // (1024 * 1024)
            if on_progress:
                on_progress(f"creating swap file ({size_mb // 1024}GB)...")

            self._swap.open()

            # Map all blocks upfront
            for bid in range(self._swap.total_blocks):
                self._swap.map_block(bid)

            if on_progress:
                on_progress(f"mapped {self._swap.total_blocks} blocks")

            self._active = True
            self._degraded = False

            # Start background monitor
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="mmap-swap-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

            total_blocks = self._swap.total_blocks
            added_gb = (total_blocks * block_size) / (1024 ** 3)

            logger.info(
                "MmapSwapEngine activated: %d blocks, %.2f GB on %s",
                total_blocks,
                added_gb,
                device_path,
            )

            return {
                "success": True,
                "method": "mmap_swap",
                "swap_path": swap_path,
                "added_gb": added_gb,
                "total_blocks": total_blocks,
                "needs_reboot": False,
            }

        except Exception as exc:
            logger.error("Failed to activate mmap swap: %s", exc)
            return {
                "success": False,
                "method": "mmap_swap",
                "error": str(exc),
                "needs_reboot": False,
            }

    def deactivate(self) -> Dict[str, Any]:
        """Stop monitoring and release all resources."""
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=10)
            self._monitor_thread = None

        if self._swap is not None:
            self._swap.close()
            self._swap = None

        self._active = False
        self._degraded = False

        logger.info("MmapSwapEngine deactivated")
        return {"success": True}

    def status(self) -> Dict[str, Any]:
        """Return a snapshot of the current engine state."""
        if not self._active or self._swap is None:
            return {
                "active": False,
                "swap_path": self._swap_path,
                "total_blocks": 0,
                "mapped_blocks": 0,
                "evicted_blocks": 0,
                "device_state": "unknown",
                "degraded": self._degraded,
            }

        blocks = self._swap.get_block_stats()
        mapped = sum(1 for b in blocks if b.state == "mapped")
        evicted = sum(1 for b in blocks if b.state == "evicted")

        device_ok = self._swap.is_device_present()

        return {
            "active": True,
            "swap_path": self._swap_path,
            "total_blocks": self._swap.total_blocks,
            "mapped_blocks": mapped,
            "evicted_blocks": evicted,
            "device_state": "connected" if device_ok else "disconnected",
            "degraded": self._degraded,
        }

    # ── Internal ───────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Background thread that polls device presence."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=_MONITOR_INTERVAL)
            if self._stop_event.is_set():
                break

            if self._swap is None:
                continue

            present = self._swap.is_device_present()

            if present:
                self._breaker.record_success()
                if self._degraded and self._breaker.state == BreakerState.CLOSED:
                    self._on_reconnect()
            else:
                self._breaker.record_failure("device not present")
                if not self._degraded and self._breaker.state == BreakerState.OPEN:
                    self._on_disconnect()

    def _on_disconnect(self) -> None:
        """Called when the breaker trips due to device loss."""
        logger.warning("Device disconnected -- entering degraded mode")
        if self._swap is not None:
            for bid in list(self._swap.blocks):
                blk = self._swap.blocks[bid]
                if blk.mmap_obj is not None:
                    try:
                        blk.mmap_obj.close()
                    except Exception:
                        pass
                    blk.mmap_obj = None
                blk.state = "evicted"

        self._degraded = True
        if self._on_state_change:
            self._on_state_change("degraded")

    def _on_reconnect(self) -> None:
        """Called when the breaker recovers after device reconnection."""
        logger.info("Device reconnected -- restoring blocks")
        if self._swap is not None:
            try:
                self._swap.close()
                self._swap.open()
                for bid in range(self._swap.total_blocks):
                    self._swap.map_block(bid)
            except Exception as exc:
                logger.error("Failed to restore blocks: %s", exc)
                return

        self._degraded = False
        if self._on_state_change:
            self._on_state_change("restored")
