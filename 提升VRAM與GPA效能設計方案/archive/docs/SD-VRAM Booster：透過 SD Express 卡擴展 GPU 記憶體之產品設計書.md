# SD-VRAM Booster：透過 SD Express 卡擴展 GPU 記憶體之產品設計書

**製作者：Peter Yang**
**日期：2026 年 3 月 21 日**

---

## 1. 產品概述

### 1.1 背景與動機

在人工智慧與大型語言模型（LLM）快速發展的時代，GPU 的顯示記憶體（VRAM）容量已成為限制模型運行規模的首要瓶頸。以 NVIDIA GeForce RTX 5070 為例，其僅搭載 12GB VRAM，但當前主流的開源大型語言模型（如 Llama-3 70B 的 Q8 量化版本）所需記憶體動輒超過 30GB。傳統的解決方案是購買更高階的顯示卡或使用資料中心級 GPU（如 A100 80GB 或 H100），但這對獨立開發者與中小型企業而言成本過於高昂。

近年來，業界已開始探索利用 NVMe SSD 作為 GPU 記憶體的延伸層。NVIDIA 推出了 **Storage-Next** 計畫與 **GPUDirect Storage** 技術，允許 GPU 直接存取 NVMe 儲存裝置上的資料 [1]。2026 年 3 月，開源社群也出現了名為 **GreenBoost** 的 Linux 核心模組，透過 CUDA 使用者空間攔截層，將系統 RAM 與 NVMe SSD 透明地轉化為 GPU 可用的虛擬 VRAM [2]。同時，KIOXIA 也發表了專為 GPU 記憶體擴展設計的 **GP Series SSD**，採用 XL-FLASH 儲存級記憶體技術，提供 512 位元組的資料存取粒度與極低延遲 [3]。

這些發展證明了一個關鍵事實：**透過 PCIe 介面連接的外部儲存裝置，完全可以作為 GPU VRAM 的有效擴展層**。本產品概念由此出發，提出一個更進一步的問題：「既然 NVMe SSD 可以擴展 VRAM，為什麼 SD 卡不行？」

答案是：**SD Express 卡完全可以**。根據 SD Association 發布的 SD 8.0 規範，最新的 SD Express 記憶卡已全面導入 PCIe Gen.4 介面與 NVMe 應用協定，其理論傳輸頻寬可達 3,940 MB/s [4]。這意味著 SD Express 卡在通訊協定層面與 NVMe SSD 完全一致，差異僅在於物理外形尺寸與通道數量。本產品「**SD-VRAM Booster**」正是基於此技術基礎，將 SD Express 卡轉化為 GPU VRAM 的經濟型、可熱插拔擴展方案。

### 1.2 產品定位

SD-VRAM Booster 並非要取代 GPU 的原生 VRAM，而是作為一個「記憶體溢出層（Memory Overflow Tier）」。其核心價值在於：讓原本因 VRAM 不足而無法運行的大型 AI 模型，能夠以可接受的效能降幅在消費級 GPU 上順利執行。

---

## 2. 核心技術原理

### 2.1 分層記憶體架構（Hierarchical Memory Architecture）

SD-VRAM Booster 的核心設計理念是「分層記憶體架構」，系統將可用記憶體劃分為三個層級，並根據資料的存取頻率（熱度）進行動態調度。這與現代 CPU 的 L1/L2/L3 快取階層概念一脈相承，只是將其擴展到了 GPU 與外部儲存裝置之間。

| 記憶體層級 | 儲存介質 | 理論頻寬 | 延遲等級 | 資料類型 | 角色定位 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **L1 (熱資料)** | GPU VRAM (GDDR6X) | ~1,008 GB/s | 奈秒 (ns) | 活躍張量、當前運算權重 | 主要運算記憶體 |
| **L2 (溫資料)** | 系統 RAM (DDR5) | ~64-100 GB/s | 十奈秒級 | 次頻繁存取的緩衝資料 | 高速緩衝層 |
| **L3 (冷資料)** | SD Express 卡陣列 | ~3.9-15.6 GB/s | 微秒 (μs) | KV Cache、非活躍模型層 | 大容量擴展層 |

