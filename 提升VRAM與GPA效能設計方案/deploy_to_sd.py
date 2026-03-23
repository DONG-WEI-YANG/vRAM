"""
Deploy to SD Card — 開發者用部署工具
=====================================
偵測可移除式磁碟，將 VRAM Booster 部署到 SD 卡上。

Usage:
  python deploy_to_sd.py          # 自動偵測 SD 卡並部署
  python deploy_to_sd.py E        # 指定磁碟代號
"""

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
IS_WINDOWS = platform.system().lower() == "windows"
RELEASE_DIR = SCRIPT_DIR / "release" / ("windows" if IS_WINDOWS else "linux")
DEPLOY_FILES = (
    ["VRAM_Booster.exe", "autorun.inf"] if IS_WINDOWS
    else ["VRAM_Booster", "autorun.sh", "install-udev.sh", "README"]
)


def find_removable_drives() -> list[dict]:
    """偵測所有可移除式磁碟"""
    if IS_WINDOWS:
        return _find_removable_windows()
    return _find_removable_linux()


def _find_removable_windows() -> list[dict]:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Volume | Where-Object {$_.DriveType -eq 'Removable' -and $_.DriveLetter -and $_.Size -gt 0} | "
             "Select-Object DriveLetter,FileSystemLabel,Size,SizeRemaining | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            if isinstance(data, dict):
                data = [data]
            return [
                {
                    "letter": v["DriveLetter"],
                    "label": v.get("FileSystemLabel", ""),
                    "size_gb": v.get("Size", 0) / (1024 ** 3),
                    "free_gb": v.get("SizeRemaining", 0) / (1024 ** 3),
                }
                for v in data if v.get("DriveLetter")
            ]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        print(f"Detection failed: {e}")
    return []


def _find_removable_linux() -> list[dict]:
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,RM,SIZE,LABEL,FSTYPE"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return []

        drives = []
        data = json.loads(r.stdout)
        for dev in data.get("blockdevices", []):
            if not dev.get("rm"):
                continue
            for part in dev.get("children", [dev]):
                mp = part.get("mountpoint")
                if not mp or mp in ("/", "/boot", "/home"):
                    continue
                try:
                    usage = shutil.disk_usage(mp)
                    drives.append({
                        "letter": mp,
                        "label": part.get("label", "") or "",
                        "size_gb": usage.total / (1024 ** 3),
                        "free_gb": usage.free / (1024 ** 3),
                    })
                except OSError:
                    pass
        return drives
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        print(f"Detection failed: {e}")
    return []


def deploy(letter: str) -> bool:
    """部署 VRAM Booster 到指定磁碟"""
    if IS_WINDOWS:
        dest = Path(f"{letter}:\\")
    else:
        dest = Path(letter)

    if not dest.exists():
        print(f"  [FAIL] {dest} not found")
        return False

    # 檢查 release 檔案
    for fname in DEPLOY_FILES:
        src = RELEASE_DIR / fname
        if not src.exists():
            print(f"  [FAIL] 找不到 {src}")
            print(f"    請先執行 build 產生 exe")
            return False

    # 複製檔案（用 binary write + fsync 確保完整寫入，避免 exFAT 損壞）
    for fname in DEPLOY_FILES:
        src = RELEASE_DIR / fname
        dst = dest / fname
        existing = dst.exists()

        with open(src, "rb") as f_in:
            data = f_in.read()
        if existing:
            dst.unlink()
        with open(dst, "wb") as f_out:
            f_out.write(data)
            f_out.flush()
            os.fsync(f_out.fileno())

        # 驗證
        with open(dst, "rb") as f_check:
            header = f_check.read(4)
        written_size = dst.stat().st_size
        if written_size != len(data):
            print(f"  [FAIL] {fname} size mismatch: {written_size} != {len(data)}")
            return False

        action = "update" if existing else "deploy"
        size_mb = written_size / (1024 * 1024)
        print(f"  [OK] {action} {fname} ({size_mb:.1f} MB)")

    # Linux: 設定執行權限
    if not IS_WINDOWS:
        for fname in ["VRAM_Booster", "autorun.sh", "install-udev.sh"]:
            f = dest / fname
            if f.exists():
                os.chmod(f, 0o755)

    # 驗證
    exe_name = "VRAM_Booster.exe" if IS_WINDOWS else "VRAM_Booster"
    exe_path = dest / exe_name
    if exe_path.exists():
        print(f"\n  [OK] Deploy done! Run {exe_name} after inserting the card.")
        return True
    return False


def main():
    print("=" * 50)
    print("  VRAM Booster — 部署到 SD 卡")
    print("=" * 50)
    print()

    # 指定磁碟代號
    if len(sys.argv) > 1:
        letter = sys.argv[1].upper().rstrip(":\\")
        print(f"目標: {letter}:\\")
        deploy(letter)
        return

    # 自動偵測
    print("偵測可移除式磁碟...\n")
    drives = find_removable_drives()

    if not drives:
        print("  未偵測到可移除式磁碟。請插入 SD 卡後重試。")
        return

    for i, d in enumerate(drives):
        label = f" ({d['label']})" if d['label'] else ""
        print(f"  [{i + 1}] {d['letter']}:{label}  "
              f"{d['size_gb']:.0f}GB (可用 {d['free_gb']:.0f}GB)")

    print()

    if len(drives) == 1:
        choice = drives[0]
        print(f"只有一個磁碟，自動選擇 {choice['letter']}:\\")
    else:
        try:
            idx = int(input("選擇磁碟 (輸入編號): ")) - 1
            choice = drives[idx]
        except (ValueError, IndexError):
            print("取消")
            return

    print(f"\n部署到 {choice['letter']}:\\...")
    deploy(choice["letter"])


if __name__ == "__main__":
    main()
