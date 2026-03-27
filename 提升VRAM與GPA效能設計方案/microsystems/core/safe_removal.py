"""
Safe Removal Manager
=====================
Four-phase device removal with drain progress and emergency fallback.

State machine:
  IDLE -> PREFLIGHT -> DRAINING -> DETACHING -> READY
                         |
                    FORCE_EJECT -> READY
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional

from .safety_policy import PolicyLimits

logger = logging.getLogger(__name__)

# Timeouts
DRAIN_WARN_SECONDS = 300        # 5 minutes -> suggest force eject
DRAIN_HARD_LIMIT_SECONDS = 600  # 10 minutes -> auto force eject


class RemovalState(str, Enum):
    IDLE = "idle"
    PREFLIGHT = "preflight"
    DRAINING = "draining"
    DETACHING = "detaching"
    READY = "ready"
    FORCE_EJECT = "force_eject"


@dataclass
class PreflightResult:
    can_remove_immediately: bool
    current_usage_mb: float
    system_ram_available_mb: float
    can_absorb: bool
    estimated_drain_seconds: float
    warnings: list


@dataclass
class DrainProgress:
    remaining_mb: float
    total_mb: float
    drain_rate_mbs: float
    eta_seconds: float
    phase: str  # "draining" | "detaching" | "ready"


def _get_available_ram_mb() -> float:
    """Return available physical RAM in MB."""
    try:
        if platform.system().lower() == "windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return mem.ullAvailPhys / (1024 ** 2)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 8000.0  # safe fallback


@dataclass
class _DeviceDrainState:
    """Internal per-device drain tracking."""
    state: RemovalState = RemovalState.IDLE
    mount_letter: str = ""
    drive_letter: str = ""
    total_mb: float = 0
    drain_start_time: float = 0
    drain_thread: Optional[threading.Thread] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class SafeRemovalManager:
    """
    Manages the four-phase safe removal flow for VHD pagefile devices.

    Usage:
        mgr = SafeRemovalManager()
        mgr.set_vhd_engine(vhd_engine)
        result = mgr.preflight_check("G", policy)
        if not result.can_remove_immediately:
            mgr.start_drain("G", on_progress=callback)
        # ... or mgr.force_eject("G")
    """

    def __init__(self):
        self._states: Dict[str, _DeviceDrainState] = {}
        self._vhd_engine = None
        self._lock = threading.Lock()

    def set_vhd_engine(self, engine) -> None:
        """Inject the VhdPagefileEngine for pagefile operations."""
        self._vhd_engine = engine

    def _get_state(self, device_id: str) -> _DeviceDrainState:
        with self._lock:
            if device_id not in self._states:
                self._states[device_id] = _DeviceDrainState()
            return self._states[device_id]

    def state(self, device_id: str) -> RemovalState:
        """Return current removal state for a device."""
        return self._get_state(device_id).state

    def preflight_check(self, device_id: str,
                        policy: PolicyLimits) -> PreflightResult:
        """
        Phase 0: Check if device can be removed and estimate drain time.

        device_id is the VHD mount letter (e.g. "G").
        """
        ds = self._get_state(device_id)
        ds.state = RemovalState.PREFLIGHT
        ds.mount_letter = device_id

        # Get pagefile usage
        usage = {"current_usage_mb": 0, "allocated_mb": 0}
        if self._vhd_engine:
            usage = self._vhd_engine.get_pagefile_usage(device_id)

        current_mb = usage.get("current_usage_mb", 0)
        ram_avail = _get_available_ram_mb()

        warnings = []
        if ram_avail < policy.system_ram_reserve_gb * 1024:
            warnings.append(
                f"Low RAM: {ram_avail:.0f} MB available, "
                f"{policy.system_ram_reserve_gb * 1024:.0f} MB recommended reserve"
            )

        can_absorb = ram_avail > current_mb
        if not can_absorb and current_mb > 0:
            warnings.append(
                f"RAM ({ram_avail:.0f} MB) may not absorb all pages "
                f"({current_mb:.0f} MB) — drain may be slow"
            )

        # Rough ETA: assume ~50 MB/s drain rate as baseline
        drain_rate = 50.0
        eta = current_mb / drain_rate if current_mb > 0 else 0

        return PreflightResult(
            can_remove_immediately=(current_mb == 0),
            current_usage_mb=current_mb,
            system_ram_available_mb=ram_avail,
            can_absorb=can_absorb,
            estimated_drain_seconds=eta,
            warnings=warnings,
        )

    def start_drain(self, device_id: str,
                    on_progress: Optional[Callable[[DrainProgress], None]] = None,
                    on_complete: Optional[Callable[[], None]] = None,
                    on_timeout_warn: Optional[Callable[[], None]] = None) -> None:
        """
        Phase 1: Remove pagefile from Windows list and monitor drain.

        Runs in a background thread. Calls on_progress periodically.
        Auto-transitions to Phase 2 (detach) when drain completes.
        """
        ds = self._get_state(device_id)
        ds.state = RemovalState.DRAINING
        ds.cancel_event.clear()
        ds.drain_start_time = time.time()

        # Get initial usage for total
        usage = {"current_usage_mb": 0}
        if self._vhd_engine:
            usage = self._vhd_engine.get_pagefile_usage(device_id)
        ds.total_mb = usage.get("current_usage_mb", 0)

        def drain_loop():
            # Step 1: Remove pagefile from Windows list
            if self._vhd_engine and hasattr(self._vhd_engine, '_remove_pagefile_on_volume'):
                self._vhd_engine._remove_pagefile_on_volume(device_id)

            warned = False
            prev_mb = ds.total_mb
            prev_time = time.time()

            # Step 2: Monitor drain
            while not ds.cancel_event.is_set():
                usage = {"current_usage_mb": 0}
                if self._vhd_engine:
                    usage = self._vhd_engine.get_pagefile_usage(device_id)

                remaining = usage.get("current_usage_mb", 0)
                now = time.time()
                elapsed = now - ds.drain_start_time
                dt = now - prev_time

                # Calculate drain rate
                rate = (prev_mb - remaining) / dt if dt > 0 else 0
                rate = max(rate, 0)
                eta = remaining / rate if rate > 0 else 999

                prev_mb = remaining
                prev_time = now

                progress = DrainProgress(
                    remaining_mb=remaining,
                    total_mb=ds.total_mb,
                    drain_rate_mbs=rate,
                    eta_seconds=eta,
                    phase="draining",
                )
                if on_progress:
                    on_progress(progress)

                # Drain complete
                if remaining == 0:
                    ds.state = RemovalState.DETACHING
                    if on_progress:
                        on_progress(DrainProgress(
                            remaining_mb=0, total_mb=ds.total_mb,
                            drain_rate_mbs=0, eta_seconds=0,
                            phase="detaching",
                        ))
                    self._do_detach(device_id)
                    ds.state = RemovalState.READY
                    if on_progress:
                        on_progress(DrainProgress(
                            remaining_mb=0, total_mb=ds.total_mb,
                            drain_rate_mbs=0, eta_seconds=0,
                            phase="ready",
                        ))
                    if on_complete:
                        on_complete()
                    return

                # Timeout warning at 5 minutes
                if elapsed > DRAIN_WARN_SECONDS and not warned:
                    warned = True
                    if on_timeout_warn:
                        on_timeout_warn()

                # Hard limit at 10 minutes
                if elapsed > DRAIN_HARD_LIMIT_SECONDS:
                    logger.warning("Drain hard limit reached for %s, forcing eject",
                                   device_id)
                    self._do_force_eject(device_id)
                    ds.state = RemovalState.READY
                    if on_complete:
                        on_complete()
                    return

                ds.cancel_event.wait(timeout=1.0)

            # Cancelled
            if ds.cancel_event.is_set() and ds.state == RemovalState.DRAINING:
                logger.info("Drain cancelled for %s, restoring pagefile", device_id)
                ds.state = RemovalState.IDLE

        ds.drain_thread = threading.Thread(target=drain_loop, daemon=True)
        ds.drain_thread.start()

    def cancel_drain(self, device_id: str) -> None:
        """Cancel an in-progress drain and restore the pagefile."""
        ds = self._get_state(device_id)
        if ds.state == RemovalState.DRAINING:
            ds.cancel_event.set()

    def force_eject(self, device_id: str) -> None:
        """Emergency bypass: flush + force detach."""
        ds = self._get_state(device_id)

        # Cancel any running drain
        if ds.state == RemovalState.DRAINING:
            ds.cancel_event.set()
            if ds.drain_thread:
                ds.drain_thread.join(timeout=2)

        ds.state = RemovalState.FORCE_EJECT
        self._do_force_eject(device_id)
        ds.state = RemovalState.READY

    def _do_detach(self, device_id: str) -> None:
        """Phase 2: Detach VHD and clean up."""
        if not self._vhd_engine:
            return

        lock = getattr(self._vhd_engine, '_lock', None)
        devices = getattr(self._vhd_engine, '_devices', [])

        if lock:
            lock.acquire()
        try:
            for dev in list(devices):
                if dev.mount_letter == device_id:
                    try:
                        dev.bridge.detach()
                        dev.bridge.close()
                    except Exception as e:
                        logger.warning("Detach error for %s: %s", device_id, e)

                    try:
                        devices.remove(dev)
                        if hasattr(self._vhd_engine, '_release_letter'):
                            self._vhd_engine._release_letter(dev.mount_letter)
                    except Exception:
                        pass
                    break
        finally:
            if lock:
                lock.release()

    def _do_force_eject(self, device_id: str) -> None:
        """Force eject: remove pagefile + force detach with brief flush."""
        if not self._vhd_engine:
            return

        # Best-effort pagefile removal
        try:
            if hasattr(self._vhd_engine, '_remove_pagefile_on_volume'):
                self._vhd_engine._remove_pagefile_on_volume(device_id)
        except Exception as e:
            logger.warning("Force pagefile removal failed for %s: %s", device_id, e)

        # Brief wait for flush
        time.sleep(2)

        # Force detach
        self._do_detach(device_id)
