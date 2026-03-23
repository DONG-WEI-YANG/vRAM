# SD-VRAM Expansion: 透過 SD Express 卡擴展 GPU 記憶體之產品設計文件

**製作者：Peter Yang**

## 1. 產品概述與技術背景

在當前人工智慧與大型語言模型（LLM）快速發展的時代，GPU 的顯示記憶體（VRAM）容量成為限制模型運行規模與效能的最大瓶頸。雖然頂級 GPU 如 NVIDIA RTX 4090 擁有 24GB 的 VRAM，但對於動輒需要 30GB 以上記憶體的大型模型而言仍顯不足。傳統解決方案是購買多張高階顯示卡或使用昂貴的資料中心級 GPU（如 A100/H100），這對一般開發者與中小型企業而言成本過高。

近年來，業界開始探索使用 NVMe SSD 作為 GPU 記憶體的擴展層（例如 NVIDIA 的 GPUDirect Storage 與開源的 GreenBoost 驅動程式 [1]）。既然 SSD 可以透過 PCIe 介面擴展 VRAM，本產品概念旨在回答一個關鍵問題：「既然 SSD 可以，為什麼 SD 卡不行？」

事實上，最新的 **SD Express** 記憶卡規格已全面導入 PCIe 介面與 NVMe 協定 [2]。根據 SD 8.0 規範，採用 PCIe Gen.4 x2 通道的 SD Express 卡，其理論傳輸頻寬可達 3,940 MB/s [3]。這意味著 SD Express 卡在架構上完全具備作為小型化、可熱插拔之 NVMe 儲存裝置的潛力，進而可被用作 GPU VRAM 的經濟型擴展層。

## 2. 核心技術原理

本產品的核心技術建立在「分層記憶體架構（Hierarchical Memory Architecture）」與「直接記憶體存取（DMA）」的基礎上，將 SD Express 卡轉化為 GPU 的虛擬 VRAM。

### 2.1 記憶體分層機制
系統將記憶體劃分為三個層級，根據資料的存取頻率進行動態調度：
1. **L1 - GPU VRAM (熱資料)**：存放當前正在運算的張量（Tensors）與最頻繁存取的模型權重。擁有最高頻寬（如 GDDR6X 的 1,008 GB/s）與最低延遲。
2. **L2 - 系統 RAM (溫資料)**：存放次頻繁存取的資料。頻寬約為 50-100 GB/s。
3. **L3 - SD Express 卡 (冷資料)**：存放龐大的 KV Cache（鍵值快取）或非活躍的模型層權重。透過 PCIe Gen.4 介面，頻寬可達 3.94 GB/s。

### 2.2 NVMe over SD Express 與 CUDA 整合
由於 SD Express 卡本質上運行 NVMe 協定，本產品可利用類似微軟 DirectStorage 或 NVIDIA GPUDirect 的技術，建立從 SD 卡到 GPU VRAM 的直接資料路徑（Direct Data Path）。
透過客製化的核心模組（Kernel Module）與 CUDA 使用者空間攔截層（User-space Shim），系統會將超過實體 VRAM 容量的記憶體分配請求（如 `cudaMalloc`）重定向至 SD Express 卡上的分頁檔 [1]。對上層應用程式（如 PyTorch 或 Ollama）而言，這個過程是完全透明的，應用程式會認為 GPU 擁有了數十 GB 甚至上百 GB 的可用 VRAM。

## 3. 產品架構設計

本產品命名為 **"SD-VRAM Booster"**，包含硬體轉接介面與軟體驅動堆疊兩大部分。

### 3.1 硬體架構：SD-VRAM 擴展卡與讀卡機
為了充分發揮 SD Express 的 PCIe Gen.4 頻寬，硬體設計包含：
*   **PCIe 轉接卡 (Add-in Card)**：一張安裝於主機板 PCIe x4 或 x8 插槽的擴充卡，卡上搭載 2 至 4 個 SD Express 插槽。支援 RAID 0 陣列模式，若同時插入四張 3,940 MB/s 的 SD Express 卡，理論最大聚合頻寬可逼近 15 GB/s，這已接近低階系統 RAM 的頻寬水準。
*   **M.2 轉 SD Express 模組**：針對筆記型電腦或迷你電腦（Mini PC），提供將閒置 M.2 NVMe 插槽轉換為 SD Express 讀卡槽的微型模組，讓筆電也能輕易擴展 GPU VRAM。

### 3.2 軟體架構：動態記憶體調度驅動
軟體堆疊是實現效能最大化的關鍵，包含三個核心組件：
*   **VRAM Paging Kernel Module**：作業系統層級的核心驅動，負責將 SD Express 卡格式化為專用的連續記憶體區塊（非傳統檔案系統），並透過 DMA-BUF 將這些區塊匯出給 GPU 使用。
*   **CUDA Intercept Library**：透過 `LD_PRELOAD` 機制攔截 CUDA API 呼叫。當偵測到大型記憶體分配請求（如 LLM 的 KV Cache 分配）時，將其導向 SD 卡儲存區。
*   **Predictive Prefetcher (預測性預取引擎)**：由於 SD 卡的延遲仍高於 VRAM，軟體內建基於 AI 的預取演算法。在執行 LLM 推論時，引擎會預測下一層神經網路需要的權重，提前將資料從 SD 卡透過 PCIe 匯流排非同步傳輸至 GPU VRAM，從而掩蓋 I/O 延遲。

