#!/usr/bin/env python3
"""
三種架構 VRAM 擴展能力完整對比分析
SD 卡 vs USB 儲存裝置 vs 外接硬碟盒
製作者：DONG. WEI YANG
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.style as mplstyle
mplstyle.use('seaborn-v0_8-darkgrid')
import numpy as np
from matplotlib.patches import Patch

plt.rcParams['font.family'] = 'Noto Sans CJK SC'
plt.rcParams['axes.unicode_minus'] = False

# ============================================================
# 三種架構的規格定義（基於真實物理數據）
# ============================================================

# 架構 A：SD 卡（SD Express，PCIe/NVMe 原生直通）
SD_CARD = {
    "SD Express\nGen3 x1": {"bw_mbs": 985,  "cap_gb": 128,  "latency_us": 10, "protocol": "PCIe 3.0 x1 + NVMe"},
    "SD Express\nGen3 x2": {"bw_mbs": 1970, "cap_gb": 256,  "latency_us": 10, "protocol": "PCIe 3.0 x2 + NVMe"},
    "SD Express\nGen4 x1": {"bw_mbs": 1970, "cap_gb": 512,  "latency_us": 8,  "protocol": "PCIe 4.0 x1 + NVMe"},
    "SD Express\nGen4 x2": {"bw_mbs": 3940, "cap_gb": 1024, "latency_us": 8,  "protocol": "PCIe 4.0 x2 + NVMe"},
}

# 架構 B：USB 儲存裝置（隨身碟/外接 SSD，走 xHCI 控制器，有 Bridge 轉換）
USB_STORAGE = {
    "USB 3.2 Gen1\n隨身碟":     {"bw_mbs": 400,  "cap_gb": 128,  "latency_us": 200, "protocol": "xHCI → USB-SATA Bridge"},
    "USB 3.2 Gen2\n隨身碟":     {"bw_mbs": 800,  "cap_gb": 256,  "latency_us": 150, "protocol": "xHCI → USB-SATA Bridge"},
    "USB 3.2 Gen2\n外接 SSD":   {"bw_mbs": 1000, "cap_gb": 1024, "latency_us": 100, "protocol": "xHCI → USB-NVMe Bridge"},
    "USB 3.2 Gen2x2\n外接 SSD": {"bw_mbs": 1800, "cap_gb": 2048, "latency_us": 80,  "protocol": "xHCI → USB-NVMe Bridge"},
}

# 架構 C：外接硬碟盒（USB4/TB 外接 NVMe 盒，PCIe Tunneling）
ENCLOSURE = {
    "USB4 v1\nNVMe 外接盒":  {"bw_mbs": 3800,  "cap_gb": 2048, "latency_us": 15, "protocol": "PCIe Tunneling (40Gbps)"},
    "USB4 v2\nNVMe 外接盒":  {"bw_mbs": 7500,  "cap_gb": 4096, "latency_us": 12, "protocol": "PCIe Tunneling (80Gbps)"},
    "Thunderbolt 4\nNVMe 外接盒": {"bw_mbs": 3000,  "cap_gb": 4096, "latency_us": 15, "protocol": "PCIe Tunneling (40Gbps)"},
    "Thunderbolt 5\nNVMe 外接盒": {"bw_mbs": 10000, "cap_gb": 8192, "latency_us": 10, "protocol": "PCIe Tunneling (120Gbps)"},
}

# GPU 參考
GPUS = {
    "RTX 4060 (8GB)":  8,
    "RTX 4070 (12GB)": 12,
    "RTX 4090 (24GB)": 24,
    "RTX 5090 (32GB)": 32,
}

# 顏色定義
COLOR_SD = '#4caf50'       # 綠色
COLOR_USB = '#2196f3'      # 藍色
COLOR_ENCLOSURE = '#ff9800' # 橘色
COLOR_BASE = '#f44336'     # 紅色

def usable_gb(cap_gb):
    return max(0, cap_gb * 0.9 - 1.0)

def calc_tps(overflow_gb, bw_mbs, latency_us):
    if overflow_gb <= 0:
        return 18.0
    read_per_token_mb = (overflow_gb * 1024) / 32
    read_time_ms = read_per_token_mb / bw_mbs * 1000 + latency_us / 1000
    compute_time_ms = 55
    return 1000 / (compute_time_ms + read_time_ms)

def calc_context(vram_gb, model_gb, ext_gb, kv_per_token=131072):
    vram_kv = max(0, vram_gb - model_gb)
    total_kv = vram_kv + ext_gb
    return int(total_kv * 1024 * 1024 * 1024 / kv_per_token)


# ============================================================
# 圖表 1：三種架構頻寬 + 延遲 + 容量 總覽
# ============================================================
def plot_overview():
    fig, axes = plt.subplots(1, 3, figsize=(22, 10))

    all_data = [
        ("SD 卡\n(PCIe/NVMe 原生直通)", SD_CARD, COLOR_SD),
        ("USB 儲存裝置\n(xHCI + Bridge 轉換)", USB_STORAGE, COLOR_USB),
        ("外接硬碟盒\n(PCIe Tunneling)", ENCLOSURE, COLOR_ENCLOSURE),
    ]

    for idx, (title, specs, color) in enumerate(all_data):
        ax = axes[idx]
        names = list(specs.keys())
        bws = [specs[n]["bw_mbs"] for n in names]
        caps = [specs[n]["cap_gb"] for n in names]
        lats = [specs[n]["latency_us"] for n in names]

        x = np.arange(len(names))
        w = 0.35

        bars1 = ax.bar(x - w/2, bws, w, color=color, alpha=0.85, label='頻寬 (MB/s)')
        ax2 = ax.twinx()
        bars2 = ax2.bar(x + w/2, caps, w, color=color, alpha=0.4, label='容量 (GB)')

        ax.set_xticks(x)
        ax.set_xticklabels(names, fontsize=8, ha='center')
        ax.set_ylabel('頻寬 (MB/s)', fontsize=10, color=color)
        ax2.set_ylabel('最大容量 (GB)', fontsize=10, color='gray')
        ax.set_title(title, fontsize=13, fontweight='bold', pad=15)

        for bar, bw in zip(bars1, bws):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 50,
                    f'{bw:,}', ha='center', va='bottom', fontsize=8, fontweight='bold', color=color)
        for bar, cap in zip(bars2, caps):
            ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 20,
                     f'{cap:,}GB', ha='center', va='bottom', fontsize=7, color='gray')

        # 延遲標註
        for i, lat in enumerate(lats):
            ax.text(i, -max(bws)*0.08, f'延遲: {lat}μs', ha='center', fontsize=7, color='#666')

        ax.legend(loc='upper left', fontsize=8)
        ax2.legend(loc='upper right', fontsize=8)

    fig.suptitle('三種架構規格總覽：頻寬、容量與延遲\n製作者：DONG. WEI YANG',
                 fontsize=16, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig('/home/ubuntu/three_arch_overview.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 1: three_arch_overview.png")


# ============================================================
# 圖表 2：VRAM 提升倍數三方對比
# ============================================================
def plot_vram_boost():
    fig, ax = plt.subplots(figsize=(20, 10))

    gpu_names = list(GPUS.keys())
    gpu_vrams = list(GPUS.values())

    # 每種架構選最佳規格
    scenarios = [
        # SD 卡
        ("SD Gen3 x1\n(128GB, 985MB/s)", usable_gb(128), COLOR_SD, 0.7),
        ("SD Gen4 x2\n(1TB, 3940MB/s)", usable_gb(1024), COLOR_SD, 1.0),
        # USB 儲存
        ("USB 3.2 Gen2\n隨身碟 (256GB)", usable_gb(256), COLOR_USB, 0.7),
        ("USB 3.2 Gen2x2\nSSD (2TB)", usable_gb(2048), COLOR_USB, 1.0),
        # 外接硬碟盒
        ("USB4 v2\n外接盒 (4TB)", usable_gb(4096), COLOR_ENCLOSURE, 0.7),
        ("TB5\n外接盒 (8TB)", usable_gb(8192), COLOR_ENCLOSURE, 1.0),
    ]

    x = np.arange(len(gpu_names))
    n = len(scenarios)
    width = 0.12

    for i, (label, ext_gb, color, alpha) in enumerate(scenarios):
        ratios = [(vram + ext_gb) / vram for vram in gpu_vrams]
        bars = ax.bar(x + i * width, ratios, width, label=label, color=color, alpha=alpha)
        for bar, ratio in zip(bars, ratios):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 2,
                    f'{ratio:.0f}x', ha='center', va='bottom', fontsize=7, fontweight='bold')

    ax.set_xlabel('GPU 型號', fontsize=12)
    ax.set_ylabel('VRAM 提升倍數（擴展後 / 原始）', fontsize=12)
    ax.set_title('三種架構 VRAM 提升倍數對比\n（數值 = 擴展後總記憶體 ÷ 原始 VRAM）\n製作者：DONG. WEI YANG',
                 fontsize=14, fontweight='bold')
    ax.set_xticks(x + width * 2.5)
    ax.set_xticklabels(gpu_names, fontsize=11)
    ax.legend(fontsize=8, loc='upper right', ncol=3,
              title='■ 綠色=SD卡  ■ 藍色=USB儲存  ■ 橘色=外接硬碟盒')
    ax.axhline(y=1, color='red', linestyle='--', alpha=0.3)

    plt.tight_layout()
    plt.savefig('/home/ubuntu/three_arch_vram_boost.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 2: three_arch_vram_boost.png")


# ============================================================
# 圖表 3：70B 模型推理速度三方對比
# ============================================================
def plot_inference_speed():
    fig, axes = plt.subplots(1, 2, figsize=(20, 9))

    # 左圖：各規格頻寬橫向比較
    ax1 = axes[0]
    all_items = []
    for name, spec in SD_CARD.items():
        all_items.append((name, spec["bw_mbs"], spec["latency_us"], "SD 卡", COLOR_SD))
    for name, spec in USB_STORAGE.items():
        all_items.append((name, spec["bw_mbs"], spec["latency_us"], "USB 儲存", COLOR_USB))
    for name, spec in ENCLOSURE.items():
        all_items.append((name, spec["bw_mbs"], spec["latency_us"], "外接硬碟盒", COLOR_ENCLOSURE))

    names = [item[0].replace('\n', ' ') for item in all_items]
    bws = [item[1] for item in all_items]
    colors = [item[4] for item in all_items]

    y_pos = range(len(names))
    bars = ax1.barh(y_pos, bws, color=colors, alpha=0.85, height=0.7)
    ax1.set_yticks(y_pos)
    ax1.set_yticklabels(names, fontsize=7)
    ax1.set_xlabel('實際頻寬 (MB/s)', fontsize=11)
    ax1.set_title('三種架構頻寬比較', fontsize=13, fontweight='bold')

    for bar, bw in zip(bars, bws):
        ax1.text(bw + 50, bar.get_y() + bar.get_height()/2,
                f'{bw:,} MB/s', va='center', fontsize=7, fontweight='bold')

    legend_elements = [
        Patch(facecolor=COLOR_SD, label='SD 卡 (PCIe 直通)'),
        Patch(facecolor=COLOR_USB, label='USB 儲存 (xHCI Bridge)'),
        Patch(facecolor=COLOR_ENCLOSURE, label='外接硬碟盒 (PCIe Tunnel)'),
    ]
    ax1.legend(handles=legend_elements, loc='lower right', fontsize=9)

    # 右圖：Llama-3 70B Q4 推理速度
    ax2 = axes[1]
    gpu_vram = 12  # RTX 4070
    model_size = 40  # 70B Q4
    overflow = model_size - gpu_vram

    results = []
    for name, spec in SD_CARD.items():
        tps = calc_tps(overflow, spec["bw_mbs"], spec["latency_us"])
        results.append((name.replace('\n', ' '), tps, COLOR_SD))
    for name, spec in USB_STORAGE.items():
        tps = calc_tps(overflow, spec["bw_mbs"], spec["latency_us"])
        results.append((name.replace('\n', ' '), tps, COLOR_USB))
    for name, spec in ENCLOSURE.items():
        tps = calc_tps(overflow, spec["bw_mbs"], spec["latency_us"])
        results.append((name.replace('\n', ' '), tps, COLOR_ENCLOSURE))

    r_names = [r[0] for r in results]
    r_tps = [r[1] for r in results]
    r_colors = [r[2] for r in results]

    bars2 = ax2.barh(range(len(r_names)), r_tps, color=r_colors, alpha=0.85, height=0.7)
    ax2.set_yticks(range(len(r_names)))
    ax2.set_yticklabels(r_names, fontsize=7)
    ax2.set_xlabel('推理速度 (tokens/s)', fontsize=11)
    ax2.set_title('Llama-3 70B (Q4) 在 RTX 4070 上的推理速度\n(28GB 溢出需從外部讀取)', fontsize=12, fontweight='bold')

    for bar, tps in zip(bars2, r_tps):
        ax2.text(tps + 0.03, bar.get_y() + bar.get_height()/2,
                f'{tps:.2f} tok/s', va='center', fontsize=7, fontweight='bold')

    ax2.axvline(x=0, color='red', linestyle='--', alpha=0.3)
    ax2.text(0.05, -1, '無擴展 = OOM 崩潰', fontsize=9, color='red', style='italic')
    ax2.legend(handles=legend_elements, loc='lower right', fontsize=9)

    fig.suptitle('三種架構 — 頻寬與推理速度對比\n製作者：DONG. WEI YANG',
                 fontsize=15, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig('/home/ubuntu/three_arch_inference.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 3: three_arch_inference.png")


# ============================================================
# 圖表 4：Context Window 提升三方對比
# ============================================================
def plot_context_boost():
    fig, ax = plt.subplots(figsize=(18, 10))

    gpu_vram = 12
    model_size = 4.5  # Llama-3 8B Q4
    kv_per_token = 131072

    base_ctx = calc_context(gpu_vram, model_size, 0, kv_per_token)

    results = [("純 VRAM\n(無擴展)", base_ctx, 1.0, COLOR_BASE)]

    # SD 卡最佳
    for name, spec in [("SD Gen3 x1\n(128GB)", SD_CARD["SD Express\nGen3 x1"]),
                        ("SD Gen4 x2\n(1TB)", SD_CARD["SD Express\nGen4 x2"])]:
        ext = usable_gb(spec["cap_gb"])
        ctx = calc_context(gpu_vram, model_size, ext, kv_per_token)
        results.append((name, ctx, ctx/base_ctx, COLOR_SD))

    # USB 儲存最佳
    for name, spec in [("USB 3.2 Gen2\n隨身碟 (256GB)", USB_STORAGE["USB 3.2 Gen2\n隨身碟"]),
                        ("USB 3.2 Gen2x2\nSSD (2TB)", USB_STORAGE["USB 3.2 Gen2x2\n外接 SSD"])]:
        ext = usable_gb(spec["cap_gb"])
        ctx = calc_context(gpu_vram, model_size, ext, kv_per_token)
        results.append((name, ctx, ctx/base_ctx, COLOR_USB))

    # 外接硬碟盒最佳
    for name, spec in [("USB4 v2\n外接盒 (4TB)", ENCLOSURE["USB4 v2\nNVMe 外接盒"]),
                        ("TB5\n外接盒 (8TB)", ENCLOSURE["Thunderbolt 5\nNVMe 外接盒"])]:
        ext = usable_gb(spec["cap_gb"])
        ctx = calc_context(gpu_vram, model_size, ext, kv_per_token)
        results.append((name, ctx, ctx/base_ctx, COLOR_ENCLOSURE))

    names = [r[0] for r in results]
    contexts_k = [r[1] / 1000 for r in results]
    boosts = [r[2] for r in results]
    colors = [r[3] for r in results]

    bars = ax.bar(range(len(names)), contexts_k, color=colors, alpha=0.85, width=0.7)

    for bar, ctx_raw, boost in zip(bars, [r[1] for r in results], boosts):
        if ctx_raw > 1000000:
            label = f'{ctx_raw/1000000:.1f}M tokens\n({boost:.0f}x)'
        else:
            label = f'{ctx_raw/1000:.0f}K tokens\n({boost:.0f}x)'
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(contexts_k)*0.02,
                label, ha='center', va='bottom', fontsize=9, fontweight='bold')

    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontsize=8, ha='center')
    ax.set_ylabel('最大 Context Window (千 tokens)', fontsize=12)
    ax.set_title('Llama-3 8B (Q4) 在 RTX 4070 上的 Context Window 提升\n'
                 '三種架構對比 | 製作者：DONG. WEI YANG', fontsize=14, fontweight='bold')
    ax.set_yscale('log')

    legend_elements = [
        Patch(facecolor=COLOR_BASE, label='純 VRAM (基準)'),
        Patch(facecolor=COLOR_SD, label='SD 卡 (PCIe 直通)'),
        Patch(facecolor=COLOR_USB, label='USB 儲存 (xHCI Bridge)'),
        Patch(facecolor=COLOR_ENCLOSURE, label='外接硬碟盒 (PCIe Tunnel)'),
    ]
    ax.legend(handles=legend_elements, loc='upper left', fontsize=11)

    plt.tight_layout()
    plt.savefig('/home/ubuntu/three_arch_context.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 4: three_arch_context.png")


# ============================================================
# 圖表 5：綜合評分雷達圖
# ============================================================
def plot_radar():
    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw=dict(polar=True))

    categories = ['頻寬', '容量', '延遲\n(低=好)', '便攜性', '普及度', '價格\n(低=好)']
    N = len(categories)

    # 評分 (1-10)
    # SD 卡：頻寬中等、容量小、延遲極低、便攜極高、普及度低(SD Express新)、價格中
    sd_scores =        [5, 3, 10, 10, 3, 5]
    # USB 儲存：頻寬低、容量中、延遲高、便攜高、普及度極高、價格低
    usb_scores =       [3, 5, 4,  8,  10, 8]
    # 外接硬碟盒：頻寬極高、容量極大、延遲低、便攜中、普及度中、價格低
    enclosure_scores = [9, 10, 8,  5,  6, 4]

    angles = [n / float(N) * 2 * np.pi for n in range(N)]
    angles += angles[:1]

    sd_scores += sd_scores[:1]
    usb_scores += usb_scores[:1]
    enclosure_scores += enclosure_scores[:1]

    ax.plot(angles, sd_scores, 'o-', linewidth=2, color=COLOR_SD, label='SD 卡')
    ax.fill(angles, sd_scores, alpha=0.15, color=COLOR_SD)

    ax.plot(angles, usb_scores, 's-', linewidth=2, color=COLOR_USB, label='USB 儲存裝置')
    ax.fill(angles, usb_scores, alpha=0.15, color=COLOR_USB)

    ax.plot(angles, enclosure_scores, 'D-', linewidth=2, color=COLOR_ENCLOSURE, label='外接硬碟盒')
    ax.fill(angles, enclosure_scores, alpha=0.15, color=COLOR_ENCLOSURE)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories, fontsize=12, fontweight='bold')
    ax.set_ylim(0, 10)
    ax.set_yticks([2, 4, 6, 8, 10])
    ax.set_yticklabels(['2', '4', '6', '8', '10'], fontsize=8)
    ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=12)
    ax.set_title('三種架構綜合評分\n製作者：DONG. WEI YANG', fontsize=14, fontweight='bold', pad=30)

    plt.tight_layout()
    plt.savefig('/home/ubuntu/three_arch_radar.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 5: three_arch_radar.png")


# ============================================================
# 圖表 6：架構差異示意圖（資料路徑比較）
# ============================================================
def plot_architecture_diff():
    fig, axes = plt.subplots(1, 3, figsize=(22, 7))

    arch_data = [
        {
            "title": "架構 A：SD 卡\n(PCIe/NVMe 原生直通)",
            "color": COLOR_SD,
            "layers": [
                ("GPU", "#e53935"),
                ("PCIe Bus", "#ff7043"),
                ("NVMe 控制器", COLOR_SD),
                ("SD Express 卡", "#81c784"),
            ],
            "arrows": "直通",
            "conversions": 0,
            "latency": "8-10 μs",
            "note": "零協定轉換\n最低延遲",
        },
        {
            "title": "架構 B：USB 儲存裝置\n(xHCI + Bridge 轉換)",
            "color": COLOR_USB,
            "layers": [
                ("GPU", "#e53935"),
                ("PCIe Bus", "#ff7043"),
                ("xHCI 控制器", "#64b5f6"),
                ("USB-NVMe Bridge", "#42a5f5"),
                ("NAND Flash", COLOR_USB),
            ],
            "arrows": "轉換 x2",
            "conversions": 2,
            "latency": "80-200 μs",
            "note": "2 次協定轉換\n延遲最高",
        },
        {
            "title": "架構 C：外接硬碟盒\n(PCIe Tunneling)",
            "color": COLOR_ENCLOSURE,
            "layers": [
                ("GPU", "#e53935"),
                ("PCIe Bus", "#ff7043"),
                ("PCIe Router", "#ffb74d"),
                ("USB4/TB 線纜", COLOR_ENCLOSURE),
                ("NVMe SSD", "#ffe082"),
            ],
            "arrows": "封裝",
            "conversions": 1,
            "latency": "10-15 μs",
            "note": "PCIe 封裝傳輸\n接近原生延遲",
        },
    ]

    for idx, arch in enumerate(arch_data):
        ax = axes[idx]
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 10)
        ax.set_aspect('equal')
        ax.axis('off')

        ax.set_title(arch["title"], fontsize=12, fontweight='bold', color=arch["color"], pad=10)

        layers = arch["layers"]
        n = len(layers)
        y_start = 8.5
        y_step = 1.5

        for i, (name, color) in enumerate(layers):
            y = y_start - i * y_step
            rect = plt.Rectangle((1.5, y - 0.4), 7, 0.8, facecolor=color, alpha=0.7,
                                  edgecolor='white', linewidth=2, zorder=2)
            ax.add_patch(rect)
            ax.text(5, y, name, ha='center', va='center', fontsize=10,
                    fontweight='bold', color='white', zorder=3)

            if i < n - 1:
                ax.annotate('', xy=(5, y - 0.5), xytext=(5, y - y_step + 0.5),
                           arrowprops=dict(arrowstyle='<->', color='#333', lw=2))

        # 底部標註
        ax.text(5, 0.8, f'協定轉換次數: {arch["conversions"]}', ha='center',
                fontsize=11, fontweight='bold', color=arch["color"])
        ax.text(5, 0.2, f'延遲: {arch["latency"]}', ha='center',
                fontsize=10, color='#666')

    fig.suptitle('三種架構資料路徑比較\n製作者：DONG. WEI YANG',
                 fontsize=15, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig('/home/ubuntu/three_arch_datapath.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("[✓] 圖表 6: three_arch_datapath.png")


# ============================================================
# 輸出數據摘要
# ============================================================
def print_summary():
    print("\n" + "=" * 70)
    print("  三種架構 VRAM 擴展能力數據摘要")
    print("  製作者：DONG. WEI YANG")
    print("=" * 70)

    print("\n[架構 A：SD 卡]")
    for name, spec in SD_CARD.items():
        ext = usable_gb(spec["cap_gb"])
        clean = name.replace('\n', ' ')
        print(f"  {clean}: +{ext:.0f} GB | {spec['bw_mbs']:,} MB/s | 延遲 {spec['latency_us']}μs | {spec['protocol']}")

    print("\n[架構 B：USB 儲存裝置]")
    for name, spec in USB_STORAGE.items():
        ext = usable_gb(spec["cap_gb"])
        clean = name.replace('\n', ' ')
        print(f"  {clean}: +{ext:.0f} GB | {spec['bw_mbs']:,} MB/s | 延遲 {spec['latency_us']}μs | {spec['protocol']}")

    print("\n[架構 C：外接硬碟盒]")
    for name, spec in ENCLOSURE.items():
        ext = usable_gb(spec["cap_gb"])
        clean = name.replace('\n', ' ')
        print(f"  {clean}: +{ext:.0f} GB | {spec['bw_mbs']:,} MB/s | 延遲 {spec['latency_us']}μs | {spec['protocol']}")

    print("\n[以 RTX 4070 (12GB) + Llama-3 70B Q4 (40GB) 為例]")
    overflow = 40 - 12
    print(f"  溢出量: {overflow} GB")
    print(f"  {'方案':<35} {'推理速度':>10} {'Context (8B)':>15}")
    print(f"  {'-'*65}")

    for name, spec in SD_CARD.items():
        tps = calc_tps(overflow, spec["bw_mbs"], spec["latency_us"])
        ctx = calc_context(12, 4.5, usable_gb(spec["cap_gb"]))
        clean = f"SD: {name.replace(chr(10), ' ')}"
        print(f"  {clean:<35} {tps:>8.2f} t/s {ctx:>12,} tok")

    for name, spec in USB_STORAGE.items():
        tps = calc_tps(overflow, spec["bw_mbs"], spec["latency_us"])
        ctx = calc_context(12, 4.5, usable_gb(spec["cap_gb"]))
        clean = f"USB: {name.replace(chr(10), ' ')}"
        print(f"  {clean:<35} {tps:>8.2f} t/s {ctx:>12,} tok")

    for name, spec in ENCLOSURE.items():
        tps = calc_tps(overflow, spec["bw_mbs"], spec["latency_us"])
        ctx = calc_context(12, 4.5, usable_gb(spec["cap_gb"]))
        clean = f"盒: {name.replace(chr(10), ' ')}"
        print(f"  {clean:<35} {tps:>8.2f} t/s {ctx:>12,} tok")


# ============================================================
if __name__ == "__main__":
    print("=" * 70)
    print("  三種架構 VRAM 擴展能力完整對比分析")
    print("  SD 卡 vs USB 儲存裝置 vs 外接硬碟盒")
    print("  製作者：DONG. WEI YANG")
    print("=" * 70)

    plot_overview()
    plot_vram_boost()
    plot_inference_speed()
    plot_context_boost()
    plot_radar()
    plot_architecture_diff()
    print_summary()

    print("\n" + "=" * 70)
    print("  所有圖表已生成完成！")
    print("=" * 70)
