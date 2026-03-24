# Userspace Swap Engine — B+ Strategy Design Spec

**Date**: 2026-03-24
**Status**: Draft
**Scope**: AI workload memory expansion via mmap swap on external storage

---

## 1. Problem Statement

Windows kernel blocks `NtCreatePagingFile` on all external devices (USB SSD, SD card, VHD). Tested exhaustively:

| Device | Filesystem | DriveType | Result |
|--------|-----------|-----------|--------|
| Internal SSD (C:) | NTFS | Fixed | SUCCESS |
| USB SSD (F:) | NTFS | Fixed | NOT_SUPPORTED |
| SD card (E:) | exFAT | Removable | INVALID_DEVICE_REQUEST |
| VHD on any drive | NTFS | Fixed | NOT_SUPPORTED |

**Root cause**: Kernel walks the device stack; any non-internal controller (USB, virtual) is rejected. No userspace workaround exists for the pagefile mechanism.

**Design intent**: Use external hardware (SD card / USB SSD) to expand available memory for AI model loading, without consuming computer resources.

---

## 2. Solution: Userspace Mmap Swap

Bypass Windows pagefile entirely. Create a swap file on the external device and memory-map it via `CreateFileMapping` + `MapViewOfFile`. The OS handles demand-paging to/from the file transparently.

**Target**: AI workloads (Ollama / llama.cpp) via CUDA interception. Not system-wide.

### 2.1 Why This Works

- `CreateFileMapping` with a file handle works on **any** filesystem, **any** device
- No kernel restriction — it's just a file I/O operation
- The mapped pages are backed by the external device's storage
- Windows VMM handles page faults → reads from file → fills RAM → serves to GPU
- When RAM is full, VMM evicts mapped pages (they can be re-read from file, no pagefile write needed)

### 2.2 What It Does NOT Do

- Does NOT increase system commit limit (only benefits our process)
- Does NOT help non-AI applications
- Does NOT replace Windows pagefile for general use

---

## 3. Architecture

```
┌────────────────────────────────────────────────────────┐
│                   BoosterApp (GUI)                      │
│  detect → confirm → activate → monitor                  │
└────────────────┬───────────────────────────────────────┘
                 │
┌────────────────▼───────────────────────────────────────┐
│              MmapSwapEngine (new)                       │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  SwapFile    │  │ CircuitBreaker│  │ MemoryBudget  │  │
│  │  Manager     │  │ (device FSM) │  │ (tier alloc)  │  │
│  └──────┬──────┘  └──────┬───────┘  └───────┬───────┘  │
│         │                │                   │          │
│  ┌──────▼──────────────────────────────────▼────────┐  │
│  │              BlockAllocator                       │  │
│  │  mmap regions ←→ block table ←→ LRU tracker      │  │
│  └──────┬───────────────────────────────────────────┘  │
│         │                                               │
│  ┌──────▼───────────────────────────────────────────┐  │
│  │           FallbackChain                           │  │
│  │  SD card read → RAM cache → compress(INT8) → drop │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

---

## 4. Components

### 4.1 SwapFile Manager

Manages the physical file on the external device.

```python
class SwapFileManager:
    """Create, map, and manage swap file on external storage."""

    def create(self, device_path: str, size_bytes: int) -> None:
        """Pre-allocate swap file on device."""
        # Write zeros in 1MB chunks (ensures space is allocated, not sparse)
        # On exFAT/NTFS both work

    def map(self, offset: int, length: int) -> memoryview:
        """Map a region of the swap file into process memory."""
        # CreateFileMapping + MapViewOfFile
        # Returns a memoryview that reads/writes directly to device

    def unmap(self, view: memoryview) -> None:
        """Unmap a previously mapped region."""

    def is_device_present(self) -> bool:
        """Check if the underlying device is still accessible."""
        # Quick stat() on swap file path

    def close(self) -> None:
        """Unmap all regions, close file handles."""
