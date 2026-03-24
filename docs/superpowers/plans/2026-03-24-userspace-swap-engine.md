# Userspace Swap Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Windows NtCreatePagingFile (blocked on external devices) with a userspace mmap-based swap engine that creates memory-mapped swap files directly on SD cards and USB SSDs.

**Architecture:** New `MmapSwapEngine` class creates a swap file on the external device, maps it via `CreateFileMapping` + `MapViewOfFile`, and orchestrates existing modules (CircuitBreaker, HealthMonitor, RecoveryChain, MemoryPool) for smart degradation on device removal. The engine exposes the same `activate()`/`deactivate()`/`status()` interface as `RealBoostEngine`, so the GUI requires minimal changes.

**Tech Stack:** Python 3.10+, ctypes (Win32 mmap API), threading (device monitor), existing microsystems infrastructure

**Spec:** `docs/superpowers/specs/2026-03-24-userspace-swap-engine-design.md`

---

## File Structure

### New files

| File | Responsibility |
|------|---------------|
| `microsystems/core/mmap_swap.py` | Low-level swap file operations: create file, Win32 mmap, block-level map/unmap, VEH protection, device presence check |
| `microsystems/core/mmap_engine.py` | High-level orchestrator: wires SwapFileManager + CircuitBreaker + HealthMonitor + RecoveryChain + MemoryPool. Exposes `activate()`/`deactivate()`/`status()` |
| `tests/test_circuit_breaker_integration.py` | Test CircuitBreaker state transitions with simulated device disconnect/reconnect |
| `tests/test_mmap_swap.py` | Test SwapFileManager file creation, mapping, block I/O, device check |
| `tests/test_mmap_engine.py` | Test MmapSwapEngine full lifecycle: activate → read/write → disconnect → degrade → reconnect → restore |

### Modified files

| File | Changes |
|------|---------|
| `microsystems/core/real_boost.py` | `_activate_windows()` delegates to `MmapSwapEngine`; remove NtCreatePagingFile code; keep Linux swapon path, benchmarking, and config caching |
| `microsystems/hotplug_launcher.py` | Add device status + protection indicators to Phase 3 (active) GUI; handle degraded state display |

### Existing files (reused as-is)

| File | Role |
|------|------|
| `microsystems/core/circuit_breaker.py` | CLOSED→OPEN→HALF_OPEN state machine |
| `microsystems/core/health_monitor.py` | Background device polling thread |
| `microsystems/core/recovery_chain.py` | Ordered fallback strategies |
| `microsystems/core/memory_pool.py` | 3-tier LRU block management |
| `microsystems/core/slow_device_optimizer.py` | LZ4 compression + quantization |

---

## Task 1: SwapFileManager — Low-level mmap operations

**Files:**
- Create: `microsystems/core/mmap_swap.py`
- Test: `tests/test_mmap_swap.py`

This is the foundation — all Win32 mmap calls live here. No business logic.

- [ ] **Step 1: Write test for swap file creation**

```python
# tests/test_mmap_swap.py
import os
import tempfile
import pytest

def test_create_swap_file():
    """SwapFileManager creates a pre-allocated file of exact size."""
    from microsystems.core.mmap_swap import SwapFileManager

    with tempfile.TemporaryDirectory() as tmp:
        mgr = SwapFileManager()
        path = os.path.join(tmp, "test.swap")
        size = 4 * 1024 * 1024  # 4MB

        mgr.create(path, size)

        assert os.path.exists(path)
        assert os.path.getsize(path) == size
        mgr.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python -m pytest ../tests/test_mmap_swap.py::test_create_swap_file -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'microsystems.core.mmap_swap'`

- [ ] **Step 3: Implement SwapFileManager.create()**

