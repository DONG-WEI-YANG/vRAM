"""
Cross-platform health monitor for memory expansion.
Watches device health, swap usage, and handles safe removal.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    active: bool = False
    device_path: str = ""
    swap_path: str = ""
    swap_size_gb: float = 0.0
    swap_used_pct: float = 0.0
    device_present: bool = True
    speed_mbs: float = 0.0
    uptime_s: float = 0.0
    error: str = ""


class Monitor:
    """Background health monitor with callbacks."""

    def __init__(
        self,
        device_path: str,
        swap_path: str,
        check_interval_s: float = 5.0,
        on_status: Optional[Callable[[HealthStatus], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        self._device_path = device_path
        self._swap_path = swap_path
        self._interval = check_interval_s
        self._on_status = on_status
        self._on_disconnect = on_disconnect
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._start_time = 0.0

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._start_time = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vram-monitor")
        self._thread.start()
        logger.info("Monitor started: %s", self._device_path)

    def stop(self) -> None:
        self._active = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def get_status(self) -> HealthStatus:
        return self._check()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            status = self._check()
            if self._on_status:
                self._on_status(status)
            if not status.device_present and self._on_disconnect:
                logger.warning("Device disconnected: %s", self._device_path)
                self._on_disconnect()
                break

    def _check(self) -> HealthStatus:
        status = HealthStatus(
            active=self._active,
            device_path=self._device_path,
            swap_path=self._swap_path,
            uptime_s=time.time() - self._start_time if self._start_time else 0,
        )

        # Check device present
        status.device_present = os.path.isdir(self._device_path)
        if not status.device_present:
            status.error = "Device not found"
            return status

        # Check swap usage
        system = platform.system()
        if system == "Windows":
            status.swap_used_pct = self._check_windows_swap()
        else:
            status.swap_used_pct = self._check_linux_swap()

        return status

    def _check_windows_swap(self) -> float:
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_PageFileUsage | "
                 "Select-Object Name, AllocatedBaseSize, CurrentUsage | ConvertTo-Json"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                import json
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                for pf in data:
                    if pf.get("AllocatedBaseSize", 0) > 0:
                        return pf["CurrentUsage"] / pf["AllocatedBaseSize"] * 100
        except Exception:
            pass
        return 0.0

    def _check_linux_swap(self) -> float:
        try:
            r = subprocess.run(
                ["swapon", "--show=NAME,SIZE,USED", "--bytes", "--noheadings"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (r.stdout or "").strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3 and self._swap_path in parts[0]:
                    total = int(parts[1])
                    used = int(parts[2])
                    return used / total * 100 if total > 0 else 0.0
        except Exception:
            pass
        return 0.0
