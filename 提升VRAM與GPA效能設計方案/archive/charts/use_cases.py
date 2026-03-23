import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

fig, ax = plt.subplots(figsize=(16, 10))
ax.set_xlim(0, 16)
ax.set_ylim(0, 10)
ax.axis('off')
ax.set_facecolor('#F8F9FA')
fig.patch.set_facecolor('#F8F9FA')

# Title
ax.text(8, 9.5, 'SD-VRAM Booster - Product Lineup & Use Cases',
        fontsize=18, fontweight='bold', ha='center', va='center',
        color='#1a1a2e')

# Product 1: PCIe Add-in Card
rect1 = mpatches.FancyBboxPatch((0.5, 5.5), 4.5, 3.5, boxstyle="round,pad=0.3",
                                 facecolor='#E3F2FD', edgecolor='#1565C0', linewidth=2)
ax.add_patch(rect1)
ax.text(2.75, 8.5, 'PCIe Expansion Card', fontsize=13, fontweight='bold',
        ha='center', color='#1565C0')
ax.text(2.75, 7.8, '4x SD Express Slots', fontsize=10, ha='center', color='#333')
ax.text(2.75, 7.3, 'PCIe x8 Interface', fontsize=10, ha='center', color='#333')
ax.text(2.75, 6.8, 'Max 15.6 GB/s (RAID 0)', fontsize=10, ha='center', color='#333')
ax.text(2.75, 6.2, 'Up to 16TB Capacity', fontsize=10, ha='center', color='#666')

# Product 2: M.2 Module
rect2 = mpatches.FancyBboxPatch((5.75, 5.5), 4.5, 3.5, boxstyle="round,pad=0.3",
                                 facecolor='#E8F5E9', edgecolor='#2E7D32', linewidth=2)
ax.add_patch(rect2)
ax.text(8, 8.5, 'M.2 Adapter Module', fontsize=13, fontweight='bold',
        ha='center', color='#2E7D32')
ax.text(8, 7.8, '1x SD Express Slot', fontsize=10, ha='center', color='#333')
ax.text(8, 7.3, 'M.2 NVMe Interface', fontsize=10, ha='center', color='#333')
ax.text(8, 6.8, 'Max 3.94 GB/s', fontsize=10, ha='center', color='#333')
ax.text(8, 6.2, 'For Laptops & Mini PCs', fontsize=10, ha='center', color='#666')

# Product 3: USB4 Dock
rect3 = mpatches.FancyBboxPatch((11, 5.5), 4.5, 3.5, boxstyle="round,pad=0.3",
                                 facecolor='#FFF3E0', edgecolor='#E65100', linewidth=2)
ax.add_patch(rect3)
ax.text(13.25, 8.5, 'USB4 External Dock', fontsize=13, fontweight='bold',
        ha='center', color='#E65100')
ax.text(13.25, 7.8, '2x SD Express Slots', fontsize=10, ha='center', color='#333')
ax.text(13.25, 7.3, 'USB4 / Thunderbolt 4', fontsize=10, ha='center', color='#333')
ax.text(13.25, 6.8, 'Max 5 GB/s (USB4)', fontsize=10, ha='center', color='#333')
ax.text(13.25, 6.2, 'Portable & Hot-Swap', fontsize=10, ha='center', color='#666')

# Use Cases
use_cases = [
    ('LLM Inference\n(30B+ Models)', '#FF5252', 1.5),
    ('Edge AI\nDeployment', '#448AFF', 4.5),
    ('Stable Diffusion\nHigh-Res Gen', '#69F0AE', 7.5),
    ('AI Model\nSwap Library', '#FFD740', 10.5),
    ('KV Cache\nExpansion', '#E040FB', 13.5),
]

for label, color, x in use_cases:
    rect = mpatches.FancyBboxPatch((x-1.2, 0.5), 2.4, 2.2, boxstyle="round,pad=0.2",
                                    facecolor=color, edgecolor='#333', linewidth=1.5, alpha=0.8)
    ax.add_patch(rect)
    ax.text(x, 1.6, label, fontsize=10, fontweight='bold', ha='center', va='center', color='white')

# Arrows from products to use cases
ax.text(8, 4.5, 'Target Use Cases', fontsize=14, fontweight='bold',
        ha='center', va='center', color='#555',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='#EEEEEE', edgecolor='#999'))

for x in [2.75, 8, 13.25]:
    ax.annotate('', xy=(8, 4.9), xytext=(x, 5.5),
                arrowprops=dict(arrowstyle='->', color='#666', lw=1.5))

for x_uc in [1.5, 4.5, 7.5, 10.5, 13.5]:
    ax.annotate('', xy=(x_uc, 2.7), xytext=(8, 4.1),
                arrowprops=dict(arrowstyle='->', color='#666', lw=1.5))

plt.tight_layout()
plt.savefig('/home/ubuntu/product_lineup.png', dpi=150, bbox_inches='tight',
            facecolor='#F8F9FA', edgecolor='none')
plt.close()
print("Product lineup chart saved.")
