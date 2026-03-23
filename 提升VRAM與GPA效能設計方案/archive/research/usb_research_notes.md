# USB 加速 GPU 效能 — 技術研究筆記

## USB 各版本頻寬比較

| USB 版本 | 最大頻寬 | 實際有效頻寬 | 協定 |
|----------|---------|-------------|------|
| USB 2.0 | 480 Mbps (60 MB/s) | ~35 MB/s | USB Host |
| USB 3.0 (3.2 Gen1) | 5 Gbps (625 MB/s) | ~450 MB/s | USB Host |
| USB 3.1 (3.2 Gen2) | 10 Gbps (1.25 GB/s) | ~1 GB/s | USB Host |
| USB 3.2 Gen2x2 | 20 Gbps (2.5 GB/s) | ~2 GB/s | USB Host |
| USB4 v1.0 | 40 Gbps (5 GB/s) | ~3.5 GB/s | PCIe Tunneling |
| USB4 v2.0 | 80 Gbps (10 GB/s) | ~8 GB/s | PCIe Tunneling |
| USB4 v2.0 非對稱 | 120 Gbps (15 GB/s) 單向 | ~12 GB/s | PCIe Tunneling |
| Thunderbolt 3 | 40 Gbps (5 GB/s) | ~2.8 GB/s (PCIe) | PCIe Tunneling |
| Thunderbolt 4 | 40 Gbps (5 GB/s) | ~3.2 GB/s (PCIe) | PCIe Tunneling |
| Thunderbolt 5 | 80/120 Gbps | ~8-12 GB/s | PCIe Tunneling |

## USB vs SD Express 架構差異

### SD Express
- 直接使用 PCIe + NVMe 協定
- SD 卡控制器直接是 NVMe 控制器
- 無協定轉換開銷
- 頻寬: 985 ~ 3,940 MB/s

### USB 儲存裝置
- USB 3.x: 使用 USB Mass Storage / UAS 協定，需要 USB → SATA/NVMe 橋接晶片
- USB4: 支援 PCIe Tunneling，可直接傳輸 PCIe 封包
- USB4 NVMe 外接盒: USB4 控制器 → PCIe Tunnel → NVMe SSD

### 關鍵差異
1. USB 3.x 有協定轉換開銷（USB → NVMe bridge），增加延遲
2. USB4 透過 PCIe Tunneling 可以幾乎無開銷地傳輸 PCIe 封包
3. USB4 的 PCIe Tunneling 與 SD Express 的 PCIe 模式在協定層面非常相似
4. USB 有更多的裝置類型支援（隨身碟、外接 SSD、eGPU）

## USB 加速的三種可能路徑

### 路徑 A: USB 隨身碟/外接 SSD 作為 VRAM 擴展
- 類似 SD-VRAM Booster 的概念
- USB 3.2 Gen2: ~1 GB/s → 可用但較慢
- USB4 NVMe 外接盒: ~3.5-8 GB/s → 與 SD Express 相當甚至更快
- Windows ReadyBoost 已有類似概念（但僅用於 HDD 快取）

### 路徑 B: USB4 eGPU 外接顯卡
- 透過 USB4/TB4 連接外部 GPU
- PCIe 3.0 x4 通道 (~32 Gbps)
- 已有商業產品（Razer Core X, ONEXGPU 等）
- 但這是「加 GPU」不是「擴展 VRAM」

### 路徑 C: USB4 PCIe 擴展 AI 加速器
- M.2 形態的 AI 加速卡透過 USB4 外接
- 如 Coral TPU, Hailo-8 等
- USB4 V2 提供足夠頻寬

## 與 SD-VRAM Booster 的比較

| 特性 | SD Express | USB 3.2 | USB4 |
|------|-----------|---------|------|
| 協定 | PCIe + NVMe (原生) | USB Host (需橋接) | PCIe Tunneling |
| 最大頻寬 | 3,940 MB/s | 2,500 MB/s | 10,000 MB/s |
| 延遲 | 低 (原生 PCIe) | 中 (橋接開銷) | 低 (PCIe Tunnel) |
| 裝置類型 | SD 卡 | 隨身碟/外接 SSD | NVMe SSD/eGPU |
| 熱插拔 | 是 | 是 | 是 |
| 普及度 | 低 (SD Express 新) | 極高 | 中 (逐漸普及) |
| 攜帶性 | 極高 (卡片) | 高 (隨身碟) | 中 (外接盒) |
| 容量 | 最大 4TB | 最大 4TB+ | 最大 8TB+ |

## Windows ReadyBoost 參考
- 使用 USB 隨身碟作為 HDD 讀取快取
- 利用隨身碟的隨機讀取速度優於 HDD
- 對 SSD 系統無效（SSD 已經夠快）
- 概念可延伸：用 USB NVMe 外接盒作為 VRAM 擴展快取

## eGPU 實測數據
- USB4/TB4 eGPU: PCIe 3.0 x4 通道
- 實際 GPU 效能約為內建的 70-85%（受限於 PCIe 頻寬）
- VRAM 不受影響（VRAM 在外接 GPU 上）
- USB4 V2 eGPU: 效能更接近內建

## 結論
USB 加速 GPU 效能的可行性取決於 USB 版本：
- USB 3.x: 可用於 VRAM 擴展（swap/pagefile），但頻寬受限
- USB4: 透過 PCIe Tunneling 可達到與 SD Express 相當的效能
- USB4 V2: 頻寬甚至超過 SD Express Gen4 x2
- USB 的最大優勢：裝置普及度遠高於 SD Express

## eGPU PCIe Tunneling 架構（來自 Reddit 詳細分析）

### 信號路徑
Host APU → PCI Router → TB/USB4 Router → Redriver → 40Gbps Cable → TB/USB4 Router → PCI Router → GPU

### 關鍵發現
- 信號需要經過多個硬體區塊，每個都會增加延遲和降低頻寬
- AMD APU 整合 TB 控制器，但需要 redriver 晶片
- Intel 系統使用獨立的 TB 控制器（Titan Ridge, Alpine Ridge, Maple Ridge）
- PCIe 3.0 x4 = 16 Gbps, PCIe 4.0 x4 = 32 Gbps
- 40 Gbps USB4/TB4 理論上可支援 PCIe 3.0 x4 全速

### USB4 eGPU 實測
- USB4 v1 (40Gbps): 約 PCIe 3.0 x4 效能
- USB4 v2 (80Gbps): 約 PCIe 4.0 x4 效能
- TB5 (120Gbps 非對稱): 接近 PCIe 4.0 x4 全速

### 對 VRAM 擴展的啟示
- USB4 的 PCIe Tunneling 不只能連接 eGPU
- 也可以連接 NVMe SSD 作為 VRAM 擴展
- USB4 NVMe 外接盒 = 透過 PCIe Tunnel 直接存取 NVMe
- 這與 SD Express 的 PCIe 模式在本質上相同