*註：頻寬數據基於 SD Association 官方規範 [4] 與現有硬體標準。GDDR6X 數據以 RTX 4090 的 384-bit 匯流排為參考。*

在 LLM 推論的典型工作負載中，並非所有模型權重都需要同時駐留在 GPU VRAM 中。Transformer 架構的模型是逐層（Layer-by-Layer）進行前向傳播的，這意味著在任一時刻，GPU 僅需存取當前正在運算的那一層或少數幾層的權重。其餘層的權重可以安全地存放在較慢但容量更大的儲存層中，待需要時再透過 PCIe 匯流排非同步載入。

### 2.2 NVMe over SD Express 協定整合

SD Express 卡的技術核心在於其採用了與 NVMe SSD 完全相同的通訊協定棧。根據 SD Association 的規範 [4]，SD Express 卡透過 SD 卡插槽的第二排接腳（Pin Row）提供 PCIe 通道，並在其上運行 NVMe 應用協定。這意味著從作業系統的角度來看，一張插入 SD Express 讀卡機的 SD 卡，與一顆安裝在 M.2 插槽中的 NVMe SSD 在驅動層面幾乎沒有差異。

這個特性使得現有的 NVMe-to-GPU 記憶體擴展技術（如 GPUDirect Storage、DirectStorage DMA、以及 GreenBoost 核心模組 [2]）可以幾乎無縫地移植到 SD Express 卡上。SD-VRAM Booster 的軟體驅動正是利用了這一協定相容性，將 SD Express 卡註冊為系統中的 NVMe 區塊裝置，再透過 DMA-BUF 機制將其記憶體區塊匯出給 GPU 使用。

### 2.3 CUDA 攔截與透明化

為了讓上層 AI 應用程式（如 PyTorch、Ollama、Stable Diffusion WebUI）無需任何程式碼修改即可享受擴展的記憶體容量，SD-VRAM Booster 的軟體堆疊採用了「CUDA 攔截（CUDA Interception）」技術。這項技術的靈感來自 GreenBoost 開源專案 [2]，其運作方式如下：

透過 Linux 的 `LD_PRELOAD` 機制，系統會在 CUDA 應用程式啟動前注入一個自訂的共享函式庫（`libsdvram_cuda.so`）。這個函式庫會攔截所有關鍵的 CUDA 記憶體管理 API 呼叫（包括 `cudaMalloc`、`cudaMallocAsync`、`cudaFree` 等）。當偵測到記憶體分配請求超過實體 VRAM 的可用容量時，攔截層會自動將該請求重定向至 SD Express 卡上的預分配記憶體區塊。對於上層應用程式而言，它所獲得的仍然是一個合法的 CUDA 裝置指標（Device Pointer），完全不知道底層的資料實際上存放在 SD 卡中。

---

## 3. 系統架構

以下為 SD-VRAM Booster 的完整系統架構圖，展示從應用層到硬體層的資料流向與各組件之間的關係。

![SD-VRAM Booster 系統架構圖](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/Wy20pIciY3BS8T4sfgTdaq-images_1774071951030_na1fn_L2hvbWUvdWJ1bnR1L2FyY2hpdGVjdHVyZQ.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L1d5MjBwSWNpWTNCUzhUNHNmZ1RkYXEtaW1hZ2VzXzE3NzQwNzE5NTEwMzBfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwyRnlZMmhwZEdWamRIVnlaUS5wbmciLCJDb25kaXRpb24iOnsiRGF0ZUxlc3NUaGFuIjp7IkFXUzpFcG9jaFRpbWUiOjE3OTg3NjE2MDB9fX1dfQ__&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=CuvuTMA7zSzqT5VqfcHmIkUMolupwK1fxH2mxNwD2-KJbEd8xsxJ6BE9pn~3XHuIZ3DvverfJBwblmL6k2OIk~bwDmPqIVJ-V7zsuh5e69TuCR0wCnd1D6xMGv6MHuDd338vLYZHGk0QjdKlA8sY82SxZWfa4JsS82~7HDL-mdgFYxMu0Ko1OCTsdHXUmo3uWr1HNjdVppqyrJYMPEAKofTsaTwmd-DzusH8ij9DxJotl~18z2-LBO5bBj-T2PaMh~ItMI-Rv8ngrN3gkjNcdK2FMOEMGcYyg-si2SiMvUmd0whdw84za~tm8U09UNaaqRQ70DNy9ok5bgkGLkdi8A__)

