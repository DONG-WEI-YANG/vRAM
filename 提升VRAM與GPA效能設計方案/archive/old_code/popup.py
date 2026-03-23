"""
隨插即用確認彈窗 — SD 卡插入後自動彈出
製作者：Peter Yang | 純 tkinter，零依賴
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional


def show_boost_prompt(report) -> bool:
    """
    顯示 VRAM 擴展確認彈窗
    回傳 True = 使用者同意啟動擴展
    回傳 False = 使用者拒絕
    """
    result = {"accepted": False}

    root = tk.Tk()
    root.title("SD-VRAM Booster")
    root.configure(bg="#1a1a2e")
    root.resizable(False, False)

    # 視窗置中
    w, h = 520, 480
    sx = root.winfo_screenwidth() // 2 - w // 2
    sy = root.winfo_screenheight() // 2 - h // 2
    root.geometry(f"{w}x{h}+{sx}+{sy}")

    # 保持在最上層
    root.attributes("-topmost", True)

    # ─── 標題區域 ────────────────────────────────────────────
    title_frame = tk.Frame(root, bg="#16213e", pady=12)
    title_frame.pack(fill="x")

    tk.Label(title_frame, text="SD-VRAM Booster",
             font=("Segoe UI", 18, "bold"), fg="#00d4ff",
             bg="#16213e").pack()
    tk.Label(title_frame, text="by Peter Yang",
             font=("Segoe UI", 9), fg="#888888",
             bg="#16213e").pack()

    # ─── 偵測結果區域 ────────────────────────────────────────
    info_frame = tk.Frame(root, bg="#1a1a2e", padx=20, pady=10)
    info_frame.pack(fill="both", expand=True)

    # OS 資訊
    _info_row(info_frame, "作業系統",
              report.os_info.os_name if report.os_info else "Unknown",
              row=0)

    # GPU 資訊
    gpu_text = report.gpus[0].name if report.gpus else "未偵測到"
    vram_text = f"{report.gpus[0].vram_total_mb} MB" if report.gpus else "N/A"
    _info_row(info_frame, "GPU", gpu_text, row=1)
    _info_row(info_frame, "VRAM", vram_text, row=2)

    # SD 卡資訊
    if report.sd_cards:
        sd = report.sd_cards[0]
        _info_row(info_frame, "SD 卡", sd.model_name, row=3)
        _info_row(info_frame, "容量", f"{sd.capacity_gb:.0f} GB (可用 {sd.free_gb:.0f} GB)", row=4)
        _info_row(info_frame, "模式", sd.mode.value, row=5)
        _info_row(info_frame, "可擴展 VRAM",
                  f"{sd.usable_for_vram_gb:.1f} GB",
                  row=6, highlight=True)
    else:
        _info_row(info_frame, "SD 卡", "未偵測到", row=3, warn=True)

    # ─── 狀態訊息 ────────────────────────────────────────────
    status_frame = tk.Frame(root, bg="#1a1a2e", padx=20, pady=5)
    status_frame.pack(fill="x")

    if report.can_boost:
        status_color = "#00e676"
        status_text = "✓ 系統相容，可以啟動 VRAM 擴展"
    else:
        status_color = "#ff5252"
        status_text = f"✗ {report.boost_reason}"

    tk.Label(status_frame, text=status_text,
             font=("Segoe UI", 11, "bold"), fg=status_color,
             bg="#1a1a2e", wraplength=460).pack()

    # ─── 按鈕區域 ────────────────────────────────────────────
    btn_frame = tk.Frame(root, bg="#1a1a2e", pady=15)
    btn_frame.pack(fill="x")

    def on_accept():
        result["accepted"] = True
        root.destroy()

    def on_decline():
        result["accepted"] = False
        root.destroy()

    if report.can_boost:
        accept_btn = tk.Button(
            btn_frame, text="  啟動 VRAM 擴展  ",
            font=("Segoe UI", 12, "bold"),
            fg="white", bg="#00c853", activebackground="#00e676",
            relief="flat", cursor="hand2", padx=20, pady=8,
            command=on_accept)
        accept_btn.pack(side="left", padx=(80, 10))

        decline_btn = tk.Button(
            btn_frame, text="  暫不使用  ",
            font=("Segoe UI", 11),
            fg="#aaaaaa", bg="#333333", activebackground="#444444",
            relief="flat", cursor="hand2", padx=15, pady=8,
            command=on_decline)
        decline_btn.pack(side="left", padx=10)
    else:
        close_btn = tk.Button(
            btn_frame, text="  關閉  ",
            font=("Segoe UI", 11),
            fg="white", bg="#555555", activebackground="#666666",
            relief="flat", cursor="hand2", padx=30, pady=8,
            command=on_decline)
        close_btn.pack()

    # 關閉視窗 = 拒絕
    root.protocol("WM_DELETE_WINDOW", on_decline)

    root.mainloop()
    return result["accepted"]


def _info_row(parent, label: str, value: str, row: int,
              highlight: bool = False, warn: bool = False):
    """建立一行資訊顯示"""
    tk.Label(parent, text=f"{label}:", font=("Segoe UI", 10),
             fg="#888888", bg="#1a1a2e", anchor="e", width=14
             ).grid(row=row, column=0, sticky="e", pady=3)

    fg = "#00d4ff" if highlight else ("#ff5252" if warn else "#e0e0e0")
    tk.Label(parent, text=value, font=("Segoe UI", 10, "bold" if highlight else ""),
             fg=fg, bg="#1a1a2e", anchor="w"
             ).grid(row=row, column=1, sticky="w", padx=(10, 0), pady=3)
