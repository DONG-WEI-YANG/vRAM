"""
Cross-platform hotplug event listener.
Windows: WMI Win32_VolumeChangeEvent via PowerShell subprocess
Linux:   pyudev or polling /proc/mounts
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class HotplugEvent(Enum):
    ARRIVED = "arrived"
    REMOVED = "removed"


@dataclass
class DeviceChange:
    event: HotplugEvent
    path: str           # "E:\\" or "/media/sdcard"
    name: str = ""


class HotplugWatcher:
    """
    Watches for storage device insertion/removal events.
    Calls on_change(DeviceChange) when a device is inserted or removed.
    """

    def __init__(self, on_change: Callable[[DeviceChange], None]):
        self._on_change = on_change
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._system = platform.system()

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="hotplug-watcher",
        )
        self._thread.start()
        logger.info("Hotplug watcher started (%s)", self._system)

    def stop(self) -> None:
        self._active = False
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        if self._system == "Windows":
            self._run_windows()
        elif self._system == "Linux":
            self._run_linux()
        else:
            self._run_polling()

    # ── Windows: WMI event subscription ──

    def _run_windows(self) -> None:
        """
        Use PowerShell WMI event subscription to detect volume changes.
        Falls back to polling if WMI fails.
        """
        ps_script = (
            "$q = 'SELECT * FROM Win32_VolumeChangeEvent WHERE EventType=2 OR EventType=3';"
            "$w = Register-WmiEvent -Query $q -SourceIdentifier VolumeChange;"
            "while ($true) {"
            "  $e = Wait-Event -SourceIdentifier VolumeChange -Timeout 5;"
            "  if ($e) {"
            "    $t = $e.SourceEventArgs.NewEvent.EventType;"
            "    $d = $e.SourceEventArgs.NewEvent.DriveName;"
            "    Remove-Event -SourceIdentifier VolumeChange;"
            "    Write-Output ('{0}|{1}' -f $t, $d);"
            "  }"
            "}"
        )

        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except Exception as e:
            logger.warning("WMI watcher failed, falling back to polling: %s", e)
            self._run_polling()
            return

        try:
            while self._active and proc.poll() is None:
                line = proc.stdout.readline().strip()
                if not line:
                    continue
                parts = line.split("|", 1)
                if len(parts) == 2:
                    event_type, drive = parts
                    # EventType: 2 = device arrival, 3 = device removal
                    if event_type == "2":
                        path = drive if drive.endswith("\\") else drive + "\\"
                        logger.info("WMI: device arrived %s", path)
                        self._on_change(DeviceChange(
                            event=HotplugEvent.ARRIVED, path=path,
                        ))
                    elif event_type == "3":
                        path = drive if drive.endswith("\\") else drive + "\\"
                        logger.info("WMI: device removed %s", path)
                        self._on_change(DeviceChange(
                            event=HotplugEvent.REMOVED, path=path,
                        ))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ── Linux: monitor /proc/mounts ──

    def _run_linux(self) -> None:
        """Watch /proc/mounts for changes."""
        prev_mounts = self._read_linux_mounts()

        while self._active:
            time.sleep(2)
            curr_mounts = self._read_linux_mounts()

            # New mounts
            for path in curr_mounts - prev_mounts:
                if self._is_external_mount(path):
                    logger.info("Mount arrived: %s", path)
                    self._on_change(DeviceChange(
                        event=HotplugEvent.ARRIVED, path=path,
                    ))

            # Removed mounts
            for path in prev_mounts - curr_mounts:
                if self._is_external_mount(path):
                    logger.info("Mount removed: %s", path)
                    self._on_change(DeviceChange(
                        event=HotplugEvent.REMOVED, path=path,
                    ))

            prev_mounts = curr_mounts

    @staticmethod
    def _read_linux_mounts() -> set:
        mounts = set()
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mounts.add(parts[1])
        except OSError:
            pass
        return mounts

    @staticmethod
    def _is_external_mount(path: str) -> bool:
        return any(path.startswith(p) for p in ("/media/", "/mnt/", "/run/media/"))

    # ── Fallback: polling ──

    def _run_polling(self) -> None:
        """Poll drive letters every 3 seconds."""
        prev = self._get_drives()
        while self._active:
            time.sleep(3)
            curr = self._get_drives()
            for d in curr - prev:
                self._on_change(DeviceChange(event=HotplugEvent.ARRIVED, path=d))
            for d in prev - curr:
                self._on_change(DeviceChange(event=HotplugEvent.REMOVED, path=d))
            prev = curr

    @staticmethod
    def _get_drives() -> set:
        if platform.system() == "Windows":
            import string
            drives = set()
            for letter in string.ascii_uppercase:
                path = f"{letter}:\\"
                if os.path.isdir(path):
                    drives.add(path)
            return drives
        else:
            return HotplugWatcher._read_linux_mounts()