## 4. 效能與規格比較

以下表格呈現不同記憶體層級與本產品（SD-VRAM Booster 搭配單張與四張 SD Express 卡）的效能比較。

| 儲存/記憶體類型 | 介面協定 | 理論最大頻寬 (GB/s) | 延遲等級 | 容量成本 (預估) | 適用資料類型 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **GPU VRAM (GDDR6X)** | 內部匯流排 | ~1,008.0 | 奈秒 (ns) | 極高 | 活躍張量、熱權重 |
| **系統 RAM (DDR5)** | 記憶體通道 | ~64.0 - 100.0 | 十奈秒 | 中高 | 溫資料、緩衝區 |
| **高階 NVMe SSD** | PCIe 5.0 x4 | ~14.0 | 微秒 (μs) | 中低 | 冷資料、KV Cache |
| **SD Express 卡 (單張)** | PCIe 4.0 x2 / NVMe | ~3.9 | 微秒 (μs) | 低 | 冷資料、非活躍權重 |
| **SD-VRAM Booster (4卡陣列)**| PCIe 4.0 x8 / NVMe | ~15.6 | 微秒 (μs) | 中低 | 大容量 KV Cache |

*註：頻寬數據基於 SD Association 規範與現有硬體標準 [2] [3]。*

## 5. 產品優勢與市場定位

### 5.1 為什麼選擇 SD 卡而非直接使用 SSD？
雖然 NVMe SSD 也能達到擴展 VRAM 的效果，但 SD-VRAM 方案具備以下獨特優勢：
1.  **極致的便攜性與熱插拔**：開發者可以將不同的 AI 模型（如 Llama-3、Stable Diffusion）預先存放在不同的 SD Express 卡中。需要切換模型時，只需像更換遊戲卡匣一樣抽換 SD 卡，無需重新下載或在硬碟間搬移數十 GB 的資料。
2.  **空間受限設備的救星**：對於無法安裝大型 PCIe SSD 的邊緣運算裝置（Edge Devices）、無人機或單板電腦（如 Raspberry Pi 級別的 AI 運算板），SD 卡插槽是唯一可行的超高速儲存擴充方案。
3.  **模組化升級成本低**：使用者可以先購買單張 SD Express 卡進行擴充，隨著模型變大再逐步添購記憶卡加入陣列，初期建置成本低於直接購買大容量高階 NVMe SSD。

### 5.2 目標客群
*   **在地端運行 LLM 的獨立開發者與研究員**：預算有限，無法購買具備 24GB+ VRAM 的高階顯示卡，但需要運行 30B 以上參數的大型語言模型。
*   **邊緣 AI 設備製造商**：需要在體積受限的物聯網設備上部署大型 AI 模型，利用 SD Express 卡作為經濟的記憶體擴展方案。
*   **AI 藝術創作者**：需要生成超高解析度圖片或長影片，這些任務極度消耗 VRAM。

## 6. 潛在挑戰與解決方案

1.  **寫入壽命 (Endurance) 問題**：
    *   *挑戰*：SD 卡的快閃記憶體顆粒若頻繁作為虛擬記憶體進行分頁交換（Swapping），可能會迅速耗盡寫入壽命。
    *   *解決方案*：驅動程式將實作「唯讀優先（Read-Mostly）」策略。SD 卡主要用於存放靜態的模型權重與唯讀的 KV Cache 歷史記錄。頻繁更新的動態變數仍強制保留在系統 RAM 或實體 VRAM 中。
2.  **持續傳輸速度下降**：
    *   *挑戰*：部分 SD 卡在 SLC 快取耗盡後，持續讀寫速度會大幅下降。
    *   *解決方案*：產品建議搭配通過 SD Association 嚴格速度等級認證（如 SD Express Speed Class）的工業級或高階攝影級 SD 卡，確保穩定的持續讀取效能。

## 7. 結論

「SD-VRAM Booster」透過結合最新的 SD Express PCIe/NVMe 規格與先進的 CUDA 記憶體攔截技術，成功將 SD 卡轉化為 GPU VRAM 的延伸。這不僅證明了「既然 SSD 可以，SD 卡也能」的技術可行性，更為 AI 時代的記憶體瓶頸提供了一個具備熱插拔便利性、高度模組化且成本低廉的創新解決方案。

---

### References
[1] Michael Larabel. (2026). Open-Source "GreenBoost" Driver Aims To Augment NVIDIA GPUs vRAM With System RAM & NVMe To Handle Larger LLMs. Phoronix. https://www.phoronix.com/news/Open-Source-GreenBoost-NVIDIA
[2] SD Association. (n.d.). Bus Speed (Default Speed/High Speed/UHS/SD Express). https://www.sdcard.org/developers/sd-standard-overview/bus-speed-default-speed-high-speed-uhs-sd-express/
[3] SD Association. (n.d.). SD Express Speed Class – As introduced in SD 9.1 Specification. https://www.sdcard.org/wp-content/uploads/2023/10/SDExpressSpeedClassSD9_1Specification.pdf
