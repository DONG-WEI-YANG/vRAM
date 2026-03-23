# Enclosure-VRAM Booster v0.1.0

**透過 USB4/Thunderbolt 外接 NVMe 硬碟盒擴展 GPU VRAM**

製作者：**DONG. WEI YANG**

---

## 產品概述

Enclosure-VRAM Booster 是一套隨插即用的 VRAM 擴展系統，透過 USB4/Thunderbolt 外接 NVMe 硬碟盒的 **PCIe Tunneling** 技術，將外接 SSD 的儲存空間轉化為 GPU 可用的擴展記憶體。

與 SD 卡和 USB 儲存裝置不同，外接硬碟盒走的是 **PCIe Tunneling**（僅 1 次協定封裝），延遲低至 10-15μs，頻寬最高可達 10,000 MB/s（Thunderbolt 5），是三種架構中容量與頻寬的絕對王者。

## 核心特性

- **雙 OS 自動偵測**：自動判斷 Windows / Linux，部署對應驅動
- **隨插即偵測**：外接硬碟盒插入後自動偵測，彈出確認視窗
- **零安裝**：PyInstaller 打包成獨立執行檔，不需要安裝 Python
- **一鍵啟動**：Windows 雙擊 `.bat`，Linux 執行 `.sh`
- **三層記憶體管理**：VRAM → RAM → 外接盒 NVMe
- **即時監控儀表板**：tkinter GUI 顯示記憶體狀態與效能指標

## 支援的連接協定

| 協定 | 頻寬 | 延遲 | 適用場景 |
|------|------|------|---------|
| USB4 v1 | 3,800 MB/s | 15 μs | 主流方案 |
| USB4 v2 | 7,500 MB/s | 12 μs | 高階方案 |
| Thunderbolt 3 | 2,800 MB/s | 18 μs | 舊款 Mac/PC |
| Thunderbolt 4 | 3,000 MB/s | 15 μs | 現代筆電 |
| Thunderbolt 5 | 10,000 MB/s | 10 μs | 旗艦方案 |

## 快速開始

### 方法 1: 直接執行（需要 Python 3.8+）

```bash
# Linux
chmod +x setup_and_run.sh
./setup_and_run.sh

# Windows
setup_and_run.bat
```

### 方法 2: 打包成獨立執行檔

```bash
python3 scripts/build.py
# 產出 dist/EnclosureVRAMBooster (Linux) 或 dist/EnclosureVRAMBooster.exe (Windows)
```

### 方法 3: Demo 模擬測試

```bash
python3 run_demo.py
```

## 專案結構

```
enclosure-vram-booster/
├── encvram/
│   ├── __init__.py              # 主套件
│   ├── main.py                  # 主程式進入點
│   ├── demo.py                  # Demo 模擬模組
│   ├── detectors/
│   │   ├── os_detector.py       # OS 偵測器 (Windows/Linux)
│   │   ├── gpu_detector.py      # GPU 偵測器 (NVIDIA/AMD)
│   │   ├── enclosure_detector.py # 外接硬碟盒偵測器
│   │   └── system_scanner.py    # 系統掃描器
│   ├── memory/
│   │   └── tier_manager.py      # 三層記憶體管理器
│   ├── engine/
│   │   └── booster.py           # 核心引擎
│   └── gui/
│       ├── popup.py             # 確認彈窗
│       └── dashboard.py         # 即時監控儀表板
├── scripts/
│   └── build.py                 # PyInstaller 打包腳本
├── setup_and_run.bat            # Windows 一鍵啟動
├── setup_and_run.sh             # Linux 一鍵啟動
├── run_demo.py                  # Demo 測試腳本
└── README.md
```

## 與其他方案的比較

| 特性 | SD-VRAM Booster | USB-VRAM Booster | Enclosure-VRAM Booster |
|------|----------------|-----------------|----------------------|
| 協定路徑 | PCIe 原生 | xHCI + Bridge | PCIe Tunneling |
| 協定轉換 | 0 次 | 2 次 | 1 次 |
| 最大頻寬 | 3,940 MB/s | 2,500 MB/s | 10,000 MB/s |
| 最大容量 | 1 TB | 4 TB | 8+ TB |
| 延遲 | 8-10 μs | 80-200 μs | 10-15 μs |
| 最佳用途 | 模型卡匣、筆電 | 入門方案 | 容量與效能兼顧 |

---

**DONG. WEI YANG** | Enclosure-VRAM Booster v0.1.0
