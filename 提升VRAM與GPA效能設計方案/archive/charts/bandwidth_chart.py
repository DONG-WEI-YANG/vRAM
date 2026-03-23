import matplotlib.pyplot as plt
import matplotlib
import numpy as np

matplotlib.rcParams['font.family'] = ['DejaVu Sans']

categories = [
    'GPU VRAM\n(GDDR6X)',
    'HBM3\n(H100)',
    'System RAM\n(DDR5)',
    'NVMe SSD\n(PCIe 5.0 x4)',
    'SD-VRAM Booster\n(4-card RAID)',
    'SD Express\n(Single, Gen4 x2)',
    'UHS-III',
    'UHS-II'
]

bandwidth = [1008, 3350, 89.6, 14, 15.6, 3.94, 0.624, 0.312]

colors = ['#FF4444', '#FF6666', '#4488FF', '#44BB44', '#FFD700', '#FFAA00', '#888888', '#AAAAAA']

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18, 8))

# Left chart: log scale bar chart
bars = ax1.barh(categories, bandwidth, color=colors, edgecolor='#333333', linewidth=0.8, height=0.6)
ax1.set_xscale('log')
ax1.set_xlabel('Bandwidth (GB/s) - Log Scale', fontsize=12, fontweight='bold')
ax1.set_title('Memory / Storage Bandwidth Comparison', fontsize=14, fontweight='bold')
ax1.invert_yaxis()

for bar, val in zip(bars, bandwidth):
    ax1.text(val * 1.3, bar.get_y() + bar.get_height()/2, f'{val} GB/s',
             va='center', ha='left', fontsize=10, fontweight='bold')

ax1.set_xlim(0.1, 10000)
ax1.grid(axis='x', alpha=0.3, linestyle='--')

# Right chart: cost-effectiveness comparison (estimated)
types = ['RTX 4090\n(24GB VRAM)', 'DDR5 RAM\n(64GB)', 'NVMe SSD\n(2TB)', 'SD Express\n4-card (4TB)']
cost_per_gb = [50, 3.1, 0.06, 0.05]
capacity = [24, 64, 2000, 4000]

bar_colors = ['#FF4444', '#4488FF', '#44BB44', '#FFD700']

bars2 = ax2.bar(types, capacity, color=bar_colors, edgecolor='#333333', linewidth=0.8, width=0.5)
ax2.set_ylabel('Total Capacity (GB)', fontsize=12, fontweight='bold')
ax2.set_title('Capacity Comparison by Solution', fontsize=14, fontweight='bold')

for bar, cap in zip(bars2, capacity):
    ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 50,
             f'{cap} GB', ha='center', va='bottom', fontsize=11, fontweight='bold')

ax2.set_ylim(0, 5000)
ax2.grid(axis='y', alpha=0.3, linestyle='--')

plt.tight_layout(pad=3)
plt.savefig('/home/ubuntu/bandwidth_comparison.png', dpi=150, bbox_inches='tight',
            facecolor='white', edgecolor='none')
plt.close()
print("Chart saved.")
