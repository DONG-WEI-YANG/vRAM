"""
VRAM Booster — Host Mode UI
=============================
主機模式：從 C:\ 執行，管理所有外接裝置的記憶體擴充。

Features:
  - 即時偵測外接裝置插拔
  - 顯示每個裝置的 pagefile 狀態和使用量
  - 安全彈出按鈕（確認 pagefile 無使用 → 移除 → 安全拔卡）
  - 總記憶體擴充量顯示
  - 系統記憶體狀態概覽
"""

import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# Colors
BG = "#1a1a2e"        # Dark navy background
BG_CARD = "#16213e"    # Card background
ACCENT = "#0f3460"     # Accent blue
GREEN = "#00b894"      # Active/safe
ORANGE = "#fdcb6e"     # Warning
RED = "#e17055"        # Error/danger
WHITE = "#dfe6e9"      # Text
GRAY = "#636e72"       # Muted text


class DeviceCard(tk.Frame):
    """一張外接裝置的狀態卡片。"""

    def __init__(self, parent, device_info: Dict, on_eject=None, **kwargs):
        super().__init__(parent, bg=BG_CARD, padx=12, pady=8, **kwargs)
        self.device_info = device_info
        self.on_eject = on_eject
        self._build()

    def _build(self):
        drive = self.device_info.get("drive", "?")
        mount = self.device_info.get("mount", "?")
        swap_gb = self.device_info.get("swap_gb", 0)
        speed = self.device_info.get("speed_mbs", 0)
        usage_mb = self.device_info.get("pagefile_usage_mb", 0)
        safe = self.device_info.get("safe_to_remove", True)
        degraded = self.device_info.get("degraded", False)

        # Header: drive letter + status dot
        header = tk.Frame(self, bg=BG_CARD)
        header.pack(fill=tk.X)

        status_color = RED if degraded else (GREEN if safe else ORANGE)
        status_text = "OFFLINE" if degraded else ("SAFE" if safe else "IN USE")

        tk.Label(header, text=f"  {drive}:\\", font=("Consolas", 16, "bold"),
                 fg=WHITE, bg=BG_CARD).pack(side=tk.LEFT)
        tk.Label(header, text=f"  [{status_text}]", font=("Consolas", 10),
                 fg=status_color, bg=BG_CARD).pack(side=tk.LEFT)

        # Eject button
        if not degraded:
            eject_btn = tk.Button(
                header, text="Safe Eject", font=("Arial", 9),
                bg=ACCENT, fg=WHITE, activebackground=RED, activeforeground=WHITE,
                relief=tk.FLAT, padx=8, pady=2,
                command=lambda: self.on_eject(drive) if self.on_eject else None,
            )
            eject_btn.pack(side=tk.RIGHT, padx=4)

        # Details
        details = tk.Frame(self, bg=BG_CARD)
        details.pack(fill=tk.X, pady=(4, 0))

        info_text = f"VHD \u2192 {mount}:\\   |   {swap_gb:.1f} GB swap   |   {speed:.0f} MB/s"
        tk.Label(details, text=info_text, font=("Consolas", 9),
                 fg=GRAY, bg=BG_CARD).pack(side=tk.LEFT)

        # Usage bar
        if not degraded:
            bar_frame = tk.Frame(self, bg=BG_CARD)
            bar_frame.pack(fill=tk.X, pady=(4, 0))

            usage_pct = min(100, (usage_mb / max(1, swap_gb * 1024)) * 100)
            bar_color = GREEN if usage_pct < 30 else (ORANGE if usage_pct < 70 else RED)

            tk.Label(bar_frame, text=f"PF Usage: {usage_mb} MB ({usage_pct:.0f}%)",
                     font=("Consolas", 8), fg=bar_color, bg=BG_CARD).pack(side=tk.LEFT)


