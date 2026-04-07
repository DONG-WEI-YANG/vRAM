"""
Build portable executables for SD card deployment.

Creates:
  dist/vram-boost.exe     (Windows)
  dist/vram-boost          (Linux)

These can be copied directly to an SD card.
"""

import os
import platform
import subprocess
import sys


def build():
    system = platform.system()
    entry = os.path.join(os.path.dirname(__file__), "portable", "run.py")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "vram-boost",
        "--add-data", f"core{os.pathsep}vram_boost/core",
        "--hidden-import", "vram_boost.core.detect",
        "--hidden-import", "vram_boost.core.measure",
        "--hidden-import", "vram_boost.core.policy",
        "--hidden-import", "vram_boost.core.extend",
        "--hidden-import", "vram_boost.core.monitor",
        entry,
    ]

    if system == "Windows":
        cmd.extend(["--uac-admin"])  # Request admin on launch

    print(f"Building for {system}...")
    subprocess.run(cmd, check=True)
    print(f"\nDone! Copy dist/vram-boost{'exe' if system == 'Windows' else ''} to SD card.")


if __name__ == "__main__":
    build()
