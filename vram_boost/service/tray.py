"""
System tray icon for vram-boost.
Shows status, allows pause/resume, safe removal.

Uses pystray (cross-platform: Windows + Linux + macOS).
Falls back to console-only if pystray not available.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from vram_boost.core.extend import is_admin
from vram_boost.service.daemon import Daemon

logger = logging.getLogger(__name__)


def _create_icon_image():
    """Create a simple colored icon (no external file needed)."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Green circle with "V" for VRAM
        draw.ellipse([4, 4, 60, 60], fill=(46, 204, 113))
        draw.text((18, 14), "V", fill="white")
        return img
    except ImportError:
        # Fallback: 1x1 pixel
        try:
            from PIL import Image
            return Image.new("RGB", (16, 16), (46, 204, 113))
        except ImportError:
            return None


def run_with_tray():
    """Run daemon with system tray icon."""
    try:
        import pystray
        from pystray import MenuItem as Item
    except ImportError:
        print("pystray not installed. Running console-only mode.")
        print("Install with: pip install pystray pillow")
        _run_console_only()
        return

    daemon = Daemon()
    daemon_thread = threading.Thread(target=daemon.run, daemon=True)

    def on_status(icon, item):
        s = daemon.status()
        count = s["managed_devices"]
        devices = ", ".join(d["path"] for d in s["devices"]) or "none"
        icon.notify(
            f"Devices: {count}\n{devices}",
            "vram-boost Status",
        )

    def on_quit(icon, item):
        daemon.stop()
        icon.stop()

    icon_image = _create_icon_image()
    if icon_image is None:
        print("PIL not available. Running console-only mode.")
        _run_console_only()
        return

    menu = pystray.Menu(
        Item("Status", on_status),
        Item("Quit", on_quit),
    )

    icon = pystray.Icon("vram-boost", icon_image, "vram-boost", menu)

    daemon_thread.start()

    # Update tooltip periodically
    def update_tooltip():
        while daemon._active:
            s = daemon.status()
            count = s["managed_devices"]
            icon.title = f"vram-boost: {count} device(s)"
            time.sleep(5)

    tooltip_thread = threading.Thread(target=update_tooltip, daemon=True)
    tooltip_thread.start()

    icon.run()


def _run_console_only():
    """Fallback: run daemon in console without tray icon."""
    import signal

    daemon = Daemon()

    def sighandler(sig, frame):
        print("\nStopping...")
        daemon.stop()

    signal.signal(signal.SIGINT, sighandler)

    print("vram-boost daemon running (console mode). Ctrl+C to stop.")
    daemon.run()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not is_admin():
        print("vram-boost requires administrator/root privileges.")
        if platform.system() == "Windows":
            import ctypes
            print("Requesting elevation...")
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable,
                f'"{os.path.abspath(__file__)}"', None, 1,
            )
        else:
            print(f"Run: sudo {sys.executable} {__file__}")
        return

    run_with_tray()


if __name__ == "__main__":
    main()
