"""
四種 VRAM 擴展架構競品對比分析
SSD 直連 vs SD 卡 vs USB 儲存 vs 外接硬碟盒
製作者：DONG. WEI YANG
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.style as style
style.use('dark_background')
matplotlib.rcParams['font.family'] = 'Noto Sans CJK SC'
matplotlib.rcParams['axes.unicode_minus'] = False

import numpy as np

# ─── 四種架構的真實物理數據 ───────────────────────────

# 顏色定義
C_SSD = "#ff6b6b"      # 紅色 — SSD 競品基準
C_SD  = "#00ff88"      # 綠色 — SD 卡
C_USB = "#ffaa00"      # 橙色 — USB 儲存
C_ENC = "#4ecdc4"      # 青色 — 外接硬碟盒

colors4 = [C_SSD, C_SD, C_USB, C_ENC]
names4 = ["SSD 直連\n(NVMe M.2)", "SD 卡\n(SD Express)", "USB 儲存\n(xHCI Bridge)", "外接硬碟盒\n(PCIe Tunnel)"]
names4_short = ["SSD 直連", "SD 卡", "USB 儲存", "外接硬碟盒"]

# ─── 圖 1: 架構資料路徑 + 頻寬 + 延遲 總覽 ─────────────

fig, axes = plt.subplots(1, 3, figsize=(20, 7))

# 1a: 頻寬比較 (最大值)
ax = axes[0]
bw_max = [14000, 3940, 2500, 10000]  # MB/s
bw_min = [3500, 985, 625, 2800]
bw_typical = [7000, 1970, 1250, 5000]

x = np.arange(4)
bars = ax.bar(x, bw_max, 0.6, color=colors4, alpha=0.3, edgecolor=colors4, linewidth=2, label='最大值')
bars2 = ax.bar(x, bw_typical, 0.6, color=colors4, alpha=0.6, label='典型值')
bars3 = ax.bar(x, bw_min, 0.6, color=colors4, alpha=0.9, label='最低值')

for i, (mx, tp, mn) in enumerate(zip(bw_max, bw_typical, bw_min)):
    ax.text(i, mx + 200, f'{mx:,}', ha='center', fontsize=9, color=colors4[i], fontweight='bold')

ax.set_xticks(x)
ax.set_xticklabels(names4, fontsize=9)
ax.set_ylabel("頻寬 (MB/s)", fontsize=11)
ax.set_title("頻寬比較\n(越高越好)", fontsize=13, fontweight='bold')
ax.legend(fontsize=8)
ax.grid(axis='y', alpha=0.2)

# 1b: 延遲比較
ax = axes[1]
latency = [4, 9, 140, 13]  # μs 平均
lat_colors = [C_SSD, C_SD, C_USB, C_ENC]

bars = ax.barh(x, latency, 0.6, color=lat_colors, alpha=0.8, edgecolor='white', linewidth=0.5)
for i, v in enumerate(latency):
    ax.text(v + 2, i, f'{v} μs', va='center', fontsize=11, color=lat_colors[i], fontweight='bold')

ax.set_yticks(x)
ax.set_yticklabels(names4, fontsize=9)
ax.set_xlabel("延遲 (μs)", fontsize=11)
ax.set_title("延遲比較\n(越低越好)", fontsize=13, fontweight='bold')
ax.set_xlim(0, 180)
ax.grid(axis='x', alpha=0.2)

# 標記 USB 的延遲問題
ax.annotate("⚠ 雙重協定轉換\n延遲高 35 倍", xy=(140, 2), xytext=(140, 3.2),
            fontsize=9, color=C_USB, ha='center',
            arrowprops=dict(arrowstyle='->', color=C_USB))

# 1c: 協定轉換次數
ax = axes[2]
conversions = [0, 0, 2, 1]
conv_labels = [
    "GPU→PCIe→NVMe\n(原生直通)",
    "GPU→PCIe→NVMe\n(SD Express 原生)",
    "GPU→PCIe→xHCI\n→Bridge→Flash\n(雙重轉換)",
    "GPU→PCIe→Router\n→TB線纜→NVMe\n(單次封裝)"
]

bars = ax.bar(x, conversions, 0.6, color=colors4, alpha=0.8, edgecolor='white', linewidth=0.5)
for i, (v, label) in enumerate(zip(conversions, conv_labels)):
    ax.text(i, v + 0.08, f'{v} 次', ha='center', fontsize=12, color=colors4[i], fontweight='bold')
    ax.text(i, -0.5, label, ha='center', fontsize=7.5, color=colors4[i], alpha=0.8)

ax.set_xticks(x)
ax.set_xticklabels(names4, fontsize=9)
ax.set_ylabel("協定轉換次數", fontsize=11)
ax.set_title("協定轉換次數\n(越少越好)", fontsize=13, fontweight='bold')
ax.set_ylim(-1.5, 3)
ax.grid(axis='y', alpha=0.2)

plt.suptitle("四種 VRAM 擴展架構 — 核心指標對比", fontsize=16, fontweight='bold', y=1.02)
plt.tight_layout()
plt.savefig("/home/ubuntu/four_arch_core_metrics.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ 核心指標對比圖已儲存")


# ─── 圖 2: LLM 推理效能對比 (基於 RTX 4070 12GB) ──────

fig, axes = plt.subplots(1, 2, figsize=(18, 8))

# 2a: 70B 模型推理速度 (tok/s) — 各規格細分
ax = axes[0]

configs = [
    # (名稱, tok/s, 顏色, 架構)
    # SSD 直連
    ("SSD Gen3 x4\n(3.5 GB/s)", 3.17, C_SSD),
    ("SSD Gen4 x4\n(7 GB/s)", 6.33, C_SSD),
    ("SSD Gen5 x4\n(14 GB/s)", 12.67, C_SSD),
    # SD 卡
    ("SD Gen3 x1\n(985 MB/s)", 0.89, C_SD),
    ("SD Gen3 x2\n(1.97 GB/s)", 1.78, C_SD),
    ("SD Gen4 x2\n(3.94 GB/s)", 3.54, C_SD),
    # USB 儲存
    ("USB 3.2 Gen1\n(625 MB/s)", 0.57, C_USB),
    ("USB 3.2 Gen2\n(1.25 GB/s)", 1.14, C_USB),
    ("USB 3.2 Gen2x2\n(2.5 GB/s)", 1.81, C_USB),
    # 外接硬碟盒
    ("TB3\n(2.8 GB/s)", 2.53, C_ENC),
    ("TB4/USB4v1\n(3.8 GB/s)", 3.44, C_ENC),
    ("USB4v2\n(7.5 GB/s)", 5.33, C_ENC),
    ("TB5\n(10 GB/s)", 6.92, C_ENC),
]

y_pos = np.arange(len(configs))
names_c = [c[0] for c in configs]
vals = [c[1] for c in configs]
cols = [c[2] for c in configs]

bars = ax.barh(y_pos, vals, 0.7, color=cols, alpha=0.8, edgecolor='white', linewidth=0.3)
for i, v in enumerate(vals):
    ax.text(v + 0.15, i, f'{v:.2f} t/s', va='center', fontsize=8, color=cols[i], fontweight='bold')

# 標記實用門檻
ax.axvline(x=3.0, color='white', linestyle='--', alpha=0.4, linewidth=1)
ax.text(3.1, len(configs)-0.5, "實用門檻\n(3 tok/s)", fontsize=8, color='white', alpha=0.5)

ax.set_yticks(y_pos)
ax.set_yticklabels(names_c, fontsize=7.5)
ax.set_xlabel("Llama-3 70B Q4 推理速度 (tokens/s)", fontsize=11)
ax.set_title("70B 模型推理速度\n(RTX 4070 12GB 基準)", fontsize=13, fontweight='bold')
ax.grid(axis='x', alpha=0.2)
ax.invert_yaxis()

# 2b: 容量 vs 成本 vs 效能 氣泡圖
ax = axes[1]

bubble_data = [
    # (名稱, 容量GB, 每TB成本USD, 70B tps, 顏色)
    ("SSD Gen4\n2TB", 2048, 80, 6.33, C_SSD),
    ("SSD Gen4\n4TB", 4096, 100, 6.33, C_SSD),
    ("SSD Gen5\n4TB", 4096, 150, 12.67, C_SSD),
    ("SD Gen4 x2\n1TB", 1024, 150, 3.54, C_SD),
    ("SD Gen3 x2\n512GB", 512, 120, 1.78, C_SD),
    ("USB Gen2x2\n2TB", 2048, 80, 1.81, C_USB),
    ("USB Gen2\n1TB", 1024, 70, 1.14, C_USB),
    ("TB4 外接盒\n4TB", 4096, 120, 3.44, C_ENC),
    ("USB4v2 外接盒\n4TB", 4096, 115, 5.33, C_ENC),
    ("TB5 外接盒\n8TB", 8192, 130, 6.92, C_ENC),
]

for name, cap, cost, tps, color in bubble_data:
    size = cap / 6
    ax.scatter(tps, cap/1024, s=size, c=color, alpha=0.65, edgecolors='white', linewidth=1)
    ax.annotate(name, (tps, cap/1024), textcoords="offset points",
                xytext=(8, 5), fontsize=7, color=color, alpha=0.9)

# 實用門檻
ax.axvline(x=3.0, color='white', linestyle='--', alpha=0.3)
ax.text(3.1, 8.5, "實用門檻", fontsize=8, color='white', alpha=0.4)

ax.set_xlabel("70B 推理速度 (tok/s)", fontsize=11)
ax.set_ylabel("容量 (TB)", fontsize=11)
ax.set_title("容量 vs 效能 (氣泡 = 容量大小)", fontsize=13, fontweight='bold')
ax.grid(True, alpha=0.15)

from matplotlib.lines import Line2D
legend_elements = [
    Line2D([0], [0], marker='o', color='w', markerfacecolor=C_SSD, markersize=12, label='SSD 直連 (競品基準)'),
    Line2D([0], [0], marker='o', color='w', markerfacecolor=C_SD, markersize=12, label='SD 卡'),
    Line2D([0], [0], marker='o', color='w', markerfacecolor=C_USB, markersize=12, label='USB 儲存'),
    Line2D([0], [0], marker='o', color='w', markerfacecolor=C_ENC, markersize=12, label='外接硬碟盒'),
]
ax.legend(handles=legend_elements, fontsize=9, loc='upper left')

plt.suptitle("四種架構 — LLM 推理效能與容量對比", fontsize=16, fontweight='bold', y=1.02)
plt.tight_layout()
plt.savefig("/home/ubuntu/four_arch_llm_performance.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ LLM 推理效能對比圖已儲存")


# ─── 圖 3: 八維度雷達圖 + 場景加權評分 ────────────────

fig, axes = plt.subplots(1, 2, figsize=(18, 8))

dimensions = [
    "頻寬\n(速度)", "延遲\n(反應)", "容量\n(擴展)", "成本效益\n(CP值)",
    "便攜性\n(攜帶)", "相容性\n(通用)", "市場成熟度\n(可買到)", "安裝難度\n(易用)"
]

# 基於物理數據的評分 (0-10)
scores = {
    "SSD 直連": [
        9.5,   # 頻寬: Gen5 14GB/s 最強
        10.0,  # 延遲: 3-5μs 最低
        8.0,   # 容量: 單顆 8TB, 可 RAID
        7.0,   # 成本: $80-150/TB
        1.0,   # 便攜: 固定在主機板上
        6.0,   # 相容: 需要 M.2 插槽 + 主機板
        9.0,   # 市場: NVMe SSD 到處買
        3.0,   # 安裝: 需開機殼、安裝軟體
    ],
    "SD 卡": [
        6.5,   # 頻寬: 3940 MB/s
        9.5,   # 延遲: 8-10μs
        3.0,   # 容量: 最大 1TB
        5.5,   # 成本: $150/TB
        10.0,  # 便攜: 指甲大小
        4.0,   # 相容: 需 SD Express 讀卡機
        3.0,   # 市場: SD Express 稀少
        9.0,   # 安裝: 插入即可
    ],
    "USB 儲存": [
        4.0,   # 頻寬: 2500 MB/s
        2.0,   # 延遲: 140μs
        6.0,   # 容量: 4TB
        8.5,   # 成本: $80/TB
        8.0,   # 便攜: 隨身碟大小
        10.0,  # 相容: 所有電腦
        10.0,  # 市場: 到處買
        9.5,   # 安裝: 插入即可
    ],
    "外接硬碟盒": [
        10.0,  # 頻寬: TB5 10GB/s
        8.5,   # 延遲: 10-15μs
        10.0,  # 容量: 8TB+
        7.0,   # 成本: $120/TB
        3.0,   # 便攜: 外接盒+線纜
        4.5,   # 相容: 需 USB4/TB4+
        4.0,   # 市場: 逐漸普及
        6.0,   # 安裝: 需外接盒+SSD
    ],
}

# 雷達圖
ax = axes[0]
angles = np.linspace(0, 2 * np.pi, len(dimensions), endpoint=False).tolist()
angles += angles[:1]

for i, (name, score_list) in enumerate(scores.items()):
    values = score_list + score_list[:1]
    ax.plot(angles, values, 'o-', linewidth=2, color=colors4[i], label=name, markersize=5)
    ax.fill(angles, values, alpha=0.08, color=colors4[i])

ax.set_xticks(angles[:-1])
ax.set_xticklabels(dimensions, fontsize=8.5)
ax.set_ylim(0, 11)
ax.set_yticks([2, 4, 6, 8, 10])
ax.set_yticklabels(["2", "4", "6", "8", "10"], fontsize=8)
ax.legend(loc='upper right', bbox_to_anchor=(1.35, 1.1), fontsize=10)
ax.set_title("八維度綜合評分", fontsize=13, fontweight='bold', pad=20)
ax.grid(True, alpha=0.3)

# 場景加權評分
ax2 = axes[1]

scenarios = {
    "AI 研究者\n(效能優先)": [0.25, 0.20, 0.20, 0.05, 0.02, 0.08, 0.10, 0.10],
    "一般使用者\n(易用優先)": [0.10, 0.05, 0.10, 0.20, 0.10, 0.25, 0.15, 0.05],
    "行動工作者\n(攜帶優先)": [0.15, 0.10, 0.10, 0.15, 0.25, 0.10, 0.05, 0.10],
    "企業部署\n(穩定優先)": [0.20, 0.15, 0.15, 0.10, 0.02, 0.15, 0.15, 0.08],
}

x_s = np.arange(len(scenarios))
width = 0.2

for i, (arch_name, score_list) in enumerate(scores.items()):
    weighted = []
    for sc_name, weights in scenarios.items():
        ws = sum(s * w for s, w in zip(score_list, weights))
        weighted.append(ws)
    bars = ax2.bar(x_s + i * width, weighted, width, label=arch_name,
                   color=colors4[i], alpha=0.85, edgecolor='white', linewidth=0.3)
    for bar, val in zip(bars, weighted):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                 f'{val:.1f}', ha='center', va='bottom', fontsize=8.5, fontweight='bold',
                 color=colors4[i])

ax2.set_xticks(x_s + width * 1.5)
ax2.set_xticklabels(list(scenarios.keys()), fontsize=9)
ax2.set_ylabel("加權總分 (滿分 10)", fontsize=11)
ax2.set_ylim(0, 11)
ax2.set_title("不同使用場景的最佳方案", fontsize=13, fontweight='bold')
ax2.legend(fontsize=8.5, loc='upper right')
ax2.grid(axis='y', alpha=0.2)

plt.suptitle("四種架構 — 綜合效益評分", fontsize=16, fontweight='bold', y=1.02)
plt.tight_layout()
plt.savefig("/home/ubuntu/four_arch_radar_scores.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ 雷達圖 + 場景評分已儲存")


# ─── 圖 4: 最終結論 — 競品定位矩陣 ───────────────────

fig, ax = plt.subplots(figsize=(14, 10))

# 定位矩陣: X=便利性, Y=效能
# SSD: 高效能, 低便利
# SD: 中效能, 高便利
# USB: 低效能, 高便利
# 外接盒: 高效能, 中便利

positions = {
    "SSD 直連\n(現有競品)": (2.5, 9.5, 800, C_SSD),
    "SD 卡\n(我們的產品)": (8.5, 6.0, 500, C_SD),
    "USB 儲存\n(我們的產品)": (9.0, 3.0, 600, C_USB),
    "外接硬碟盒\n(我們的產品)": (4.5, 8.5, 900, C_ENC),
}

for name, (px, py, size, color) in positions.items():
    ax.scatter(px, py, s=size, c=color, alpha=0.7, edgecolors='white', linewidth=2, zorder=5)
    ax.annotate(name, (px, py), textcoords="offset points",
                xytext=(0, -30), fontsize=12, color=color, fontweight='bold', ha='center')

# 象限標記
ax.axhline(y=5.5, color='white', linestyle='--', alpha=0.15)
ax.axvline(x=5.5, color='white', linestyle='--', alpha=0.15)

ax.text(1.5, 10.2, "高效能 / 低便利\n(專業級)", fontsize=10, color='white', alpha=0.3, ha='center')
ax.text(8.5, 10.2, "高效能 / 高便利\n(理想區)", fontsize=10, color='#00ff88', alpha=0.5, ha='center', fontweight='bold')
ax.text(1.5, 0.8, "低效能 / 低便利\n(不推薦)", fontsize=10, color='white', alpha=0.2, ha='center')
ax.text(8.5, 0.8, "低效能 / 高便利\n(入門級)", fontsize=10, color='white', alpha=0.3, ha='center')

# 畫箭頭表示我們的產品優勢方向
ax.annotate("", xy=(7, 7.5), xytext=(3.5, 9.0),
            arrowprops=dict(arrowstyle='->', color=C_SD, lw=2, alpha=0.4))
ax.text(5.2, 8.8, "SD 卡填補\n便攜空白", fontsize=9, color=C_SD, alpha=0.6, ha='center')

# 高亮最佳效益區
from matplotlib.patches import FancyBboxPatch
rect = FancyBboxPatch((6, 6.5), 4, 4, boxstyle="round,pad=0.3",
                       facecolor='#00ff88', alpha=0.05, edgecolor='#00ff88', linewidth=1)
ax.add_patch(rect)

ax.set_xlabel("便利性 (安裝難度 + 便攜性 + 相容性)", fontsize=12)
ax.set_ylabel("效能 (頻寬 + 延遲 + 容量)", fontsize=12)
ax.set_xlim(0, 11)
ax.set_ylim(0, 11)
ax.set_title("四種架構 — 競品定位矩陣\nby DONG. WEI YANG", fontsize=15, fontweight='bold')
ax.grid(True, alpha=0.1)

plt.tight_layout()
plt.savefig("/home/ubuntu/four_arch_positioning.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ 競品定位矩陣已儲存")


# ─── 圖 5: 最終結論表格 ──────────────────────────────

fig, ax = plt.subplots(figsize=(18, 9))
ax.axis('off')

data = [
    ["評估維度", "SSD 直連 (NVMe M.2)\n【現有競品基準】", "SD 卡 (SD Express)\n【我們的產品】",
     "USB 儲存 (xHCI)\n【我們的產品】", "外接硬碟盒 (PCIe Tunnel)\n【我們的產品】"],
    ["協定路徑", "PCIe→NVMe\n(原生直通)", "PCIe→NVMe\n(SD Express 原生)", "PCIe→xHCI→Bridge\n(雙重轉換)", "PCIe→Router→NVMe\n(單次封裝)"],
    ["協定轉換", "0 次 ★", "0 次 ★", "2 次", "1 次"],
    ["最大頻寬", "14,000 MB/s ★\n(Gen5 x4)", "3,940 MB/s\n(Gen4 x2)", "2,500 MB/s\n(Gen2x2)", "10,000 MB/s\n(TB5)"],
    ["延遲", "3-5 μs ★", "8-10 μs", "80-200 μs", "10-15 μs"],
    ["最大容量", "8 TB (可RAID)", "1 TB", "4 TB", "8+ TB ★"],
    ["成本/TB", "$80-150", "$150", "$80 ★", "$120"],
    ["便攜性", "★ (固定)", "★★★★★ (指甲)", "★★★★ (隨身碟)", "★★ (外接盒)"],
    ["相容性", "★★★ (需M.2)", "★★ (需讀卡機)", "★★★★★ (通用)", "★★★ (需USB4/TB)"],
    ["70B 推理", "6.33 tok/s\n(Gen4)", "3.54 tok/s\n(Gen4x2)", "1.81 tok/s\n(Gen2x2)", "6.92 tok/s ★\n(TB5)"],
    ["Context 提升", "490x\n(4TB Gen4)", "130x\n(1TB)", "130x\n(1TB)", "490x ★\n(8TB TB5)"],
    ["市場現狀", "已有產品\n(Phison aiDAPTIV+)", "全新概念\n(無競品)", "全新概念\n(無競品)", "全新概念\n(無競品)"],
    ["核心定位", "效能基準\n(專業固定式)", "便攜之王\n(模型卡匣)", "入門首選\n(最易取得)", "效能之王\n(外接式最強)"],
]

table = ax.table(cellText=data, loc='center', cellLoc='center')
table.auto_set_font_size(False)
table.set_fontsize(9)
table.scale(1, 2.0)

# 設定顏色
header_colors = ['#16213e', C_SSD, C_SD, C_USB, C_ENC]
for i in range(len(data)):
    for j in range(5):
        cell = table[i, j]
        if i == 0:
            cell.set_facecolor(header_colors[j] if j > 0 else '#16213e')
            alpha = 0.4 if j > 0 else 1.0
            cell.set_facecolor(header_colors[j] if j > 0 else '#16213e')
            cell.get_text().set_alpha(1.0)
            cell.set_text_props(fontweight='bold', color='white', fontsize=9)
        elif i == len(data) - 1:
            cell.set_facecolor(header_colors[j] if j > 0 else '#16213e')
            cell.set_text_props(fontweight='bold', color='white' if j != 1 else 'black', fontsize=9.5)
        else:
            cell.set_facecolor('#1a1a2e' if i % 2 == 0 else '#0f0f23')
            cell.set_text_props(color='white', fontsize=8.5)
            text = str(data[i][j])
            if "★" in text and j > 0:
                cell.set_text_props(color=header_colors[j], fontweight='bold', fontsize=9)
        cell.set_edgecolor('#333')

ax.set_title("四種 VRAM 擴展架構 — 完整競品對比分析\nby DONG. WEI YANG",
             fontsize=16, fontweight='bold', color='white', pad=20)

plt.tight_layout()
plt.savefig("/home/ubuntu/four_arch_final_table.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ 最終結論表格已儲存")

print("\n=== 四種架構競品分析完成 ===")