```

**Key decisions**:
- Pre-allocate full file size to avoid fragmentation
- Map in **fixed-size blocks** (default 64MB) rather than one giant mapping
  - Allows partial unmap on device removal
  - Reduces address space pressure on 32-bit edge cases
- File path: `{device}:\vram_boost.swap`

### 4.2 Circuit Breaker (from OSS120BCLI recovery.py)

Device state machine for graceful transitions.

```
     ┌──────────────────────────────────┐
     │                                  │
     ▼                                  │
  CLOSED ──(device missing)──► OPEN ────┘
  (normal)     2 failed         (degraded)
               polls            │
                                │ (cooldown 5s)
                                ▼
                            HALF_OPEN
                            (probe)
                           /        \
                     success         fail
                        │              │
                        ▼              ▼
                     CLOSED          OPEN
                     (restored)     (stay degraded)
```

**States**:

| State | Behavior |
|-------|----------|
| CLOSED | Normal operation. All swap I/O goes to device. Poll device every 5s. |
| OPEN | Device disconnected. All swap reads fall back to RAM/compress. New writes go to RAM only. |
| HALF_OPEN | Try one probe read from device. If success → CLOSED. If fail → OPEN. |

**Transitions**:
- CLOSED → OPEN: 2 consecutive failed device polls (not 1, to avoid false triggers)
- OPEN → HALF_OPEN: After 5s cooldown
- HALF_OPEN → CLOSED: Probe read succeeds, re-mmap swap file
- HALF_OPEN → OPEN: Probe read fails, reset cooldown

**Integration**: Wraps all `SwapFileManager` operations. On any I/O exception (`OSError`, `PermissionError`), increments failure counter and triggers state transition.

### 4.3 Block Allocator + LRU Tracker

Manages individual blocks within the swap file. Tracks access patterns for smart degradation.

```python
@dataclass
class SwapBlock:
    block_id: int
    offset: int           # Offset in swap file
    size: int             # Block size (fixed, e.g., 64MB)
    label: str            # What's stored ("layer_42_weights", "kv_cache_7")
    state: str            # "mapped" | "evicted" | "compressed"
    access_count: int     # Total accesses (for LRU ranking)
    last_access: float    # time.monotonic() of last access
    ram_copy: bool        # Whether a RAM fallback copy exists
```

**LRU Tracker** (from OSS120BCLI semantic_store.py pattern):
- Every `read_block()` / `write_block()` call updates `access_count` and `last_access`
- On device disconnect, sort blocks by `access_count` descending
- Top N blocks (that fit in available RAM) = "hot" → keep
- Remaining blocks = "cold" → mark evicted, model degrades

**Block lifecycle**:
```
allocate → mapped (on device)
    ↓ device disconnect
evicted (data lost from process view)
    ↓ device reconnect
mapped (re-read from device, data intact)
```

### 4.4 Memory Budget (from OSS120BCLI context_budget.py)

Dynamic allocation of memory across tiers.

```python
@dataclass
class MemoryBudget:
    gpu_vram: int      # GPU memory budget (bytes)
    ram: int           # System RAM budget (bytes)
    external: int      # External device budget (bytes)

    def rebalance_on_disconnect(self) -> None:
        """Device lost: redistribute external budget to RAM."""
        # external → 0
        # ram += whatever RAM can actually absorb
        # Trigger compression if ram can't absorb all

    def rebalance_on_reconnect(self) -> None:
        """Device restored: move data back to external."""
        # Restore original budget split
        # Migrate cold blocks back to device
```

**Budget rules**:
- Normal: `gpu=VRAM_SIZE, ram=FREE_RAM*50%, external=SWAP_SIZE`
- Degraded: `gpu=VRAM_SIZE, ram=FREE_RAM*80%, external=0`
- RAM cap at 80% free to avoid starving other processes

### 4.5 Fallback Chain (from OSS120BCLI recovery.py)

Ordered fallback strategy when device is unavailable.

```
1. Read from device (normal path)
   ↓ fails
2. Read from RAM cache (if block was recently accessed)
   ↓ not cached
3. Compress remaining blocks (FP16→INT8, 2x shrink)
   ↓ still doesn't fit
4. Quantize further (INT8→INT4, 4x shrink from original)
   ↓ still doesn't fit
