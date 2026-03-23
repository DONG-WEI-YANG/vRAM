#!/usr/bin/env python3
"""
USB-VRAM Booster — 效能計算與視覺化
比較 USB 各版本 vs SD Express 的 VRAM 擴展效能
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# 設定中文字體
plt.style.use('seaborn-v0_8-darkgrid')
plt.rcParams['font.family'] = 'Noto Sans CJK SC'
plt.rcParams['axes.unicode_minus'] = False

# ─── 資料定義 ────────────────────────────────────────────────

# 介面頻寬 (MB/s)
interfaces = {
    "USB 2.0": {"max_bw": 60, "real_bw": 35, "protocol": "USB Host", "pcie_tunnel": False},
    "USB 3.0\n(Gen1)": {"max_bw": 625, "real_bw": 450, "protocol": "USB Host", "pcie_tunnel": False},
    "USB 3.2\n(Gen2)": {"max_bw": 1250, "real_bw": 1000, "protocol": "USB Host", "pcie_tunnel": False},
    "USB 3.2\n(Gen2x2)": {"max_bw": 2500, "real_bw": 2000, "protocol": "USB Host", "pcie_tunnel": False},
    "USB4 v1\n(40Gbps)": {"max_bw": 5000, "real_bw": 3500, "protocol": "PCIe Tunnel", "pcie_tunnel": True},
    "USB4 v2\n(80Gbps)": {"max_bw": 10000, "real_bw": 8000, "protocol": "PCIe Tunnel", "pcie_tunnel": True},
    "USB4 v2\n(120G非對稱)": {"max_bw": 15000, "real_bw": 12000, "protocol": "PCIe Tunnel", "pcie_tunnel": True},
    "TB3": {"max_bw": 5000, "real_bw": 2800, "protocol": "PCIe Tunnel", "pcie_tunnel": True},
    "TB4": {"max_bw": 5000, "real_bw": 3200, "protocol": "PCIe Tunnel", "pcie_tunnel": True},
    "TB5": {"max_bw": 10000, "real_bw": 8000, "protocol": "PCIe Tunnel", "pcie_tunnel": True},
    "SD Exp\nGen3x1": {"max_bw": 985, "real_bw": 880, "protocol": "PCIe Native", "pcie_tunnel": True},
    "SD Exp\nGen3x2": {"max_bw": 1969, "real_bw": 1750, "protocol": "PCIe Native", "pcie_tunnel": True},
    "SD Exp\nGen4x2": {"max_bw": 3940, "real_bw": 3500, "protocol": "PCIe Native", "pcie_tunnel": True},
}

# ─── 圖表 1: 頻寬比較 ───────────────────────────────────────

fig, ax = plt.subplots(figsize=(18, 8))

names = list(interfaces.keys())
max_bws = [interfaces[n]["max_bw"] for n in names]
real_bws = [interfaces[n]["real_bw"] for n in names]
colors = []
for n in names:
    if "SD" in n:
        colors.append("#00d4ff")
    elif "TB" in n:
        colors.append("#ff9800")
    elif interfaces[n]["pcie_tunnel"]:
        colors.append("#4caf50")
    else:
        colors.append("#f44336")

x = np.arange(len(names))
width = 0.35

bars1 = ax.bar(x - width/2, max_bws, width, label='理論最大頻寬', color=colors, alpha=0.4, edgecolor='white')
bars2 = ax.bar(x + width/2, real_bws, width, label='實際有效頻寬', color=colors, alpha=0.9, edgecolor='white')

# 標記 VRAM 可用門檻
ax.axhline(y=200, color='red', linestyle='--', linewidth=1.5, alpha=0.7)
ax.text(len(names)-1, 250, 'VRAM 擴展最低門檻 (200 MB/s)', ha='right', fontsize=10, color='red')

# 標記 VRAM 頻寬參考
ax.axhline(y=900000, color='gray', linestyle=':', linewidth=1, alpha=0.3)

for bar, val in zip(bars2, real_bws):
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 100,
            f'{val:,}', ha='center', va='bottom', fontsize=8, fontweight='bold')

ax.set_ylabel('頻寬 (MB/s)', fontsize=12)
ax.set_title('USB 各版本 vs SD Express vs Thunderbolt — 頻寬比較', fontsize=16, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(names, fontsize=9)
ax.legend(fontsize=11)
ax.set_ylim(0, 16000)

# 圖例
legend_patches = [
    mpatches.Patch(color='#f44336', label='USB Host 協定 (需橋接)'),
    mpatches.Patch(color='#4caf50', label='USB4 PCIe Tunneling'),
    mpatches.Patch(color='#ff9800', label='Thunderbolt'),
    mpatches.Patch(color='#00d4ff', label='SD Express (PCIe 原生)'),
]
ax.legend(handles=legend_patches, loc='upper left', fontsize=10)

plt.tight_layout()
plt.savefig('/home/ubuntu/usb_bandwidth_comparison.png', dpi=150, bbox_inches='tight')
plt.close()

# ─── 圖表 2: LLM 推理效能預估 ───────────────────────────────

# 以 Llama-3 70B (Q4, 40GB) 在 RTX 4070 (12GB) 為例
# 溢出 28GB 需要從外部儲存讀取
overflow_gb = 28
compute_ms = 55  # GPU 計算時間

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18, 8))

# 選取可用的介面
usable = {
    "USB 3.0": 450,
    "USB 3.2 Gen2": 1000,
    "USB 3.2 Gen2x2": 2000,
    "USB4 v1 (40G)": 3500,
    "USB4 v2 (80G)": 8000,
    "USB4 v2 (120G)": 12000,
    "SD Exp Gen3x1": 880,
    "SD Exp Gen3x2": 1750,
    "SD Exp Gen4x2": 3500,
    "TB4": 3200,
    "TB5": 8000,
}

names_u = list(usable.keys())
bws = list(usable.values())

# 計算推理速度
tps_list = []
for bw in bws:
    load_time_ms = (overflow_gb * 1024) / bw * 1000
    tps = 1000 / (compute_ms + load_time_ms)
    tps_list.append(tps)

colors_u = []
for n in names_u:
    if "SD" in n:
        colors_u.append("#00d4ff")
    elif "TB" in n:
        colors_u.append("#ff9800")
    elif "USB4" in n:
        colors_u.append("#4caf50")
    else:
        colors_u.append("#f44336")

bars = ax1.barh(names_u, tps_list, color=colors_u, edgecolor='white', alpha=0.85)
for bar, tps in zip(bars, tps_list):
    ax1.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height()/2,
             f'{tps:.2f} tok/s', va='center', fontsize=10, fontweight='bold')

ax1.set_xlabel('推理速度 (tokens/s)', fontsize=12)
ax1.set_title('Llama-3 70B (Q4) 推理速度\n(RTX 4070 12GB, 溢出 28GB)', fontsize=13, fontweight='bold')
ax1.set_xlim(0, max(tps_list) * 1.3)

# 圖表 2b: Context Window 提升
# 以 Llama-3 8B 為例，KV per token = 131072 bytes
kv_per_token = 131072
vram_gb = 12
model_gb = 4.5
overhead_gb = 1.0
vram_kv_space = vram_gb - model_gb - overhead_gb  # 6.5 GB

storage_sizes = {
    "USB 128GB 隨身碟": 128 * 0.85,
    "USB 256GB SSD": 256 * 0.9,
    "USB 512GB SSD": 512 * 0.9,
    "USB 1TB SSD": 1024 * 0.9,
    "USB 2TB SSD": 2048 * 0.9,
    "SD 256GB": 256 * 0.85,
    "SD 512GB": 512 * 0.85,
    "SD 1TB": 1024 * 0.85,
}

names_s = list(storage_sizes.keys())
usable_gbs = list(storage_sizes.values())

vram_context = int(vram_kv_space * (1024**3) / kv_per_token)
boosted_contexts = []
for gb in usable_gbs:
    total_kv = (vram_kv_space + gb) * (1024**3)
    ctx = int(total_kv / kv_per_token)
    boosted_contexts.append(ctx / 1000)  # 轉為 K tokens

colors_s = []
for n in names_s:
    if "SD" in n:
        colors_s.append("#00d4ff")
    else:
        colors_s.append("#4caf50")

bars2 = ax2.barh(names_s, boosted_contexts, color=colors_s, edgecolor='white', alpha=0.85)
ax2.axvline(x=vram_context/1000, color='red', linestyle='--', linewidth=2, alpha=0.7)
ax2.text(vram_context/1000 + 50, len(names_s)-0.5, f'純 VRAM: {vram_context/1000:.0f}K',
         fontsize=10, color='red')

for bar, ctx in zip(bars2, boosted_contexts):
    ratio = ctx / (vram_context/1000)
    ax2.text(bar.get_width() + 50, bar.get_y() + bar.get_height()/2,
             f'{ctx:.0f}K ({ratio:.0f}x)', va='center', fontsize=10, fontweight='bold')

ax2.set_xlabel('最大 Context Window (K tokens)', fontsize=12)
ax2.set_title('Context Window 提升\n(Llama-3 8B, RTX 4070 12GB)', fontsize=13, fontweight='bold')

plt.tight_layout()
plt.savefig('/home/ubuntu/usb_performance_analysis.png', dpi=150, bbox_inches='tight')
plt.close()

# ─── 圖表 3: USB vs SD Express 架構差異圖 ───────────────────

fig, ax = plt.subplots(figsize=(16, 10))
ax.set_xlim(0, 16)
ax.set_ylim(0, 12)
ax.axis('off')
ax.set_title('USB vs SD Express — 架構差異比較', fontsize=18, fontweight='bold', pad=20)

# SD Express 路徑 (上半部)
ax.text(8, 11.2, 'SD Express 架構（PCIe 原生）', fontsize=14, fontweight='bold',
        ha='center', color='#00d4ff')

# SD 卡
rect = mpatches.FancyBboxPatch((0.5, 9.5), 3, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#1a237e', edgecolor='#00d4ff', linewidth=2)
ax.add_patch(rect)
ax.text(2, 10.1, 'SD Express 卡\n(NVMe 控制器)', ha='center', va='center', color='white', fontsize=10)

# 箭頭
ax.annotate('', xy=(5, 10.1), xytext=(3.5, 10.1),
            arrowprops=dict(arrowstyle='->', color='#00d4ff', lw=2))
ax.text(4.25, 10.5, 'PCIe\n(原生)', ha='center', fontsize=9, color='#00d4ff')

# SD Express 讀卡機
rect = mpatches.FancyBboxPatch((5, 9.5), 3, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#1a237e', edgecolor='#00d4ff', linewidth=2)
ax.add_patch(rect)
ax.text(6.5, 10.1, 'SD Express 讀卡機\n(PCIe 通道)', ha='center', va='center', color='white', fontsize=10)

ax.annotate('', xy=(9.5, 10.1), xytext=(8, 10.1),
            arrowprops=dict(arrowstyle='->', color='#00d4ff', lw=2))
ax.text(8.75, 10.5, 'PCIe', ha='center', fontsize=9, color='#00d4ff')

# CPU/GPU
rect = mpatches.FancyBboxPatch((9.5, 9.5), 3, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#1a237e', edgecolor='#00d4ff', linewidth=2)
ax.add_patch(rect)
ax.text(11, 10.1, 'CPU / GPU\n(PCIe Root)', ha='center', va='center', color='white', fontsize=10)

ax.text(14, 10.1, '零協定轉換\n延遲最低', ha='center', va='center', fontsize=11,
        color='#00d4ff', fontweight='bold',
        bbox=dict(boxstyle='round', facecolor='#0d1b2a', edgecolor='#00d4ff'))

# USB 3.x 路徑 (中間)
ax.text(8, 7.7, 'USB 3.x 架構（需要橋接晶片）', fontsize=14, fontweight='bold',
        ha='center', color='#f44336')

# USB 儲存裝置
rect = mpatches.FancyBboxPatch((0.5, 6), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#4a0000', edgecolor='#f44336', linewidth=2)
ax.add_patch(rect)
ax.text(1.75, 6.6, 'NVMe SSD', ha='center', va='center', color='white', fontsize=10)

# 橋接晶片
ax.annotate('', xy=(4, 6.6), xytext=(3, 6.6),
            arrowprops=dict(arrowstyle='->', color='#f44336', lw=2))
ax.text(3.5, 7.0, 'PCIe', ha='center', fontsize=9, color='#f44336')

rect = mpatches.FancyBboxPatch((4, 6), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#4a0000', edgecolor='#ff5252', linewidth=2)
ax.add_patch(rect)
ax.text(5.25, 6.6, 'USB-NVMe\n橋接晶片', ha='center', va='center', color='white', fontsize=10)

ax.annotate('', xy=(7.5, 6.6), xytext=(6.5, 6.6),
            arrowprops=dict(arrowstyle='->', color='#f44336', lw=2))
ax.text(7, 7.0, 'USB', ha='center', fontsize=9, color='#f44336')

rect = mpatches.FancyBboxPatch((7.5, 6), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#4a0000', edgecolor='#f44336', linewidth=2)
ax.add_patch(rect)
ax.text(8.75, 6.6, 'USB 控制器\n(xHCI)', ha='center', va='center', color='white', fontsize=10)

ax.annotate('', xy=(11, 6.6), xytext=(10, 6.6),
            arrowprops=dict(arrowstyle='->', color='#f44336', lw=2))

rect = mpatches.FancyBboxPatch((11, 6), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#4a0000', edgecolor='#f44336', linewidth=2)
ax.add_patch(rect)
ax.text(12.25, 6.6, 'CPU / GPU', ha='center', va='center', color='white', fontsize=10)

ax.text(14.5, 6.6, '2次協定轉換\n延遲較高', ha='center', va='center', fontsize=11,
        color='#f44336', fontweight='bold',
        bbox=dict(boxstyle='round', facecolor='#1a0000', edgecolor='#f44336'))

# USB4 路徑 (下半部)
ax.text(8, 4.2, 'USB4 架構（PCIe Tunneling）', fontsize=14, fontweight='bold',
        ha='center', color='#4caf50')

# USB4 NVMe 外接盒
rect = mpatches.FancyBboxPatch((0.5, 2.5), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#1b5e20', edgecolor='#4caf50', linewidth=2)
ax.add_patch(rect)
ax.text(1.75, 3.1, 'NVMe SSD', ha='center', va='center', color='white', fontsize=10)

ax.annotate('', xy=(4, 3.1), xytext=(3, 3.1),
            arrowprops=dict(arrowstyle='->', color='#4caf50', lw=2))
ax.text(3.5, 3.5, 'PCIe', ha='center', fontsize=9, color='#4caf50')

rect = mpatches.FancyBboxPatch((4, 2.5), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#1b5e20', edgecolor='#4caf50', linewidth=2)
ax.add_patch(rect)
ax.text(5.25, 3.1, 'USB4 控制器\n(PCIe Router)', ha='center', va='center', color='white', fontsize=10)

ax.annotate('', xy=(7.5, 3.1), xytext=(6.5, 3.1),
            arrowprops=dict(arrowstyle='->', color='#4caf50', lw=2))
ax.text(7, 3.5, 'PCIe\nTunnel', ha='center', fontsize=9, color='#4caf50')

rect = mpatches.FancyBboxPatch((7.5, 2.5), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#1b5e20', edgecolor='#4caf50', linewidth=2)
ax.add_patch(rect)
ax.text(8.75, 3.1, 'USB4 控制器\n(PCIe Router)', ha='center', va='center', color='white', fontsize=10)

ax.annotate('', xy=(11, 3.1), xytext=(10, 3.1),
            arrowprops=dict(arrowstyle='->', color='#4caf50', lw=2))
ax.text(10.5, 3.5, 'PCIe', ha='center', fontsize=9, color='#4caf50')

rect = mpatches.FancyBboxPatch((11, 2.5), 2.5, 1.2, boxstyle="round,pad=0.1",
                                facecolor='#1b5e20', edgecolor='#4caf50', linewidth=2)
ax.add_patch(rect)
ax.text(12.25, 3.1, 'CPU / GPU', ha='center', va='center', color='white', fontsize=10)

ax.text(14.5, 3.1, 'PCIe 封裝傳輸\n延遲接近原生', ha='center', va='center', fontsize=11,
        color='#4caf50', fontweight='bold',
        bbox=dict(boxstyle='round', facecolor='#0a1a0a', edgecolor='#4caf50'))

# 底部說明
ax.text(8, 0.8, '結論：USB4 的 PCIe Tunneling 讓 USB 外接 NVMe SSD 的效能接近 SD Express，\n'
        '而 USB4 V2 (80Gbps) 甚至超越 SD Express Gen4 x2 (3,940 MB/s)',
        ha='center', va='center', fontsize=12, fontweight='bold',
        bbox=dict(boxstyle='round', facecolor='#1a1a2e', edgecolor='#ffab00', linewidth=2),
        color='#ffab00')

plt.savefig('/home/ubuntu/usb_vs_sd_architecture.png', dpi=150, bbox_inches='tight',
            facecolor='#0d1117', edgecolor='none')
plt.close()

# ─── 圖表 4: 產品線定位矩陣 ─────────────────────────────────

fig, ax = plt.subplots(figsize=(14, 10))
ax.set_xlim(0, 14)
ax.set_ylim(0, 10)
ax.axis('off')
ax.set_title('USB-VRAM Booster 產品線定位', fontsize=18, fontweight='bold', pad=20)

products = [
    {"name": "Lite\n(USB 3.2 隨身碟)", "x": 2, "y": 7.5, "bw": "~1 GB/s",
     "target": "入門體驗", "price": "$15-30", "color": "#f44336", "size": 1.3},
    {"name": "Standard\n(USB4 NVMe 外接盒)", "x": 6, "y": 7.5, "bw": "~3.5 GB/s",
     "target": "主流 AI 推理", "price": "$50-80", "color": "#4caf50", "size": 1.5},
    {"name": "Pro\n(USB4 V2 NVMe)", "x": 10, "y": 7.5, "bw": "~8 GB/s",
     "target": "專業 AI 工作站", "price": "$100-150", "color": "#2196f3", "size": 1.7},
    {"name": "Ultra\n(TB5 NVMe RAID)", "x": 12.5, "y": 7.5, "bw": "~12 GB/s",
     "target": "極致效能", "price": "$200+", "color": "#9c27b0", "size": 1.5},
]

for p in products:
    circle = plt.Circle((p["x"], p["y"]), p["size"], facecolor=p["color"],
                         alpha=0.2, edgecolor=p["color"], linewidth=2)
    ax.add_patch(circle)
    ax.text(p["x"], p["y"]+0.3, p["name"], ha='center', va='center',
            fontsize=11, fontweight='bold', color=p["color"])
    ax.text(p["x"], p["y"]-0.7, f'{p["bw"]}\n{p["target"]}\n{p["price"]}',
            ha='center', va='center', fontsize=9, color='gray')

# 箭頭表示升級路徑
for i in range(len(products)-1):
    ax.annotate('', xy=(products[i+1]["x"]-products[i+1]["size"]-0.2, products[i+1]["y"]),
                xytext=(products[i]["x"]+products[i]["size"]+0.2, products[i]["y"]),
                arrowprops=dict(arrowstyle='->', color='white', lw=1.5, alpha=0.5))

# 與 SD-VRAM Booster 的對比
ax.text(7, 3.5, 'vs SD-VRAM Booster', fontsize=14, fontweight='bold',
        ha='center', color='#00d4ff')

sd_products = [
    {"name": "SD Express\nGen3x1", "x": 3, "y": 2, "bw": "~880 MB/s", "color": "#00d4ff"},
    {"name": "SD Express\nGen3x2", "x": 7, "y": 2, "bw": "~1.75 GB/s", "color": "#00d4ff"},
    {"name": "SD Express\nGen4x2", "x": 11, "y": 2, "bw": "~3.5 GB/s", "color": "#00d4ff"},
]

for p in sd_products:
    rect = mpatches.FancyBboxPatch((p["x"]-1.2, p["y"]-0.6), 2.4, 1.2,
                                    boxstyle="round,pad=0.1",
                                    facecolor='#0d1b2a', edgecolor=p["color"], linewidth=2)
    ax.add_patch(rect)
    ax.text(p["x"], p["y"]+0.1, p["name"], ha='center', va='center',
            fontsize=10, fontweight='bold', color=p["color"])
    ax.text(p["x"], p["y"]-0.35, p["bw"], ha='center', va='center',
            fontsize=9, color='gray')

# 優勢比較
ax.text(7, 0.5, 'USB 優勢：裝置普及度高、容量更大、USB4 V2 頻寬更高\n'
        'SD 優勢：體積極小、熱插拔更方便、可作為 AI 模型卡匣',
        ha='center', va='center', fontsize=11,
        bbox=dict(boxstyle='round', facecolor='#1a1a2e', edgecolor='#666', linewidth=1),
        color='white')

plt.savefig('/home/ubuntu/usb_product_lineup.png', dpi=150, bbox_inches='tight',
            facecolor='#0d1117', edgecolor='none')
plt.close()

print("✓ 圖表已生成:")
print("  1. usb_bandwidth_comparison.png — USB 各版本頻寬比較")
print("  2. usb_performance_analysis.png — LLM 推理效能與 Context Window 預估")
print("  3. usb_vs_sd_architecture.png — USB vs SD Express 架構差異")
print("  4. usb_product_lineup.png — 產品線定位矩陣")
