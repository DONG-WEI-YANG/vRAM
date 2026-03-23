# 效能研究筆記 - 真實數據

## GreenBoost 實測數據 (來源: GitLab README)
- 系統: i9-14900KF, RTX 5070 12GB, 64GB DDR4-3600, Samsung 990 EVO Plus 4TB NVMe
- 模型: glm-4.7-flash:q8_0 (31.8 GB)
- 三層記憶體架構:
  - T1: VRAM 12GB, ~336 GB/s
  - T2: System DDR pool 51GB, ~32 GB/s (PCIe 4.0 x16) 或 ~64 GB/s (PCIe 5.0 x16)
  - T3: NVMe swap 64GB, ~1.8 GB/s (安全溢出，正常不會觸及)
- 關鍵事實: GreenBoost 不是快取，模型權重永久駐留在 VRAM + DDR 中
- GPU 透過 PCIe DMA 直接存取 DDR 中的權重，無需 CPU 複製
- Prefill 速度比 CPU offload (ik_llama.cpp) 快 5-10 倍

## Reddit 討論中的效能數據
- 有用戶報告 Token per Second: 14.56 tokens/s (RTX 5070 + GreenBoost)

## 物理頻寬極限 (可驗證的事實)
- GDDR6X VRAM: ~336 GB/s (RTX 5070)
- PCIe 4.0 x16: ~32 GB/s (理論), 實測約 25-28 GB/s
- PCIe 4.0 x1: ~2 GB/s (理論), 實測約 1.5-1.8 GB/s
- PCIe 3.0 x1: ~1 GB/s (理論), 實測約 0.8-0.9 GB/s
- SD Express Gen3 x1: ~985 MB/s (理論最大)
- SD Express Gen4 x1: ~1,969 MB/s (理論最大)
- SD Express Gen4 x2: ~3,940 MB/s (理論最大)
- NVMe SSD (Gen4): ~7,000 MB/s (實測)
- DDR4-3600: ~28.8 GB/s (理論)

## 關鍵瓶頸分析
- SD Express 最大頻寬 3.94 GB/s vs NVMe SSD 7 GB/s → SD 卡約為 NVMe 的 56%
- SD Express 最大頻寬 3.94 GB/s vs DDR4 28.8 GB/s → SD 卡約為 DDR 的 14%
- SD Express 最大頻寬 3.94 GB/s vs VRAM 336 GB/s → SD 卡約為 VRAM 的 1.2%

## 誠實結論
- SD 卡作為 VRAM 擴展，效能遠低於原生 VRAM
- 但核心價值是: 讓「完全無法運行」的模型變成「可以運行（較慢）」
- 類似 GreenBoost T3 層的角色，不是追求速度，而是追求可行性
- 適合場景: LLM 推理（非訓練）、模型載入後的生成階段

## Phison aiDAPTIV+ 實測數據 (來源: Tom's Hardware, CES 2026)
- 宣稱推理速度提升最高 10 倍（相比無 aiDAPTIV+ 的情況）
- 核心機制：將 KV cache 溢出到 SSD 而非丟棄，避免重新計算
- 120B 參數 MoE 模型可在 32GB DRAM 上運行（傳統需 96GB）
- 主要提升的是 Time to First Token (TTFT)，而非 tokens/s
- 10x 提升是因為避免了 KV cache 重算，不是頻寬提升

## Kioxia GP Series SSD (來源: StorageReview, 2026/3/16)
- 專為 GPU 記憶體擴展設計的 SSD
- 使用 XL-FLASH 技術，支援 512B 存取粒度（傳統 NVMe 是 4KB）
- 配合 NVIDIA Storage-Next 架構
- 25.6TB 容量，PCIe 5.0 介面

## 真實效能分析框架
關鍵區分：
1. **頻寬受限場景**（模型權重載入）：SD 卡頻寬是瓶頸
2. **延遲受限場景**（token 生成）：每次只讀取少量資料，頻寬不是主要瓶頸
3. **KV cache 場景**（長上下文）：aiDAPTIV+ 的 10x 提升來自這裡
