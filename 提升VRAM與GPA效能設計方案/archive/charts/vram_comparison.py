#!/usr/bin/env python3
"""
SD-VRAM vs USB-VRAM 架構 VRAM 擴展能力完整對比分析
製作者：DONG. WEI YANG
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.style as mplstyle
mplstyle.use('seaborn-v0_8-darkgrid')
import matplotlib.font_manager as fm
import numpy as np

# 設定中文字體
plt.rcParams['font.family'] = 'Noto Sans CJK SC'
plt.rcParams['axes.unicode_minus'] = False

# ============================================================
# 物理數據定義（基於真實規格）
# ============================================================

# GPU 原生 VRAM
GPUS = {
    "RTX 3060":    {"vram_gb": 12, "bandwidth_gbs": 360},
    "RTX 4060":    {"vram_gb": 8,  "bandwidth_gbs": 272},
    "RTX 4070":    {"vram_gb": 12, "bandwidth_gbs": 504},
    "RTX 4080":    {"vram_gb": 16, "bandwidth_gbs": 717},
    "RTX 4090":    {"vram_gb": 24, "bandwidth_gbs": 1008},
    "RTX 5090":    {"vram_gb": 32, "bandwidth_gbs": 1792},
    "RX 7900 XTX": {"vram_gb": 24, "bandwidth_gbs": 960},
}

# SD 卡規格（真實頻寬）
SD_SPECS = {
    "SD Express Gen3 x1\n(985 MB/s)":   {"bw_mbs": 985,  "max_cap_gb": 128},
    "SD Express Gen3 x2\n(1,970 MB/s)": {"bw_mbs": 1970, "max_cap_gb": 256},
    "SD Express Gen4 x1\n(1,970 MB/s)": {"bw_mbs": 1970, "max_cap_gb": 512},
    "SD Express Gen4 x2\n(3,940 MB/s)": {"bw_mbs": 3940, "max_cap_gb": 1024},
}

# USB 規格（真實頻寬）
USB_SPECS = {
    "USB 3.2 Gen1\n(625 MB/s)":     {"bw_mbs": 625,   "max_cap_gb": 256},
    "USB 3.2 Gen2\n(1,250 MB/s)":   {"bw_mbs": 1250,  "max_cap_gb": 512},
    "USB 3.2 Gen2x2\n(2,500 MB/s)": {"bw_mbs": 2500,  "max_cap_gb": 2048},
    "USB4 v1\n(5,000 MB/s)":        {"bw_mbs": 5000,  "max_cap_gb": 2048},
    "USB4 v2\n(10,000 MB/s)":       {"bw_mbs": 10000, "max_cap_gb": 4096},
    "Thunderbolt 5\n(12,000 MB/s)": {"bw_mbs": 12000, "max_cap_gb": 4096},
}

# LLM 模型大小
MODELS = {
    "Llama-3 8B (Q4)":    {"size_gb": 4.5,  "kv_per_token_bytes": 131072},
    "Llama-3 8B (FP16)":  {"size_gb": 16,   "kv_per_token_bytes": 131072},
    "Mistral 7B (Q4)":    {"size_gb": 4.2,  "kv_per_token_bytes": 131072},
    "Llama-3 70B (Q4)":   {"size_gb": 40,   "kv_per_token_bytes": 655360},
    "Llama-3 70B (FP16)": {"size_gb": 140,  "kv_per_token_bytes": 655360},
    "Qwen-2 72B (Q4)":    {"size_gb": 42,   "kv_per_token_bytes": 655360},
}


def calc_vram_expansion(device_cap_gb, reserved_gb=1.0):
    """計算可用於 VRAM 擴展的容量"""
    return max(0, device_cap_gb * 0.9 - reserved_gb)


def calc_inference_tps(model_size_gb, vram_gb, ext_bw_mbs, ext_portion_gb):
    """計算推理速度 (tokens/s)"""
    if ext_portion_gb <= 0:
        return 18.0  # 純 VRAM 原生速度
    # 每次推理需從外部讀取溢出部分的一個 layer
    # 簡化模型：假設每 token 需讀取溢出部分的 1/32
    read_per_token_mb = (ext_portion_gb * 1024) / 32
    read_time_ms = read_per_token_mb / ext_bw_mbs * 1000
    compute_time_ms = 55  # GPU 計算時間
    return 1000 / (compute_time_ms + read_time_ms)


def calc_context_window(vram_gb, model_size_gb, ext_cap_gb, kv_per_token):
    """計算最大 Context Window"""
    vram_kv_space = max(0, vram_gb - model_size_gb)
    total_kv_space = vram_kv_space + ext_cap_gb
    return int(total_kv_space * 1024 * 1024 * 1024 / kv_per_token)


# ============================================================
# 圖表 1：VRAM 擴展容量對比（堆疊條形圖）
# ============================================================
def plot_vram_expansion():
    fig, axes = plt.subplots(1, 2, figsize=(20, 10))

    # SD 卡擴展
    ax1 = axes[0]
    gpu_names = list(GPUS.keys())
    sd_names = list(SD_SPECS.keys())
    x = np.arange(len(gpu_names))
    width = 0.18
    colors_sd = ['#66bb6a', '#43a047', '#2e7d32', '#1b5e20']

    for i, (sd_name, sd_spec) in enumerate(SD_SPECS.items()):
        expand_gb = calc_vram_expansion(sd_spec["max_cap_gb"])
        totals = [GPUS[g]["vram_gb"] + expand_gb for g in gpu_names]
        bars = ax1.bar(x + i * width, totals, width, label=sd_name, color=colors_sd[i], alpha=0.85)

    # 原生 VRAM 基準線
    for j, g in enumerate(gpu_names):
        ax1.hlines(GPUS[g]["vram_gb"], j - 0.15, j + 0.75, colors='red', linestyles='dashed', linewidth=1.5)

    ax1.set_xlabel('GPU 型號', fontsize=12)
    ax1.set_ylabel('總可用記憶體 (GB)', fontsize=12)
    ax1.set_title('SD-VRAM Booster\n各 SD Express 規格的 VRAM 擴展', fontsize=14, fontweight='bold')
    ax1.set_xticks(x + width * 1.5)
    ax1.set_xticklabels(gpu_names, rotation=30, ha='right', fontsize=9)
    ax1.legend(fontsize=8, loc='upper left')
    ax1.set_yscale('log')
    ax1.set_ylim(5, 2000)
    ax1.axhline(y=40, color='orange', linestyle=':', alpha=0.5, label='70B Q4 需求')
    ax1.axhline(y=140, color='red', linestyle=':', alpha=0.5, label='70B FP16 需求')
    ax1.text(0, 42, '70B Q4 (40GB)', fontsize=8, color='orange')
    ax1.text(0, 148, '70B FP16 (140GB)', fontsize=8, color='red')

    # USB 擴展
    ax2 = axes[1]
    usb_names = list(USB_SPECS.keys())
    width_u = 0.12
    colors_usb = ['#42a5f5', '#1e88e5', '#1565c0', '#0d47a1', '#ff8f00', '#e65100']

    for i, (usb_name, usb_spec) in enumerate(USB_SPECS.items()):
        expand_gb = calc_vram_expansion(usb_spec["max_cap_gb"])
        totals = [GPUS[g]["vram_gb"] + expand_gb for g in gpu_names]
        bars = ax2.bar(x + i * width_u, totals, width_u, label=usb_name, color=colors_usb[i], alpha=0.85)

    for j, g in enumerate(gpu_names):
        ax2.hlines(GPUS[g]["vram_gb"], j - 0.15, j + 0.75, colors='red', linestyles='dashed', linewidth=1.5)

    ax2.set_xlabel('GPU 型號', fontsize=12)
    ax2.set_ylabel('總可用記憶體 (GB)', fontsize=12)
    ax2.set_title('USB-VRAM Booster\n各 USB 規格的 VRAM 擴展', fontsize=14, fontweight='bold')
    ax2.set_xticks(x + width_u * 2.5)
    ax2.set_xticklabels(gpu_names, rotation=30, ha='right', fontsize=9)
    ax2.legend(fontsize=7, loc='upper left')
    ax2.set_yscale('log')
    ax2.set_ylim(5, 5000)
    ax2.axhline(y=40, color='orange', linestyle=':', alpha=0.5)
    ax2.axhline(y=140, color='red', linestyle=':', alpha=0.5)
    ax2.text(0, 42, '70B Q4 (40GB)', fontsize=8, color='orange')
    ax2.text(0, 148, '70B FP16 (140GB)', fontsize=8, color='red')

    fig.suptitle('SD vs USB 架構 — VRAM 擴展容量對比\n製作者：DONG. WEI YANG',
                 fontsize=16, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig('/home/ubuntu/vram_expansion_comparison.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 1: vram_expansion_comparison.png")


# ============================================================
# 圖表 2：頻寬與推理速度對比
# ============================================================
def plot_bandwidth_and_tps():
    fig, axes = plt.subplots(1, 2, figsize=(20, 9))

    # 頻寬比較
    ax1 = axes[0]
    all_specs = {}
    for name, spec in SD_SPECS.items():
        all_specs[f"SD: {name}"] = {"bw": spec["bw_mbs"], "type": "SD", "cap": spec["max_cap_gb"]}
    for name, spec in USB_SPECS.items():
        all_specs[f"USB: {name}"] = {"bw": spec["bw_mbs"], "type": "USB", "cap": spec["max_cap_gb"]}

    names = list(all_specs.keys())
    bws = [all_specs[n]["bw"] for n in names]
    colors = ['#4caf50' if all_specs[n]["type"] == "SD" else '#2196f3' for n in names]

    bars = ax1.barh(range(len(names)), bws, color=colors, alpha=0.85, height=0.7)
    ax1.set_yticks(range(len(names)))
    ax1.set_yticklabels([n.replace("SD: ", "").replace("USB: ", "") for n in names], fontsize=8)
    ax1.set_xlabel('最大頻寬 (MB/s)', fontsize=11)
    ax1.set_title('頻寬比較', fontsize=13, fontweight='bold')

    for i, (bar, bw) in enumerate(zip(bars, bws)):
        ax1.text(bw + 100, bar.get_y() + bar.get_height()/2,
                f'{bw:,} MB/s', va='center', fontsize=8, fontweight='bold')

    # 圖例
    from matplotlib.patches import Patch
    legend_elements = [Patch(facecolor='#4caf50', label='SD Express'),
                       Patch(facecolor='#2196f3', label='USB / TB')]
    ax1.legend(handles=legend_elements, loc='lower right', fontsize=10)

    # 推理速度比較（以 RTX 4070 12GB + Llama-3 70B Q4 為例）
    ax2 = axes[1]
    gpu_vram = 12  # RTX 4070
    model_size = 40  # 70B Q4
    overflow = model_size - gpu_vram  # 28 GB 溢出

    results = []
    # SD 方案
    for name, spec in SD_SPECS.items():
        tps = calc_inference_tps(model_size, gpu_vram, spec["bw_mbs"], overflow)
        results.append({"name": f"SD: {name}", "tps": tps, "type": "SD"})
    # USB 方案
    for name, spec in USB_SPECS.items():
        tps = calc_inference_tps(model_size, gpu_vram, spec["bw_mbs"], overflow)
        results.append({"name": f"USB: {name}", "tps": tps, "type": "USB"})

    r_names = [r["name"] for r in results]
    r_tps = [r["tps"] for r in results]
    r_colors = ['#4caf50' if r["type"] == "SD" else '#2196f3' for r in results]

    bars2 = ax2.barh(range(len(r_names)), r_tps, color=r_colors, alpha=0.85, height=0.7)
    ax2.set_yticks(range(len(r_names)))
    ax2.set_yticklabels([n.replace("SD: ", "").replace("USB: ", "") for n in r_names], fontsize=8)
    ax2.set_xlabel('推理速度 (tokens/s)', fontsize=11)
    ax2.set_title('Llama-3 70B (Q4) 在 RTX 4070 上的推理速度\n(28GB 溢出需從外部讀取)', fontsize=12, fontweight='bold')

    for bar, tps in zip(bars2, r_tps):
        ax2.text(tps + 0.05, bar.get_y() + bar.get_height()/2,
                f'{tps:.2f} tok/s', va='center', fontsize=8, fontweight='bold')

    ax2.axvline(x=0, color='red', linestyle='--', alpha=0.3)
    ax2.text(0.05, -0.8, '無擴展 = OOM 崩潰', fontsize=9, color='red', style='italic')
    ax2.legend(handles=legend_elements, loc='lower right', fontsize=10)

    fig.suptitle('SD vs USB 架構 — 頻寬與推理速度對比\n製作者：DONG. WEI YANG',
                 fontsize=15, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig('/home/ubuntu/bandwidth_tps_comparison.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 2: bandwidth_tps_comparison.png")


# ============================================================
# 圖表 3：Context Window 提升倍數
# ============================================================
def plot_context_window():
    fig, ax = plt.subplots(figsize=(18, 10))

    gpu_vram = 12  # RTX 4070
    model = MODELS["Llama-3 8B (Q4)"]
    model_size = model["size_gb"]
    kv_per_token = model["kv_per_token_bytes"]

    # 基準：純 VRAM
    base_context = calc_context_window(gpu_vram, model_size, 0, kv_per_token)

    results = []
    # 原生
    results.append({"name": "純 VRAM\n(無擴展)", "context": base_context, "boost": 1.0, "type": "base"})

    # SD 方案
    for name, spec in SD_SPECS.items():
        ext_gb = calc_vram_expansion(spec["max_cap_gb"])
        ctx = calc_context_window(gpu_vram, model_size, ext_gb, kv_per_token)
        boost = ctx / base_context if base_context > 0 else 0
        results.append({"name": f"SD:\n{name}", "context": ctx, "boost": boost, "type": "SD"})

    # USB 方案
    for name, spec in USB_SPECS.items():
        ext_gb = calc_vram_expansion(spec["max_cap_gb"])
        ctx = calc_context_window(gpu_vram, model_size, ext_gb, kv_per_token)
        boost = ctx / base_context if base_context > 0 else 0
        results.append({"name": f"USB:\n{name}", "context": ctx, "boost": boost, "type": "USB"})

    names = [r["name"] for r in results]
    contexts = [r["context"] for r in results]
    boosts = [r["boost"] for r in results]
    colors = []
    for r in results:
        if r["type"] == "base":
            colors.append('#f44336')
        elif r["type"] == "SD":
            colors.append('#4caf50')
        else:
            colors.append('#2196f3')

    bars = ax.bar(range(len(names)), [c / 1000 for c in contexts], color=colors, alpha=0.85, width=0.7)

    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontsize=7, ha='center')
    ax.set_ylabel('最大 Context Window (千 tokens)', fontsize=12)
    ax.set_title('Llama-3 8B (Q4) 在 RTX 4070 上的 Context Window 提升\n'
                 '製作者：DONG. WEI YANG', fontsize=14, fontweight='bold')

    for bar, ctx, boost in zip(bars, contexts, boosts):
        if ctx > 1000000:
            label = f'{ctx/1000000:.1f}M\n({boost:.0f}x)'
        else:
            label = f'{ctx/1000:.0f}K\n({boost:.0f}x)'
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 50,
                label, ha='center', va='bottom', fontsize=8, fontweight='bold')

    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor='#f44336', label='純 VRAM (基準)'),
        Patch(facecolor='#4caf50', label='SD Express 擴展'),
        Patch(facecolor='#2196f3', label='USB / TB 擴展'),
    ]
    ax.legend(handles=legend_elements, loc='upper left', fontsize=11)
    ax.set_yscale('log')

    plt.tight_layout()
    plt.savefig('/home/ubuntu/context_window_comparison.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 3: context_window_comparison.png")


# ============================================================
# 圖表 4：綜合效能矩陣（哪些模型可以跑）
# ============================================================
def plot_model_capability_matrix():
    fig, axes = plt.subplots(1, 2, figsize=(22, 12))

    gpu_list = ["RTX 4060\n(8GB)", "RTX 4070\n(12GB)", "RTX 4090\n(24GB)", "RTX 5090\n(32GB)"]
    gpu_vrams = [8, 12, 24, 32]
    model_list = list(MODELS.keys())
    model_sizes = [MODELS[m]["size_gb"] for m in model_list]

    def get_status(vram, model_size, ext_gb, ext_bw):
        total = vram + ext_gb
        if model_size <= vram:
            return 3, "原生"  # 完全在 VRAM
        elif model_size <= total:
            overflow = model_size - vram
            tps = calc_inference_tps(model_size, vram, ext_bw, overflow)
            if tps >= 5:
                return 2, f"{tps:.1f}"
            elif tps >= 1:
                return 1, f"{tps:.1f}"
            else:
                return 0.5, f"{tps:.2f}"
        else:
            return 0, "OOM"

    # SD Express Gen4 x2 (最佳 SD)
    ax1 = axes[0]
    sd_spec = SD_SPECS["SD Express Gen4 x2\n(3,940 MB/s)"]
    sd_ext = calc_vram_expansion(sd_spec["max_cap_gb"])

    matrix_sd = np.zeros((len(model_list), len(gpu_list)))
    labels_sd = [['' for _ in gpu_list] for _ in model_list]

    for i, (model_name, model_size) in enumerate(zip(model_list, model_sizes)):
        for j, (gpu_name, vram) in enumerate(zip(gpu_list, gpu_vrams)):
            # 無擴展
            if model_size <= vram:
                matrix_sd[i][j] = 3
                labels_sd[i][j] = f"原生\n18 tok/s"
            else:
                status, label = get_status(vram, model_size, sd_ext, sd_spec["bw_mbs"])
                matrix_sd[i][j] = status
                if status > 0:
                    labels_sd[i][j] = f"擴展\n{label} tok/s"
                else:
                    labels_sd[i][j] = "OOM"

    cmap = plt.cm.RdYlGn
    im1 = ax1.imshow(matrix_sd, cmap=cmap, aspect='auto', vmin=0, vmax=3)
    ax1.set_xticks(range(len(gpu_list)))
    ax1.set_xticklabels(gpu_list, fontsize=10)
    ax1.set_yticks(range(len(model_list)))
    ax1.set_yticklabels(model_list, fontsize=10)
    ax1.set_title('SD-VRAM Booster\n(SD Express Gen4 x2, 1TB, 3,940 MB/s)', fontsize=13, fontweight='bold')

    for i in range(len(model_list)):
        for j in range(len(gpu_list)):
            color = 'white' if matrix_sd[i][j] < 1.5 else 'black'
            ax1.text(j, i, labels_sd[i][j], ha='center', va='center',
                    fontsize=8, fontweight='bold', color=color)

    # USB4 V2 (最佳 USB)
    ax2 = axes[1]
    usb_spec = USB_SPECS["USB4 v2\n(10,000 MB/s)"]
    usb_ext = calc_vram_expansion(usb_spec["max_cap_gb"])

    matrix_usb = np.zeros((len(model_list), len(gpu_list)))
    labels_usb = [['' for _ in gpu_list] for _ in model_list]

    for i, (model_name, model_size) in enumerate(zip(model_list, model_sizes)):
        for j, (gpu_name, vram) in enumerate(zip(gpu_list, gpu_vrams)):
            if model_size <= vram:
                matrix_usb[i][j] = 3
                labels_usb[i][j] = f"原生\n18 tok/s"
            else:
                status, label = get_status(vram, model_size, usb_ext, usb_spec["bw_mbs"])
                matrix_usb[i][j] = status
                if status > 0:
                    labels_usb[i][j] = f"擴展\n{label} tok/s"
                else:
                    labels_usb[i][j] = "OOM"

    im2 = ax2.imshow(matrix_usb, cmap=cmap, aspect='auto', vmin=0, vmax=3)
    ax2.set_xticks(range(len(gpu_list)))
    ax2.set_xticklabels(gpu_list, fontsize=10)
    ax2.set_yticks(range(len(model_list)))
    ax2.set_yticklabels(model_list, fontsize=10)
    ax2.set_title('USB-VRAM Booster\n(USB4 V2, 4TB, 10,000 MB/s)', fontsize=13, fontweight='bold')

    for i in range(len(model_list)):
        for j in range(len(gpu_list)):
            color = 'white' if matrix_usb[i][j] < 1.5 else 'black'
            ax2.text(j, i, labels_usb[i][j], ha='center', va='center',
                    fontsize=8, fontweight='bold', color=color)

    # 色條
    cbar = fig.colorbar(im2, ax=axes, orientation='horizontal', fraction=0.04, pad=0.12,
                        ticks=[0, 1, 2, 3])
    cbar.ax.set_xticklabels(['OOM\n(無法運行)', '慢速\n(<5 tok/s)', '可用\n(5+ tok/s)', '原生速度\n(18 tok/s)'],
                            fontsize=10)

    fig.suptitle('SD vs USB 架構 — 模型相容性矩陣\n'
                 '綠色=可運行 | 黃色=慢速但可用 | 紅色=無法運行\n'
                 '製作者：DONG. WEI YANG',
                 fontsize=15, fontweight='bold', y=1.05)
    plt.tight_layout()
    plt.savefig('/home/ubuntu/model_capability_matrix.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 4: model_capability_matrix.png")


# ============================================================
# 圖表 5：VRAM 提升倍數總覽
# ============================================================
def plot_vram_boost_ratio():
    fig, ax = plt.subplots(figsize=(16, 9))

    gpu_names = ["RTX 4060 (8GB)", "RTX 4070 (12GB)", "RTX 4090 (24GB)", "RTX 5090 (32GB)"]
    gpu_vrams = [8, 12, 24, 32]

    scenarios = [
        ("SD Gen3 x1 (128GB)", calc_vram_expansion(128), '#a5d6a7'),
        ("SD Gen4 x2 (1TB)", calc_vram_expansion(1024), '#2e7d32'),
        ("USB 3.2 Gen2 (512GB)", calc_vram_expansion(512), '#90caf9'),
        ("USB4 v1 (2TB)", calc_vram_expansion(2048), '#1565c0'),
        ("USB4 v2 (4TB)", calc_vram_expansion(4096), '#0d47a1'),
        ("TB5 (4TB)", calc_vram_expansion(4096), '#e65100'),
    ]

    x = np.arange(len(gpu_names))
    width = 0.12

    for i, (label, ext_gb, color) in enumerate(scenarios):
        ratios = [(vram + ext_gb) / vram for vram in gpu_vrams]
        bars = ax.bar(x + i * width, ratios, width, label=label, color=color, alpha=0.85)
        for bar, ratio in zip(bars, ratios):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                    f'{ratio:.0f}x', ha='center', va='bottom', fontsize=7, fontweight='bold')

    ax.set_xlabel('GPU 型號', fontsize=12)
    ax.set_ylabel('VRAM 提升倍數 (擴展後 / 原始)', fontsize=12)
    ax.set_title('SD vs USB 架構 — VRAM 提升倍數總覽\n'
                 '（數值 = 擴展後總記憶體 / 原始 VRAM）\n'
                 '製作者：DONG. WEI YANG',
                 fontsize=14, fontweight='bold')
    ax.set_xticks(x + width * 2.5)
    ax.set_xticklabels(gpu_names, fontsize=11)
    ax.legend(fontsize=9, loc='upper right', ncol=2)
    ax.axhline(y=1, color='red', linestyle='--', alpha=0.3, label='無擴展')

    plt.tight_layout()
    plt.savefig('/home/ubuntu/vram_boost_ratio.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 5: vram_boost_ratio.png")


# ============================================================
# 執行所有圖表
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  SD vs USB 架構 VRAM 擴展能力對比分析")
    print("  製作者：DONG. WEI YANG")
    print("=" * 60)

    plot_vram_expansion()
    plot_bandwidth_and_tps()
    plot_context_window()
    plot_model_capability_matrix()
    plot_vram_boost_ratio()

    # 輸出數據摘要
    print("\n" + "=" * 60)
    print("  數據摘要")
    print("=" * 60)

    print("\n[SD-VRAM Booster 最大擴展能力]")
    for name, spec in SD_SPECS.items():
        ext = calc_vram_expansion(spec["max_cap_gb"])
        clean_name = name.replace('\n', ' ')
        print(f"  {clean_name}: +{ext:.0f} GB (頻寬 {spec['bw_mbs']:,} MB/s)")

    print("\n[USB-VRAM Booster 最大擴展能力]")
    for name, spec in USB_SPECS.items():
        ext = calc_vram_expansion(spec["max_cap_gb"])
        clean_name = name.replace('\n', ' ')
        print(f"  {clean_name}: +{ext:.0f} GB (頻寬 {spec['bw_mbs']:,} MB/s)")

    print("\n[以 RTX 4070 (12GB) 為例的 VRAM 提升倍數]")
    vram = 12
    all_scenarios = [
        ("SD Gen3 x1 (128GB)", calc_vram_expansion(128)),
        ("SD Gen4 x2 (1TB)", calc_vram_expansion(1024)),
        ("USB 3.2 Gen2 (512GB)", calc_vram_expansion(512)),
        ("USB4 v1 (2TB)", calc_vram_expansion(2048)),
        ("USB4 v2 (4TB)", calc_vram_expansion(4096)),
        ("TB5 (4TB)", calc_vram_expansion(4096)),
    ]
    for name, ext in all_scenarios:
        total = vram + ext
        ratio = total / vram
        print(f"  {name}: {vram}GB → {total:.0f}GB ({ratio:.0f}x 提升)")

    print("\n" + "=" * 60)
    print("  所有圖表已生成完成！")
    print("=" * 60)
