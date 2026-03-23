# SD 卡偵測機制研究筆記

## SD Express 卡的兩種初始化模式

根據 SD Association 的 Host Implementation Guideline：

1. **SD-First 模式（推薦）**：
   - 主機先以傳統 SD 介面初始化卡片
   - 讀取 CSD/CID/SCR 暫存器，獲取卡片能力資訊
   - 若偵測到卡片支援 PCIe/NVMe，再切換至 SD Express 模式
   - 向下相容所有 SD 卡（UHS-I、UHS-II、UHS-III）

2. **PCIe-First 模式**：
   - 主機直接以 PCIe 介面初始化
   - 卡片以標準 NVMe 裝置身份出現（PCIe Class ID: 01h->08h->02h）
   - 不向下相容傳統 SD 卡
   - 適用於半嵌入式場景

## SD Express 卡的 NVMe 識別
- SD Express 卡在 PCIe 模式下被識別為「Standard NVMe Device」
- 使用標準 NVMe 驅動即可操作
- 支援 PCIe 熱插拔

## 不同規格 SD 卡的偵測方式
- CID 暫存器（128-bit）：製造商 ID、產品名稱、序號
- CSD 暫存器：容量、速度等級、匯流排寬度
- SCR 暫存器：SD 規格版本、支援的匯流排介面
- 透過 CMD8 / ACMD41 等命令進行能力協商