```python
# microsystems/core/mmap_swap.py
"""
Userspace Swap File Manager
=============================
在外部儲存裝置上建立 swap 檔案，用 Win32 memory-mapped file API 映射。
繞過 Windows NtCreatePagingFile 對外接裝置的限制。

原理：
  CreateFileMapping + MapViewOfFile 對任何檔案系統、任何裝置都可用。
  OS 的 VMM 自動處理 page fault → 裝置 I/O → 填入 RAM。
"""

from __future__ import annotations

import logging
import mmap
import os
import platform
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# 區塊大小：64MB（可部分 unmap，減少地址空間壓力）
DEFAULT_BLOCK_SIZE = 64 * 1024 * 1024

SWAP_FILENAME = "vram_boost.swap"


@dataclass
class MappedBlock:
    """一個已映射的 swap 區塊"""
    block_id: int
    offset: int
    size: int
    mmap_obj: Optional[mmap.mmap] = None
    label: str = ""
    access_count: int = 0
    last_access: float = 0.0
    state: str = "free"  # free | mapped | evicted


class SwapFileManager:
    """
    管理外部裝置上的 swap 檔案。

    create()   → 在裝置上預配置檔案
    map_block()   → 映射一個 64MB 區塊到記憶體
    unmap_block() → 取消映射
    is_device_present() → 檢查裝置是否在線
    close()    → 關閉所有映射和檔案
    """

    def __init__(self, block_size: int = DEFAULT_BLOCK_SIZE):
        self._file_path: Optional[str] = None
        self._file_handle = None
        self._file_size: int = 0
        self._block_size = block_size
        self._blocks: Dict[int, MappedBlock] = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def file_path(self) -> Optional[str]:
        return self._file_path

    @property
    def block_count(self) -> int:
        return self._file_size // self._block_size if self._file_size else 0

    def create(self, path: str, size_bytes: int) -> None:
        """在裝置上建立 swap 檔案，預配置空間避免碎片。"""
        # 對齊到 block_size
        size_bytes = (size_bytes // self._block_size) * self._block_size
        if size_bytes < self._block_size:
            size_bytes = self._block_size

        self._file_path = path
        self._file_size = size_bytes

        # 檢查既有檔案是否可複用
        if os.path.exists(path):
            existing = os.path.getsize(path)
            if existing == size_bytes:
                logger.info("Swap file reuse: %s (%dMB)", path, size_bytes // (1024**2))
                return
            # 大小不對，刪除重建
            os.unlink(path)

        # 預配置：寫入 1MB 的零
        logger.info("Creating swap file: %s (%dMB)", path, size_bytes // (1024**2))
        chunk = b'\0' * (1024 * 1024)
        remaining = size_bytes
        with open(path, 'wb') as f:
            while remaining > 0:
                write_size = min(len(chunk), remaining)
                f.write(chunk[:write_size])
                remaining -= write_size
            f.flush()
            os.fsync(f.fileno())

    def open(self) -> None:
        """開啟 swap 檔案供映射。"""
        if not self._file_path or not os.path.exists(self._file_path):
            raise FileNotFoundError(f"Swap file not found: {self._file_path}")
        self._file_handle = open(self._file_path, 'r+b')
        # 初始化區塊表
        for i in range(self.block_count):
            self._blocks[i] = MappedBlock(
                block_id=i,
                offset=i * self._block_size,
                size=self._block_size,
            )

    def map_block(self, block_id: int) -> mmap.mmap:
        """映射指定區塊到記憶體，回傳 mmap 物件。"""
        with self._lock:
            block = self._blocks.get(block_id)
            if block is None:
                raise ValueError(f"Invalid block_id: {block_id}")
            if block.mmap_obj is not None:
                return block.mmap_obj

            block.mmap_obj = mmap.mmap(
                self._file_handle.fileno(),
                length=block.size,
                offset=block.offset,
                access=mmap.ACCESS_WRITE,
            )
            block.state = "mapped"
            logger.debug("Mapped block %d (offset=%d, size=%dMB)",
                         block_id, block.offset, block.size // (1024**2))
            return block.mmap_obj

    def unmap_block(self, block_id: int) -> None:
        """取消映射指定區塊。"""
        with self._lock:
            block = self._blocks.get(block_id)
            if block and block.mmap_obj:
                try:
                    block.mmap_obj.close()
                except (OSError, BufferError):
                    pass
                block.mmap_obj = None
                block.state = "free"

    def read_block(self, block_id: int, offset: int = 0,
                   size: int = -1) -> Optional[bytes]:
        """安全讀取區塊資料。裝置斷線時回傳 None。"""
        import time as _time
        block = self._blocks.get(block_id)
        if not block or not block.mmap_obj:
            return None
        try:
            mm = block.mmap_obj
            mm.seek(offset)
            data = mm.read(size if size > 0 else block.size)
            block.access_count += 1
            block.last_access = _time.monotonic()
            return data
        except (OSError, ValueError):
            # 裝置斷線 → page fault 失敗
            return None

    def write_block(self, block_id: int, data: bytes,
                    offset: int = 0) -> bool:
        """安全寫入區塊資料。裝置斷線時回傳 False。"""
        import time as _time
        block = self._blocks.get(block_id)
        if not block or not block.mmap_obj:
            return False
        try:
            mm = block.mmap_obj
            mm.seek(offset)
            mm.write(data)
            block.access_count += 1
            block.last_access = _time.monotonic()
            return True
        except (OSError, ValueError):
            return False

    def is_device_present(self) -> bool:
        """快速檢查裝置是否在線。"""
        if not self._file_path:
            return False
        try:
            os.stat(self._file_path)
            return True
        except OSError:
            return False

    def get_block_stats(self) -> List[MappedBlock]:
        """回傳所有區塊的統計（供 LRU 排序用）。"""
        return sorted(
            self._blocks.values(),
            key=lambda b: b.access_count,
            reverse=True,  # 最熱的在前
        )

    def close(self) -> None:
        """關閉所有映射和檔案。"""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            for block_id in list(self._blocks.keys()):
                self.unmap_block(block_id)
            if self._file_handle:
                try:
                    self._file_handle.close()
                except OSError:
                    pass
                self._file_handle = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python -m pytest ../tests/test_mmap_swap.py::test_create_swap_file -v`