class HostUI:
    """主機模式的裝置管理 UI。"""

    REFRESH_INTERVAL = 5000  # 5 秒更新一次

    def __init__(self):
        self._engine = None  # RealBoostEngine or VhdPagefileEngine
        self._root = None
        self._running = False
        self._device_cards: List[DeviceCard] = []
        self._engine_lock = threading.Lock()

    def run(self):
        """啟動 UI 主迴圈。"""
        self._root = tk.Tk()
        self._root.title("VRAM Booster \u2014 Host Mode")
        self._root.configure(bg=BG)
        self._root.geometry("480x600")
        self._root.resizable(False, True)
        self._root.protocol("WM_DELETE_WINDOW", self._quit)
        self._running = True

        self._build_ui()
        self._auto_activate()
        self._schedule_refresh()

        self._root.mainloop()

    def _build_ui(self):
        """建構 UI 元件。"""
        # Title bar
        title_frame = tk.Frame(self._root, bg=ACCENT, padx=16, pady=12)
        title_frame.pack(fill=tk.X)
        tk.Label(title_frame, text="VRAM Booster", font=("Arial", 18, "bold"),
                 fg=WHITE, bg=ACCENT).pack(side=tk.LEFT)
        tk.Label(title_frame, text="HOST MODE", font=("Arial", 10),
                 fg=ORANGE, bg=ACCENT).pack(side=tk.RIGHT)

        # System info section
        self._sys_frame = tk.Frame(self._root, bg=BG, padx=16, pady=8)
        self._sys_frame.pack(fill=tk.X)

        self._ram_label = tk.Label(self._sys_frame, text="RAM: ...",
                                    font=("Consolas", 10), fg=WHITE, bg=BG)
        self._ram_label.pack(anchor=tk.W)

        self._pf_label = tk.Label(self._sys_frame, text="System PF: ...",
                                   font=("Consolas", 10), fg=WHITE, bg=BG)
        self._pf_label.pack(anchor=tk.W)

        self._total_label = tk.Label(self._sys_frame, text="Total Expansion: ...",
                                      font=("Consolas", 12, "bold"), fg=GREEN, bg=BG)
        self._total_label.pack(anchor=tk.W, pady=(4, 0))

        # Separator
        ttk.Separator(self._root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=16)

        # Section header
        header_frame = tk.Frame(self._root, bg=BG, padx=16, pady=8)
        header_frame.pack(fill=tk.X)
        tk.Label(header_frame, text="External Devices", font=("Arial", 12, "bold"),
                 fg=WHITE, bg=BG).pack(side=tk.LEFT)

        self._status_label = tk.Label(header_frame, text="scanning...",
                                       font=("Arial", 9), fg=GRAY, bg=BG)
        self._status_label.pack(side=tk.RIGHT)

        # Scrollable device list
        self._device_frame = tk.Frame(self._root, bg=BG, padx=16)
        self._device_frame.pack(fill=tk.BOTH, expand=True)

        # Bottom bar with manual scan button
        bottom = tk.Frame(self._root, bg=ACCENT, padx=16, pady=8)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Button(bottom, text="Scan Devices", font=("Arial", 9),
                  bg=BG_CARD, fg=WHITE, relief=tk.FLAT, padx=12,
                  command=self._manual_scan).pack(side=tk.LEFT)

        tk.Button(bottom, text="Deactivate All", font=("Arial", 9),
                  bg=RED, fg=WHITE, relief=tk.FLAT, padx=12,
                  command=self._deactivate_all).pack(side=tk.RIGHT)

    def _auto_activate(self):
        """背景自動偵測裝置並啟動。"""
        def do():
            with self._engine_lock:
                try:
                    from .core.real_boost import RealBoostEngine
                    engine = RealBoostEngine()

                    # 掃描所有外接裝置（不限定單一磁碟）
                    # 從 scan 找到的第一個磁碟代號開始
                    drives = engine._scan_external_drives("")
                    if not drives:
                        self._update_status("No external devices found")
                        return

                    primary = drives[0]
                    result = engine.activate(
                        primary, use_percent=80.0,
                        on_progress=lambda msg: self._update_status(msg),
                    )

                    if result.get("success"):
                        self._engine = engine
                        self._update_status(f"Active: {result.get('method', 'unknown')}")
                    else:
                        self._update_status(f"Failed: {result.get('error', '?')}")

                except Exception as e:
                    logger.error("Auto-activate failed: %s", e)
                    self._update_status(f"Error: {e}")

        threading.Thread(target=do, daemon=True).start()

    def _schedule_refresh(self):
        """定期更新 UI。"""
        if not self._running:
            return
        self._refresh_display()
        self._root.after(self.REFRESH_INTERVAL, self._schedule_refresh)

    def _refresh_display(self):
        """更新系統資訊和裝置列表。"""
        # Update system info
        try:
            from .core.real_boost import RealBoostEngine
            mem = RealBoostEngine.get_system_memory()
            ram_gb = mem.get("physical_total", 0) / (1024**3)
            pf_gb = mem.get("swap_total", 0) / (1024**3)
            avail_gb = mem.get("physical_available", 0) / (1024**3)

            self._ram_label.config(text=f"RAM: {ram_gb:.1f} GB  (Available: {avail_gb:.1f} GB)")
            self._pf_label.config(text=f"System Pagefile: {pf_gb:.1f} GB")
        except Exception:
            pass

        # Update device cards
        if self._engine:
            status = self._engine.status()
            self._update_device_cards(status)

            # Total expansion
            added = status.get("swap_size_gb", status.get("real_swap_gb", 0))
            method = status.get("method", "none")
            system_wide = status.get("system_wide", False)
            scope = "system-wide" if system_wide else "process-only"
            self._total_label.config(
                text=f"+ {added:.1f} GB via {method} ({scope})",
                fg=GREEN if system_wide else ORANGE,
            )

    def _update_device_cards(self, status: Dict):
        """更新裝置卡片列表。"""
        # Clear existing cards
        for card in self._device_cards:
            card.destroy()
        self._device_cards.clear()

        # Get device list from either VHD or mmap status
        devices = (status.get("vhd_devices") or
                   status.get("linux_devices") or
                   status.get("devices") or [])

        if not devices:
            lbl = tk.Label(self._device_frame, text="No active devices",
                          font=("Consolas", 10), fg=GRAY, bg=BG)
            lbl.pack(pady=20)
            self._device_cards.append(lbl)
            return

        for dev_info in devices:
            card = DeviceCard(
                self._device_frame, dev_info,
                on_eject=self._safe_eject_device,
            )
            card.pack(fill=tk.X, pady=4)
            self._device_cards.append(card)

        active_count = sum(1 for d in devices if not d.get("degraded"))
        self._status_label.config(text=f"{active_count}/{len(devices)} active")

    def _safe_eject_device(self, drive_letter: str):
        """安全彈出指定裝置。"""
        if not self._engine:
            return

        # Confirm dialog
        if not messagebox.askyesno(
            "Safe Eject",
            f"Safely eject device {drive_letter}:\\?\n\n"
            f"Windows will migrate any active pages before removal.",
            parent=self._root,
        ):
            return

        self._update_status(f"Ejecting {drive_letter}:\\...")

        def do():
            try:
                # Use VHD engine's safe_remove if available
                if hasattr(self._engine, '_vhd_engine') and self._engine._vhd_engine:
                    result = self._engine._vhd_engine.safe_remove_device(
                        drive_letter,
                        on_progress=lambda msg: self._update_status(msg),
                    )
                elif hasattr(self._engine, '_linux_swap_engine') and self._engine._linux_swap_engine:
                    result = self._engine._linux_swap_engine.safe_remove_device(
                        drive_letter,
                        on_progress=lambda msg: self._update_status(msg),
                    )
                else:
                    result = {"success": False, "message": "No active engine"}

                if result.get("success"):
                    self._root.after(0, lambda: messagebox.showinfo(
                        "Safe Eject",
                        f"Device {drive_letter}:\\ safely ejected.\n"
                        f"You can now remove the device.",
                        parent=self._root))
                    self._update_status(f"{drive_letter}:\\ ejected")
                else:
                    self._root.after(0, lambda: messagebox.showwarning(
                        "Eject Warning",
                        f"Could not safely eject {drive_letter}:\\.\n"
                        f"{result.get('message', 'Unknown error')}",
                        parent=self._root))

            except Exception as e:
                logger.error("Safe eject error: %s", e)
                self._update_status(f"Eject error: {e}")

        threading.Thread(target=do, daemon=True).start()

    def _manual_scan(self):
        """手動重新掃描裝置。"""
        self._update_status("Scanning...")
        if self._engine:
            # Trigger hot-detect scan
            try:
                self._engine._hot_detect_scan()
                self._update_status("Scan complete")
            except Exception as e:
                self._update_status(f"Scan error: {e}")
        else:
            self._auto_activate()

    def _deactivate_all(self):
        """停用所有擴充。"""
        if not messagebox.askyesno(
            "Deactivate All",
            "Remove all memory expansion?\n"
            "Active pages will be migrated back to system pagefile.",
            parent=self._root,
        ):
            return

        def do():
            if self._engine:
                self._engine.deactivate()
                self._engine = None
                self._update_status("All deactivated")

        threading.Thread(target=do, daemon=True).start()

    def _update_status(self, msg: str):
        """Thread-safe status update."""
        if self._root and self._running:
            self._root.after(0, lambda: self._status_label.config(text=msg))

    def _quit(self):
        """關閉 UI，安全清理。"""
        self._running = False

        def shutdown():
            with self._engine_lock:
                if self._engine:
                    try:
                        self._engine.deactivate()
                    except Exception as e:
                        logger.warning("Deactivate error: %s", e)
                    self._engine = None

            # Close log handlers
            for handler in logging.root.handlers[:]:
                try:
                    handler.close()
                    logging.root.removeHandler(handler)
                except Exception:
                    pass

        t = threading.Thread(target=shutdown, daemon=True)
        t.start()
        t.join(timeout=15)

        if self._root:
            self._root.destroy()


def main():
    """Host mode entry point."""
    import atexit

    def cleanup():
        for handler in logging.root.handlers[:]:
            try:
                handler.close()
                logging.root.removeHandler(handler)
            except Exception:
                pass

    atexit.register(cleanup)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Check admin
    if platform.system().lower() == "windows":
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            # Re-launch as admin
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable,
                " ".join([f'"{a}"' for a in sys.argv]), None, 1)
            sys.exit(0)

    app = HostUI()
    app.run()


if __name__ == "__main__":
    main()
