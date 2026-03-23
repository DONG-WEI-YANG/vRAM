"""
SD-VRAM Booster 即時監控儀表板
製作者：Peter Yang | 純 tkinter，零依賴
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
from typing import Optional


class DashboardApp:
    """
    即時監控儀表板
    顯示 GPU VRAM、SD 卡狀態、記憶體分層、Context Window 預估
    """

    BG = "#0f0f23"
    BG2 = "#1a1a2e"
    BG3 = "#16213e"
    FG = "#e0e0e0"
    ACCENT = "#00d4ff"
    GREEN = "#00e676"
    ORANGE = "#ffab00"
    RED = "#ff5252"
    GRAY = "#666666"

    def __init__(self, booster):
        self.booster = booster
        self.root: Optional[tk.Tk] = None
        self._running = False
        self._update_interval = 2000  # ms

        # GUI 元件參考
        self._tier_bars = []
        self._log_text = None
        self._status_label = None
        self._boost_btn = None
        self._ctx_labels = {}

    def run(self):
        """啟動儀表板"""
        self.root = tk.Tk()
        self.root.title("SD-VRAM Booster Dashboard — by Peter Yang")
        self.root.configure(bg=self.BG)
        self.root.minsize(900, 640)

        # 視窗置中
        w, h = 960, 700
        sx = self.root.winfo_screenwidth() // 2 - w // 2
        sy = self.root.winfo_screenheight() // 2 - h // 2
        self.root.geometry(f"{w}x{h}+{sx}+{sy}")

        self._build_ui()
        self._running = True
        self._schedule_update()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _build_ui(self):
        """建構 UI 佈局"""
        # ─── 頂部標題列 ─────────────────────────────────────
        header = tk.Frame(self.root, bg=self.BG3, pady=8)
        header.pack(fill="x")

        tk.Label(header, text="SD-VRAM Booster",
                 font=("Segoe UI", 16, "bold"), fg=self.ACCENT,
                 bg=self.BG3).pack(side="left", padx=15)

        tk.Label(header, text="by Peter Yang",
                 font=("Segoe UI", 9), fg=self.GRAY,
                 bg=self.BG3).pack(side="left", padx=5)

        self._status_label = tk.Label(
            header, text="● 掃描中...",
            font=("Segoe UI", 10, "bold"), fg=self.ORANGE,
            bg=self.BG3)
        self._status_label.pack(side="right", padx=15)

        # ─── 主內容區 ───────────────────────────────────────
        main = tk.Frame(self.root, bg=self.BG)
        main.pack(fill="both", expand=True, padx=10, pady=5)

        # 左側面板：記憶體分層
        left = tk.Frame(main, bg=self.BG, width=480)
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))

        self._build_tier_panel(left)
        self._build_context_panel(left)

        # 右側面板：系統資訊 + 日誌
        right = tk.Frame(main, bg=self.BG, width=440)
        right.pack(side="right", fill="both", expand=True, padx=(5, 0))

        self._build_info_panel(right)
        self._build_log_panel(right)

        # ─── 底部控制列 ─────────────────────────────────────
        footer = tk.Frame(self.root, bg=self.BG2, pady=8)
        footer.pack(fill="x")

        self._boost_btn = tk.Button(
            footer, text="  啟動 VRAM 擴展  ",
            font=("Segoe UI", 11, "bold"),
            fg="white", bg="#00c853", activebackground="#00e676",
            relief="flat", cursor="hand2", padx=20, pady=5,
            command=self._toggle_boost)
        self._boost_btn.pack(side="left", padx=15)

        tk.Button(
            footer, text="  重新掃描  ",
            font=("Segoe UI", 10),
            fg=self.FG, bg="#333333", activebackground="#444444",
            relief="flat", cursor="hand2", padx=15, pady=5,
            command=self._rescan).pack(side="left", padx=5)

        tk.Button(
            footer, text="  匯出報告  ",
            font=("Segoe UI", 10),
            fg=self.FG, bg="#333333", activebackground="#444444",
            relief="flat", cursor="hand2", padx=15, pady=5,
            command=self._export_report).pack(side="left", padx=5)

    # ─── 記憶體分層面板 ─────────────────────────────────────

    def _build_tier_panel(self, parent):
        """記憶體分層視覺化"""
        frame = tk.LabelFrame(parent, text=" 記憶體分層架構 ",
                              font=("Segoe UI", 11, "bold"),
                              fg=self.ACCENT, bg=self.BG2,
                              labelanchor="nw", padx=10, pady=8)
        frame.pack(fill="x", pady=(0, 5))

        self._tier_bars = []
        tier_configs = [
            ("Tier 0: GPU VRAM", self.GREEN, "最快 ~900 GB/s"),
            ("Tier 1: System RAM", self.ORANGE, "中等 ~50 GB/s"),
            ("Tier 2: SD Express", self.ACCENT, "擴展 ~1-4 GB/s"),
        ]

        for i, (name, color, speed) in enumerate(tier_configs):
            row = tk.Frame(frame, bg=self.BG2)
            row.pack(fill="x", pady=4)

            tk.Label(row, text=name, font=("Segoe UI", 9, "bold"),
                     fg=color, bg=self.BG2, width=20, anchor="w"
                     ).pack(side="left")

            # 進度條容器
            bar_frame = tk.Frame(row, bg="#333333", height=20)
            bar_frame.pack(side="left", fill="x", expand=True, padx=5)
            bar_frame.pack_propagate(False)

            bar_fill = tk.Frame(bar_frame, bg=color, width=0)
            bar_fill.place(x=0, y=0, relheight=1.0)

            bar_label = tk.Label(bar_frame, text="-- / -- GB",
                                 font=("Segoe UI", 8), fg="white",
                                 bg="#333333")
            bar_label.place(relx=0.5, rely=0.5, anchor="center")

            tk.Label(row, text=speed, font=("Segoe UI", 8),
                     fg=self.GRAY, bg=self.BG2, width=16, anchor="e"
                     ).pack(side="right")

            self._tier_bars.append({
                "frame": bar_frame, "fill": bar_fill,
                "label": bar_label, "color": color
            })

    # ─── Context Window 預估面板 ─────────────────────────────

    def _build_context_panel(self, parent):
        """Context Window 提升預估"""
        frame = tk.LabelFrame(parent, text=" Context Window 預估 ",
                              font=("Segoe UI", 11, "bold"),
                              fg=self.ACCENT, bg=self.BG2,
                              labelanchor="nw", padx=10, pady=8)
        frame.pack(fill="x", pady=(0, 5))

        models = ["Llama-3-8B", "Qwen-2.5-32B", "Llama-3-70B"]
        for i, model in enumerate(models):
            row = tk.Frame(frame, bg=self.BG2)
            row.pack(fill="x", pady=2)

            tk.Label(row, text=model, font=("Segoe UI", 9),
                     fg=self.FG, bg=self.BG2, width=16, anchor="w"
                     ).pack(side="left")

            lbl_before = tk.Label(row, text="--K", font=("Segoe UI", 9),
                                  fg=self.GRAY, bg=self.BG2, width=10)
            lbl_before.pack(side="left")

            tk.Label(row, text="→", font=("Segoe UI", 9),
                     fg=self.GRAY, bg=self.BG2).pack(side="left", padx=3)

            lbl_after = tk.Label(row, text="--K", font=("Segoe UI", 9, "bold"),
                                 fg=self.GREEN, bg=self.BG2, width=10)
            lbl_after.pack(side="left")

            lbl_boost = tk.Label(row, text="(--x)", font=("Segoe UI", 9, "bold"),
                                 fg=self.ACCENT, bg=self.BG2, width=8)
            lbl_boost.pack(side="left")

            self._ctx_labels[model] = {
                "before": lbl_before, "after": lbl_after, "boost": lbl_boost
            }

    # ─── 系統資訊面板 ───────────────────────────────────────

    def _build_info_panel(self, parent):
        """系統資訊"""
        frame = tk.LabelFrame(parent, text=" 系統資訊 ",
                              font=("Segoe UI", 11, "bold"),
                              fg=self.ACCENT, bg=self.BG2,
                              labelanchor="nw", padx=10, pady=8)
        frame.pack(fill="x", pady=(0, 5))

        self._info_labels = {}
        fields = ["作業系統", "GPU", "VRAM", "SD 卡", "SD 模式", "擴展狀態"]
        for i, field in enumerate(fields):
            row = tk.Frame(frame, bg=self.BG2)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{field}:", font=("Segoe UI", 9),
                     fg=self.GRAY, bg=self.BG2, width=10, anchor="e"
                     ).pack(side="left")
            lbl = tk.Label(row, text="掃描中...", font=("Segoe UI", 9),
                           fg=self.FG, bg=self.BG2, anchor="w")
            lbl.pack(side="left", padx=(8, 0))
            self._info_labels[field] = lbl

    # ─── 日誌面板 ───────────────────────────────────────────

    def _build_log_panel(self, parent):
        """即時日誌"""
        frame = tk.LabelFrame(parent, text=" 即時日誌 ",
                              font=("Segoe UI", 11, "bold"),
                              fg=self.ACCENT, bg=self.BG2,
                              labelanchor="nw", padx=5, pady=5)
        frame.pack(fill="both", expand=True)

        self._log_text = tk.Text(
            frame, bg="#0a0a1a", fg="#00ff88",
            font=("Consolas", 9), relief="flat",
            wrap="word", state="disabled",
            insertbackground=self.GREEN)
        self._log_text.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(self._log_text, command=self._log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self._log_text.configure(yscrollcommand=scrollbar.set)

    # ─── 更新邏輯 ───────────────────────────────────────────

    def _schedule_update(self):
        """定期更新 UI"""
        if not self._running:
            return
        self._update_ui()
        self.root.after(self._update_interval, self._schedule_update)

    def _update_ui(self):
        """更新所有 UI 元件"""
        status = self.booster.get_full_status()
        report = status.get("system_report")
        tiers_data = status.get("memory_tiers", {})
        tiers = tiers_data.get("tiers", [])

        # 更新狀態標籤
        if status["is_boosted"]:
            self._status_label.configure(text="● VRAM 擴展運作中", fg=self.GREEN)
            self._boost_btn.configure(text="  停用 VRAM 擴展  ", bg=self.RED)
        else:
            self._status_label.configure(text="● 待命中", fg=self.ORANGE)
            self._boost_btn.configure(text="  啟動 VRAM 擴展  ", bg="#00c853")

        # 更新記憶體分層條
        for i, tier_bar in enumerate(self._tier_bars):
            if i < len(tiers):
                t = tiers[i]
                cap = t["capacity_mb"]
                used = t["used_mb"]
                free = t["free_mb"]
                pct = t["usage_percent"]

                # 更新進度條
                bar_width = tier_bar["frame"].winfo_width()
                fill_width = max(1, int(bar_width * pct / 100))
                tier_bar["fill"].place(x=0, y=0, width=fill_width, relheight=1.0)

                # 更新文字
                cap_gb = cap / 1024
                used_gb = used / 1024
                tier_bar["label"].configure(
                    text=f"{used_gb:.1f} / {cap_gb:.1f} GB ({pct:.0f}%)")
            else:
                tier_bar["label"].configure(text="未偵測")

        # 更新系統資訊
        if report:
            os_name = report.get("os", {}).get("os_name", "Unknown")
            self._info_labels["作業系統"].configure(text=os_name)

            gpus = report.get("gpus", [])
            if gpus:
                self._info_labels["GPU"].configure(text=gpus[0].get("name", "N/A"))
                vram = gpus[0].get("vram_total_mb", 0)
                self._info_labels["VRAM"].configure(text=f"{vram} MB ({vram/1024:.1f} GB)")
            else:
                self._info_labels["GPU"].configure(text="未偵測到", fg=self.RED)

            sds = report.get("sd_cards", [])
            if sds:
                self._info_labels["SD 卡"].configure(
                    text=f"{sds[0].get('model_name', 'SD')} ({sds[0].get('capacity_gb', 0):.0f}GB)")
                self._info_labels["SD 模式"].configure(text=sds[0].get("mode", "Unknown"))
            else:
                self._info_labels["SD 卡"].configure(text="未偵測到", fg=self.RED)

            boost_status = "✓ 擴展中" if status["is_boosted"] else report.get("boost_reason", "待命")
            color = self.GREEN if status["is_boosted"] else self.ORANGE
            self._info_labels["擴展狀態"].configure(text=boost_status, fg=color)

        # 更新 Context Window 預估
        for model_name, labels in self._ctx_labels.items():
            ctx = self.booster.calculate_context_window_boost(model_name)
            before = ctx["vram_only_context"]
            after = ctx["boosted_context"]
            ratio = ctx["boost_ratio"]

            before_str = self._format_tokens(before)
            after_str = self._format_tokens(after)

            labels["before"].configure(text=before_str)
            labels["after"].configure(text=after_str)
            labels["boost"].configure(text=f"({ratio:.0f}x)")

        # 更新日誌
        self._update_log(status.get("log", []))

    def _update_log(self, log_entries: list):
        """更新日誌面板"""
        if not self._log_text:
            return
        self._log_text.configure(state="normal")
        current = self._log_text.get("1.0", "end-1c")
        new_text = "\n".join(log_entries)
        if new_text != current:
            self._log_text.delete("1.0", "end")
            self._log_text.insert("1.0", new_text)
            self._log_text.see("end")
        self._log_text.configure(state="disabled")

    # ─── 按鈕事件 ───────────────────────────────────────────

    def _toggle_boost(self):
        """切換 VRAM 擴展"""
        if self.booster._is_boosted:
            result = self.booster.deactivate_boost()
        else:
            result = self.booster.activate_boost()

        if not result["success"]:
            messagebox.showwarning("SD-VRAM Booster", result["message"])

    def _rescan(self):
        """重新掃描系統"""
        def scan():
            self.booster.scan_system()
        threading.Thread(target=scan, daemon=True).start()

    def _export_report(self):
        """匯出報告"""
        try:
            import json
            report_json = json.dumps(
                self.booster.get_full_status(),
                indent=2, ensure_ascii=False, default=str)

            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
                title="匯出系統報告")
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(report_json)
                messagebox.showinfo("SD-VRAM Booster", f"報告已匯出至:\n{path}")
        except Exception as e:
            messagebox.showerror("錯誤", str(e))

    def _on_close(self):
        """關閉視窗"""
        self._running = False
        if self.booster._is_boosted:
            if messagebox.askyesno("SD-VRAM Booster",
                                   "VRAM 擴展仍在運作中，是否停用後關閉？"):
                self.booster.deactivate_boost()
        self.root.destroy()

    @staticmethod
    def _format_tokens(n: int) -> str:
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        elif n >= 1_000:
            return f"{n/1_000:.0f}K"
        return str(n)
