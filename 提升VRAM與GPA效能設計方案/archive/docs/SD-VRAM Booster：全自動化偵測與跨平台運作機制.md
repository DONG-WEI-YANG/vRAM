# SD-VRAM Booster：全自動化偵測與跨平台運作機制

**製作者：Peter Yang**

為了回答「是否需要先偵測 SD 卡規格」以及「產品如何讓 Windows 或 Linux 應用 VRAM 提升」等核心問題，並確保使用者體驗的極致簡化，本文件詳細解析 SD-VRAM Booster 的**全自動化運作流程**。

## 1. 核心設計理念：零設定 (Zero-Configuration)

SD-VRAM Booster 的設計哲學是「隨插即用，自動最佳化」。使用者不需要手動選擇作業系統，也不需要手動指定 SD 卡的規格。整個系統包含三個層級的自動偵測：
1.  **環境自動偵測**：安裝程式自動判斷是 Windows 還是 Linux，並部署對應的底層驅動。
2.  **硬體自動偵測**：自動識別插入的 SD 卡是否具備 PCIe/NVMe 能力，並測試頻寬。
3.  **負載自動調度**：AI 應用程式啟動時，驅動程式自動攔截記憶體請求，動態分配 VRAM。

---

## 2. 第一層：作業系統與環境自動偵測

為了實現跨平台支援，SD-VRAM Booster 提供一個**統一安裝程式 (Unified Installer)**。

當使用者執行安裝程式時，系統會進行以下自動判斷：
*   **OS 核心判斷**：
    *   若偵測到 Windows (NT Kernel)：安裝程式會自動部署基於 **DirectStorage API** 與 **CUDA Virtual Memory Management (VMM)** 的背景服務 (`sdvram_service.exe`)。
    *   若偵測到 Linux (Linux Kernel)：安裝程式會自動編譯並掛載 `greenboost.ko` 核心模組，並設定 `LD_PRELOAD` 環境變數以注入 `libsdvram_cuda.so` 攔截層。
*   **GPU 環境偵測**：安裝程式會自動呼叫 `nvidia-smi` 檢查實體 VRAM 容量（例如偵測到 RTX 4070 12GB），並將此數據作為後續記憶體調度的基準線。

使用者完全不需要知道背後的技術差異，只需點擊「安裝」即可完成環境配置。

---

## 3. 第二層：SD 卡規格自動偵測與初始化

當使用者將 SD 卡插入 SD-VRAM Booster 的硬體插槽時，硬體控制器會執行標準化的「SD-First 偵測流程」[1]：

### 步驟一：基礎 SD 模式喚醒
硬體首先以傳統的低速 SD 模式（3.3V，SPI 或 SD 1-bit 模式）喚醒卡片。這是為了確保完全的向下相容性，避免損壞不支援高電壓的舊款 SD 卡。

### 步驟二：讀取硬體暫存器 (CID / CSD / SCR)
主機控制器會發送指令讀取 SD 卡內建的硬體暫存器：
*   **CID (Card Identification Register)**：確認卡片製造商與序號。
*   **CSD (Card-Specific Data Register)**：確認卡片的總容量。
*   **SCR (SD Configuration Register)**：這是最關鍵的一步。控制器透過讀取 SCR，確認該卡片是否支援 SD 7.0/8.0 規範，以及是否具備 PCIe 介面。

### 步驟三：PCIe / NVMe 模式切換與交握
若 SCR 暫存器回報這是一張 SD Express 卡，主機控制器會發送特定指令，指示卡片切換至 PCIe 模式。
此時，SD 卡會關閉傳統的 SD 針腳，啟用第二排的 PCIe 針腳。對作業系統而言，這張 SD 卡現在會以一個**標準的 NVMe 固態硬碟（PCIe Class ID: 01h->08h->02h）**的身份重新連接到系統匯流排上 [1]。

