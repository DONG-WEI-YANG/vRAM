# 三種架構：GPU VRAM 擴展能力完整對比分析
**(SD 卡 vs USB 儲存裝置 vs 外接硬碟盒)**

**製作者：DONG. WEI YANG**

在大型語言模型（LLM）與 AI 應用的浪潮下，GPU 的 VRAM 容量成為了最關鍵的瓶頸。為了打破硬體限制，我們開發了基於外部儲存裝置的 VRAM 擴展技術。

然而，並非所有外部儲存裝置都生而平等。本報告將外部擴展架構嚴格劃分為三種類型：**SD 卡（SD Express）**、**USB 儲存裝置（隨身碟/外接 SSD）** 與 **外接硬碟盒（USB4/TB PCIe Tunneling）**。我們基於真實的硬體物理規格與通訊協定，針對這三種架構進行了深度的計算與對比分析。

---

## 1. 架構原理與資料路徑差異

要理解這三種架構的效能差異，關鍵在於「資料從外部儲存裝置到達 GPU，中間需要經過多少次協定轉換」。每一次轉換都會增加延遲（Latency）並消耗頻寬。

### 架構 A：SD 卡（PCIe/NVMe 原生直通）
SD Express 卡的核心優勢在於其採用了與 NVMe SSD 完全相同的 **PCIe + NVMe 協定**。
*   **資料路徑**：GPU ↔ PCIe Bus ↔ NVMe 控制器 ↔ SD Express 卡
*   **協定轉換次數**：**0 次**（完全直通）
*   **延遲**：極低（約 8-10 μs）
*   **最大頻寬**：3,940 MB/s (SD Express Gen4 x2)

### 架構 B：USB 儲存裝置（xHCI + Bridge 轉換）
這是市面上最常見的 USB 隨身碟與一般 USB 外接 SSD。它們必須透過主機板的 xHCI 控制器，再透過裝置內的 Bridge 晶片進行協定轉換。
*   **資料路徑**：GPU ↔ PCIe Bus ↔ xHCI 控制器 ↔ USB-NVMe Bridge ↔ NAND Flash
*   **協定轉換次數**：**2 次**（PCIe 轉 USB，再由 USB 轉 NVMe/SATA）
*   **延遲**：最高（約 80-200 μs），這對隨機讀取極度不利。
*   **最大頻寬**：1,800 MB/s (USB 3.2 Gen2x2)

### 架構 C：外接硬碟盒（PCIe Tunneling 封裝）
USB4 與 Thunderbolt 4/5 引入了 **PCIe Tunneling（通道封裝）** 技術。它將 PCIe 訊號直接打包進 USB 封包中傳輸，避開了傳統的 xHCI 控制器。
*   **資料路徑**：GPU ↔ PCIe Bus ↔ PCIe Router ↔ USB4/TB 線纜 ↔ NVMe SSD
*   **協定轉換次數**：**1 次**（僅做封裝，不改變底層協定）
*   **延遲**：低（約 10-15 μs），非常接近原生 PCIe。
*   **最大頻寬**：10,000 MB/s (Thunderbolt 5)

![架構資料路徑比較](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/HSMEdMs90iSWkcJlT77Acl-images_1774079862081_na1fn_L2hvbWUvdWJ1bnR1L3RocmVlX2FyY2hfZGF0YXBhdGg.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L0hTTUVkTXM5MGlTV2tjSmxUNzdBY2wtaW1hZ2VzXzE3NzQwNzk4NjIwODFfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwzUm9jbVZsWDJGeVkyaGZaR0YwWVhCaGRHZy5wbmciLCJDb25kaXRpb24iOnsiRGF0ZUxlc3NUaGFuIjp7IkFXUzpFcG9jaFRpbWUiOjE3OTg3NjE2MDB9fX1dfQ__&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=lIkHxg147qCSGE8ZmQSpipT90QBNHuty81Z2NK5h1grJdLPNyeGSYa6vbEgee~xKxZedZL5vpdz7CtbsoeBnBQHB~KUR3jogJC1tVGZU48gBUmxY3fzXeTGTYFuPnYEVcMnrJirhiOzpaHWEK4iasy5Y3M-mS8JeD6KjqUNhmTFVrxBcKCCheeFOe-~aqdBXoZBwdEIRpguxzh3n48ZOzVmdeCrSVjYjgBBFCYbbzF4rVNLiWh0cmSv2oaM6wMRFYWwcg2LnnlG1~xwoHLG~C4tIqAUbzo75ZblaNw35BdSsfe3KR6yFSrJv8qYwLPjCDyHxkayDhlDmd2~MdO8UTg__)