### 3.1 硬體架構

SD-VRAM Booster 規劃三種硬體產品形態，以覆蓋從桌上型工作站到邊緣運算裝置的不同應用場景。

![產品線與應用場景](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/Wy20pIciY3BS8T4sfgTdaq-images_1774071951030_na1fn_L2hvbWUvdWJ1bnR1L3Byb2R1Y3RfbGluZXVw.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L1d5MjBwSWNpWTNCUzhUNHNmZ1RkYXEtaW1hZ2VzXzE3NzQwNzE5NTEwMzBfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwzQnliMlIxWTNSZmJHbHVaWFZ3LnBuZyIsIkNvbmRpdGlvbiI6eyJEYXRlTGVzc1RoYW4iOnsiQVdTOkVwb2NoVGltZSI6MTc5ODc2MTYwMH19fV19&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=VGilu9pXlydpZZ6N2zQnrkrJV6M8qV~4Q5B2Avqa1UMLf2SMQEyE1YYLvQaaH3exLu~wNs9~vA3wy4z38w4PHQgWwxhE5hTyKWmg5aaG1lObeD~NdedoiDLZJAOF~sZlY~WBjCkD150TvdseQGHm10ZHLD2NOmovMeBFeYW6-cNezI~b1oDrzmdi4XeOPKE3W-Saw-CmdNhbAC0n98FeAMBiaGtU65KLAfpjxIjhIurLlup6I0GFwxx-tLSzUYLEGmyIRy80RAb4ymgmwDAi0VGeYIO9uorUKgIWusak5oI45dZdqodFnt~YQbFBN30l2wRE3VFaUiI~nug2QKWxVQ__)

| 產品型號 | 外形規格 | 介面 | SD Express 插槽數 | 最大聚合頻寬 | 目標裝置 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Booster Pro** | PCIe 半高卡 | PCIe x8 | 4 | ~15.6 GB/s | 桌上型工作站 |
| **Booster Lite** | M.2 2280 模組 | M.2 NVMe | 1 | ~3.94 GB/s | 筆記型電腦、Mini PC |
| **Booster Dock** | 外接盒 | USB4 / TB4 | 2 | ~5 GB/s | 攜帶型、通用裝置 |

**Booster Pro（PCIe 擴充卡）** 是旗艦產品。這張安裝於主機板 PCIe x8 插槽的擴充卡上搭載了四個 SD Express 插槽，並內建 PCIe Switch 晶片以支援 RAID 0 陣列模式。當同時插入四張支援 PCIe Gen.4 x2 的 SD Express 卡時，理論最大聚合頻寬可達約 15.6 GB/s。若搭配四張 4TB 的 SDUC Express 卡，總擴展容量可達 16TB，這對於需要同時載入多個大型模型的研究場景極具吸引力。

**Booster Lite（M.2 轉接模組）** 則針對空間受限的筆記型電腦與迷你電腦設計。使用者只需將閒置的 M.2 NVMe 插槽插入此微型轉接模組，即可獲得一個 SD Express 讀卡槽。雖然僅支援單張 SD 卡，但 3.94 GB/s 的頻寬對於在筆電上運行中型 LLM 已足夠實用。

