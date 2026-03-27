"""
VRAM Booster — Host Mode UI
=============================
主機模式：從 C:\\ 執行，管理所有外接裝置的記憶體擴充。

Features:
  - 即時偵測外接裝置插拔
  - 顯示每個裝置的 pagefile 狀態和使用量
  - Safety Policy 面板（智慧預設 + Advanced 滑桿）
  - 四階段安全彈出（preflight → drain → detach → ready）
  - 總記憶體擴充量顯示
  - 系統記憶體狀態概覽
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Dict, Any, Optional, List

from .core.safety_policy import SafetyPolicy, PolicyLimits, GLOBAL_POLICY_PATH
from .core.safe_removal import SafeRemovalManager, RemovalState, DrainProgress

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


class PolicyPanel(tk.Frame):
    """Collapsible safety policy panel with Advanced slider controls."""

    def __init__(self, parent, on_policy_changed=None, **kwargs):
        super().__init__(parent, bg=BG, **kwargs)
        self._expanded = False
        self._policy: Optional[PolicyLimits] = None
        self._capacity_gb: float = 0
        self._speed_mbs: float = 0
        self._on_policy_changed = on_policy_changed
        self._device_config_path: Optional[Path] = None
        self._sliders = {}
        self._build()

    def _build(self):
        # Header row
        header = tk.Frame(self, bg=BG)
        header.pack(fill=tk.X, padx=16, pady=(8, 0))

        tk.Label(header, text="Safety Policy", font=("Arial", 11, "bold"),
                 fg=WHITE, bg=BG).pack(side=tk.LEFT)

        self._toggle_btn = tk.Button(
            header, text="Advanced \u25bc", font=("Arial", 9),
            bg=BG, fg=GRAY, relief=tk.FLAT, padx=4,
            command=self._toggle_advanced,
        )
        self._toggle_btn.pack(side=tk.RIGHT)

        # Summary line
        self._summary_label = tk.Label(
            self, text="No device connected",
            font=("Consolas", 9), fg=GRAY, bg=BG,
        )
        self._summary_label.pack(anchor=tk.W, padx=16)

        # Advanced panel (hidden by default)
        self._adv_frame = tk.Frame(self, bg=BG_CARD, padx=12, pady=8)

        slider_defs = [
            ("device_reserved_gb", "Device Reserved", "GB"),
            ("pagefile_min_gb", "Pagefile Min", "GB"),
            ("pagefile_max_gb", "Pagefile Max", "GB"),
            ("system_ram_reserve_pct", "RAM Reserve", "%"),
        ]

        for key, label, unit in slider_defs:
            row = tk.Frame(self._adv_frame, bg=BG_CARD)
            row.pack(fill=tk.X, pady=2)

            tk.Label(row, text=f"{label}:", font=("Consolas", 9),
                     fg=WHITE, bg=BG_CARD, width=16, anchor=tk.W).pack(side=tk.LEFT)

            scale = tk.Scale(
                row, from_=0, to=100, orient=tk.HORIZONTAL,
                bg=BG_CARD, fg=WHITE, troughcolor=ACCENT,
                highlightthickness=0, length=160, showvalue=False,
                command=lambda val, k=key: self._on_slider_change(k, val),
            )
            scale.pack(side=tk.LEFT, padx=4)

            val_label = tk.Label(row, text="0", font=("Consolas", 9, "bold"),
                                 fg=GREEN, bg=BG_CARD, width=8)
            val_label.pack(side=tk.LEFT)

            self._sliders[key] = {"scale": scale, "label": val_label, "unit": unit}

        # Validation message
        self._validation_label = tk.Label(
            self._adv_frame, text="", font=("Arial", 8),
            fg=RED, bg=BG_CARD, wraplength=300, justify=tk.LEFT,
        )
        self._validation_label.pack(fill=tk.X, pady=(4, 0))

        # Buttons row
        btn_row = tk.Frame(self._adv_frame, bg=BG_CARD)
        btn_row.pack(fill=tk.X, pady=(4, 0))

        tk.Button(btn_row, text="Reset to Smart Defaults", font=("Arial", 8),
                  bg=ACCENT, fg=WHITE, relief=tk.FLAT, padx=6,
                  command=self._reset_defaults).pack(side=tk.LEFT)

        tk.Button(btn_row, text="Apply", font=("Arial", 8),
                  bg=GREEN, fg=WHITE, relief=tk.FLAT, padx=12,
                  command=self._apply).pack(side=tk.RIGHT)

    def update_policy(self, policy: PolicyLimits, capacity_gb: float,
                      speed_mbs: float, device_config_path=None):
        """Update panel with current policy values."""
        self._policy = policy
        self._capacity_gb = capacity_gb
        self._speed_mbs = speed_mbs
        self._device_config_path = device_config_path

        self._summary_label.config(
            text=f"Device reserve: {policy.device_reserved_gb:.0f} GB  |  "
                 f"PF: {policy.pagefile_min_gb:.0f}~{policy.pagefile_max_gb:.0f} GB",
            fg=WHITE,
        )

        self._update_slider("device_reserved_gb", 0, capacity_gb * 0.5,
                            policy.device_reserved_gb, "GB")
        self._update_slider("pagefile_min_gb", 0.5, policy.pagefile_max_gb,
                            policy.pagefile_min_gb, "GB")
        self._update_slider("pagefile_max_gb", policy.pagefile_min_gb,
                            capacity_gb * 0.9, policy.pagefile_max_gb, "GB")
        self._update_slider("system_ram_reserve_pct", 5, 50, 20, "%")

    def _update_slider(self, key, from_val, to_val, current, unit):
        s = self._sliders.get(key)
        if not s:
            return
        s["scale"].config(from_=from_val, to=to_val)
        s["scale"].set(current)
        s["label"].config(text=f"{current:.1f} {unit}")

    def _on_slider_change(self, key, val):
        s = self._sliders.get(key)
        if s:
            v = float(val)
            s["label"].config(text=f"{v:.1f} {s['unit']}")
        self._validate_current()

    def _validate_current(self):
        if not self._policy:
            return
        pf_max = float(self._sliders["pagefile_max_gb"]["scale"].get())
        pf_min = float(self._sliders["pagefile_min_gb"]["scale"].get())
        reserved = float(self._sliders["device_reserved_gb"]["scale"].get())

        errors = []
        if pf_min > pf_max:
            errors.append("Pagefile min > max")
        if reserved + pf_max > self._capacity_gb:
            errors.append(
                f"Reserved + PF max ({reserved + pf_max:.1f} GB) "
                f"> device capacity ({self._capacity_gb:.1f} GB)")

        self._validation_label.config(
            text="\n".join(errors) if errors else "",
            fg=RED if errors else GREEN,
        )

    def _toggle_advanced(self):
        self._expanded = not self._expanded
        if self._expanded:
            self._adv_frame.pack(fill=tk.X, padx=16, pady=(4, 8))
            self._toggle_btn.config(text="Advanced \u25b2")
        else:
            self._adv_frame.pack_forget()
            self._toggle_btn.config(text="Advanced \u25bc")

    def _reset_defaults(self):
        if self._capacity_gb > 0:
            defaults = SafetyPolicy.compute_smart_defaults(
                self._capacity_gb, self._speed_mbs)
            self.update_policy(defaults, self._capacity_gb,
                               self._speed_mbs, self._device_config_path)

    def _apply(self):
        """Save current slider values to global + device config."""
        overrides = {
            "device_reserved_gb": float(self._sliders["device_reserved_gb"]["scale"].get()),
            "pagefile_min_gb": float(self._sliders["pagefile_min_gb"]["scale"].get()),
            "pagefile_max_gb": float(self._sliders["pagefile_max_gb"]["scale"].get()),
        }
        ram_pct = float(self._sliders["system_ram_reserve_pct"]["scale"].get())

        global_overrides = dict(overrides)
        global_overrides["system_ram_reserve_pct"] = ram_pct
        SafetyPolicy.save_global_policy(GLOBAL_POLICY_PATH, global_overrides)

        if self._device_config_path:
            SafetyPolicy.save_device_override(
                Path(self._device_config_path), overrides)

        self._validation_label.config(text="Saved", fg=GREEN)

        if self._on_policy_changed:
            self._on_policy_changed(self._policy)


class DrainProgressCard(tk.Frame):
    """Shows drain progress during safe removal."""

    def __init__(self, parent, drive_letter: str,
                 on_force_eject=None, on_cancel=None, **kwargs):
        super().__init__(parent, bg=BG_CARD, padx=12, pady=8, **kwargs)
        self.drive_letter = drive_letter
        self._on_force_eject = on_force_eject
        self._on_cancel = on_cancel
        self._build()

    def _build(self):
        # Header
        header = tk.Frame(self, bg=BG_CARD)
        header.pack(fill=tk.X)

        self._status_label = tk.Label(
            header, text=f"  {self.drive_letter}:\\  [DRAINING...]",
            font=("Consolas", 14, "bold"), fg=ORANGE, bg=BG_CARD,
        )
        self._status_label.pack(side=tk.LEFT)

        # Progress info
        self._info_label = tk.Label(
            self, text="Draining pagefile...",
            font=("Consolas", 9), fg=WHITE, bg=BG_CARD,
        )
        self._info_label.pack(anchor=tk.W, pady=(4, 0))

        # Progress bar (canvas)
        bar_frame = tk.Frame(self, bg=BG_CARD)
        bar_frame.pack(fill=tk.X, pady=(4, 0))

        self._bar_canvas = tk.Canvas(
            bar_frame, height=16, bg=ACCENT, highlightthickness=0,
        )
        self._bar_canvas.pack(fill=tk.X)

        # Stats line
        self._stats_label = tk.Label(
            self, text="Speed: -- MB/s  |  ETA: --",
            font=("Consolas", 8), fg=GRAY, bg=BG_CARD,
        )
        self._stats_label.pack(anchor=tk.W, pady=(2, 0))

        # Timeout warning (hidden initially)
        self._warn_label = tk.Label(
            self, text="", font=("Arial", 8), fg=RED, bg=BG_CARD,
        )
        self._warn_label.pack(anchor=tk.W)

        # Buttons
        btn_row = tk.Frame(self, bg=BG_CARD)
        btn_row.pack(fill=tk.X, pady=(4, 0))

        tk.Button(
            btn_row, text="Force Eject", font=("Arial", 9),
            bg=RED, fg=WHITE, relief=tk.FLAT, padx=8,
            command=lambda: self._on_force_eject(self.drive_letter)
            if self._on_force_eject else None,
        ).pack(side=tk.LEFT, padx=4)

        tk.Button(
            btn_row, text="Cancel", font=("Arial", 9),
            bg=GRAY, fg=WHITE, relief=tk.FLAT, padx=8,
            command=lambda: self._on_cancel(self.drive_letter)
            if self._on_cancel else None,
        ).pack(side=tk.RIGHT, padx=4)

    def update_progress(self, progress: DrainProgress):
        """Update the progress display."""
        if progress.phase == "ready":
            self._status_label.config(
                text=f"  {self.drive_letter}:\\  [READY TO REMOVE]", fg=GREEN)
            self._info_label.config(text="VHD detached. Safe to unplug hardware.")
            self._stats_label.config(text="")
            self._draw_bar(1.0, GREEN)
            return

        if progress.phase == "detaching":
            self._status_label.config(
                text=f"  {self.drive_letter}:\\  [DETACHING...]", fg=ORANGE)
            self._info_label.config(text="Detaching VHD...")
            return

        # Draining
        done = progress.total_mb - progress.remaining_mb
        pct = done / max(1, progress.total_mb)

        self._info_label.config(
            text=f"Draining: {progress.remaining_mb:.0f} / {progress.total_mb:.0f} MB"
        )

        eta_str = (f"{progress.eta_seconds:.0f} sec"
                   if progress.eta_seconds < 999 else "calculating...")
        self._stats_label.config(
            text=f"Speed: {progress.drain_rate_mbs:.1f} MB/s  |  ETA: ~{eta_str}"
        )

        bar_color = GREEN if pct > 0.7 else (ORANGE if pct > 0.3 else RED)
        self._draw_bar(pct, bar_color)

    def show_timeout_warning(self):
        self._warn_label.config(text="Taking long. Consider Force Eject.")

    def _draw_bar(self, pct: float, color: str):
        self._bar_canvas.delete("all")
        w = self._bar_canvas.winfo_width() or 300
        filled = int(w * min(1.0, pct))
        self._bar_canvas.create_rectangle(0, 0, filled, 16, fill=color, outline="")


class HostUI:
    """主機模式的裝置管理 UI。"""

    REFRESH_INTERVAL = 5000  # 5 秒更新一次

    def __init__(self):
        self._engine = None  # RealBoostEngine or VhdPagefileEngine
        self._root = None
        self._running = False
        self._device_cards: List = []
        self._engine_lock = threading.Lock()
        self._removal_mgr = SafeRemovalManager()

    def run(self):
        """啟動 UI 主迴圈。"""
        self._root = tk.Tk()
        self._root.title("VRAM Booster \u2014 Host Mode")
        self._root.configure(bg=BG)
        self._root.geometry("480x720")
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

        # Safety Policy panel
        self._policy_panel = PolicyPanel(
            self._root, on_policy_changed=self._on_policy_update)
        self._policy_panel.pack(fill=tk.X)

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

        # Bottom bar
        bottom = tk.Frame(self._root, bg=ACCENT, padx=16, pady=8)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Button(bottom, text="Scan Devices", font=("Arial", 9),
                  bg=BG_CARD, fg=WHITE, relief=tk.FLAT, padx=12,
                  command=self._manual_scan).pack(side=tk.LEFT)

        tk.Button(bottom, text="Deactivate All", font=("Arial", 9),
                  bg=RED, fg=WHITE, relief=tk.FLAT, padx=12,
                  command=self._deactivate_all).pack(side=tk.RIGHT)

    # ── Policy ──────────────────────────────────────────────────────────

    def _on_policy_update(self, policy: PolicyLimits):
        """Called when user changes policy via Advanced panel."""
        logger.info("Policy updated: %s", policy)

    # ── Auto-activate ───────────────────────────────────────────────────

    def _auto_activate(self):
        """背景自動偵測裝置並啟動。"""
        def do():
            with self._engine_lock:
                try:
                    from .core.real_boost import RealBoostEngine
                    engine = RealBoostEngine()

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
                        # Wire up removal manager
                        if hasattr(engine, '_vhd_engine') and engine._vhd_engine:
                            self._removal_mgr.set_vhd_engine(engine._vhd_engine)
                        self._update_status(f"Active: {result.get('method', 'unknown')}")
                    else:
                        self._update_status(f"Failed: {result.get('error', '?')}")

                except Exception as e:
                    logger.error("Auto-activate failed: %s", e)
                    self._update_status(f"Error: {e}")

        threading.Thread(target=do, daemon=True).start()

    # ── Refresh ─────────────────────────────────────────────────────────

    def _schedule_refresh(self):
        """定期更新 UI。"""
        if not self._running:
            return
        self._refresh_display()
        self._root.after(self.REFRESH_INTERVAL, self._schedule_refresh)

    def _refresh_display(self):
        """更新系統資訊和裝置列表。"""
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

        if self._engine:
            status = self._engine.status()
            self._update_device_cards(status)

            added = status.get("swap_size_gb", status.get("real_swap_gb", 0))
            method = status.get("method", "none")
            system_wide = status.get("system_wide", False)
            scope = "system-wide" if system_wide else "process-only"
            self._total_label.config(
                text=f"+ {added:.1f} GB via {method} ({scope})",
                fg=GREEN if system_wide else ORANGE,
            )

            # Update policy panel
            self._refresh_policy_panel()

    def _refresh_policy_panel(self):
        """Feed device data into the policy panel."""
        try:
            drives = getattr(self._engine, '_known_drives', set())
            if drives:
                drive = next(iter(drives))
                usage = shutil.disk_usage(f"{drive}:\\")
                capacity_gb = usage.total / (1024 ** 3)
                speed = getattr(self._engine, '_measured_rand_write_mbs', 500.0)
                device_config = Path(f"{drive}:\\") / self._engine.CONFIG_FILENAME

                policy = SafetyPolicy.load_merged_policy(
                    GLOBAL_POLICY_PATH, device_config, capacity_gb, speed)
                self._policy_panel.update_policy(
                    policy, capacity_gb, speed, device_config)
        except Exception:
            pass

    # ── Device Cards ────────────────────────────────────────────────────

    def _update_device_cards(self, status: Dict):
        """更新裝置卡片列表。"""
        # Don't refresh if a drain card is showing
        if any(isinstance(c, DrainProgressCard) for c in self._device_cards):
            return

        for card in self._device_cards:
            card.destroy()
        self._device_cards.clear()

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

    # ── Safe Eject (four-phase drain flow) ──────────────────────────────

    def _safe_eject_device(self, drive_letter: str):
        """Safe eject with four-phase drain flow."""
        if not self._engine:
            return

        # Find mount letter for this drive
        mount_letter = None
        if hasattr(self._engine, '_vhd_engine') and self._engine._vhd_engine:
            vhd = self._engine._vhd_engine
            with vhd._lock:
                for dev in vhd._devices:
                    if dev.drive_letter == drive_letter:
                        mount_letter = dev.mount_letter
                        break

        if not mount_letter:
            messagebox.showwarning("Eject", f"Device {drive_letter}:\\ not found",
                                   parent=self._root)
            return

        # Load policy for preflight
        policy = PolicyLimits(
            device_reserved_gb=4.0, pagefile_min_gb=1.0,
            pagefile_max_gb=25.0, system_ram_reserve_gb=6.4)
        if self._policy_panel._policy:
            policy = self._policy_panel._policy

        # Phase 0: Preflight
        result = self._removal_mgr.preflight_check(mount_letter, policy)

        if result.warnings:
            warn_text = "\n".join(result.warnings)
            if not messagebox.askyesno(
                "Eject Warning",
                f"Warnings:\n{warn_text}\n\nContinue with eject?",
                parent=self._root,
            ):
                return

        if result.can_remove_immediately:
            # No drain needed — direct detach
            self._removal_mgr.force_eject(mount_letter)
            messagebox.showinfo("Safe Eject",
                                f"Device {drive_letter}:\\ safely ejected.\n"
                                f"You can now remove the device.",
                                parent=self._root)
            self._update_status(f"{drive_letter}:\\ ejected")
            return

        # Phase 1: Start drain with progress UI
        self._show_drain_card(drive_letter, mount_letter)

    def _show_drain_card(self, drive_letter: str, mount_letter: str):
        """Replace device cards with a drain progress card."""
        for card in self._device_cards:
            card.destroy()
        self._device_cards.clear()

        drain_card = DrainProgressCard(
            self._device_frame, drive_letter,
            on_force_eject=lambda dl: self._handle_force_eject(mount_letter),
            on_cancel=lambda dl: self._handle_cancel_drain(mount_letter),
        )
        drain_card.pack(fill=tk.X, pady=4)
        self._device_cards.append(drain_card)

        def on_progress(progress: DrainProgress):
            if self._root and self._running:
                self._root.after(0, lambda: drain_card.update_progress(progress))

        def on_complete():
            if self._root and self._running:
                self._root.after(0, lambda: messagebox.showinfo(
                    "Safe Eject",
                    f"Device {drive_letter}:\\ safely ejected.\n"
                    f"You can now remove the device.",
                    parent=self._root))
                self._update_status(f"{drive_letter}:\\ ejected")

        def on_timeout():
            if self._root and self._running:
                self._root.after(0, drain_card.show_timeout_warning)

        self._removal_mgr.start_drain(
            mount_letter,
            on_progress=on_progress,
            on_complete=on_complete,
            on_timeout_warn=on_timeout,
        )

    def _handle_force_eject(self, mount_letter: str):
        """Force eject with confirmation dialog."""
        if not messagebox.askyesno(
            "Force Eject",
            "Force eject may cause running processes to crash.\n\n"
            "The pagefile contains swap data, not your files.\n"
            "Proceed?",
            icon="warning", parent=self._root,
        ):
            return

        self._removal_mgr.force_eject(mount_letter)
        self._update_status("Force ejected")

    def _handle_cancel_drain(self, mount_letter: str):
        """Cancel drain and restore pagefile."""
        self._removal_mgr.cancel_drain(mount_letter)
        self._update_status("Eject cancelled")

    # ── Other Controls ──────────────────────────────────────────────────

    def _manual_scan(self):
        """手動重新掃描裝置。"""
        self._update_status("Scanning...")
        if self._engine:
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
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable,
                " ".join([f'"{a}"' for a in sys.argv]), None, 1)
            sys.exit(0)

    app = HostUI()
    app.run()


if __name__ == "__main__":
    main()
