# SD-VRAM Booster

**透過 SD Express 卡擴展 GPU VRAM 的隨插即用工具**

製作者：**Peter Yang** | 版本：v0.1.0

---

## 產品概述

SD-VRAM Booster 是一款創新的 VRAM 擴展工具。SD Express 卡使用與 NVMe SSD 完全相同的 PCIe + NVMe 協定，既然 SSD 可以擴展 VRAM，SD 卡當然也可以。本工具讓使用者只需將 SD Express 卡插入電腦，系統便會自動偵測環境並詢問是否啟用 VRAM 擴展，整個過程無需安裝任何軟體或驅動程式。

核心價值並非「加速」現有運算，而是讓原本因 VRAM 不足而完全無法運行的 AI 大型語言模型得以執行，同時大幅提升 Context Window 容量（最高可達 80 倍），並支援 KV Cache 持久化與 AI 模型卡匣等進階功能。

## 運作原理

SD-VRAM Booster 採用三層記憶體分層架構，將資料依據存取頻率分配到不同的儲存層級。GPU VRAM 作為第一層（Tier 0），負責存放模型權重與活躍的 KV Cache，提供約 900 GB/s 的頻寬；系統 RAM 作為第二層（Tier 1），以約 50 GB/s 的頻寬擔任緩衝角色；SD Express 卡作為第三層（Tier 2），以 1 至 4 GB/s 的頻寬提供大容量的 KV Cache 卸載空間。

當 AI 模型的記憶體需求超過 GPU VRAM 容量時，溢出的部分會自動分配到 SD 卡上。在 Linux 系統中，這透過高優先級的 swap 檔案實現；在 Windows 系統中，則透過 pagefile 機制完成。GPU 驅動程式（NVIDIA UVM 或 AMD 的統一記憶體管理）會自動處理資料在各層級之間的搬移。

## 隨插即用流程

整個使用流程設計為全自動化。SD 卡插入後，系統會自動啟動偵測程式（Windows 透過 autorun.inf，Linux 透過 udev 規則）。偵測程式首先判斷作業系統類型，接著掃描 GPU 型號與 VRAM 容量，然後讀取 SD 卡暫存器以確認其規格與運作模式。所有偵測完成後，會彈出一個簡潔的確認視窗，顯示偵測結果並詢問使用者是否啟用 VRAM 擴展。使用者只需點擊「啟動」按鈕，背景服務便會自動完成所有設定，並開啟即時監控儀表板。

## 支援的硬體

| 類別 | 支援項目 |
|------|---------|
| 作業系統 | Windows 10/11、Linux (Kernel 5.18+) |
| GPU 廠商 | NVIDIA (nvidia-smi)、AMD (rocm-smi / sysfs) |
| SD 卡規格 | SD Express Gen3 x1/x2、Gen4 x1/x2 |
| 最低頻寬 | 200 MB/s（低於此值系統會自動拒絕） |

## 效能預估

以下為 RTX 4070 (12GB VRAM) + 512GB SD Express Gen3 x2 的典型場景：

| 模型 | 純 VRAM | SD 擴展後 | Context 提升 |
|------|---------|----------|-------------|
| Llama-3 8B (Q4) | 125 tok/s | 125 tok/s (VRAM 足夠) | 53K → 3.7M (69x) |
| Qwen-2.5 32B (Q4) | 無法運行 | 0.3 tok/s | 0 → 1.8M |
| Llama-3 70B (Q4) | 無法運行 | 0.1 tok/s | 0 → 1.5M |

## 快速開始

**方法一：直接從 SD 卡啟動（建議）**

將 `sd_card_root/` 目錄下的所有檔案複製到 SD Express 卡的根目錄。插入 SD 卡後，Windows 系統會自動彈出啟動提示，Linux 系統需先執行 `sudo bash scripts/install_udev_rule.sh` 安裝 udev 規則。

**方法二：從原始碼執行**

```bash
cd sd-vram-booster
python3 sdvram/main.py          # GUI 模式
python3 sdvram/main.py --cli    # CLI 模式
python3 run_demo.py             # Demo 模擬測試
```

**方法三：打包為獨立執行檔**

```bash
pip install pyinstaller
python3 scripts/build.py
```

打包完成後，執行檔會自動放入 `sd_card_root/windows/` 或 `sd_card_root/linux/` 目錄。

## 專案結構

```
sd-vram-booster/
├── sdvram/                    # 核心套件（純 Python 標準庫）
│   ├── main.py                # 主程式進入點
│   ├── demo.py                # Demo 模擬模式
│   ├── detectors/             # 自動偵測模組 (OS / GPU / SD)
│   ├── memory/                # 記憶體分層管理器
│   ├── engine/                # VRAM 擴展引擎
│   └── gui/                   # tkinter GUI（彈窗 + 儀表板）
├── sd_card_root/              # SD 卡目錄結構（複製到 SD 卡）
├── scripts/                   # 打包與安裝腳本
├── docs/                      # 技術文件
├── run_demo.py                # Demo 測試腳本
└── README.md
```

## 授權

Copyright (c) 2026 Peter Yang. All rights reserved.