**Booster Dock（USB4 外接盒）** 提供最大的靈活性。透過 USB4 或 Thunderbolt 4 連接，使用者可以在任何支援 USB4 的裝置上使用 SD-VRAM Booster 功能，無需打開機殼或佔用內部插槽。

### 3.2 軟體架構

軟體堆疊由三個核心組件構成，各司其職：

**VRAM Paging Kernel Module（核心分頁模組）** 是整個系統的基礎。這個 Linux 核心模組負責將 SD Express 卡格式化為專用的連續記憶體區塊（繞過傳統檔案系統以減少 I/O 開銷），並透過 DMA-BUF 框架將這些區塊匯出為可被 GPU 匯入的記憶體描述符。模組內建的監控執行緒會持續追蹤 SD 卡的健康狀態、溫度與剩餘寫入壽命。

**CUDA Intercept Library（CUDA 攔截函式庫）** 是使用者空間的核心。透過 `LD_PRELOAD` 注入後，它會攔截 CUDA 記憶體管理 API，並根據預設的策略（如分配大小閾值、資料存取模式）決定將記憶體分配導向 VRAM、系統 RAM 或 SD Express 卡。此外，它還會攔截 `cuDeviceTotalMem` 等查詢函式，向應用程式回報擴展後的總記憶體容量，確保應用程式能夠充分利用所有可用記憶體。

**Predictive Prefetcher（預測性預取引擎）** 是效能最佳化的關鍵。由於 SD Express 卡的延遲仍顯著高於 VRAM，若 GPU 在需要資料時才發起讀取請求，將會產生嚴重的效能瓶頸。預取引擎透過分析 LLM 推論的逐層執行模式，在 GPU 處理第 N 層時，即提前將第 N+1 層（甚至 N+2 層）的權重從 SD 卡非同步傳輸至 VRAM 或系統 RAM 中。這種「管線化預取（Pipelined Prefetching）」策略能有效掩蓋 I/O 延遲，將效能損失降至最低。

---

## 4. 效能分析

### 4.1 頻寬比較

下圖以對數刻度呈現各類記憶體與儲存介面的頻寬比較，以及不同方案的容量對比。

![頻寬與容量比較圖](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/Wy20pIciY3BS8T4sfgTdaq-images_1774071951030_na1fn_L2hvbWUvdWJ1bnR1L2JhbmR3aWR0aF9jb21wYXJpc29u.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L1d5MjBwSWNpWTNCUzhUNHNmZ1RkYXEtaW1hZ2VzXzE3NzQwNzE5NTEwMzBfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwySmhibVIzYVdSMGFGOWpiMjF3WVhKcGMyOXUucG5nIiwiQ29uZGl0aW9uIjp7IkRhdGVMZXNzVGhhbiI6eyJBV1M6RXBvY2hUaW1lIjoxNzk4NzYxNjAwfX19XX0_&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=D~YPhxGJHUiCDZOOvX4fXBJ7hd6a9NbztIwCeLnHuiZD3SNygQBaXPBr3nIDOThjQ5nirGFe08o6ExC~EmFfhigL-iTqvDAfWDgD7~zCI3FAU9Z5oB2SBSTexBAvq1gQhsPw1NswLESUAoF8z-weQCsXII56rGzftZDH8958WRPu34Np00ioQcJsYFGb5k5DPNCrWA4TC5pr3LSldAfpTlerVefbJUz2QX9jqryDh9YX5hTfmToo9OySEdleNI1OqX9Ukce70tjj9RHFtKMVr7zZRLTKZWr3cu7oVJ3C~bg1oYTATdTtKS8sq9N~LPppwev8ajjVSgCrGLKrQhYn3A__)