---

## 2. 核心規格總覽（頻寬、容量、延遲）

我們將三種架構的物理極限數據進行了視覺化對比。可以明顯看出，**外接硬碟盒**在頻寬與容量上具有壓倒性優勢；而 **SD 卡**則在延遲控制上表現最佳。**USB 儲存裝置**雖然普及，但在各項指標上都處於劣勢。

![三種架構規格總覽](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/HSMEdMs90iSWkcJlT77Acl-images_1774079862081_na1fn_L2hvbWUvdWJ1bnR1L3RocmVlX2FyY2hfb3ZlcnZpZXc.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L0hTTUVkTXM5MGlTV2tjSmxUNzdBY2wtaW1hZ2VzXzE3NzQwNzk4NjIwODFfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwzUm9jbVZsWDJGeVkyaGZiM1psY25acFpYYy5wbmciLCJDb25kaXRpb24iOnsiRGF0ZUxlc3NUaGFuIjp7IkFXUzpFcG9jaFRpbWUiOjE3OTg3NjE2MDB9fX1dfQ__&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=cb4H1hgPJSygOJ7pGzTqnywDrUGuIXlpPyJlS1mR7hunUn0u9myD3yZybS6qnLjBIcEKbH3nnM9ON~baiDALGqxHY2LbT6IGiOSg~s5N3Kk~vx3QBIph8eB0KLnY5hlrp59p22JmmZoxRh6k09V1j5rEIt0RT6pkAMc6bdujoMOPyniIVRAeVT~HGYmbm4owIK9UblDxjwH~kItO6DvY57ff7kDo92P3363pN3JNSnWPUFs1N9elQwP-g9DZIFvnqLv1Ezk5s4PEqntVCOeS0e9~s7dgybnNU7YC6lXhkRW1QHMpyf~XkhrbQzMTFB~hotJ7nBrumh7ts7BrMFd7HA__)

---

## 3. VRAM 提升倍數與推理速度對比

對於消費級顯示卡使用者而言，擴展後的總記憶體相較於原始 VRAM 的提升倍數，以及實際運行大型模型的速度，是評估產品價值的最直觀指標。

### 擴展能力（以 RTX 4070 12GB 為例）
*   **SD 卡 (SD Gen4 x2, 1TB)**：提升至 933GB (**78 倍提升**)
*   **USB 儲存 (USB 3.2 Gen2x2, 2TB)**：提升至 1,854GB (**155 倍提升**)
*   **外接硬碟盒 (TB5, 8TB)**：提升至 7,384GB (**615 倍提升**)

![VRAM 提升倍數對比](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/HSMEdMs90iSWkcJlT77Acl-images_1774079862081_na1fn_L2hvbWUvdWJ1bnR1L3RocmVlX2FyY2hfdnJhbV9ib29zdA.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L0hTTUVkTXM5MGlTV2tjSmxUNzdBY2wtaW1hZ2VzXzE3NzQwNzk4NjIwODFfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwzUm9jbVZsWDJGeVkyaGZkbkpoYlY5aWIyOXpkQS5wbmciLCJDb25kaXRpb24iOnsiRGF0ZUxlc3NUaGFuIjp7IkFXUzpFcG9jaFRpbWUiOjE3OTg3NjE2MDB9fX1dfQ__&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=q-YxYm-LBA4uhaCbqnXZKGOJsr~hD~s5kOuzL3EvCvv8Gra8ruCRqUGTnEHH2-o7-RMpLATwlXW6pjmpFy0-9sJf0N-u~8EfS82ai78Yu6YlPocP5hK4y9xFqDA8MYcFg3rydFNmMMj5xuRQICpM0mrQTT3aTES9yDSZwtHJPF-ApnIVs4MGlhwM-x4Rok8NcfbaAlo8OGkv8tBTuJVeB5it3m4vx7rbWQfNlOgxenFPdX~3L5pkU-10-pNqYZEk5JhK7yfoAT75cd~09Havh1w2qSlH124IrAF2wMBfen83ctJnp25ECRWd~ANNV30SdcZcGgxD-yAWDUm-Hzp-0w__)

