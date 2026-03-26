# Safety Policy + Safe Removal Design Spec

**Date:** 2026-03-26
**Status:** Approved
**Scope:** `core/safety_policy.py`, `core/safe_removal.py`, `host_ui.py` 修改

---

## 目標

將 Host Mode 從「能跑」升級為「能安全跑」：
1. **Min/Max 安全策略** — 保護裝置空間 + 限制 pagefile 大小 + 保護系統 RAM
2. **四階段安全移除** — preflight → drain → detach → ready，附即時進度回饋
3. **智慧預設 + 進階可調** — 自動計算合理值，使用者可展開 Advanced 面板微調
4. **雙層設定** — 主機存 global policy，裝置存 per-device override，合併生效

---

## 架構：方案 2（獨立策略引擎 + UI 薄層）

新建兩個 core 模組，`host_ui.py` 只負責呈現。延續現有 `core/` 放引擎的分層慣例。

### 檔案結構

**新增：**
```
microsystems/core/
├── safety_policy.py    # ~200 行
└── safe_removal.py     # ~300 行
```

**修改：**
```
microsystems/
├── host_ui.py          # 新增 PolicyPanel、DrainProgressCard、Advanced 面板
└── core/real_boost.py  # activate() 內整合 validate_activation()
```

**不改動：**
- `vhd_bridge.py`, `vhd_pagefile.py` — 底層 VHD 操作不變
- `circuit_breaker.py`, `health_monitor.py`, `recovery_chain.py` — 不動
- `cli.py` — 本次不改（未來可接 SafetyPolicy）

---

## Section 1: Safety Policy 引擎

### `core/safety_policy.py`

**核心資料結構：**

```python
@dataclass
class PolicyLimits:
    device_reserved_gb: float     # 裝置上至少保留多少空間
    pagefile_min_gb: float        # 低於此值不值得啟用
    pagefile_max_gb: float        # 單一裝置的 pagefile 上限
    system_ram_reserve_gb: float  # drain 時系統 RAM 至少保留多少
```

**智慧預設演算法：**

| 裝置容量 | device_reserved | pf_min | pf_max |
|---------|----------------|--------|--------|
| < 32 GB | 2 GB | 0.5 GB | 容量 x 60% |
| 32-128 GB | 4 GB | 1 GB | 容量 x 70% |
| 128+ GB | 8 GB | 2 GB | 容量 x 80% |

`system_ram_reserve_gb` = 系統 RAM x 20%

**公開 API：**

```python
class SafetyPolicy:
    @staticmethod
    def compute_smart_defaults(capacity_gb: float, speed_mbs: float) -> PolicyLimits

    @staticmethod
    def load_merged_policy(global_path: Path, device_path: Path,
                           capacity_gb: float, speed_mbs: float) -> PolicyLimits

    @staticmethod
    def save_global_policy(path: Path, overrides: dict) -> None

    @staticmethod
    def save_device_override(device_config_path: Path, overrides: dict) -> None

    @staticmethod
    def validate_activation(capacity_gb: float, requested_gb: float,
                            policy: PolicyLimits) -> tuple[bool, str]
```

**合併順序：**
```
smart_default(capacity, speed) ← global_defaults ← device.safety_override → 最終 PolicyLimits
```

**驗證規則：**
1. `requested >= policy.pagefile_min_gb`
2. `requested <= policy.pagefile_max_gb`
3. `capacity - requested >= policy.device_reserved_gb`

---

## Section 2: Safe Removal 引擎

### `core/safe_removal.py`

**狀態機：**
```
IDLE → PREFLIGHT → DRAINING → DETACHING → READY
                      ↓
                 FORCE_EJECT → READY
```

**Phase 0 — Preflight Check：**

```python
@dataclass
class PreflightResult:
    can_remove_immediately: bool
    current_usage_mb: float
    system_ram_available_mb: float
    can_absorb: bool
    estimated_drain_seconds: float
    warnings: list[str]
```

- 讀取 VHD pagefile 即時使用量（WMI `Win32_PageFileUsage`）
- 計算系統 RAM + 其他 pagefile 空閒容量
- `usage == 0` → `can_remove_immediately = True`
- RAM < `policy.system_ram_reserve_gb` → warning

**Phase 1 — Drain：**

```python
@dataclass
class DrainProgress:
    remaining_mb: float
    total_mb: float
    drain_rate_mbs: float
    eta_seconds: float
    phase: str  # "draining" | "detaching" | "ready"
```

- 從 Windows pagefile list 移除該裝置的 pagefile（WMI / registry）
- 每秒監控回報 `remaining_mb`, `drain_rate_mbs`, `eta_seconds`
- `remaining_mb == 0` → 自動進入 Phase 2

**Phase 2 — Detach：**
- 刪除 VHD 上的 pagefile 檔案
- `DetachVirtualDisk()` 卸載 VHD
- 刪除 VHD 檔案

**Phase 3 — Ready：**
- 回呼 UI `on_ready(drive_letter)` → 綠燈
- 可選呼叫 `CM_Request_Device_Eject` 安全移除硬體