5. Drop coldest blocks (model degrades, context shrinks)
```

Each level is tried in order. The chain stops at the first level that provides enough memory to continue inference.

**Compression integration** (from existing `slow_device_optimizer.py`):
- LZ4 for weight data (4-8 GB/s decompress, 2-4x ratio)
- Quantization for model layers (FP16→INT8→INT4)
- Only applied during degraded mode, reversed on reconnect

### 4.6 Device Health Monitor

Background thread polling device status.

```python
class DeviceMonitor(threading.Thread):
    """Poll device presence, feed CircuitBreaker."""

    def run(self):
        while self._running:
            present = self._swap_manager.is_device_present()
            self._circuit_breaker.report(present)

            if self._circuit_breaker.state == "OPEN":
                self._on_disconnect()   # Trigger degradation
            elif self._circuit_breaker.state == "CLOSED" and self._was_open:
                self._on_reconnect()    # Trigger recovery

            time.sleep(self._poll_interval)  # 5s normal, 2s in HALF_OPEN
```

---

## 5. Data Flow

### 5.1 Normal Operation (Device Present)

```
AI App (Ollama) → cudaMalloc(large_tensor)
    → CUDAInterceptor: "too big for VRAM"
    → BlockAllocator: allocate block on swap file
    → SwapFileManager: mmap region from device
    → Return pointer to mapped memory
    → AI App reads/writes directly (OS handles page faults → device I/O)
```

### 5.2 Device Disconnect

```
DeviceMonitor: poll fails (2x)
    → CircuitBreaker: CLOSED → OPEN
    → MemoryBudget: rebalance (external=0, ram↑)
    → BlockAllocator:
        1. Sort blocks by access_count (hot → cold)
        2. Hot blocks: mark for RAM retention
        3. Cold blocks: mark evicted
    → FallbackChain:
        1. If RAM can hold hot blocks → done
        2. If not → compress hot blocks (FP16→INT8)
        3. If still not → drop coldest, shrink model
    → CUDAInterceptor: update available memory report
    → GUI: show "degraded" status with remaining capacity
```

### 5.3 Device Reconnect

```
DeviceMonitor: poll succeeds in HALF_OPEN
    → CircuitBreaker: HALF_OPEN → CLOSED
    → SwapFileManager: re-open + re-mmap
    → BlockAllocator:
        1. Evicted blocks: re-map from device (data still on disk)
        2. Compressed blocks: decompress + write back to device
    → MemoryBudget: restore original split
    → FallbackChain: deactivate compression
    → GUI: show "restored" status
```

---

## 6. Exception Handling

### 6.1 Access Violation on Mapped Pages

When device is removed, mapped pages become invalid. Any access triggers `EXCEPTION_IN_PAGE_ERROR`.

**Strategy**: Structured Exception Handling (SEH) via `ctypes` or `signal`.

```python
def safe_read_block(self, block_id: int) -> Optional[bytes]:
    """Read block with SEH protection."""
    try:
        view = self._views[block_id]
        data = bytes(view)  # This triggers page fault → device read
        return data
    except OSError:
        # Page fault failed → device gone
        self._circuit_breaker.report(False)
        return self._fallback_chain.get(block_id)
```

**Windows-specific**: Register a Vectored Exception Handler (VEH) to catch `EXCEPTION_IN_PAGE_ERROR` (0xB9) before it crashes the process:

```python
def _install_veh(self):
    """Install Vectored Exception Handler for page fault protection."""
    # EXCEPTION_IN_PAGE_ERROR = 0xC0000006
    # Handler: mark block as evicted, return EXCEPTION_CONTINUE_EXECUTION
```

### 6.2 File Handle Invalidation

When device is removed, open file handles become invalid.

**Strategy**: All file operations wrapped in try/except. On `OSError`, close handles and let CircuitBreaker manage reconnection.

### 6.3 Race Conditions

Device might be removed DURING a read/write operation.

**Strategy**:
- All block I/O wrapped in `try/except`
- CircuitBreaker uses a lock to prevent concurrent state transitions
- BlockAllocator uses per-block locks for concurrent access safety

---

## 7. Integration with Existing Code

### 7.1 Replace RealBoostEngine (Windows path)

```python
# Before (real_boost.py):
result = engine.activate(letter, use_percent=80.0)
# → NtCreatePagingFile (blocked on external devices)

