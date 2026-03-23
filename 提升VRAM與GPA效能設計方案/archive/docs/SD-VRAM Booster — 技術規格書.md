# SD-VRAM Booster — 技術規格書

**版本**: v0.1.0
**製作者**: Peter Yang
**日期**: 2026-03-21

---

## 1. 產品概述

SD-VRAM Booster 是一款隨插即用的 VRAM 擴展工具，透過 SD Express 卡的 PCIe/NVMe 協定，將 SD 卡空間轉化為 GPU 可用的擴展記憶體，讓原本因 VRAM 不足而無法運行的 AI 模型得以執行。

## 2. 系統需求

| 項目 | 最低需求 | 建議配置 |
|------|---------|---------|
| 作業系統 | Windows 10 21H2 / Linux Kernel 5.18+ | Windows 11 / Ubuntu 24.04+ |
| GPU | NVIDIA GTX 1060 / AMD RX 580 | NVIDIA RTX 4060+ / AMD RX 7600+ |
| SD 卡 | SD Express Gen3 x1 (985 MB/s) | SD Express Gen4 x2 (3,940 MB/s) |
| SD 讀卡機 | SD Express 相容 PCIe 讀卡機 | 內建 SD Express 插槽 |
| 系統 RAM | 8 GB | 16 GB+ |
| Python | 3.8+ (僅原始碼模式) | 不需要 (打包模式) |

## 3. 軟體架構

```
sd-vram-booster/
├── sdvram/                    # 核心套件
│   ├── __init__.py            # 版本資訊
│   ├── main.py                # 主程式進入點
│   ├── demo.py                # Demo 模擬模式
│   ├── detectors/             # 偵測模組
│   │   ├── os_detector.py     # OS 自動偵測 (Win/Linux)
│   │   ├── gpu_detector.py    # GPU 偵測 (NVIDIA/AMD)
│   │   ├── sd_detector.py     # SD 卡偵測與規格分類
│   │   └── system_scanner.py  # 系統掃描整合器
│   ├── memory/                # 記憶體管理
│   │   └── tier_manager.py    # 三層記憶體分層管理
│   ├── engine/                # 核心引擎
│   │   └── booster.py         # VRAM 擴展引擎
│   └── gui/                   # 圖形介面
│       ├── popup.py           # 確認彈窗
│       └── dashboard.py       # 即時監控儀表板
├── sd_card_root/              # SD 卡目錄結構
│   ├── autorun.inf            # Windows 自動執行
│   ├── windows/start.bat      # Windows 啟動腳本
│   └── linux/start.sh         # Linux 啟動腳本
├── scripts/                   # 工具腳本
│   ├── build.py               # PyInstaller 打包
│   └── install_udev_rule.sh   # Linux udev 自動偵測
└── docs/                      # 文件
    ├── spec.md                # 技術規格書
    └── agent.md               # 開發紀錄
```

## 4. 偵測流程

### 4.1 自動偵測順序

1. **OS 偵測**: `platform.system()` → 判斷 Windows / Linux
2. **GPU 偵測**:
   - NVIDIA: `nvidia-smi --query-gpu` (CSV 輸出)
   - AMD: `rocm-smi` 或 `/sys/class/drm/card*/device/`
   - 後備: `lspci` (Linux) / `Get-CimInstance Win32_VideoController` (Windows)
3. **SD 卡偵測**:
   - Linux MMC: `/sys/class/mmc_host/` → 讀取卡片暫存器
   - Linux NVMe SD: `lsblk -J` → 過濾 NVMe + SD 關鍵字
   - Windows: `Get-WmiObject Win32_LogicalDisk` + `Get-PhysicalDisk`

### 4.2 SD 卡規格分級

| 模式 | 協定 | 最大頻寬 | 可用於 VRAM |
|------|------|---------|-----------|
| UHS-I | SD Bus | 104 MB/s | 否 (頻寬不足) |
| UHS-II | SD Bus | 312 MB/s | 否 (頻寬不足) |
| SD Express Gen3 x1 | PCIe 3.0 + NVMe | 985 MB/s | 是 |
| SD Express Gen3 x2 | PCIe 3.0 + NVMe | 1,969 MB/s | 是 |
| SD Express Gen4 x1 | PCIe 4.0 + NVMe | 1,969 MB/s | 是 |
| SD Express Gen4 x2 | PCIe 4.0 + NVMe | 3,940 MB/s | 是 |

最低可用頻寬門檻: **200 MB/s**

## 5. 記憶體分層架構

| 層級 | 儲存媒介 | 典型頻寬 | 典型延遲 | 用途 |
|------|---------|---------|---------|------|
| Tier 0 | GPU VRAM | ~900 GB/s | ~1 μs | 模型權重、活躍 KV Cache |
| Tier 1 | System RAM | ~50 GB/s | ~100 μs | 溫資料緩衝 |
| Tier 2 | SD Express | ~1-4 GB/s | ~10 ms | KV Cache 卸載、模型溢出 |

## 6. VRAM 擴展機制

### Linux
- 在 SD 卡掛載點建立 swap 檔案 (`.sdvram_swap`)
- 使用 `mkswap` + `swapon -p 10` 啟用高優先級 swap
- GPU 驅動透過 UVM (Unified Virtual Memory) 自動使用

### Windows
- 在 SD 卡建立 pagefile (`sdvram_pagefile.sys`)
- 透過 WMI `Win32_PageFileSetting` 設定
- GPU 驅動透過 WDDM 自動使用

## 7. 效能預估公式

### 推理速度 (tokens/s)
```
若模型完全在 VRAM 內:
  TPS = 1000 / compute_time_ms

若模型溢出到 SD 卡:
  load_time_ms = overflow_GB × 1024 / SD_bandwidth_MBs × 1000
  TPS = 1000 / (compute_time_ms + load_time_ms)
```

### Context Window 計算
```
KV Cache per token = 2 × num_layers × num_heads × head_dim × 2 (bytes, FP16)
Max context = available_memory_bytes / KV_per_token
```

## 8. 安全機制

- SD 卡頻寬低於 200 MB/s 時自動拒絕
- 停用時安全清除 swap/pagefile
- 關閉儀表板前確認是否停用擴展
- 保留 SD 卡 10% 空間給系統使用