從頻寬角度來看，SD Express 卡（單張 ~3.94 GB/s）確實遠低於 GPU 原生 VRAM（~1,008 GB/s），差距約為 250 倍。然而，這並不意味著 SD-VRAM Booster 毫無用處。關鍵在於理解 LLM 推論的記憶體存取模式：在 Transformer 模型的前向傳播過程中，GPU 在任一時刻僅需高速存取當前層的權重矩陣，而其餘層的權重處於閒置狀態。只要預取引擎能在 GPU 完成當前層運算之前，將下一層的權重載入 VRAM，SD 卡的頻寬瓶頸就不會成為實際的效能限制因素。

### 4.2 資料流向

下圖展示了當 AI 應用程式請求 40GB 記憶體時，SD-VRAM Booster 如何將資料分配至三個記憶體層級。

![資料流向圖](https://private-us-east-1.manuscdn.com/sessionFile/tLTWAu85NNmYt4YJ6F3dMN/sandbox/Wy20pIciY3BS8T4sfgTdaq-images_1774071951030_na1fn_L2hvbWUvdWJ1bnR1L2RhdGFfZmxvdw.png?Policy=eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9wcml2YXRlLXVzLWVhc3QtMS5tYW51c2Nkbi5jb20vc2Vzc2lvbkZpbGUvdExUV0F1ODVOTm1ZdDRZSjZGM2RNTi9zYW5kYm94L1d5MjBwSWNpWTNCUzhUNHNmZ1RkYXEtaW1hZ2VzXzE3NzQwNzE5NTEwMzBfbmExZm5fTDJodmJXVXZkV0oxYm5SMUwyUmhkR0ZmWm14dmR3LnBuZyIsIkNvbmRpdGlvbiI6eyJEYXRlTGVzc1RoYW4iOnsiQVdTOkVwb2NoVGltZSI6MTc5ODc2MTYwMH19fV19&Key-Pair-Id=K2HSFNDJXOU9YS&Signature=SCPQCrZ7XPTUmVNtx6gfpW8XsvfYIVEhMpwyX7xE7IegoWOZSi7QbuwmRA5~LckCVy7qOuVCWT1xSA~Fkzy6KexSfG~19a--H~5TuJ5D2LFaugB9Lg0JGdVeTuERYOdtCMKgRak9mBm6dJSCAcD3Eq2lU1sJjvJii7JkNqGsnw3XHE8AgukBjgJ0A7l9F6oYzhmkaZQpaXGYzVc5lpEmaOr~U~Yd80obXN5lHHvtJ1S0Jsvw1DDqjAxqdjPIeRMOppCbcr9DnzeA46-LSYNOsEkQXhcSFePn322hbONvg3c18WQBL9~G8FlXWgmnW46kRt1wqk50hxN7U4~IHwCjrg__)

在此範例中，一個需要 40GB 記憶體的 LLM 模型被分配如下：12GB 最頻繁存取的熱資料（當前運算層的權重與中間結果）存放在 GPU VRAM 中；16GB 溫資料（即將被存取的鄰近層權重）存放在系統 RAM 中作為高速緩衝；剩餘 12GB 冷資料（距離當前運算較遠的層權重與歷史 KV Cache）存放在 SD Express 卡陣列中。預測性預取引擎會持續監控運算進度，提前將冷資料從 SD 卡提升至 RAM 或 VRAM。

### 4.3 預估效能影響

以下表格預估了在不同場景下，使用 SD-VRAM Booster 相較於純 VRAM 運行的效能表現。

| 應用場景 | 模型大小 | GPU VRAM | 無擴展時 | 使用 SD-VRAM Booster | 預估效能 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Llama-3 8B (Q8) | ~8.5 GB | 12 GB | 正常運行 | 不需要 | 100% |
| Llama-3 70B (Q4) | ~40 GB | 12 GB | 無法運行 | 可運行 | 約 40-60% |
| Llama-3 70B (Q4) | ~40 GB | 24 GB | 無法運行 | 可運行 | 約 60-80% |
| SD XL (高解析度) | ~20 GB | 12 GB | OOM 錯誤 | 可運行 | 約 50-70% |

*註：效能預估基於類似技術（如 GreenBoost [2] 與 aiDAPTIV+ [5]）的已知表現推算，實際效能將取決於具體的模型架構、量化方式與硬體配置。*

---

## 5. 產品優勢與差異化

### 5.1 相較於 NVMe SSD 擴展方案的獨特優勢

雖然 NVMe SSD 在原始頻寬上優於 SD Express 卡，但 SD-VRAM Booster 在以下面向提供了 NVMe SSD 方案無法比擬的價值。

**熱插拔模型卡匣（Hot-Swap Model Cartridge）** 是最具吸引力的功能。開發者可以將不同的 AI 模型（如 Llama-3、Mistral、Stable Diffusion 的各種版本）預先存放在不同的 SD Express 卡中。需要切換模型時，只需像更換遊戲卡匣一樣抽換 SD 卡，系統會自動偵測並載入新模型，無需重新下載或在硬碟間搬移數十 GB 的資料。這種「實體模型庫」的概念，為 AI 工作流程帶來了前所未有的便利性。

**邊緣運算裝置的唯一選擇** 也是一大優勢。對於無人機、機器人、工業視覺系統等空間極度受限的邊緣 AI 裝置而言，安裝 M.2 或 U.2 規格的 NVMe SSD 往往不切實際。然而，幾乎所有嵌入式運算平台都配備了 SD 卡插槽。SD-VRAM Booster 使這些裝置能夠在不改變硬體設計的前提下，大幅擴展其 AI 推論能力。

**漸進式模組化升級** 則降低了初期投資門檻。使用者可以先購買單張 SD Express 卡進行小規模擴充，隨著需求增長再逐步添購記憶卡加入陣列。相較於一次性購買大容量高階 NVMe SSD，這種漸進式的投資模式更符合個人開發者與小型團隊的預算規劃。

### 5.2 目標客群

SD-VRAM Booster 瞄準三個核心市場區隔。第一是**在地端運行 LLM 的獨立開發者與研究員**，他們預算有限，無法購買具備 24GB 以上 VRAM 的高階顯示卡，但需要運行 30B 以上參數的大型語言模型進行研究或開發。第二是**邊緣 AI 設備製造商**，他們需要在體積受限的物聯網設備上部署大型 AI 模型，利用 SD Express 卡作為經濟且小巧的記憶體擴展方案。第三是**AI 藝術創作者**，他們需要生成超高解析度圖片或長影片，這些任務極度消耗 VRAM，而 SD-VRAM Booster 能讓他們在消費級 GPU 上完成原本需要專業級硬體才能處理的創作。

---

## 6. 潛在挑戰與對策

### 6.1 寫入壽命（Endurance）

SD 卡的快閃記憶體顆粒若頻繁作為虛擬記憶體進行分頁交換（Swapping），可能會迅速耗盡寫入壽命。對此，SD-VRAM Booster 的驅動程式採用「**唯讀優先（Read-Mostly）**」策略。在 LLM 推論場景中，模型權重在載入後幾乎不會被修改，SD 卡上的資料以讀取操作為主，寫入操作極為稀少。驅動程式會將頻繁更新的動態變數（如梯度、優化器狀態）強制保留在系統 RAM 或實體 VRAM 中，避免對 SD 卡進行不必要的寫入。此外，驅動程式內建的磨損均衡控制器會監控每張 SD 卡的健康狀態，並在壽命接近臨界值時發出警告。

### 6.2 持續傳輸速度衰減

部分 SD 卡在 SLC 快取耗盡後，持續讀寫速度會大幅下降（例如從 880 MB/s 降至 210 MB/s）。SD-VRAM Booster 透過兩種機制應對此問題：首先，產品認證計畫會與 SD 卡製造商合作，建立「SD-VRAM Certified」認證標章，僅推薦通過嚴格持續速度測試的工業級或高階 SD Express 卡。其次，軟體層面的預取引擎會根據 SD 卡的即時傳輸速度動態調整預取深度，在速度下降時增加預取提前量，以維持穩定的資料供給。

### 6.3 SD Express 生態系統尚未成熟

截至 2026 年初，支援 SD Express 的記憶卡與讀卡機產品仍相對有限。SanDisk 與 ADATA 已推出首批消費級 SD Express 卡 [6]，但市場滲透率仍低。SD-VRAM Booster 的市場策略是在早期階段鎖定技術愛好者與 AI 開發者社群，透過開源驅動程式與活躍的社群支援建立口碑，待 SD Express 生態系統成熟後再擴展至大眾市場。

---

## 7. 發展路線圖

| 階段 | 時程 | 里程碑 |
| :--- | :--- | :--- |
| **Phase 1：概念驗證** | 2026 Q2 | 完成軟體驅動原型，在現有 SD Express 讀卡機上驗證 CUDA 攔截與記憶體擴展功能 |
| **Phase 2：硬體原型** | 2026 Q3-Q4 | 完成 Booster Pro PCIe 擴充卡的工程樣品，進行 RAID 0 陣列效能測試 |
| **Phase 3：Beta 測試** | 2027 Q1 | 向 AI 開發者社群發放 Beta 測試套件，收集回饋並優化預取演算法 |
| **Phase 4：量產上市** | 2027 Q2 | Booster Pro 與 Booster Lite 正式量產上市 |
| **Phase 5：生態擴展** | 2027 H2 | 推出 Booster Dock、建立 SD-VRAM Certified 認證計畫、發布 Windows 驅動 |

---

## 8. 結論

「SD-VRAM Booster」透過結合最新的 SD Express PCIe/NVMe 規格與先進的 CUDA 記憶體攔截技術，成功將 SD 卡轉化為 GPU VRAM 的延伸。這個產品概念不僅回答了「既然 SSD 可以擠出 VRAM，為什麼 SD 卡不行？」這個問題，更為 AI 時代的記憶體瓶頸提供了一個兼具熱插拔便利性、高度模組化與低成本的創新解決方案。

SD Express 規格的出現，使得 SD 卡不再僅僅是相機與手機的儲存媒介，而是成為了具備 NVMe 級效能的微型儲存裝置。SD-VRAM Booster 正是抓住了這個技術轉折點，將一個看似不可能的想法——用 SD 卡擴展 GPU 記憶體——變成了一個技術上可行、商業上有價值的產品。

---

## References

[1]: NVIDIA. (2019). GPUDirect Storage: A Direct Path Between Storage and GPU Memory. NVIDIA Developer Blog. https://developer.nvidia.com/blog/gpudirect-storage/

[2]: Larabel, M. (2026). Open-Source "GreenBoost" Driver Aims To Augment NVIDIA GPUs vRAM With System RAM & NVMe To Handle Larger LLMs. Phoronix. https://www.phoronix.com/news/Open-Source-GreenBoost-NVIDIA

[3]: Smith, L. (2026). KIOXIA GP Series SSD Extends GPU Memory with XL-FLASH for NVIDIA Storage-Next AI Workloads. StorageReview. https://www.storagereview.com/news/kioxia-gp-series-ssd-extends-gpu-memory-with-xl-flash-for-nvidia-storage-next-ai-workloads

[4]: SD Association. (n.d.). Bus Speed (Default Speed/High Speed/UHS/SD Express). https://www.sdcard.org/developers/sd-standard-overview/bus-speed-default-speed-high-speed-uhs-sd-express/

[5]: ProX PC. (2026). Expanding GPU VRAM with NVMe: ProX PC & MiPhi AI Breakthrough. https://www.proxpc.com/blogs/expanding-gpu-vram-with-nvme-prox-pc-miphi-ai-breakthrough

[6]: SanDisk. (2025). SanDisk microSD Express 256GB Memory Card. https://www.sandisk.com/products/memory-cards/microsd-cards/sandisk-microsd-express-memory-card
