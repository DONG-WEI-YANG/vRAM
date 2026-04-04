r"""
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

# Windows: 隱藏子程序視窗
_NO_WINDOW = 0
if platform.system().lower() == "windows":
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _run_hidden(cmd, **kwargs):
    """執行子程序，Windows 上不彈出視窗"""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if _NO_WINDOW:
        kwargs.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def get_my_drive() -> Optional[str]:
    """
    找到自己所在的磁碟機代號。

    判斷方式（Windows）：
      1. 先從 exe 路徑提取磁碟代號，排除系統碟
      2. Fallback: 用 BusType 掃描所有外接裝置（不依賴 DriveType）
    """
    candidates = []

    # 1. PyInstaller 打包時，用 sys.executable 的原始路徑
    if getattr(sys, 'frozen', False):
        candidates.append(sys.executable)
        if sys.argv:
            candidates.append(os.path.abspath(sys.argv[0]))
    else:
        candidates.append(os.path.abspath(__file__))

    # 2. 從候選路徑中找出非系統碟
    sys_drive = os.environ.get("SystemDrive", "C:")[0].upper() if platform.system().lower() == "windows" else ""
    for path in candidates:
        drive = os.path.splitdrive(path)[0]
        if drive and len(drive) >= 2:
            letter = drive[0].upper()
            if letter != sys_drive:
                return letter

    # 3. Fallback: 用 BusType 掃描外接裝置（修復：不再只找 Removable）
    if platform.system().lower() == "windows":
        try:
            from .core.device_query import get_external_drive_letters
            ext_drives = get_external_drive_letters()
            if ext_drives:
                return ext_drives[0]["letter"]
        except Exception as e:
            logger.warning("get_my_drive fallback failed: %s", e)
    else:
        try:
            r = _run_hidden(
                ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,RM,SIZE,TYPE,TRAN"],
                timeout=8,
            )
            if r.returncode == 0:
                data = json.loads(r.stdout)
                for dev in data.get("blockdevices", []):
                    tran = (dev.get("tran") or "").lower()
                    if tran not in ("usb", "nvme", "mmc") and not dev.get("rm"):
                        continue
                    for part in dev.get("children", [dev]):
                        mp = part.get("mountpoint")
                        if mp and mp not in ("/", "/boot", "/home"):
                            return mp
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass

    return None


def detect_mode() -> str:
    """
    偵測運行模式：
    - "device": exe 在外接裝置上（現有行為）
    - "host": exe 在系統碟上（主機模式）
    """
    drive = get_my_drive()
    if drive is None:
        return "host"  # 無法辨識外接磁碟 → 視為主機模式

    # 檢查 exe 所在磁碟是否為系統碟
    if platform.system().lower() == "windows":
        sys_drive = os.environ.get("SystemDrive", "C:")[0].upper()
        if drive.upper() == sys_drive:
            return "host"
    else:
        # Linux: 如果在 /usr, /opt, /home 等系統路徑 → host mode
        exe_path = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
        if not exe_path.startswith(("/mnt/", "/media/", "/run/media/")):
            return "host"

    return "device"


def detect_device_type(letter: str) -> Dict[str, Any]:
    """
    偵測指定磁碟是什麼裝置。
    Windows: letter = "E" (磁碟代號)
    Linux: letter = "/media/user/SDCARD" (掛載點)
    """
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
        return _detect_device_type_linux(info, letter)

    try:
        # Volume info
        r = _run_hidden(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Volume -DriveLetter {letter} -ErrorAction Stop | "
             "Select-Object FileSystemLabel,FileSystem,Size,SizeRemaining,DriveType | ConvertTo-Json"],
            timeout=8,
        )
        if r.returncode == 0 and r.stdout.strip():
            v = json.loads(r.stdout)
            info["label"] = v.get("FileSystemLabel", "")
            info["fs"] = v.get("FileSystem", "")
            info["capacity_gb"] = v.get("Size", 0) / (1024 ** 3)
            info["free_gb"] = v.get("SizeRemaining", 0) / (1024 ** 3)
            info["is_removable"] = v.get("DriveType") == "Removable"  # 初始值，後面用 BusType 校正

        # Physical disk info
        r2 = _run_hidden(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Partition -DriveLetter {letter} -ErrorAction Stop | "
             "Select-Object DiskNumber | ConvertTo-Json"],
            timeout=8,
        )
        if r2.returncode == 0 and r2.stdout.strip():
            dn = json.loads(r2.stdout).get("DiskNumber")
            if dn is not None:
                r3 = _run_hidden(
                    ["powershell", "-NoProfile", "-Command",
                     f"Get-PhysicalDisk -DeviceNumber {dn} -ErrorAction Stop | "
                     "Select-Object FriendlyName,BusType,MediaType,SpindleSpeed | ConvertTo-Json"],
                    timeout=8,
                )
                if r3.returncode == 0 and r3.stdout.strip():
                    pd = json.loads(r3.stdout)
                    info["friendly_name"] = pd.get("FriendlyName", "")
                    info["bus"] = pd.get("BusType", "")
                    info["media"] = pd.get("MediaType", "")
                    spindle = pd.get("SpindleSpeed", 0)
                    info["is_rotational"] = spindle and spindle > 0

                    # BusType 校正 is_removable — USB/SD/TB 裝置一律視為可移除
                    from .core.device_query import classify_device, EXTERNAL_BUS_TYPES
                    bus = info["bus"]
                    if bus in EXTERNAL_BUS_TYPES or bus == "NVMe":
                        info["is_removable"] = True

                    # 用 classify_device() 統一分類（BusType 驅動）
                    info["type"] = classify_device(
                        bus_type=bus,
                        media_type=info["media"],
                        friendly_name=info["friendly_name"],
                        spindle_speed=pd.get("SpindleSpeed", 0),
                        capacity_gb=info["capacity_gb"],
                    )

    except Exception as e:
        logger.warning("Detection failed: %s", e)

    return info


def _detect_device_type_linux(info: Dict, mount_point: str) -> Dict:
    """Linux: 用 lsblk + sysfs 偵測裝置類型"""
    try:
        usage = shutil.disk_usage(mount_point)
        info["capacity_gb"] = usage.total / (1024 ** 3)
        info["free_gb"] = usage.free / (1024 ** 3)
    except OSError:
        return info

    try:
        # 找到掛載點對應的區塊裝置
        r = _run_hidden(
            ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,RM,TRAN,ROTA,MODEL,FSTYPE,LABEL,TYPE"],
            timeout=8,
        )
        if r.returncode != 0:
            return info

        data = json.loads(r.stdout)
        for dev in data.get("blockdevices", []):
            children = dev.get("children", [])
            # 也檢查裝置本身（無分區的情況）
            all_parts = children + [dev]
            for part in all_parts:
                if part.get("mountpoint") != mount_point:
                    continue

                info["is_removable"] = bool(dev.get("rm"))
                info["friendly_name"] = dev.get("model", "") or ""
                info["label"] = part.get("label", "") or ""
                info["fs"] = part.get("fstype", "") or ""
                info["is_rotational"] = bool(dev.get("rota"))

                tran = (dev.get("tran") or "").lower()
                info["bus"] = tran
                model_lower = info["friendly_name"].lower()

                # 分類
                if tran == "nvme":
                    info["type"] = "nvme_enclosure" if info["is_removable"] else "sd_express"
                elif tran in ("usb",):
                    if info["is_rotational"]:
                        info["type"] = "hdd"
                    elif info["capacity_gb"] > 200 or "ssd" in model_lower:
                        info["type"] = "usb_ssd"
                    else:
                        info["type"] = "usb_drive"
                elif "mmc" in tran or "sd" in model_lower or "card" in model_lower:
                    info["type"] = "sd_card"
                elif info["is_rotational"]:
                    info["type"] = "hdd"
                elif info["is_removable"]:
                    info["type"] = "sd_card"
                else:
                    info["type"] = "usb_drive"

                return info

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        logger.warning("Linux detection failed: %s", e)

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
        self._engine_lock = threading.Lock()  # 防止 activate/deactivate 競態
        self._engine_ready = threading.Event()  # engine 初始化完成信號

    def run(self):
        # 降低自身程序優先權，避免監控佔用前景資源
        self._lower_process_priority()

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

        # 警告：卡上有其他檔案
        used_gb = info["capacity_gb"] - info["free_gb"]
        if used_gb > 1.0:
            warn_f = tk.Frame(self._frame, bg="#3e2723", padx=10, pady=4)
            warn_f.pack(fill="x", padx=15, pady=(4, 0))
            tk.Label(warn_f,
                     text=f"* {used_gb:.0f}GB used, swap only uses free space",
                     font=("Segoe UI", 8), fg=self.ORANGE, bg="#3e2723").pack()

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

        # 裝置狀態
        device_f = tk.Frame(self._frame, bg=self.BG2, padx=12, pady=4)
        device_f.pack(fill="x", padx=12, pady=(0, 5))

        r = tk.Frame(device_f, bg=self.BG2)
        r.pack(fill="x")
        tk.Label(r, text="裝置狀態:", font=("Segoe UI", 8),
                 fg=self.GRAY, bg=self.BG2, width=8, anchor="e").pack(side="left")
        self._device_status_lbl = tk.Label(
            r, text="● 正常", font=("Segoe UI", 8, "bold"),
            fg=self.GREEN, bg=self.BG2, anchor="w")
        self._device_status_lbl.pack(side="left", padx=(6, 0))

        r2 = tk.Frame(device_f, bg=self.BG2)
        r2.pack(fill="x")
        tk.Label(r2, text="保護:", font=("Segoe UI", 8),
                 fg=self.GRAY, bg=self.BG2, width=8, anchor="e").pack(side="left")
        tk.Label(r2, text="智慧降級 (B+)", font=("Segoe UI", 8, "bold"),
                 fg=self.ACCENT, bg=self.BG2, anchor="w").pack(side="left", padx=(6, 0))

        tk.Button(self._frame, text="停止並退出", font=("Segoe UI", 9),
                  fg=self.FG, bg="#444444", relief="flat", cursor="hand2",
                  padx=10, pady=4, command=self._quit).pack(pady=8)

        # 開始監控輪詢
        self._poll_monitor()

        # 即時裝置監聽
        try:
            from .core.device_watcher import DeviceWatcher, DeviceEvent, ExpansionAction
            self._device_watcher = DeviceWatcher()
            self._device_watcher.on_change(self._on_device_event)
            self._device_watcher.start()
            logger.info("hotplug_launcher: DeviceWatcher attached")
        except Exception as e:
            logger.warning("DeviceWatcher unavailable: %s", e)
            self._device_watcher = None

    def _poll_monitor(self):
        if not self._running or self._phase != "active":
            return

        # 檢查磁碟還在不在
        drive_path = f"{self._my_drive}:\\"
        if not os.path.exists(drive_path):
            self._quit()  # 拔卡 → 退出
            return

        # 等 engine 初始化完成才讀取狀態（防競態）
        if self._engine_ready.is_set() and self._boost_engine:
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

                self._info_lbls["total"].configure(
                    text=f"{status['physical_ram_gb']:.0f} GB RAM + {status['swap_total_gb']:.0f} GB Swap")
                self._info_lbls["compress"].configure(text=f"GPU: {gpu['name']}")
                self._info_lbls["buffer"].configure(
                    text=f"VRAM: {gpu['vram_free_mb']}MB free")
                self._info_lbls["device"].configure(text=f"{self._my_drive}:\\")

                # Update device status indicator
                if hasattr(self, '_device_status_lbl'):
                    degraded = False
                    if hasattr(self._boost_engine, '_mmap_engine') and self._boost_engine._mmap_engine:
                        mmap_st = self._boost_engine._mmap_engine.status()
                        degraded = mmap_st.get("degraded", False)

                    if degraded:
                        self._device_status_lbl.configure(
                            text="● 已斷線 — 等待重新連接", fg=self.ORANGE)
                    else:
                        self._device_status_lbl.configure(
                            text=f"● 正常 ({self._my_drive}:\\)", fg=self.GREEN)

            except (OSError, ValueError, KeyError) as e:
                logger.debug("Poll error: %s", e)

        self._root.after(10000, self._poll_monitor)  # 10 秒輪詢，減少資源佔用

    def _on_device_event(self, change):
        """Handle real-time device arrival/removal from DeviceWatcher."""
        from .core.device_watcher import DeviceEvent, ExpansionAction

        if change.event == DeviceEvent.REMOVED:
            if change.drive_letter == self._my_drive:
                logger.critical("Own drive %s removed — immediate exit", change.drive_letter)
                try:
                    self._root.after(0, self._quit)
                except Exception:
                    pass
                return

            if self._phase == "active":
                msg = f"{change.drive_letter}:\\ removed"
                logger.warning("Device removed: %s", msg)
                try:
                    self._root.after(0, lambda m=msg: self._show_notification(m, self.ORANGE))
                except Exception:
                    pass

        elif change.event == DeviceEvent.ARRIVED:
            if self._phase != "active":
                return

            info = change.device_info or {}
            action = info.get("expansion_action", "ignore")
            name = info.get("friendly_name", "Unknown")
            letter = change.drive_letter

            if action == ExpansionAction.AUTO_EXPAND.value:
                msg = f"Auto-joined {letter}:\\ ({name})"
                logger.info("Auto-expand: %s", msg)
                try:
                    self._root.after(0, lambda m=msg: self._show_notification(m, self.GREEN))
                except Exception:
                    pass

            elif action == ExpansionAction.PROMPT_USER.value:
                msg_text = f"Found {name} ({letter}:\\). Add to expansion?"
                logger.info("Prompt: %s", msg_text)
                try:
                    self._root.after(0, lambda l=letter, n=name: self._show_expansion_prompt(l, n))
                except Exception:
                    pass

    def _show_notification(self, message: str, color: str):
        """Show a temporary notification bar at the top of the active view."""
        import tkinter as tk
        if not hasattr(self, '_notif_lbl'):
            self._notif_lbl = tk.Label(
                self._frame, text="", font=("Segoe UI", 9),
                fg="white", bg=self.BG2, anchor="w", padx=10, pady=4,
            )
        self._notif_lbl.configure(text=message, bg=color)
        children = self._frame.winfo_children()
        if children:
            self._notif_lbl.pack(fill="x", before=children[0])
        else:
            self._notif_lbl.pack(fill="x")
        self._root.after(8000, lambda: self._notif_lbl.pack_forget())

    def _show_expansion_prompt(self, letter: str, name: str):
        """Show a prompt asking user whether to add a device for expansion."""
        import tkinter as tk
        prompt_f = tk.Frame(self._frame, bg="#1a237e", padx=8, pady=6)
        children = self._frame.winfo_children()
        if children:
            prompt_f.pack(fill="x", before=children[0])
        else:
            prompt_f.pack(fill="x")

        tk.Label(prompt_f, text=f"Found: {name} ({letter}:\\)",
                 font=("Segoe UI", 9, "bold"), fg="white", bg="#1a237e").pack(anchor="w")

        btn_f = tk.Frame(prompt_f, bg="#1a237e")
        btn_f.pack(anchor="e", pady=(4, 0))

        def accept():
            prompt_f.destroy()
            self._show_notification(f"Added {letter}:\\ to expansion", self.GREEN)

        def decline():
            prompt_f.destroy()

        tk.Button(btn_f, text="Add", font=("Segoe UI", 8), fg="white",
                  bg="#00c853", relief="flat", padx=8, command=accept).pack(side="left", padx=4)
        tk.Button(btn_f, text="Skip", font=("Segoe UI", 8), fg=self.GRAY,
                  bg="#333333", relief="flat", padx=8, command=decline).pack(side="left")

    # ── Actions ──

    def _activate(self):
        self._show_active()
        self._boost_engine = None
        self._engine_ready.clear()

        def do():
            with self._engine_lock:
                try:
                    from .core.real_boost import RealBoostEngine

                    engine = RealBoostEngine()
                    self._boost_engine = engine
                    info = self._device_info

                    # 進度回報：從背景執行緒安全更新 tkinter UI
                    def on_progress(msg: str):
                        if hasattr(self, '_health_lbl') and self._root:
                            self._root.after(0, lambda: self._health_lbl.configure(
                                text=f">> {msg}", fg=self.ORANGE))

                    # 真實擴展：在裝置上建立 swap/pagefile
                    result = engine.activate(info["letter"], use_percent=80.0,
                                             on_progress=on_progress)

                    if result.get("success"):
                        added = result.get("added_gb", 0)
                        rand_mbs = result.get("rand_write_mbs", 0)
                        self._system = True
                        self._engine_ready.set()
                        # mmap swap 立即生效，永遠不需要重開機
                        self._root.after(0, self._show_active)
                    else:
                        error = result.get("error", "Unknown")
                        if hasattr(self, '_health_lbl'):
                            self._root.after(0, lambda: self._health_lbl.configure(
                                text=f">> {error}", fg=self.RED))

                except (ImportError, OSError, ValueError) as e:
                    logger.error("Activation failed: %s", e)
                    if hasattr(self, '_health_lbl'):
                        self._root.after(0, lambda: self._health_lbl.configure(
                            text=">> failed", fg=self.RED))

        threading.Thread(target=do, daemon=True).start()

    def _quit(self):
        self._running = False

        def shutdown():
            # 用 lock 確保不會跟 activate 同時跑
            acquired = self._engine_lock.acquire(timeout=10)
            try:
                if self._boost_engine:
                    self._boost_engine.deactivate()
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.warning("Deactivate error: %s", e)
            finally:
                if acquired:
                    self._engine_lock.release()

        # 在背景執行關機，等待足夠長讓 SD 卡 I/O 完成
        t = threading.Thread(target=shutdown, daemon=True)
        t.start()
        t.join(timeout=15)
        if t.is_alive():
            logger.warning("Deactivate timed out after 15s, forcing exit")

        self._system = None

        # 關閉所有 log file handle，釋放 exe 所在目錄的檔案鎖
        for handler in logging.root.handlers[:]:
            try:
                handler.close()
                logging.root.removeHandler(handler)
            except Exception:
                pass

        if hasattr(self, '_device_watcher') and self._device_watcher:
            self._device_watcher.stop()

        if self._root:
            self._root.destroy()

    # ── Helpers ──

    @staticmethod
    def _lower_process_priority():
        """降低自身程序優先權，讓前景應用不受影響"""
        try:
            if platform.system().lower() == "windows":
                import ctypes
                # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
                ctypes.windll.kernel32.SetPriorityClass(
                    ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
                logger.info("Process priority set to BELOW_NORMAL")
            else:
                os.nice(10)  # Linux: 降低 10 級優先權
                logger.info("Process niceness increased by 10")
        except Exception as e:
            logger.debug("Cannot lower priority: %s", e)

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


# ── UAC Elevation ──

def _is_admin() -> bool:
    """檢查是否以管理員身份執行"""
    if platform.system().lower() != "windows":
        return os.geteuid() == 0
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except (AttributeError, OSError):
        return False


def _elevate_and_restart():
    """用 UAC 對話框重新啟動自己（僅 Windows）"""
    try:
        import ctypes
        exe = sys.executable if not getattr(sys, 'frozen', False) else sys.argv[0]
        params = " ".join(sys.argv[1:])
        # ShellExecuteW with "runas" triggers UAC prompt
        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    except (AttributeError, OSError):
        pass


def _elevate_linux():
    """用 pkexec 或 sudo 提權重新啟動自己（Linux）"""
    exe = sys.executable if not getattr(sys, 'frozen', False) else os.path.abspath(sys.argv[0])
    args = sys.argv[1:]

    # 優先用 pkexec（圖形化密碼對話框）
    for elevate_cmd in ["pkexec", "sudo"]:
        if shutil.which(elevate_cmd):
            try:
                os.execvp(elevate_cmd, [elevate_cmd, exe] + args)
            except OSError:
                continue

    logger.error("Cannot elevate: neither pkexec nor sudo found")


# ── Entry Point ──

def _cleanup_at_exit():
    """atexit handler：確保即使異常退出也能釋放資源。"""
    # 關閉所有 log handler，釋放檔案鎖
    for handler in logging.root.handlers[:]:
        try:
            handler.close()
            logging.root.removeHandler(handler)
        except Exception:
            pass


def main():
    import atexit
    atexit.register(_cleanup_at_exit)

    # 持久化 log 到 exe 所在目錄（SD 卡上）
    log_handlers = [logging.StreamHandler()]
    try:
        if getattr(sys, 'frozen', False):
            log_dir = Path(sys.executable).parent
        else:
            log_dir = Path(__file__).parent
        log_file = log_dir / "vram_booster.log"
        log_handlers.append(logging.FileHandler(str(log_file), encoding="utf-8"))
    except OSError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=log_handlers,
    )

    # 需要管理員/root 權限才能建 pagefile/swap，自動提權
    if not _is_admin():
        logger.info("Not admin/root, requesting elevation...")
        if platform.system().lower() == "windows":
            _elevate_and_restart()
        else:
            _elevate_linux()
        sys.exit(0)

    mode = detect_mode()
    logger.info("Running in %s mode", mode)

    if mode == "host":
        # 主機模式：使用 Host UI
        from .host_ui import HostUI
        logger.info("Launching Host Mode UI...")
        app = HostUI()
        app.run()
    else:
        # 裝置模式：使用原有的 BoosterApp
        app = BoosterApp()
        app.run()


if __name__ == "__main__":
    main()
