"""
三種 VRAM 擴展架構綜合效益分析
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

# ─── 三種架構的真實物理數據 ───────────────────────────

architectures = {
    "SD 卡\n(SD Express)": {
        "max_bw_mbs": 3940,
        "min_bw_mbs": 985,       # Gen3 x1
        "latency_us": 9,         # 平均
        "max_capacity_gb": 1024,
        "protocol_conversions": 0,
        "cost_per_tb_usd": 150,  # SD Express 1TB 約 $150
        "hotswap": True,
        "portable": True,
        "form_factor": "指甲大小",
        "market_ready": 0.4,     # SD Express 市場滲透率低
        "host_requirement": "SD Express 讀卡機",
        # 以 RTX 4070 (12GB) + 1TB 為基準
        "vram_expansion_gb": 921,
        "expansion_ratio": 78,
        "llm_70b_tps": 3.54,
        "context_boost_x": 130,
    },
    "USB 儲存\n(xHCI Bridge)": {
        "max_bw_mbs": 2500,
        "min_bw_mbs": 625,       # USB 3.2 Gen1
        "latency_us": 140,       # 平均
        "max_capacity_gb": 4096,
        "protocol_conversions": 2,
        "cost_per_tb_usd": 80,   # USB SSD 1TB 約 $80
        "hotswap": True,
        "portable": True,
        "form_factor": "隨身碟大小",
        "market_ready": 0.95,    # USB 3.x 幾乎所有電腦都有
        "host_requirement": "USB 3.x 埠 (幾乎所有電腦)",
        "vram_expansion_gb": 921,
        "expansion_ratio": 78,
        "llm_70b_tps": 1.81,
        "context_boost_x": 130,
    },
    "外接硬碟盒\n(PCIe Tunnel)": {
        "max_bw_mbs": 10000,
        "min_bw_mbs": 2800,      # TB3
        "latency_us": 13,        # 平均
        "max_capacity_gb": 8192,
        "protocol_conversions": 1,
        "cost_per_tb_usd": 120,  # NVMe SSD $80 + 外接盒 $40/TB
        "hotswap": True,
        "portable": False,       # 需要外接盒 + 線纜
        "form_factor": "外接盒 + 線纜",
        "market_ready": 0.35,    # USB4/TB4 尚未普及
        "host_requirement": "USB4 / Thunderbolt 4+ 埠",
        "vram_expansion_gb": 7102,
        "expansion_ratio": 592,
        "llm_70b_tps": 6.92,
        "context_boost_x": 490,
    },
}

# ─── 圖 1: 多維度量化評分雷達圖 ───────────────────────

fig, axes = plt.subplots(1, 2, figsize=(18, 8))

# 評分維度 (0-10 分)
dimensions = [
    "頻寬\n(速度)",
    "延遲\n(反應)",
    "容量\n(擴展)",
    "成本效益\n(CP值)",
    "便攜性\n(攜帶)",
    "相容性\n(通用)",
    "市場成熟度\n(可買到)",
    "安裝難度\n(易用)",
]

# 基於物理數據計算評分
scores = {
    "SD 卡\n(SD Express)": [
        6.5,   # 頻寬: 3940 MB/s，中上
        9.5,   # 延遲: 9μs，極低（0次轉換）
        3.0,   # 容量: 最大 1TB，受限
        5.5,   # 成本: $150/TB，偏貴
        10.0,  # 便攜: 指甲大小，最佳
        4.0,   # 相容: 需要 SD Express 讀卡機，少見
        3.0,   # 市場: SD Express 卡和讀卡機都稀少
        9.0,   # 安裝: 插入即可
    ],
    "USB 儲存\n(xHCI Bridge)": [
        4.0,   # 頻寬: 2500 MB/s，受限於雙重轉換
        2.0,   # 延遲: 140μs，最差
        6.0,   # 容量: 最大 4TB
        8.5,   # 成本: $80/TB，最便宜
        8.0,   # 便攜: 隨身碟大小
        10.0,  # 相容: 幾乎所有電腦都有 USB 3.x
        10.0,  # 市場: 到處買得到
        9.5,   # 安裝: 插入即可
    ],
    "外接硬碟盒\n(PCIe Tunnel)": [
        10.0,  # 頻寬: 10000 MB/s，最強
        8.5,   # 延遲: 13μs，接近原生
        10.0,  # 容量: 8TB+，最大
        7.0,   # 成本: $120/TB，中等
        3.0,   # 便攜: 外接盒+線纜
        4.5,   # 相容: 需要 USB4/TB4+
        4.0,   # 市場: USB4/TB4 逐漸普及
        6.0,   # 安裝: 需要外接盒+SSD+線纜
    ],
}

# 雷達圖
ax = axes[0]
angles = np.linspace(0, 2 * np.pi, len(dimensions), endpoint=False).tolist()
angles += angles[:1]

colors = ["#00ff88", "#ffaa00", "#e94560"]
for i, (name, score_list) in enumerate(scores.items()):
    values = score_list + score_list[:1]
    ax.plot(angles, values, 'o-', linewidth=2, color=colors[i], label=name, markersize=6)
    ax.fill(angles, values, alpha=0.1, color=colors[i])

ax.set_xticks(angles[:-1])
ax.set_xticklabels(dimensions, fontsize=9)
ax.set_ylim(0, 11)
ax.set_yticks([2, 4, 6, 8, 10])
ax.set_yticklabels(["2", "4", "6", "8", "10"], fontsize=8)
ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=10)
ax.set_title("八維度綜合評分", fontsize=14, fontweight='bold', pad=20)
ax.grid(True, alpha=0.3)

# 圖 2: 加權總分（不同使用場景）
ax2 = axes[1]

# 三種使用場景的權重
scenarios = {
    "AI 研究者\n(效能優先)": {
        "weights": [0.25, 0.20, 0.20, 0.05, 0.02, 0.08, 0.10, 0.10],
        "description": "追求最大模型、最快推理"
    },
    "一般使用者\n(易用優先)": {
        "weights": [0.10, 0.05, 0.10, 0.20, 0.10, 0.25, 0.15, 0.05],
        "description": "想跑 AI 但不想折騰"
    },
    "行動工作者\n(攜帶優先)": {
        "weights": [0.15, 0.10, 0.10, 0.15, 0.25, 0.10, 0.05, 0.10],
        "description": "筆電用戶，隨時隨地"
    },
}

x = np.arange(len(scenarios))
width = 0.25
arch_names = list(scores.keys())

for i, (arch_name, score_list) in enumerate(scores.items()):
    weighted_scores = []
    for scenario_name, scenario in scenarios.items():
        ws = sum(s * w for s, w in zip(score_list, scenario["weights"]))
        weighted_scores.append(ws)
    bars = ax2.bar(x + i * width, weighted_scores, width, label=arch_name,
                    color=colors[i], alpha=0.85, edgecolor='white', linewidth=0.5)
    for bar, val in zip(bars, weighted_scores):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.15,
                 f'{val:.1f}', ha='center', va='bottom', fontsize=10, fontweight='bold',
                 color=colors[i])

ax2.set_xticks(x + width)
ax2.set_xticklabels(list(scenarios.keys()), fontsize=10)
ax2.set_ylabel("加權總分 (滿分 10)", fontsize=11)
ax2.set_ylim(0, 11)
ax2.set_title("不同使用場景的最佳方案", fontsize=14, fontweight='bold')
ax2.legend(fontsize=9, loc='upper left')
ax2.grid(axis='y', alpha=0.3)

plt.tight_layout()
plt.savefig("/home/ubuntu/best_architecture_radar.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ 雷達圖 + 場景評分已儲存")


# ─── 圖 2: 效能 vs 成本 vs 便利性 氣泡圖 ───────────────

fig, ax = plt.subplots(figsize=(14, 8))

# X: 效能 (70B tok/s)
# Y: 成本效益 (擴展每GB的成本，越低越好 → 反轉為分數)
# 氣泡大小: 容量
# 顏色: 架構

configs = [
    # (名稱, 70B tps, 每TB成本, 容量GB, 顏色, 架構)
    ("SD Gen3 x1\n128GB", 0.89, 150, 128, "#00ff88", "SD 卡"),
    ("SD Gen3 x2\n512GB", 2.12, 150, 512, "#00ff88", "SD 卡"),
    ("SD Gen4 x2\n1TB", 3.54, 150, 1024, "#00ff88", "SD 卡"),
    ("USB 3.2 Gen1\n512GB", 0.57, 80, 512, "#ffaa00", "USB 儲存"),
    ("USB 3.2 Gen2\n1TB", 1.14, 80, 1024, "#ffaa00", "USB 儲存"),
    ("USB 3.2 Gen2x2\n2TB", 1.81, 80, 2048, "#ffaa00", "USB 儲存"),
    ("TB3 外接盒\n2TB", 2.68, 120, 2048, "#e94560", "外接硬碟盒"),
    ("TB4 外接盒\n4TB", 2.87, 120, 4096, "#e94560", "外接硬碟盒"),
    ("USB4 v1 外接盒\n4TB", 3.64, 110, 4096, "#e94560", "外接硬碟盒"),
    ("USB4 v2 外接盒\n4TB", 5.33, 115, 4096, "#e94560", "外接硬碟盒"),
    ("TB5 外接盒\n8TB", 6.92, 130, 8192, "#e94560", "外接硬碟盒"),
]

for name, tps, cost, cap, color, arch in configs:
    # 氣泡大小與容量成正比
    size = cap / 8  # 縮放
    # 成本效益 = 容量/成本 (越大越好)
    cost_eff = (cap / 1024) / (cost / 100)  # 正規化

    ax.scatter(tps, cost_eff, s=size, c=color, alpha=0.7, edgecolors='white', linewidth=1)
    ax.annotate(name, (tps, cost_eff), textcoords="offset points",
                xytext=(8, 8), fontsize=7.5, color=color, alpha=0.9)

# 標記最佳區域
ax.axhspan(0.5, 10, xmin=0.4, xmax=1.0, alpha=0.05, color='#00ff88')
ax.text(5.5, 6.5, "最佳效益區\n(高速 + 高CP值)", fontsize=11, color='#00ff88',
        alpha=0.5, ha='center', style='italic')

ax.set_xlabel("70B 模型推理速度 (tokens/s)", fontsize=12)
ax.set_ylabel("成本效益指數 (容量/成本，越高越好)", fontsize=12)
ax.set_title("效能 vs 成本效益 vs 容量 (氣泡大小 = 容量)", fontsize=14, fontweight='bold')

# 手動圖例
from matplotlib.lines import Line2D
legend_elements = [
    Line2D([0], [0], marker='o', color='w', markerfacecolor='#00ff88', markersize=12, label='SD 卡'),
    Line2D([0], [0], marker='o', color='w', markerfacecolor='#ffaa00', markersize=12, label='USB 儲存'),
    Line2D([0], [0], marker='o', color='w', markerfacecolor='#e94560', markersize=12, label='外接硬碟盒'),
]
ax.legend(handles=legend_elements, fontsize=11, loc='upper left')
ax.grid(True, alpha=0.2)

plt.tight_layout()
plt.savefig("/home/ubuntu/best_architecture_bubble.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ 效能vs成本氣泡圖已儲存")


# ─── 圖 3: 最終結論圖 — 各場景推薦 ───────────────────

fig, ax = plt.subplots(figsize=(16, 7))
ax.axis('off')

# 結論表格
conclusion_data = [
    ["評估維度", "SD 卡 (SD Express)", "USB 儲存 (xHCI)", "外接硬碟盒 (PCIe Tunnel)"],
    ["最大頻寬", "3,940 MB/s", "2,500 MB/s", "10,000 MB/s ★"],
    ["延遲", "8-10 μs ★", "80-200 μs", "10-15 μs"],
    ["最大容量", "1 TB", "4 TB", "8+ TB ★"],
    ["成本/TB", "$150", "$80 ★", "$120"],
    ["便攜性", "★★★★★", "★★★★", "★★"],
    ["相容性", "★★", "★★★★★", "★★★"],
    ["70B 推理", "3.54 tok/s", "1.81 tok/s", "6.92 tok/s ★"],
    ["Context 提升", "130x", "130x", "490x ★"],
    ["綜合效益", "便攜之王", "入門首選", "效能之王 ★"],
]

table = ax.table(cellText=conclusion_data, loc='center', cellLoc='center')
table.auto_set_font_size(False)
table.set_fontsize(11)
table.scale(1, 1.8)

# 設定顏色
for i in range(len(conclusion_data)):
    for j in range(4):
        cell = table[i, j]
        if i == 0:
            cell.set_facecolor('#16213e')
            cell.set_text_props(fontweight='bold', color='white', fontsize=12)
        elif i == len(conclusion_data) - 1:
            colors_map = ['#16213e', '#00ff88', '#ffaa00', '#e94560']
            cell.set_facecolor(colors_map[j])
            if j > 0:
                cell.set_text_props(fontweight='bold', color='white' if j != 1 else 'black', fontsize=12)
            else:
                cell.set_text_props(fontweight='bold', color='white', fontsize=12)
        else:
            cell.set_facecolor('#1a1a2e' if i % 2 == 0 else '#0f0f23')
            cell.set_text_props(color='white')
            # 標記星號的格子
            if "★" in str(conclusion_data[i][j]):
                cell.set_text_props(color='#00ff88', fontweight='bold')
        cell.set_edgecolor('#333')

ax.set_title("三種架構綜合效益對比 — by DONG. WEI YANG",
             fontsize=16, fontweight='bold', color='#e94560', pad=20)

plt.tight_layout()
plt.savefig("/home/ubuntu/best_architecture_conclusion.png", dpi=150, bbox_inches='tight',
            facecolor='#0f0f23', edgecolor='none')
plt.close()
print("✓ 結論圖已儲存")

print("\n=== 分析完成 ===")