### 步驟四：效能分級與資源池分配
軟體驅動程式接手，對這個新出現的 NVMe 裝置進行 5 秒的背景頻寬測試。
*   **高效能池（頻寬 > 1,500 MB/s）**：如 SD Express Gen4，將被自動加入 VRAM 擴展資源池。
*   **拒絕池（頻寬 < 500 MB/s 或非 NVMe 模式）**：如誤插的傳統 UHS-I/II 卡，驅動程式會拒絕將其作為 VRAM，僅允許作業系統將其當作普通隨身碟使用，並跳出警告提示使用者。

---

## 4. 第三層：跨平台驅動與負載調度

當環境與硬體都準備就緒後，SD-VRAM Booster 如何讓 AI 應用程式「以為」自己有更多 VRAM？

### 4.1 Linux 環境：CUDA API 攔截 (LD_PRELOAD)
在 Linux 系統上，SD-VRAM Booster 採用類似開源專案 GreenBoost 的架構 [2]。
1.  **記憶體匯出**：核心模組將 SD Express 卡格式化為連續的記憶體區塊，並透過 DMA-BUF 子系統匯出。
2.  **動態攔截**：當啟動 PyTorch 時，`libsdvram_cuda.so` 會攔截 `cudaMalloc()`。
3.  **無縫重定向**：若 PyTorch 要求 40GB VRAM，但實體只有 12GB，攔截層會將超出的 28GB 請求重定向到 SD Express 卡上。PyTorch 完全不知情。

### 4.2 Windows 環境：DirectStorage 與虛擬記憶體
Windows 無法輕易使用 LD_PRELOAD，因此採用微軟的 **DirectStorage API** 與 NVIDIA 的 **CUDA VMM API** [3]。
1.  **虛擬映射**：背景服務使用 `cuMemCreate` 建立一個巨大的虛擬 VRAM 位址空間，並將其物理備份指向 SD Express 卡。
2.  **硬體解壓縮與直達**：當 GPU 需要讀取 SD 卡上的模型權重時，呼叫 DirectStorage API。這能繞過 CPU，直接指示 NVMe 控制器將資料透過 PCIe 匯流排 DMA 傳輸到 GPU 的實體 VRAM 中。

### 4.3 跨平台架構對比表

| 平台 | 安裝程式行為 | 記憶體攔截技術 | 資料傳輸機制 | 應用程式相容性 |
| :--- | :--- | :--- | :--- | :--- |
| **Linux** | 自動編譯 Kernel Module | `LD_PRELOAD` CUDA Hook | DMA-BUF / GPUDirect | 無需修改，隨插即用 |
| **Windows** | 自動註冊 Background Service | CUDA VMM API 映射 | DirectStorage API | 無需修改，隨插即用 |

---

## 5. 總結：全自動化使用者旅程

假設使用者買了一張 RTX 4070 (12GB VRAM)，想運行需要 30GB VRAM 的 Llama-3 70B 模型：

1.  **統一安裝**：使用者下載 `SD-VRAM_Installer` 並執行。程式自動偵測到這是 Windows 11，並自動安裝 DirectStorage 版本的背景服務。
2.  **硬體插入**：使用者將兩張 1TB 的 SD Express 卡插入外接盒。硬體自動讀取 SCR 暫存器，確認是 PCIe 規格，並將其切換為 NVMe 模式。
3.  **效能驗證**：Windows 驅動在背景測試頻寬達標，系統右下角跳出通知：「已成功擴展 2TB 虛擬 VRAM」。
4.  **無縫啟動**：使用者直接打開 Ollama 載入模型。Ollama 請求 30GB VRAM，驅動程式自動攔截並調度，模型順利運行。

透過這三層自動化設計，SD-VRAM Booster 實現了真正的「隨插即用」，讓一般使用者也能輕鬆突破硬體限制。

---

### 參考文獻
[1] SD Association. (2020). SD Express Cards with PCIe and NVMe Interfaces White Paper.
[2] Larabel, M. (2026). Open-Source "GreenBoost" Driver Aims To Augment NVIDIA GPUs vRAM With System RAM & NVMe To Handle Larger LLMs. Phoronix.
[3] NVIDIA. (2020). Introducing Low-Level GPU Virtual Memory Management. NVIDIA Developer Blog.
