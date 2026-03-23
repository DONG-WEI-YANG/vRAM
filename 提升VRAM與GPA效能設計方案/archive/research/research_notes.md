# 研究筆記：SD 卡擴展 GPU VRAM 產品設計

## 1. 現有 SSD 擴展 VRAM 技術

### NVIDIA Storage-Next / GPUDirect Storage
- NVIDIA 推出 Storage-Next 計畫，允許 GPU 直接存取 flash 儲存作為 HBM 的擴展
- GPUDirect Storage 提供 NVMe 到 GPU 記憶體的直接資料路徑
- NVIDIA CMX (Context Memory Storage) 使用 BlueField-4 DPU 實現 KV cache 擴展

### KIOXIA GP Series SSD
- 使用 XL-FLASH (SCM 儲存級記憶體) 技術
- 為 GPU 發起的 AI 工作負載設計
- 512-byte 資料存取粒度
- 低延遲、高 IOPS
- 預計 2026 年底提供樣品

### GreenBoost 開源驅動 (2026年3月)
- Linux 核心模組 + CUDA 使用者空間 shim
- 三層 GPU 記憶體擴展：VRAM → DDR4 RAM → NVMe SSD
- 核心模組分配 pinned DDR4 pages (2MB compound pages)
- 透過 DMA-BUF 匯出，GPU 透過 cudaImportExternalMemory 匯入
- PCIe 4.0 x16 提供約 32 GB/s 頻寬
- CUDA shim 攔截 cudaMalloc 等函式，大於 256MB 的分配重定向到核心模組
- 目標：在 RTX 5070 12GB 上跑 31.8GB 的 LLM 模型

### Panmnesia CXL-GPU
- 使用 CXL (Compute Express Link) 協議
- PCIe 物理層上的記憶體擴展
- 雙位數奈秒延遲
- CES 2025 創新獎

### ProX PC & MiPhi AI aiDAPTIV+
- 使用高耐久 SLC NAND NVMe 作為 GPU 的活動記憶體層
- 成功在有限 VRAM 上運行 Llama 3.1 70B

## 2. SD 卡規格與頻寬

### SD Express (最新規格)
- SD Express PCIe Gen.3 x1 Lane: 985 MB/s (SD 7.00)
- SD Express PCIe Gen.4 x1 Lane / PCIe Gen.3 x2 Lane: 1,970 MB/s (SD 8.00)
- SD Express PCIe Gen.4 x2 Lane: 3,940 MB/s (SD 8.00)
- **關鍵：SD Express 使用 PCIe + NVMe 協議！**

### UHS 系列
- UHS-I: 50-104 MB/s
- UHS-II: 156 MB/s (Full Duplex) / 312 MB/s (Half Duplex)
- UHS-III: 312-624 MB/s (Full Duplex)

### 實際產品
- SanDisk microSD Express 256GB: 讀 880 MB/s, 寫 650 MB/s, 持續寫 210 MB/s
- ADATA SD Express 256GB: 讀寫 800/700 MB/s (PCIe Gen3x1 + NVMe)
- SD 卡容量可達 4TB (SDUC 規格)

## 3. GPU VRAM 頻寬比較

### 各種記憶體頻寬
- GDDR6 (RTX 4090): ~1,008 GB/s (384-bit bus)
- GDDR6X (RTX 4090): ~1,008 GB/s
- HBM2e (A100): ~2,039 GB/s
- HBM3 (H100): ~3,350 GB/s
- PCIe 4.0 x16: ~32 GB/s
- PCIe 5.0 x16: ~64 GB/s
- SD Express Gen4 x2: ~3.94 GB/s
- SD Express Gen4 x1: ~1.97 GB/s
- UHS-II: ~0.312 GB/s

## 4. 關鍵洞察

### 為什麼 SSD 可以擴展 VRAM？
1. NVMe SSD 透過 PCIe 連接，頻寬可達 7-14 GB/s (Gen4/Gen5)
2. 不是替代 VRAM，而是作為溢出層 (overflow tier)
3. 適用於不需要全速存取的資料（如 KV cache、模型權重的冷資料）
4. 分層記憶體管理：熱資料在 VRAM，溫資料在 RAM，冷資料在 NVMe

### SD Express 卡的優勢
1. **使用 PCIe + NVMe 協議** — 與 NVMe SSD 相同的協議棧
2. 最高 3,940 MB/s 理論頻寬（Gen4 x2）
3. 體積小、可熱插拔
4. 成本相對較低
5. 容量可達 4TB

### SD Express 卡的挑戰
1. 頻寬遠低於 VRAM（約 1/250 到 1/1000）
2. 延遲高於 VRAM
3. 持續寫入速度可能遠低於峰值（如 210 MB/s vs 880 MB/s）
4. 需要特殊的 SD Express 讀卡器/介面
5. 耐久性問題（寫入壽命）
