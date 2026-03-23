"""
VRAM Booster — 通用隨插即用啟動器
====================================
一個 .exe 通吃六種設備。

自動偵測邏輯：
  1. 找到自己在哪個磁碟上 (E:\, F:\, etc.)
  2. 判斷該磁碟是什麼裝置 (SD / USB / HDD / NVMe)
  3. 測速 → 選最佳化 profile
  4. 啟動對應的微系統
  5. 小視窗顯示監控狀態
  6. 關視窗或拔卡 → 完全退出
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def get_my_drive() -> Optional[str]:
    """找到自己所在的磁碟機代號"""
    candidates = []

    # 1. PyInstaller 打包時，用 sys.executable 的原始路徑
    if getattr(sys, 'frozen', False):
        candidates.append(sys.executable)
        # sys.executable 在 PyInstaller onefile 可能指向暫存目錄
        # 但 os.path.dirname(sys.executable) 才是 exe 真正所在位置
        # 額外嘗試 sys.argv[0] 因為它保留了原始呼叫路徑
        if sys.argv:
            candidates.append(os.path.abspath(sys.argv[0]))
    else:
        candidates.append(os.path.abspath(__file__))

    # 2. 從候選路徑中找出可移除式磁碟
    for path in candidates:
        drive = os.path.splitdrive(path)[0]
        if drive and len(drive) >= 2:
            letter = drive[0].upper()
            # 排除 C: (系統) 和暫存目錄常見的磁碟
            if letter not in ("C",):
                return letter

    # 3. Fallback: 掃描所有可移除式磁碟
    if platform.system().lower() == "windows":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Volume | Where-Object {$_.DriveType -eq 'Removable' -and $_.DriveLetter -and $_.Size -gt 0} "
                 "| Select-Object DriveLetter | ConvertTo-Json"],
                capture_output=True, text=True, timeout=8,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                for v in data:
                    if v.get("DriveLetter"):
                        return v["DriveLetter"]
        except Exception:
            pass

    return None


def detect_device_type(letter: str) -> Dict[str, Any]:
    """偵測指定磁碟是什麼裝置"""
    info = {
        "letter": letter,
        "label": "",
        "fs": "",
        "capacity_gb": 0,
        "free_gb": 0,
        "type": "unknown",       # sd_card / usb_drive / usb_ssd / hdd / nvme_enclosure / sd_express
        "bus": "",
        "media": "",
        "friendly_name": "",
        "is_removable": False,
        "is_rotational": False,
    }

    if platform.system().lower() != "windows":
        return info

    try:
        # Volume info
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Volume -DriveLetter {letter} -ErrorAction Stop | "
             "Select-Object FileSystemLabel,FileSystem,Size,SizeRemaining,DriveType | ConvertTo-Json"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0 and r.stdout.strip():
            v = json.loads(r.stdout)
            info["label"] = v.get("FileSystemLabel", "")
            info["fs"] = v.get("FileSystem", "")
            info["capacity_gb"] = v.get("Size", 0) / (1024 ** 3)
            info["free_gb"] = v.get("SizeRemaining", 0) / (1024 ** 3)
            info["is_removable"] = v.get("DriveType") == "Removable"

        # Physical disk info
        r2 = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Partition -DriveLetter {letter} -ErrorAction Stop | "
             "Select-Object DiskNumber | ConvertTo-Json"],
            capture_output=True, text=True, timeout=8,
        )
        if r2.returncode == 0 and r2.stdout.strip():
            dn = json.loads(r2.stdout).get("DiskNumber")
            if dn is not None:
                r3 = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Get-PhysicalDisk -DeviceNumber {dn} -ErrorAction Stop | "
                     "Select-Object FriendlyName,BusType,MediaType,SpindleSpeed | ConvertTo-Json"],
                    capture_output=True, text=True, timeout=8,
                )
                if r3.returncode == 0 and r3.stdout.strip():
                    pd = json.loads(r3.stdout)
                    info["friendly_name"] = pd.get("FriendlyName", "")
                    info["bus"] = pd.get("BusType", "")
                    info["media"] = pd.get("MediaType", "")
                    spindle = pd.get("SpindleSpeed", 0)
                    info["is_rotational"] = spindle and spindle > 0

                    name_lower = info["friendly_name"].lower()
                    bus = info["bus"]
                    media = info["media"]

                    # 分類六種設備
                    if bus == "NVMe":
                        if info["is_removable"]:
                            info["type"] = "nvme_enclosure"  # 外接 NVMe = Enclosure
                        else:
                            info["type"] = "sd_express"  # 內建 NVMe 走 SD 讀卡機 = SD Express
                    elif bus == "SD" or "card" in name_lower or "sd" in name_lower:
                        info["type"] = "sd_card"
                    elif info["is_rotational"] or media == "HDD" or "hdd" in name_lower:
                        info["type"] = "hdd"
                    elif bus == "USB":
                        if media == "SSD" or info["capacity_gb"] > 200:
                            info["type"] = "usb_ssd"
                        else:
                            info["type"] = "usb_drive"
                    elif "thunderbolt" in bus.lower() or "usb4" in bus.lower():
                        info["type"] = "nvme_enclosure"
                    else:
                        info["type"] = "usb_drive"

    except Exception as e:
        logger.warning("Detection failed: %s", e)

    return info


def select_system(device_info: Dict):
    """根據裝置類型選擇對應的微系統"""
    dtype = device_info["type"]

    if dtype == "sd_express":
        from .systems.sd_vram_system import SDVRAMSystem
        return SDVRAMSystem(), "SD-VRAM Booster (Express)"
    elif dtype == "nvme_enclosure":
        from .systems.enc_vram_system import EnclosureVRAMSystem
        return EnclosureVRAMSystem(), "Enclosure-VRAM Booster"
    elif dtype == "usb_ssd":
        from .systems.usb_vram_system import USBVRAMSystem
        return USBVRAMSystem(), "USB-VRAM Booster (SSD)"
    elif dtype == "sd_card":
        from .systems.sd_legacy_system import SDLegacySystem
        return SDLegacySystem(), "SD-VRAM Booster"
    elif dtype == "hdd":
        from .systems.hdd_system import HDDVRAMSystem
        return HDDVRAMSystem(), "HDD-VRAM Booster"
    else:
        from .systems.usb_legacy_system import USBLegacySystem
        return USBLegacySystem(), "USB-VRAM Booster"


# ══════════════════════════════════════════════
#  小視窗 GUI
# ══════════════════════════════════════════════

class BoosterApp:
    """
    單一小視窗：偵測 → 確認 → 監控 → 退出
    """

    BG = "#1a1a2e"
    BG2 = "#16213e"
    FG = "#e0e0e0"
    ACCENT = "#00d4ff"
    GREEN = "#00e676"
    ORANGE = "#ffab40"
    RED = "#ff5252"
    GRAY = "#666666"

    TYPE_LABELS = {
        "sd_express": ("SD Express", "⚡"),
        "nvme_enclosure": ("NVMe 外接盒", "💎"),
        "usb_ssd": ("USB SSD", "🔷"),
        "sd_card": ("SD 記憶卡", "💾"),
        "usb_drive": ("USB 隨身碟", "🔌"),
        "hdd": ("外接硬碟", "💿"),
    }

    def __init__(self):
        self._system = None
        self._system_name = ""
        self._device_info = None
        self._my_drive = None
        self._root: Optional[tk.Tk] = None
        self._running = True
        self._phase = "detecting"  # detecting → confirm → active

    def run(self):
        self._root = tk.Tk()
        self._root.title("VRAM Booster")
        self._root.configure(bg=self.BG)
        self._root.resizable(False, False)
        self._root.attributes("-topmost", True)
        self._root.protocol("WM_DELETE_WINDOW", self._quit)

        w, h = 360, 300
        sx = self._root.winfo_screenwidth() - w - 20
        sy = self._root.winfo_screenheight() - h - 80
        self._root.geometry(f"{w}x{h}+{sx}+{sy}")

        self._frame = tk.Frame(self._root, bg=self.BG)
        self._frame.pack(fill="both", expand=True)

        # 啟動偵測
        self._show_detecting()
        self._root.after(100, self._do_detect)
        self._root.mainloop()

    # ── Phase 1: 偵測中 ──

    def _show_detecting(self):
        self._clear()
        self._phase = "detecting"
        tk.Label(self._frame, text="VRAM Booster", font=("Segoe UI", 14, "bold"),
                 fg=self.ACCENT, bg=self.BG).pack(pady=(30, 10))
        self._detect_lbl = tk.Label(self._frame, text="偵測裝置中...",
                                     font=("Segoe UI", 11), fg=self.ORANGE, bg=self.BG)
        self._detect_lbl.pack(pady=10)

    def _do_detect(self):
        """偵測自己在哪張磁碟上、那是什麼裝置"""
        self._my_drive = get_my_drive()
        if not self._my_drive:
            self._detect_lbl.configure(text="無法偵測磁碟", fg=self.RED)
            return

        self._detect_lbl.configure(text=f"偵測 {self._my_drive}:\\ ...")
        self._root.update()

        self._device_info = detect_device_type(self._my_drive)

        if self._device_info["capacity_gb"] <= 0:
            self._detect_lbl.configure(text=f"{self._my_drive}:\\ 無法讀取", fg=self.RED)
            return

        self._show_confirm()

    # ── Phase 2: 確認 ──

    def _show_confirm(self):
        self._clear()
        self._phase = "confirm"
        info = self._device_info
        dtype = info["type"]
        label, icon = self.TYPE_LABELS.get(dtype, ("儲存裝置", "📁"))

        # 效能預估
        eff = self._quick_estimate(info)

        tk.Label(self._frame, text="VRAM Booster", font=("Segoe UI", 14, "bold"),
                 fg=self.ACCENT, bg=self.BG).pack(pady=(12, 4))

        # 裝置資訊卡
        card = tk.Frame(self._frame, bg=self.BG2, padx=15, pady=10)
        card.pack(fill="x", padx=15, pady=8)

        # 頻寬顯示：慢速裝置標示「隨機 I/O」
        swap_limit = eff.get("swap_limit_gb", 0)
        if swap_limit > 0:
            bw_str = f"{eff['bw']:.0f} MB/s (隨機 I/O)"
            cap_str = f"{info['capacity_gb']:.0f} GB (swap 上限 {swap_limit:.0f} GB)"
        else:
            bw_str = f"{eff['bw']:.0f} MB/s"
            cap_str = f"{info['capacity_gb']:.0f} GB (可用 {info['free_gb']:.0f} GB)"

        rows = [
            ("裝置", f"{icon} {label}"),
            ("磁碟", f"{info['letter']}:\\  {info['label']}"),
            ("容量", cap_str),
            ("頻寬", bw_str),
            ("Context", eff["ctx_str"]),
        ]
        for lbl, val in rows:
            r = tk.Frame(card, bg=self.BG2)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=f"{lbl}:", font=("Segoe UI", 9),
                     fg=self.GRAY, bg=self.BG2, width=8, anchor="e").pack(side="left")
            fg = self.GREEN if "Context" in lbl else self.FG
            tk.Label(r, text=val, font=("Segoe UI", 9, "bold"),
                     fg=fg, bg=self.BG2, anchor="w").pack(side="left", padx=(8, 0))

        # 按鈕
        btn = tk.Frame(self._frame, bg=self.BG)
        btn.pack(pady=12)
        tk.Button(btn, text=" 啟動 ", font=("Segoe UI", 11, "bold"),
                  fg="white", bg="#00c853", activebackground=self.GREEN,
                  relief="flat", cursor="hand2", padx=24, pady=6,
                  command=self._activate).pack(side="left", padx=8)
        tk.Button(btn, text=" 退出 ", font=("Segoe UI", 10),
                  fg=self.GRAY, bg="#333333", activebackground="#444444",
                  relief="flat", cursor="hand2", padx=16, pady=6,
                  command=self._quit).pack(side="left", padx=8)

    # ── Phase 3: 運作中 ──

    def _show_active(self):
        self._clear()
        self._phase = "active"

        # 調大視窗
        self._root.geometry(f"360x320+{self._root.winfo_x()}+{self._root.winfo_y()}")

        hdr = tk.Frame(self._frame, bg=self.BG2, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text="VRAM Booster", font=("Segoe UI", 11, "bold"),
                 fg=self.ACCENT, bg=self.BG2).pack(side="left", padx=10)
        self._health_lbl = tk.Label(hdr, text="● 啟動中...", font=("Segoe UI", 9, "bold"),
                                     fg=self.ORANGE, bg=self.BG2)
        self._health_lbl.pack(side="right", padx=10)

        # 記憶體條
        tiers_f = tk.Frame(self._frame, bg=self.BG, padx=12, pady=8)
        tiers_f.pack(fill="x")

        self._bars = []
        for name, color in [("VRAM", self.GREEN), ("RAM", self.ORANGE), ("裝置", self.ACCENT)]:
            row = tk.Frame(tiers_f, bg=self.BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=name, font=("Segoe UI", 9, "bold"),
                     fg=color, bg=self.BG, width=5, anchor="w").pack(side="left")
            bar_bg = tk.Frame(row, bg="#333333", height=16)
            bar_bg.pack(side="left", fill="x", expand=True, padx=4)
            bar_bg.pack_propagate(False)
            bar_fill = tk.Frame(bar_bg, bg=color, width=0)
            bar_fill.place(x=0, y=0, relheight=1.0)
            bar_text = tk.Label(bar_bg, text="—", font=("Segoe UI", 7),
                                fg="white", bg="#333333")
            bar_text.place(relx=0.5, rely=0.5, anchor="center")
            self._bars.append({"bg": bar_bg, "fill": bar_fill, "text": bar_text})

        # 資訊
        info_f = tk.Frame(self._frame, bg=self.BG2, padx=12, pady=8)
        info_f.pack(fill="x", padx=12, pady=5)

        self._info_lbls = {}
        for key, label in [("total", "總記憶體"), ("compress", "壓縮率"),
                           ("buffer", "Buffer"), ("device", "裝置"), ("system", "系統")]:
            r = tk.Frame(info_f, bg=self.BG2)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=f"{label}:", font=("Segoe UI", 8),
                     fg=self.GRAY, bg=self.BG2, width=8, anchor="e").pack(side="left")
            lbl = tk.Label(r, text="—", font=("Segoe UI", 8, "bold"),
                           fg=self.FG, bg=self.BG2, anchor="w")
            lbl.pack(side="left", padx=(6, 0))
            self._info_lbls[key] = lbl

        self._info_lbls["system"].configure(text=self._system_name)

        tk.Button(self._frame, text="停止並退出", font=("Segoe UI", 9),
                  fg=self.FG, bg="#444444", relief="flat", cursor="hand2",
                  padx=10, pady=4, command=self._quit).pack(pady=8)

        # 開始監控輪詢
        self._poll_monitor()

    def _poll_monitor(self):
        if not self._running or self._phase != "active":
            return

        # 檢查磁碟還在不在
        drive_path = f"{self._my_drive}:\\"
        if not os.path.exists(drive_path):
            self._quit()  # 拔卡 → 退出
            return

        # 更新監控 — 真實系統記憶體數據
        if self._system and hasattr(self, '_boost_engine') and self._boost_engine:
            try:
                from .core.real_boost import RealBoostEngine
                status = self._boost_engine.status()
                gpu = RealBoostEngine.get_gpu_info()

                self._health_lbl.configure(text="● 運作中", fg=self.GREEN)

                # 三條記憶體條：GPU VRAM / RAM / 裝置(swap)
                tiers_data = [
                    (gpu["vram_used_mb"] / 1024, gpu["vram_total_mb"] / 1024),
                    ((status["physical_ram_gb"] - status["available_ram_gb"]), status["physical_ram_gb"]),
                    (status["swap_used_gb"], status["swap_total_gb"]),
                ]
                for i, (used, cap) in enumerate(tiers_data):
                    if i >= len(self._bars):
                        break
                    pct = (used / cap * 100) if cap > 0 else 0
                    bw = self._bars[i]["bg"].winfo_width()
                    fw = max(1, int(bw * pct / 100)) if bw > 0 else 1
                    self._bars[i]["fill"].place(x=0, y=0, width=fw, relheight=1.0)
                    self._bars[i]["text"].configure(text=f"{used:.1f} / {cap:.1f} GB")

                total = status["total_usable_gb"]
                self._info_lbls["total"].configure(
                    text=f"{status['physical_ram_gb']:.0f} GB RAM + {status['swap_total_gb']:.0f} GB Swap")
                self._info_lbls["compress"].configure(text=f"GPU: {gpu['name']}")
                self._info_lbls["buffer"].configure(
                    text=f"VRAM: {gpu['vram_free_mb']}MB free")
                self._info_lbls["device"].configure(text=f"{self._my_drive}:\\")

            except Exception:
                pass

        self._root.after(2000, self._poll_monitor)

    # ── Actions ──

    def _activate(self):
        self._show_active()
        self._boost_engine = None

        def do():
            try:
                from .core.real_boost import RealBoostEngine

                engine = RealBoostEngine()
                self._boost_engine = engine
                info = self._device_info

                # 真實擴展：在裝置上建立 swap/pagefile
                result = engine.activate(info["letter"], use_percent=80.0)

                if result.get("success"):
                    added = result.get("added_gb", 0)
                    method = result.get("method", "")
                    rand_mbs = result.get("rand_write_mbs", 0)
                    self._system_name = f"+{added:.0f} GB ({method})"
                    self._system = True  # 標記為已啟動

                    if hasattr(self, '_info_lbls'):
                        self._info_lbls["system"].configure(text=self._system_name)
                        # 有速度警告時顯示橙色
                        if result.get("warning"):
                            self._health_lbl.configure(
                                text=f"● 運作中 (寫入 {rand_mbs:.0f} MB/s)", fg=self.ORANGE)
                        else:
                            self._health_lbl.configure(text="● 運作中", fg=self.GREEN)
                else:
                    error = result.get("error", "Unknown")
                    if hasattr(self, '_health_lbl'):
                        self._health_lbl.configure(text=f"● {error}", fg=self.RED)

            except Exception as e:
                logger.error("Activation failed: %s", e)
                if hasattr(self, '_health_lbl'):
                    self._health_lbl.configure(text="● 失敗", fg=self.RED)

        threading.Thread(target=do, daemon=True).start()

    def _quit(self):
        self._running = False
        # 停止真實的 swap/pagefile — 移除裝置上的 swap 檔案
        if hasattr(self, '_boost_engine') and self._boost_engine:
            try:
                self._boost_engine.deactivate()
            except Exception:
                pass
        self._system = None
        if self._root:
            self._root.destroy()

    # ── Helpers ──

    def _clear(self):
        for w in self._frame.winfo_children():
            w.destroy()

    def _quick_estimate(self, info: Dict) -> Dict:
        from .core.slow_device_optimizer import SlowDeviceProfile
        from .core.real_boost import SWAP_FILL_TIME_SECONDS

        profile_map = {
            "sd_express": None, "nvme_enclosure": None, "usb_ssd": None,
            "sd_card": "sd_uhs1", "usb_drive": "usb3_flash", "hdd": "hdd_5400rpm",
        }
        profile = profile_map.get(info["type"])

        if profile is None:
            # 高速設備：顯示順序頻寬（NVMe 隨機也快）
            bw = {"sd_express": 3500, "nvme_enclosure": 8500, "usb_ssd": 3200}.get(info["type"], 1000)
            swap_limit_gb = 0  # 不限制
        else:
            eff = SlowDeviceProfile.estimate_effective_bandwidth(profile)
            # 慢速設備：顯示隨機 I/O 頻寬（swap 實際用的）
            bw = eff["rand_read_mbs"]
            rand_write = eff["rand_write_mbs"]

            # 連續公式：swap 上限 = 速度 × 可接受寫滿時間
            swap_limit_gb = rand_write * SWAP_FILL_TIME_SECONDS / 1024

        free = info.get("free_gb", 0)
        # swap 容量 = min(可用空間 × 80%, 速度上限)
        usable_gb = free * 0.8
        if swap_limit_gb > 0:
            usable_gb = min(usable_gb, swap_limit_gb)

        kv_per_token = 65536
        ctx = int(usable_gb * (1024 ** 3) / kv_per_token)

        if ctx >= 1_000_000:
            ctx_str = f"{ctx / 1_000_000:.0f}M tokens"
        elif ctx >= 1_000:
            ctx_str = f"{ctx / 1_000:.0f}K tokens"
        else:
            ctx_str = f"{ctx} tokens"

        return {
            "bw": bw,
            "mult": 1.0,  # swap 無壓縮增益
            "ctx": ctx,
            "ctx_str": ctx_str,
            "swap_limit_gb": swap_limit_gb,
        }


# ── Entry Point ──

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    app = BoosterApp()
    app.run()


if __name__ == "__main__":
    main()
