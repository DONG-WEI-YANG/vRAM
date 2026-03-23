# SSD VRAM 擴展真實效能數據

## 1. Phison aiDAPTIV+ (ProX PC 實測 2026/03/18)
- 硬體: 2x 1TB MiPhi aiDAPTIVCache NVMe (SLC NAND, 100 DWPD)
- 連接: PCIe Gen4 x4 直連主機板 M.2 插槽

### Fine-Tuning 效能
| 精度 | 模型 | GPU | Tokens/s | NVMe 使用量 |
|------|------|-----|----------|------------|
| FP16 | Llama-3.1-8B | RTX PRO 6000 (96GB) | 2,973-3,386 | 417GB |
| FP16 | Llama-3.1-8B | RTX 5090 (32GB) | 2,337 | 407GB |
| FP16 | Llama-3.1-70B | RTX PRO 6000 | 400 | 1.6TB |
| FP16 | Llama-3.1-70B | RTX 5090 | 190 | 1.4TB |

### 關鍵數據
- NVMe 直連 PCIe Gen4 x4 頻寬: 7,000 MB/s (理論), ~5,500-6,500 MB/s (實際)
- 延遲: ~3-5 μs (NVMe 原生)
- 單 GPU + 2TB NVMe 可跑 70B 模型 fine-tuning
- 405B 超過 2TB 限制無法運行

## 2. GreenBoost (開源, 2026/03)
- 三層架構: VRAM → System RAM → NVMe
- Linux kernel module
- 效能: 依據 NVMe 頻寬，推理速度約 VRAM 的 1/10 到 1/50

## 3. NVMe SSD 規格 (直連 M.2)
- PCIe Gen3 x4: 3,500 MB/s
- PCIe Gen4 x4: 7,000 MB/s
- PCIe Gen5 x4: 14,000 MB/s
- 延遲: 3-5 μs
- 容量: 1-8 TB (單顆), 可 RAID
- 協定轉換: 0 次 (PCIe 原生 NVMe)