**Force Eject（緊急旁路）：**
- Flush dirty pages（best effort, timeout 5s）
- 強制移除 pagefile + DetachVirtualDisk
- 直接進入 READY（pagefile 是 swap 資料，不是用戶檔案，最壞情況是 process crash）

**逾時保護：**
- Drain 超過 5 分鐘 → 提示「是否 Force Eject？」
- 整個流程 10 分鐘硬上限

**公開 API：**

```python
class SafeRemovalManager:
    def preflight_check(self, device_id: str, policy: PolicyLimits) -> PreflightResult
    def start_drain(self, device_id: str, on_progress: Callable) -> None
    def cancel_drain(self, device_id: str) -> None
    def force_eject(self, device_id: str) -> None
    def state(self, device_id: str) -> RemovalState
```

---

## Section 3: UI 整合

### Host Mode UI 變更

**新增 Safety Policy 面板（收合式）：**

```
⚙ Safety Policy          [Advanced ▼]
Device reserve: 4 GB | PF: 1~25 GB
┌ Advanced ─────────────────────────┐
│ Device Reserved  [====|----] 4 GB │
│ Pagefile Min     [=|-------] 1 GB │
│ Pagefile Max     [======|--] 25GB │
│ RAM Reserve      [==|------] 20%  │
│        [Reset to Smart Defaults]  │
└───────────────────────────────────┘
```

**Drain 進度取代原本的 DeviceCard：**

```
┌─ E:\ [DRAINING...] ──────────────────────┐
│ Draining pagefile...                      │
│ ████████████░░░░░░░░  872 / 1204 MB       │
│ Speed: 45 MB/s  |  ETA: ~7 sec           │
│        [Force Eject]    [Cancel]          │
└───────────────────────────────────────────┘
```

完成後：

```
┌─ E:\ [✓ READY TO REMOVE] ────────────────┐
│ VHD detached. Safe to unplug hardware.    │
└───────────────────────────────────────────┘
```

**UI 行為規則：**

| 事件 | UI 反應 |
|------|---------|
| 按 Safe Eject | preflight → usage=0 直接 detach，否則顯示 drain 進度 |
| Drain > 5 分鐘 | 進度條旁顯示「Taking long. Force eject?」|
| Force Eject | 紅色警告 dialog 確認後執行 |
| Cancel | 停止 drain，恢復為 active pagefile |
| Drain 完成 | 自動 detach → 綠燈 READY |
| Advanced 修改值 | 即時驗證（紅字警告違規），Apply 後存檔 |

---

## Section 4: 設定儲存

### 主機端 — Global Policy

```
%APPDATA%/vram_booster/safety_policy.json
```
```json
{
  "version": 1,
  "global_defaults": {
    "device_reserved_gb": "auto",
    "pagefile_min_gb": "auto",
    "pagefile_max_gb": "auto",
    "system_ram_reserve_pct": 20
  }
}
```

`"auto"` = 智慧預設。填數字 = 使用者覆寫。

### 裝置端 — Device Override

擴展現有 `.vram_boost_config.json`：
```json
{
  "card_fingerprint": "128.0GB|SDCARD_A2",
  "rand_write_mbs": 85.3,
  "swap_size_bytes": 8589934592,
  "safety_override": {
    "device_reserved_gb": 6,
    "pagefile_max_gb": 20
  }
}
```

只有使用者明確改過的欄位才出現在 `safety_override`。

### 合併順序

```
smart_default(capacity, speed) ← global_defaults ← device.safety_override → PolicyLimits
```

### 向後相容

- 舊版 config 無 `safety_override` → 當空 dict → 完全使用 smart default + global
- 不改動現有欄位結構

---

## Section 5: activate() 整合

```python
# real_boost.py activate() 加入：
policy = SafetyPolicy.load_merged_policy(global_path, device_path,
                                          capacity_gb, speed_mbs)
requested_gb = min(capacity_gb * (use_percent / 100), policy.pagefile_max_gb)
requested_gb = max(requested_gb, policy.pagefile_min_gb)

ok, reason = SafetyPolicy.validate_activation(capacity_gb, requested_gb, policy)
if not ok:
    return {"success": False, "error": reason}
```

---

## 模組呼叫關係

```
host_ui.py
  ├──→ SafetyPolicy (safety_policy.py)
  │      ├── compute_smart_defaults()
  │      ├── load_merged_policy()
  │      ├── save_global_policy()
  │      ├── save_device_override()
  │      └── validate_activation()
  ├──→ SafeRemovalManager (safe_removal.py)
  │      ├── preflight_check()
  │      ├── start_drain()
  │      ├── cancel_drain()
  │      ├── force_eject()
  │      └── state()
  └──→ RealBoostEngine (real_boost.py)
         └── activate() 內部呼叫 validate_activation()
```

---

## 驗證時機總覽

| 時機 | 動作 |
|------|------|
| 裝置插入 / activate | 載入合併 policy → 計算 pagefile 大小 → validate_activation() |
| UI Advanced 面板修改 | 即時計算 limits → 紅字警告違規 → Apply 存檔 |
| Safe Eject 按下 | 載入 system_ram_reserve → preflight 檢查 RAM 是否足夠 |
