"""
Host-resident daemon — auto-detects device hotplug and expands memory.

Windows: runs as background process (or Windows Service via install.py)
Linux:   runs as systemd service

Flow:
  1. Scan for existing external devices
  2. Watch for new device insertion
  3. On new device: speed test → evaluate → expand
  4. On device removal: safe teardown
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import sys
import time
import threading
from pathlib import Path
from typing import Dict, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from vram_boost.core.detect import detect_devices, ExternalDevice
from vram_boost.core.measure import measure_write_speed
from vram_boost.core.policy import evaluate
from vram_boost.core.extend import execute, is_admin, remove
from vram_boost.core.monitor import Monitor

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5.0  # seconds between device scans
STATE_FILE = os.path.expanduser("~/.vram-boost/state.json")


class Daemon:
    """Background daemon that auto-manages memory expansion."""

    def __init__(self):
        self._active = False
        self._managed: Dict[str, Monitor] = {}  # path → Monitor
        self._known_paths: Set[str] = set()
        self._lock = threading.Lock()

    def run(self) -> None:
        """Main daemon loop. Blocks until stopped."""
        self._active = True
        logger.info("vram-boost daemon started (poll=%ds)", POLL_INTERVAL)

        # Initial scan
        self._scan_and_expand()

        # Poll loop
        while self._active:
            time.sleep(POLL_INTERVAL)
            self._scan_and_expand()

        # Cleanup
        self._cleanup_all()
        logger.info("Daemon stopped")

    def stop(self) -> None:
        self._active = False

    def status(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "managed_devices": len(self._managed),
                "devices": [
                    {"path": path, "active": mon.get_status().active}
                    for path, mon in self._managed.items()
                ],
            }

    def _scan_and_expand(self) -> None:
        """Detect new devices and expand if policy allows."""
        devices = detect_devices()
        current_paths = {d.path for d in devices}

        # New devices
        for dev in devices:
            if dev.path not in self._known_paths:
                logger.info("New device: %s (%s, %.1f GB free)",
                            dev.name, dev.bus, dev.free_gb)
                self._try_expand(dev)

        # Removed devices
        removed = self._known_paths - current_paths
        for path in removed:
            logger.info("Device removed: %s", path)
            self._teardown(path)

        self._known_paths = current_paths

    def _try_expand(self, device: ExternalDevice) -> None:
        """Speed test, evaluate, and expand if appropriate."""
        speed = measure_write_speed(device.path)
        plan = evaluate(device, speed)

        if plan.action != "expand":
            logger.info("Skipping %s: %s", device.name, plan.reason)
            return

        logger.info("Expanding %s: %.1f GB at %.1f MB/s",
                     device.name, plan.swap_gb, speed.write_mbs)

        result = execute(plan)
        if result.success:
            logger.info("Expanded: %s → %s (%.1f GB)",
                        device.name, result.path, result.size_gb)

            # Start monitor
            mon = Monitor(
                device_path=device.path,
                swap_path=result.path,
                on_disconnect=lambda p=device.path: self._teardown(p),
            )
            mon.start()

            with self._lock:
                self._managed[device.path] = mon

            self._save_state()
        else:
            logger.error("Failed to expand %s: %s", device.name, result.error)

    def _teardown(self, path: str) -> None:
        """Stop monitoring and remove swap for a device."""
        with self._lock:
            mon = self._managed.pop(path, None)
        if mon:
            mon.stop()
        self._known_paths.discard(path)
        self._save_state()

    def _cleanup_all(self) -> None:
        with self._lock:
            for mon in self._managed.values():
                mon.stop()
            self._managed.clear()

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        state = {
            "managed": list(self._managed.keys()),
            "timestamp": time.time(),
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not is_admin():
        print("vram-boost daemon requires administrator/root privileges.")
        if platform.system() == "Windows":
            print("Run: Start-Process python -Verb RunAs -ArgumentList 'vram_boost/service/daemon.py'")
        else:
            print("Run: sudo python3 vram_boost/service/daemon.py")
        sys.exit(1)

    daemon = Daemon()

    # Handle Ctrl+C
    def sighandler(sig, frame):
        print("\nStopping daemon...")
        daemon.stop()

    signal.signal(signal.SIGINT, sighandler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, sighandler)

    print("vram-boost daemon running. Press Ctrl+C to stop.")
    daemon.run()


if __name__ == "__main__":
    main()