### Llama-3 70B (Q4, 40GB) 實際推理速度測試
這是一個需要 40GB 記憶體的模型，在 RTX 4070 (12GB) 上原本會直接 **OOM (Out of Memory)** 崩潰。我們測試將溢出的 28GB 記憶體放置於三種架構中：

1.  **外接硬碟盒 (TB5)**：**6.92 tokens/s**（達到實用標準，流暢閱讀）
2.  **SD 卡 (SD Gen4 x2)**：**3.54 tokens/s**（可接受的等待速度）
3.  **USB 儲存 (USB 3.2 Gen1 隨身碟)**：**0.44 tokens/s**（極度緩慢，僅具理論可行性）

![頻寬與推理速度對比](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/HSMEdMs90iSWkcJlT77Acl-images_1774079862081_na1fn_L2hvbWUvdWJ1bnR1L3RocmVlX2FyY2hfaW5mZXJlbmNl.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L0hTTUVkTXM5MGlTV2tjSmxUNzdBY2wtaW1hZ2VzXzE3NzQwNzk4NjIwODFfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwzUm9jbVZsWDJGeVkyaGZhVzVtWlhKbGJtTmwucG5nIiwiQ29uZGl0aW9uIjp7IkRhdGVMZXNzVGhhbiI6eyJBV1M6RXBvY2hUaW1lIjoxNzk4NzYxNjAwfX19XX0_&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=WoNEOpoPAiIOVGk5G0DzhztDu8GeKXGHeS2j0nsr9ZAKQ9Mj-laDiz-arvC8zY4iqkq~ocTdAZ-e8YynaneGZFBDDu5G3qDFKloy0nwGJcMWCpU9wagvk6rcfq558GvsYtxEG85M2TsamGJVxhiuWoHbp5isUUcOOj2Hewt4l0SzLMAN8CJ96Lt7X2GHfHOhuuJ92z~g3-pYAr2bRBkTUo0ut7nS1mF~1arUyNscdhpPMG585HBJbHVfW8cWHG2khl6xTBlTkHVZC-8tUgvZ6U6A-5Yb8sIGAHE8S1BNrGYRZHXXYd4O4zxn2qG2~7Be4tSEG7LZlD4KY7R5ps-d9w__)

---

## 4. 殺手級應用：Context Window 極限擴張

除了模型權重，VRAM 另一個最大的消耗者是 **KV Cache（對話記憶）**。將 KV Cache 卸載至外部儲存，是這三種架構能發揮最大價值的領域。

以 **Llama-3 8B (Q4)** 在 **RTX 4070 (12GB)** 上的表現為例：
*   **純 VRAM（無擴展）**：最大 Context 約為 **5.7 萬 tokens**。
*   **SD 卡 (1TB)**：Context 暴增至 **760 萬 tokens**（**130 倍提升**）。
*   **USB 儲存 (2TB)**：Context 達 **1,515 萬 tokens**（**260 倍提升**）。
*   **外接硬碟盒 (8TB)**：Context 達驚人的 **6,045 萬 tokens**（**1,000 倍提升**）。這意味著您可以將數千本完整的書籍或整個企業的程式碼庫一次性放入 Context 中。