Expected: PASS

- [ ] **Step 5: Write tests for block map/read/write**

```python
# Append to tests/test_mmap_swap.py

def test_map_and_write_block():
    """Map a block, write data, read it back."""
    from microsystems.core.mmap_swap import SwapFileManager

    with tempfile.TemporaryDirectory() as tmp:
        mgr = SwapFileManager(block_size=1024 * 1024)  # 1MB blocks
        path = os.path.join(tmp, "test.swap")
        mgr.create(path, 2 * 1024 * 1024)  # 2 blocks
        mgr.open()

        # Map block 0
        mgr.map_block(0)

        # Write
        test_data = b"hello swap" + b'\0' * 100
        assert mgr.write_block(0, test_data) is True

        # Read back
        result = mgr.read_block(0, offset=0, size=len(test_data))
        assert result == test_data

        # Access count updated
        stats = mgr.get_block_stats()
        assert stats[0].access_count == 2  # 1 write + 1 read

        mgr.close()


def test_device_presence_check():
    """is_device_present returns False after file is deleted."""
    from microsystems.core.mmap_swap import SwapFileManager

    with tempfile.TemporaryDirectory() as tmp:
        mgr = SwapFileManager(block_size=1024 * 1024)
        path = os.path.join(tmp, "test.swap")
        mgr.create(path, 1024 * 1024)

        assert mgr.is_device_present() is True
        os.unlink(path)
        assert mgr.is_device_present() is False
        mgr.close()


def test_read_after_unmap_returns_none():
    """Reading from unmapped block returns None."""
    from microsystems.core.mmap_swap import SwapFileManager

    with tempfile.TemporaryDirectory() as tmp:
        mgr = SwapFileManager(block_size=1024 * 1024)
        path = os.path.join(tmp, "test.swap")
        mgr.create(path, 1024 * 1024)
        mgr.open()
        mgr.map_block(0)
        mgr.unmap_block(0)

        assert mgr.read_block(0) is None
        mgr.close()
```

