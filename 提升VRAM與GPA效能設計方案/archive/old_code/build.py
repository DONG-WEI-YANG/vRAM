#!/usr/bin/env python3
"""
SD-VRAM Booster — 打包腳本
製作者：Peter Yang

使用 PyInstaller 將專案打包為獨立執行檔：
- Windows: SDVRAMBooster.exe (放入 sd_card_root/windows/)
- Linux:   SDVRAMBooster     (放入 sd_card_root/linux/)

使用方式:
  pip install pyinstaller
  python scripts/build.py
"""

import platform
import subprocess
import shutil
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY_POINT = os.path.join(PROJECT_ROOT, "sdvram", "main.py")


def build():
    os_type = platform.system()
    print(f"[BUILD] 作業系統: {os_type}")
    print(f"[BUILD] 專案根目錄: {PROJECT_ROOT}")

    # 確認 PyInstaller 已安裝
    try:
        import PyInstaller
        print(f"[BUILD] PyInstaller 版本: {PyInstaller.__version__}")
    except ImportError:
        print("[BUILD] 安裝 PyInstaller...")
        subprocess.run(["pip", "install", "pyinstaller"], check=True)

    # 打包命令
    cmd = [
        "pyinstaller",
        "--onefile",
        "--name", "SDVRAMBooster",
        "--clean",
        "--noconfirm",
        "--add-data", f"{os.path.join(PROJECT_ROOT, 'sdvram')}{os.pathsep}sdvram",
        ENTRY_POINT,
    ]

    # Windows 特定選項
    if os_type == "Windows":
        icon_path = os.path.join(PROJECT_ROOT, "sd_card_root", "windows", "icon.ico")
        if os.path.exists(icon_path):
            cmd.extend(["--icon", icon_path])
        cmd.append("--windowed")  # 不顯示命令列視窗

    print(f"[BUILD] 執行: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)

    # 複製到 SD 卡目錄
    dist_dir = os.path.join(PROJECT_ROOT, "dist")
    if os_type == "Windows":
        src = os.path.join(dist_dir, "SDVRAMBooster.exe")
        dst = os.path.join(PROJECT_ROOT, "sd_card_root", "windows", "SDVRAMBooster.exe")
    else:
        src = os.path.join(dist_dir, "SDVRAMBooster")
        dst = os.path.join(PROJECT_ROOT, "sd_card_root", "linux", "SDVRAMBooster")

    if os.path.exists(src):
        shutil.copy2(src, dst)
        print(f"[BUILD] ✓ 已複製至: {dst}")
        file_size = os.path.getsize(dst) / (1024 * 1024)
        print(f"[BUILD] ✓ 檔案大小: {file_size:.1f} MB")
    else:
        print(f"[BUILD] ✗ 找不到打包結果: {src}")

    # 清理
    for d in ["build", "dist", "SDVRAMBooster.spec"]:
        p = os.path.join(PROJECT_ROOT, d)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.isfile(p):
            os.remove(p)

    print("[BUILD] ✓ 打包完成！")


if __name__ == "__main__":
    build()