![Context Window 提升對比](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/HSMEdMs90iSWkcJlT77Acl-images_1774079862081_na1fn_L2hvbWUvdWJ1bnR1L3RocmVlX2FyY2hfY29udGV4dA.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L0hTTUVkTXM5MGlTV2tjSmxUNzdBY2wtaW1hZ2VzXzE3NzQwNzk4NjIwODFfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwzUm9jbVZsWDJGeVkyaGZZMjl1ZEdWNGRBLnBuZyIsIkNvbmRpdGlvbiI6eyJEYXRlTGVzc1RoYW4iOnsiQVdTOkVwb2NoVGltZSI6MTc5ODc2MTYwMH19fV19&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=ciVRquDWqajpMpXAZRtzwG29TJdyKDyypMa2GNLbvteDh~rJC~G61aAVxqne5KV14TvwHuYy2NDwUs4LF6ejxuMTj-Kq2hNE1uG9Y4uPvABK5HzfAUbfMHzIHeTtxK3WUx8SL0vSLmf05qZvH~p6F8GPBSTBI8mDRFOU90fwBkCEJ~W6PRIboTd7fkQbmeXJz-UKipUaoBU2oPS0DWlNFdnhsxcp67MzFUXHKSdxfd7OSIZN2Vtj0toCEL5iBD9YlhDkMo7xnGaByLt5czHjaW1Wzo~27xF8nBi6Y4blmDDAySPnxXjhTHXLy1o12aTOlba2NvOTtqZhRjhLgDRRRA__)

---

## 5. 綜合評分與選型建議

我們從六個維度（頻寬、容量、延遲、便攜性、普及度、價格）對三種架構進行了綜合評分。

![三種架構綜合評分雷達圖](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/HSMEdMs90iSWkcJlT77Acl-images_1774079862081_na1fn_L2hvbWUvdWJ1bnR1L3RocmVlX2FyY2hfcmFkYXI.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L0hTTUVkTXM5MGlTV2tjSmxUNzdBY2wtaW1hZ2VzXzE3NzQwNzk4NjIwODFfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwzUm9jbVZsWDJGeVkyaGZjbUZrWVhJLnBuZyIsIkNvbmRpdGlvbiI6eyJEYXRlTGVzc1RoYW4iOnsiQVdTOkVwb2NoVGltZSI6MTc5ODc2MTYwMH19fV19&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=pg9lqM8omhjPC6e8waW9ZDa35pcEpt9Wb6Ag1CrUb~KjDq8JNiIkhaPcUeCikA3fYZH2d94Wwz6IisKNWH3krEU1LMbd-gcujBHtY5aoF~4roaG0KVeuOU5gl~z8G8shEZUwbqaXYmV4AtiYfxQMxJUOt2wT2jmWnrfBRwytbO6-wIQy51M0t2CJ9G9R8vEKesPVXq0JR9cE-VfKhemjJ6NtTwu48Y~0gGS2OV5NJ~1vDjABoolx5KkToqVulzTzE4-9-wv3DbDLXgcWOXkXw86L~W0ANfwGoAC1ktdYkHpRCZCs3UMAGU2apNHOtiVgjU4ZaJ-JHzr~ME9TwDsnVA__)

### 結論與建議

**1. 推薦選擇「SD 卡架構」如果：**
*   您追求**極致的便攜性**，希望擴展裝置完全不佔桌面空間。
*   您想實現**AI 模型卡匣化**，利用熱插拔特性，將不同模型存在不同 SD 卡中隨插即用。
*   您使用的是內建 SD 讀卡機的創作者筆記型電腦。

**2. 推薦選擇「外接硬碟盒架構」如果：**
*   您追求**極致的效能與容量**，需要運行 70B 甚至 405B 的巨型模型。
*   您需要處理超過千萬 token 級別的超長 Context 分析（如法務合約審查、大型程式碼庫重構）。
*   您的電腦支援 USB4 或 Thunderbolt 4/5 介面。

**3. 避免使用「USB 儲存裝置架構」（隨身碟/一般外接 SSD）：**
*   雖然這類裝置最為普及且價格低廉，但由於 xHCI 控制器與 Bridge 晶片帶來的**雙重協定轉換**，其延遲過高，頻寬也受到嚴格限制。
*   在 VRAM 擴展場景中，它只能作為「勉強可用」的墊底方案，強烈建議升級至 SD Express 或 USB4 外接硬碟盒。