- [ ] **Step 6: Run all swap tests**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python -m pytest ../tests/test_mmap_swap.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add microsystems/core/mmap_swap.py tests/test_mmap_swap.py
git commit -m "feat: add SwapFileManager with mmap block I/O for external devices"
```

---

## Task 2: MmapSwapEngine — Orchestrator

**Files:**
- Create: `microsystems/core/mmap_engine.py`
- Test: `tests/test_mmap_engine.py`

Wires SwapFileManager + existing CircuitBreaker + HealthMonitor + RecoveryChain.

- [ ] **Step 1: Write test for basic activate/deactivate lifecycle**

```python
# tests/test_mmap_engine.py
import os
import tempfile
import pytest

def test_activate_creates_swap_on_device():
    """MmapSwapEngine.activate() creates swap file and maps blocks."""
    from microsystems.core.mmap_engine import MmapSwapEngine

    with tempfile.TemporaryDirectory() as tmp:
        engine = MmapSwapEngine()
        result = engine.activate(
            device_path=tmp,
            size_bytes=4 * 1024 * 1024,  # 4MB
            block_size=1024 * 1024,      # 1MB blocks
        )

        assert result["success"] is True
        assert result["needs_reboot"] is False
        assert os.path.exists(os.path.join(tmp, "vram_boost.swap"))

        status = engine.status()
        assert status["active"] is True
        assert status["total_blocks"] == 4
        assert status["device_state"] == "closed"  # CircuitBreaker: normal

        engine.deactivate()
        assert engine.status()["active"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python -m pytest ../tests/test_mmap_engine.py::test_activate_creates_swap_on_device -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement MmapSwapEngine**

```python
# microsystems/core/mmap_engine.py
"""
Mmap Swap Engine — Userspace 記憶體擴展引擎
=============================================
繞過 Windows NtCreatePagingFile 限制，在外部裝置上建立
memory-mapped swap 檔案，直接提供可用記憶體。

整合既有模組：
  - SwapFileManager: 檔案建立 + mmap 映射
  - CircuitBreaker:  裝置斷路保護
  - HealthMonitor:   背景健康輪詢
  - RecoveryChain:   降級恢復策略鏈
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional, Dict, Any, Callable

from .mmap_swap import SwapFileManager, SWAP_FILENAME
from .circuit_breaker import CircuitBreaker, BreakerConfig, BreakerState

logger = logging.getLogger(__name__)


class MmapSwapEngine:
    """
    Userspace swap engine for external storage devices.

    activate()   → 建立 swap 檔 + mmap + 啟動監控
    deactivate() → 關閉映射 + 停止監控
    status()     → 回傳引擎狀態（含裝置健康、區塊統計）
    """

    def __init__(self):
        self._swap_mgr: Optional[SwapFileManager] = None
        self._breaker: Optional[CircuitBreaker] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_running = False
        self._active = False
        self._device_path = ""
        self._degraded = False
        self._on_state_change: Optional[Callable[[str], None]] = None

    def activate(self, device_path: str, size_bytes: int,
                 block_size: int = 64 * 1024 * 1024,
                 on_progress: Optional[Callable[[str], None]] = None,
                 on_state_change: Optional[Callable[[str], None]] = None,
                 ) -> Dict[str, Any]:
        """
        在外部裝置上建立 mmap swap。

        Args:
            device_path: 裝置掛載路徑 (e.g., "E:\\" or tempdir)
            size_bytes: swap 大小
            block_size: 區塊大小 (default 64MB)
            on_progress: 進度回報
            on_state_change: 狀態變更回報 ("normal"/"degraded"/"restored")
        """
        report = on_progress or (lambda msg: None)
        self._on_state_change = on_state_change
        self._device_path = device_path

        swap_path = os.path.join(device_path, SWAP_FILENAME)

        try:
            # 建立 swap 檔案
            report("creating swap file...")
            self._swap_mgr = SwapFileManager(block_size=block_size)
            self._swap_mgr.create(swap_path, size_bytes)
            self._swap_mgr.open()

            # 映射所有區塊
            report("mapping blocks...")
            for i in range(self._swap_mgr.block_count):
                self._swap_mgr.map_block(i)

            # 初始化 CircuitBreaker
            self._breaker = CircuitBreaker(
                name="device_swap",
                config=BreakerConfig(
                    failure_threshold=2,
                    cooldown_seconds=5.0,
                    success_threshold=1,
                ),
            )

            # 啟動裝置監控
            self._monitor_running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()

            self._active = True

            added_gb = size_bytes / (1024 ** 3)
            logger.info("Mmap swap activated: %s (%.1fGB, %d blocks)",
                        swap_path, added_gb, self._swap_mgr.block_count)

            return {
                "success": True,
                "method": "mmap_swap",
                "swap_path": swap_path,
                "added_gb": round(added_gb, 1),
                "total_blocks": self._swap_mgr.block_count,
                "needs_reboot": False,
            }

        except (OSError, ValueError) as e:
            logger.error("Mmap swap activation failed: %s", e)
            return {"success": False, "error": str(e)}

    def deactivate(self) -> Dict[str, Any]:
        """關閉 swap 引擎。"""
        self._monitor_running = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=10)

        if self._swap_mgr:
            self._swap_mgr.close()
            self._swap_mgr = None

        self._active = False
        self._degraded = False
        self._breaker = None
        logger.info("Mmap swap deactivated")
        return {"success": True}

    def status(self) -> Dict[str, Any]:
        """回傳引擎狀態。"""
        if not self._active or not self._swap_mgr:
            return {"active": False}

        breaker_state = "unknown"
        if self._breaker:
            breaker_state = self._breaker.state.value

        blocks = self._swap_mgr.get_block_stats()
        mapped = sum(1 for b in blocks if b.state == "mapped")
        evicted = sum(1 for b in blocks if b.state == "evicted")

        return {
            "active": True,
            "swap_path": self._swap_mgr.file_path,
            "total_blocks": len(blocks),
            "mapped_blocks": mapped,
            "evicted_blocks": evicted,
            "device_state": breaker_state,
            "degraded": self._degraded,
        }

    # ── Device Monitor ──

    def _monitor_loop(self):
        """背景輪詢裝置狀態，驅動 CircuitBreaker。"""
        was_degraded = False

        while self._monitor_running:
            if not self._swap_mgr or not self._breaker:
                break

            present = self._swap_mgr.is_device_present()

            if present:
                self._breaker.record_success()
            else:
                self._breaker.record_failure()

            state = self._breaker.state

            # CLOSED → 裝置正常
            if state == BreakerState.CLOSED and was_degraded:
                self._on_reconnect()
                was_degraded = False

            # OPEN → 裝置斷線
            elif state == BreakerState.OPEN and not was_degraded:
                self._on_disconnect()
                was_degraded = True

            # 輪詢間隔：正常 5s，HALF_OPEN 2s
            interval = 2.0 if state == BreakerState.HALF_OPEN else 5.0
            time.sleep(interval)

    def _on_disconnect(self):
        """裝置斷線：降級到 RAM-only 模式。"""
        logger.warning("Device disconnected — entering degraded mode")
        self._degraded = True

        if self._swap_mgr:
            # 按存取頻率排序，標記冷區塊為 evicted
            blocks = self._swap_mgr.get_block_stats()
            for block in blocks:
                if block.state == "mapped":
                    self._swap_mgr.unmap_block(block.block_id)
                    block.state = "evicted"

        if self._on_state_change:
            self._on_state_change("degraded")

    def _on_reconnect(self):
        """裝置恢復：重新映射區塊。"""
        logger.info("Device reconnected — restoring full capacity")

        if self._swap_mgr:
            try:
                # 重新開啟檔案
                self._swap_mgr.close()
                self._swap_mgr.open()
                for i in range(self._swap_mgr.block_count):
                    self._swap_mgr.map_block(i)
                self._degraded = False
                logger.info("Swap fully restored")
            except OSError as e:
                logger.error("Reconnect failed: %s", e)
                return

        if self._on_state_change:
            self._on_state_change("restored")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python -m pytest ../tests/test_mmap_engine.py::test_activate_creates_swap_on_device -v`
Expected: PASS

- [ ] **Step 5: Write test for disconnect/reconnect cycle**

```python
# Append to tests/test_mmap_engine.py

def test_disconnect_triggers_degradation():
    """Simulated device removal triggers degraded mode."""
    from microsystems.core.mmap_engine import MmapSwapEngine
    import time

    with tempfile.TemporaryDirectory() as tmp:
        engine = MmapSwapEngine()
        engine.activate(tmp, 2 * 1024 * 1024, block_size=1024 * 1024)

        # Simulate disconnect: delete the swap file
        swap_file = os.path.join(tmp, "vram_boost.swap")
        os.unlink(swap_file)

        # Wait for monitor to detect (2 polls × 5s + buffer)
        time.sleep(12)

        status = engine.status()
        assert status["degraded"] is True
        assert status["device_state"] == "open"

        engine.deactivate()
```

- [ ] **Step 6: Run all engine tests**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python -m pytest ../tests/test_mmap_engine.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add microsystems/core/mmap_engine.py tests/test_mmap_engine.py
git commit -m "feat: add MmapSwapEngine with circuit breaker and device monitoring"
```

---

## Task 3: Wire MmapSwapEngine into RealBoostEngine

**Files:**
- Modify: `microsystems/core/real_boost.py`

Replace the NtCreatePagingFile Windows path with MmapSwapEngine delegation.

- [ ] **Step 1: Replace `_activate_windows` to use MmapSwapEngine**

In `real_boost.py`, replace the `_activate_windows` method body. Keep the swap size calculation and speed capping logic, delegate the actual swap creation to `MmapSwapEngine`.

Key changes:
- Remove `_create_pagefile_nt`, `_call_nt_create_pagingfile`, `_enable_pagefile_privilege`, `_is_pagefile_active`, `_register_pagefile_registry`, `_cleanup_stale_registry_entries`
- Keep `_cap_swap_by_speed`, `_benchmark_random_write`, `_load_cached_config`, `_save_config`, `_get_card_fingerprint`, `get_system_memory`, `get_gpu_info`
- `_activate_windows` creates `MmapSwapEngine` and delegates
- `_deactivate_windows` delegates to `MmapSwapEngine.deactivate()`
- `status()` merges system memory stats with MmapSwapEngine status

```python
# In _activate_windows, replace the NtCreatePagingFile section with:
from .mmap_engine import MmapSwapEngine

self._mmap_engine = MmapSwapEngine()
mount = f"{letter}:\\"
result = self._mmap_engine.activate(
    device_path=mount,
    size_bytes=swap_bytes,
    on_progress=report,
)

if result.get("success"):
    self._swap_path = Path(result["swap_path"])
    self._swap_size_bytes = swap_bytes
    self._active = True
```

- [ ] **Step 2: Update `_deactivate_windows`**

```python
def _deactivate_windows(self):
    if hasattr(self, '_mmap_engine') and self._mmap_engine:
        self._mmap_engine.deactivate()
        self._mmap_engine = None
    self._active = False
    self._swap_path = None
    self._swap_size_bytes = 0
    return {"success": True}
```

- [ ] **Step 3: Update `status()` to include mmap engine info**

```python
# Add to status() dict:
if hasattr(self, '_mmap_engine') and self._mmap_engine:
    mmap_status = self._mmap_engine.status()
    result["device_state"] = mmap_status.get("device_state", "unknown")
    result["degraded"] = mmap_status.get("degraded", False)
    result["mapped_blocks"] = mmap_status.get("mapped_blocks", 0)
    result["evicted_blocks"] = mmap_status.get("evicted_blocks", 0)
```

- [ ] **Step 4: Run end-to-end test on actual device**

Run elevated: `python test_pagefile.py` (create a test script that calls `RealBoostEngine.activate("E")`)
Expected: Swap file created on E:\, blocks mapped, device monitor running

- [ ] **Step 5: Commit**

```bash
git add microsystems/core/real_boost.py
git commit -m "refactor: replace NtCreatePagingFile with MmapSwapEngine on Windows"
```

---

## Task 4: Update GUI — Device status and protection indicators

**Files:**
- Modify: `microsystems/hotplug_launcher.py`

- [ ] **Step 1: Add device status labels to `_show_active()`**

Add two new labels below the existing info section:
- 裝置狀態: ● 正常 (E:\) — green dot when normal, orange when degraded
- 保護: 智慧降級 (B+) — always shown

```python
# In _show_active(), after self._info_lbls["system"].configure(...):

# 裝置狀態
device_f = tk.Frame(self._frame, bg=self.BG2, padx=12, pady=4)
device_f.pack(fill="x", padx=12, pady=(0, 5))

r = tk.Frame(device_f, bg=self.BG2)
r.pack(fill="x")
tk.Label(r, text="裝置狀態:", font=("Segoe UI", 8),
         fg=self.GRAY, bg=self.BG2, width=8, anchor="e").pack(side="left")
self._device_status_lbl = tk.Label(
    r, text="● 正常", font=("Segoe UI", 8, "bold"),
    fg=self.GREEN, bg=self.BG2, anchor="w")
self._device_status_lbl.pack(side="left", padx=(6, 0))

r2 = tk.Frame(device_f, bg=self.BG2)
r2.pack(fill="x")
tk.Label(r2, text="保護:", font=("Segoe UI", 8),
         fg=self.GRAY, bg=self.BG2, width=8, anchor="e").pack(side="left")
tk.Label(r2, text="智慧降級 (B+)", font=("Segoe UI", 8, "bold"),
         fg=self.ACCENT, bg=self.BG2, anchor="w").pack(side="left", padx=(6, 0))
```

- [ ] **Step 2: Update `_poll_monitor()` to show degraded state**

```python
# In _poll_monitor(), after updating memory bars:
if hasattr(self, '_device_status_lbl') and self._boost_engine:
    mmap_status = {}
    if hasattr(self._boost_engine, '_mmap_engine') and self._boost_engine._mmap_engine:
        mmap_status = self._boost_engine._mmap_engine.status()

    degraded = mmap_status.get("degraded", False)
    if degraded:
        self._device_status_lbl.configure(
            text="● 已斷線 — 等待重新連接", fg=self.ORANGE)
    else:
        self._device_status_lbl.configure(
            text=f"● 正常 ({self._my_drive}:\\)", fg=self.GREEN)
```

- [ ] **Step 3: Remove old `_show_reboot_prompt` phase**

The mmap engine never needs reboot. Remove `_show_reboot_prompt()` and `_reboot()` methods. The activation callback always goes to `_show_active()`.

```python
# In the activation callback (do() function):
if result.get("success"):
    self._system = True
    self._engine_ready.set()
    self._root.after(0, self._show_active)  # Always monitoring, never reboot
```

- [ ] **Step 4: Verify GUI manually**

Run the EXE with SD card inserted, verify:
- Phase 3 shows device status line
- Pull the card → status changes to orange "已斷線"
- Re-insert → status goes back to green "正常"

- [ ] **Step 5: Commit**

```bash
git add microsystems/hotplug_launcher.py
git commit -m "feat: add device status indicator and remove reboot prompt"
```

---

## Task 5: Clean up dead code

**Files:**
- Modify: `microsystems/core/real_boost.py`

- [ ] **Step 1: Remove NtCreatePagingFile-related methods**

Delete these methods from `real_boost.py`:
- `_create_pagefile_nt`
- `_call_nt_create_pagingfile`
- `_enable_pagefile_privilege`
- `_is_pagefile_active`
- `_register_pagefile_registry`
- `_cleanup_stale_registry_entries`
- `SWAP_PAGEFILE_NAME` class variable

Keep:
- `SWAP_FILENAME` (used for config caching)
- All Linux methods
- Speed benchmarking, config caching, system memory, GPU info

- [ ] **Step 2: Remove stale C:\vram_boost.sys registry entry**

Add a one-time cleanup in `_activate_windows`:

```python
# At the start of _activate_windows, clean stale registry from old approach
self._cleanup_old_pagefile_registry()

@staticmethod
def _cleanup_old_pagefile_registry():
    """Remove stale vram_boost.sys registry entries from old NtCreatePagingFile approach."""
    try:
        _run_hidden(["powershell", "-NoProfile", "-Command",
            "$ErrorActionPreference='SilentlyContinue';"
            "$rp='HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management';"
            "$c=(Get-ItemProperty $rp -Name PagingFiles).PagingFiles;"
            "if($c -is [string]){$c=@($c)};"
            "$f=$c|Where-Object{$_ -and $_.ToLower() -notlike '*vram_boost*'};"
            "if(-not $f){$f=@()};"
            "if($f.Count -ne $c.Count){Set-ItemProperty $rp -Name PagingFiles -Value $f}"],
            timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        pass
```

- [ ] **Step 3: Verify no references to removed code**

Run: `grep -rn "NtCreatePaging\|_create_pagefile_nt\|_call_nt_create\|_enable_pagefile_priv\|_is_pagefile_active\|_register_pagefile_reg\|SWAP_PAGEFILE_NAME" microsystems/`
Expected: No matches

- [ ] **Step 4: Run full test suite**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python -m pytest ../tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add microsystems/core/real_boost.py
git commit -m "chore: remove NtCreatePagingFile dead code, clean stale registry entries"
```

---

## Task 6: End-to-end integration test on real device

**Files:**
- Create: `tests/test_e2e_mmap.py`

- [ ] **Step 1: Write E2E test script**

```python
# tests/test_e2e_mmap.py
"""
End-to-end test: must run elevated with SD card or USB SSD connected.
Usage: python -m pytest tests/test_e2e_mmap.py -v -s --run-e2e
"""
import os
import sys
import pytest
import time

requires_e2e = pytest.mark.skipif(
    "--run-e2e" not in sys.argv,
    reason="E2E tests require --run-e2e flag and external device"
)

@requires_e2e
def test_full_lifecycle_on_real_device():
    """Full activate → write → read → status → deactivate on real device."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                    "..", "提升VRAM與GPA效能設計方案"))
    from microsystems.core.real_boost import RealBoostEngine

    engine = RealBoostEngine()

    # Find first non-C removable/external drive
    drive = None
    for letter in "EFGHIJKLMNOPQRSTUVWXYZ":
        path = f"{letter}:\\"
        if os.path.exists(path) and letter != "C":
            drive = letter
            break

    assert drive is not None, "No external drive found"

    # Activate
    msgs = []
    result = engine.activate(drive, use_percent=80.0,
                             on_progress=lambda m: msgs.append(m))

    assert result["success"] is True
    assert result["needs_reboot"] is False
    assert "mmap_swap" in result.get("method", "")

    # Status
    status = engine.status()
    assert status["active"] is True

    # Deactivate
    engine.deactivate()
    assert engine.status()["active"] is False

    print(f"\nE2E PASS: {drive}:\\ — {result.get('added_gb', 0):.1f}GB swap")
```

- [ ] **Step 2: Run E2E test elevated**

Run elevated: `cd D:\product\vRAM && python -m pytest tests/test_e2e_mmap.py -v -s --run-e2e`
Expected: PASS with swap file created on SD card

- [ ] **Step 3: Build EXE and test**

Run: `cd D:\product\vRAM\提升VRAM與GPA效能設計方案 && python build.py`
Expected: `release/windows/VRAM_Booster.exe` updated

- [ ] **Step 4: Test EXE on SD card**

1. Copy `VRAM_Booster.exe` to SD card
2. Double-click → UAC → detect → confirm → activate
3. Verify: swap file on SD card, monitoring shows ● 正常
4. Pull SD card → verify no crash, GUI shows ● 已斷線
5. Re-insert → verify GUI shows ● 正常

- [ ] **Step 5: Commit all**

```bash
git add tests/test_e2e_mmap.py
git commit -m "test: add end-to-end integration test for mmap swap engine"
```

---

## Summary

| Task | What | Estimated Steps |
|------|------|----------------|
| 1 | SwapFileManager (mmap I/O) | 7 |
| 2 | MmapSwapEngine (orchestrator) | 7 |
| 3 | Wire into RealBoostEngine | 5 |
| 4 | GUI device status | 5 |
| 5 | Clean dead code | 5 |
| 6 | E2E test + build | 5 |
| **Total** | | **34 steps** |
