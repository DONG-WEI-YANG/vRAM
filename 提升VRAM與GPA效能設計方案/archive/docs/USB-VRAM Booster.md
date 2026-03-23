# USB-VRAM Booster

**透過 USB 裝置擴展 GPU VRAM，讓大型 AI 模型在消費級顯示卡上運行**

**製作者：Peter Yang** | 版本：v0.1.0

---

## 產品概念

USB-VRAM Booster 利用 USB 外接儲存裝置（隨身碟、外接 SSD）作為 GPU VRAM 的擴展層。當 AI 模型大小超過實體 VRAM 時，系統自動將溢出的模型權重與 KV Cache 卸載至 USB 儲存裝置，讓原本會 OOM（Out of Memory）的大型模型得以在消費級顯示卡上運行。

核心技術利用 **USB4 的 PCIe Tunneling** 機制，讓外接 NVMe SSD 以接近原生 PCIe 的效能運作，頻寬可達 **10,000 MB/s**（USB4 V2）甚至 **12,000 MB/s**（Thunderbolt 5）。

## 功能特色

| 功能 | 說明 |
|------|------|
| 雙系統自動偵測 | 自動判斷 Windows / Linux，部署對應驅動 |
| USB 裝置自動辨識 | 偵測 USB 版本、PCIe Tunneling 支援、NVMe 模式 |
| GPU 自動偵測 | 支援 NVIDIA (nvidia-smi) 與 AMD (rocm-smi, sysfs) |
| 三層記憶體管理 | VRAM → System RAM → USB Storage 自動分層 |
| 隨插即用 | USB 插入後自動彈出確認視窗 |
| 即時儀表板 | tkinter GUI 顯示記憶體分層、效能預估 |
| 熱插拔防護 | USB 斷開時自動暫停 GPU 運算 |
| 零外部依賴 | 純 Python 標準庫，無需安裝額外套件 |

## 快速開始

### 方法 1：直接執行（需要 Python 3.8+）

```bash
# Linux
chmod +x setup_and_run.sh
./setup_and_run.sh

# Windows
setup_and_run.bat
```

### 方法 2：命令列模式

```bash
# 掃描系統
python -m usbvram.main --scan-only

# Demo 模式（無需硬體）
python -m usbvram.main --demo 1 --no-gui

# 正常啟動
python -m usbvram.main
```

### 方法 3：打包為獨立執行檔

```bash
python scripts/build.py
# 執行檔位於 dist/USB-VRAM-Booster
```

## Demo 場景

| 場景 | USB 裝置 | GPU | 協定 | 結果 |
|------|---------|-----|------|------|
| 1 | Samsung 990 Pro 1TB (USB4 外接盒) | RTX 4070 | USB4 v1 (PCIe Tunnel) | 可擴展 +876GB |
| 2 | WD Black SN850X 2TB (USB4 V2 外接盒) | RTX 4090 | USB4 v2 (PCIe Tunnel) | 可擴展 +1753GB |
| 3 | SanDisk Extreme Pro 256GB | RTX 3060 | USB 3.2 Gen2 (xHCI) | 可擴展 +212GB (有限) |
| 4 | Sabrent Rocket XTRM 4TB (TB5 外接盒) | RTX 5090 | TB5 (PCIe Tunnel) | 可擴展 +3506GB |
| 5 | Generic USB 2.0 32GB | RTX 4060 | USB 2.0 | 拒絕 (頻寬不足) |

## 專案結構

```
usb-vram-booster/
├── usbvram/
│   ├── __init__.py
│   ├── main.py              # 主程式進入點
│   ├── demo.py              # Demo 模擬模組
│   ├── detectors/
│   │   ├── os_detector.py   # OS 偵測器 (Windows/Linux)
│   │   ├── gpu_detector.py  # GPU 偵測器 (NVIDIA/AMD)
│   │   ├── usb_detector.py  # USB 裝置偵測器
│   │   └── system_scanner.py # 系統掃描器
│   ├── memory/
│   │   └── tier_manager.py  # 三層記憶體管理器
│   ├── engine/
│   │   └── booster.py       # 核心引擎
│   └── gui/
│       ├── popup.py         # 隨插即用確認彈窗
│       └── dashboard.py     # 即時監控儀表板
├── scripts/
│   └── build.py             # PyInstaller 打包腳本
├── docs/
│   └── spec.md              # 技術規格書
├── setup_and_run.bat        # Windows 一鍵啟動
├── setup_and_run.sh         # Linux 一鍵啟動
├── run_demo.py              # Demo 測試腳本
└── README.md
```

## 與 SD-VRAM Booster 的比較

| 特性 | SD-VRAM Booster | USB-VRAM Booster |
|------|----------------|-----------------|
| 協定 | PCIe + NVMe (原生) | USB Host / PCIe Tunnel |
| 最大頻寬 | 3,940 MB/s | 10,000 MB/s (USB4 V2) |
| 裝置普及度 | 低 (SD Express 新) | 極高 |
| 最大容量 | ~1TB | 4TB+ |
| 體積 | 極小 (卡片) | 中 (外接盒) |
| 最佳場景 | AI 模型卡匣 | 超長 Context / 巨型模型 |

## 授權

MIT License

---

**製作者：Peter Yang**
