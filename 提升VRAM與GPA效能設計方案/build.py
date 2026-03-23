#!/usr/bin/env python3
"""
VRAM Booster — 打包腳本
=========================
用 PyInstaller 打包為獨立 exe，放進 release/ 目錄。

Usage:
  pip install pyinstaller
  python build.py
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
MICROSYSTEMS = PROJECT_ROOT / "microsystems"
RELEASE_WIN = PROJECT_ROOT / "release" / "windows"
RELEASE_LIN = PROJECT_ROOT / "release" / "linux"


def build():
    os_type = platform.system()
    print(f"[BUILD] OS: {os_type}")
    print(f"[BUILD] Root: {PROJECT_ROOT}")

    # 確認 PyInstaller
    try:
        import PyInstaller
        print(f"[BUILD] PyInstaller: {PyInstaller.__version__}")
    except ImportError:
        print("[BUILD] Installing PyInstaller...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    # PyInstaller 無法處理中文路徑，在 temp 目錄下操作
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="vram_build_"))
    print(f"[BUILD] Temp dir: {tmp_dir}")

    # 複製 microsystems 到 temp
    tmp_ms = tmp_dir / "microsystems"
    shutil.copytree(MICROSYSTEMS, tmp_ms, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 建立入口點
    entry = tmp_dir / "_entry.py"
    entry.write_text(
        "import sys, os\n"
        "if getattr(sys, 'frozen', False):\n"
        "    os.chdir(os.path.dirname(sys.executable))\n"
        "from microsystems.hotplug_launcher import main\n"
        "main()\n",
        encoding="utf-8",
    )

    name = "VRAM_Booster"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name,
        "--clean",
        "--noconfirm",
        "--add-data", f"{tmp_ms}{os.pathsep}microsystems",
        str(entry),
    ]

    if os_type == "Windows":
        cmd.append("--windowed")

    print(f"[BUILD] Command: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(tmp_dir), check=True)

    # 複製到 release/
    dist = tmp_dir / "dist"
    if os_type == "Windows":
        src = dist / f"{name}.exe"
        dst = RELEASE_WIN / f"{name}.exe"
    else:
        src = dist / name
        dst = RELEASE_LIN / name

    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        size_mb = dst.stat().st_size / (1024 * 1024)
        print(f"[BUILD] OK: Copied to: {dst} ({size_mb:.1f} MB)")
    else:
        print(f"[BUILD] FAIL: Not found: {src}")

    # 清理 temp
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print("[BUILD] OK: Done!")


if __name__ == "__main__":
    build()