# After:
result = mmap_engine.activate(letter, use_percent=80.0)
# → CreateFileMapping on device (works everywhere)
```

**Interface preserved**: Same `activate()` / `deactivate()` / `status()` API. GUI code unchanged.

### 7.2 Reuse Existing Modules

| Existing Module | Reuse |
|----------------|-------|
| `slow_device_optimizer.py` | Compression/quantization in FallbackChain |
| `config.py` | Device specs, model profiles |
| `real_boost.py` (partial) | Speed benchmark, cached config, card fingerprint |

### 7.3 New Modules

| Module | Location | Purpose |
|--------|----------|---------|
| `mmap_swap.py` | `core/` | SwapFileManager + BlockAllocator |
| `circuit_breaker.py` | `core/` | Device state machine |
| `memory_budget.py` | `core/` | Tier allocation + rebalancing |
| `fallback_chain.py` | `core/` | Ordered degradation strategy |
| `device_monitor.py` | `core/` | Background health polling |

### 7.4 Linux Compatibility

Linux path unchanged — `swapon` works on any device. The new mmap engine is Windows-only. On Linux, fall through to existing `_activate_linux()`.

---

## 8. Performance Characteristics

### 8.1 Normal Operation

| Metric | Value |
|--------|-------|
| Swap file creation | Device speed dependent (SD: ~10min for 1.6GB, SSD: <1s) |
| Block mapping | <1ms per 64MB block (OS call) |
| Read latency | Device-dependent (SD: ~5ms random, SSD: <0.1ms) |
| RAM overhead | ~1MB for metadata (block table, LRU tracker) |
| CPU overhead | Negligible (OS handles page faults) |

### 8.2 Degraded Mode

| Metric | Value |
|--------|-------|
| Disconnect detection | 5-10s (2 failed polls × 5s interval) |
| Degradation time | <1s (LRU sort + budget rebalance) |
| Compression overhead | LZ4: ~2GB/s, INT8 quantize: ~1GB/s |
| Memory increase | Compression saves 2-4x → more layers fit in RAM |

### 8.3 Recovery

| Metric | Value |
|--------|-------|
| Reconnect detection | 2-7s (HALF_OPEN probe) |
| Full recovery time | <2s (re-mmap, no data copy needed) |

---

## 9. Size Limits

Swap size still governed by device speed formula:

```
max_swap = random_write_speed (MB/s) × 600 seconds
```

| Device | Speed | Max Swap |
|--------|-------|----------|
| SD card UHS-I | 3 MB/s | 1.8 GB |
| SD card UHS-II | 30 MB/s | 18 GB |
| USB 3.0 SSD | 100 MB/s | 60 GB |
| USB 3.2 SSD | 300 MB/s | 180 GB |
| SD Express | 800 MB/s | 480 GB |
| TB4 NVMe | 3000 MB/s | 1.8 TB |

---

## 10. GUI Changes

### 10.1 Phase 3 (Active/Monitoring) — Add Status Indicator

```
┌─ VRAM Booster ─────────── ● 運作中 ──┐
│ VRAM  ████████░░░░  4.2 / 8.0 GB     │
│ RAM   █████████░░░  24.1 / 31.1 GB   │
│ 裝置  ████░░░░░░░░  0.6 / 1.6 GB     │
│                                        │
│ 裝置狀態: ● 正常 (E:\)               │  ← new
│ 保護: 智慧降級 (B+)                   │  ← new
└────────────────────────────────────────┘
```

### 10.2 Degraded State

```
┌─ VRAM Booster ─────── ● 降級模式 ──┐
│ VRAM  ████████░░░░  4.2 / 8.0 GB   │
│ RAM   ███████████░  28.3 / 31.1 GB │
│ 裝置  ░░░░░░░░░░░░  斷線           │  ← red
│                                      │
│ 裝置狀態: ● 已斷線 — 等待重新連接   │  ← orange
│ 保護: 保留 12/18 層 (INT8 壓縮中)   │  ← info
│                                      │
│ [重新插入裝置即可自動恢復]           │
└──────────────────────────────────────┘
```

---

## 11. Testing Strategy

1. **Unit tests**: BlockAllocator, CircuitBreaker state transitions, MemoryBudget rebalance
2. **Integration test**: Full activate → read/write → simulate disconnect → verify degradation → reconnect → verify recovery
3. **Stress test**: Rapid connect/disconnect cycles (device flapping)
4. **Performance test**: Measure read latency, degradation time, recovery time
5. **Edge cases**: Device full, device read-only, permission denied, multiple devices

---

## 12. Non-Goals

- System-wide memory expansion (only AI workloads)
- Windows pagefile replacement
- Kernel driver development
- Support for non-Windows platforms via this engine (Linux uses existing swapon)
