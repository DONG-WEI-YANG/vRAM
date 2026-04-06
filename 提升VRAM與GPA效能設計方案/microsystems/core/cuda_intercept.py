"""
CUDA Interception Layer
=======================
攔截 CUDA 記憶體管理 API，將超出實體 VRAM 的分配請求
透明地重定向至外部儲存裝置。

Linux:  透過 LD_PRELOAD 注入 libsdvram_cuda.so
Windows: 透過 CUDA Driver API (cuMemCreate / cuMemMap) Virtual Memory Management

核心攔截目標：
  - cudaMalloc / cudaMallocAsync  → 記憶體分配
  - cudaFree / cudaFreeAsync      → 記憶體釋放
  - cuDeviceTotalMem              → 回報擴展後容量
  - cuMemGetInfo                  → 回報可用記憶體
"""

from __future__ import annotations

import ctypes
import logging
import mmap
import os
import platform
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)


# ── Shared Memory IPC 協議常數 ──
SHM_NAME = "sdvram_ctrl"
SHM_SIZE = 4096
# Header layout (little-endian):
#   [0:4]   magic   = 0x5644524D ("VDRM")
#   [4:8]   version = 1
#   [8:16]  vram_total (uint64)
#   [16:24] vram_used  (uint64)
#   [24:32] ram_pool   (uint64)
#   [32:40] ram_used   (uint64)
#   [40:48] ext_pool   (uint64)
#   [48:56] ext_used   (uint64)
#   [56:60] alloc_threshold (uint32)
#   [60:64] flags      (uint32) — bit0: active, bit1: request_pending
#   [64:72] req_size   (uint64) — allocation request size
#   [72:76] req_tag_len (uint32)
#   [76:140] req_tag   (64 bytes, UTF-8)
#   [140:144] resp_code (uint32) — 0=pending, 1=VRAM, 2=RAM, 3=EXTERNAL, 0xFF=OOM
#   [144:152] resp_ptr  (uint64) — virtual pointer returned
SHM_MAGIC = 0x5644524D
SHM_VERSION = 1
SHM_HDR_FMT = "<II QQ QQ QQ II Q I 64s I Q"
SHM_HDR_SIZE = struct.calcsize(SHM_HDR_FMT)

# Response codes
RESP_PENDING = 0
RESP_VRAM = 1
RESP_RAM = 2
RESP_EXTERNAL = 3
RESP_OOM = 0xFF

class EvictionPolicy(IntEnum):
    """VRAM 驅逐策略 — 決定 VRAM 滿時驅逐哪些區塊到外部儲存"""
    LRU = 0           # 最久未存取的區塊優先驅逐
    TAG_PRIORITY = 1  # 權重 > KV-cache > 其他（LLM 專用）
    SIZE_FIRST = 2    # 最大區塊優先驅逐（最大化釋放空間）


class AllocLocation(Enum):
    VRAM = "vram"
    RAM = "ram"
    EXTERNAL = "external"


_LOCATION_TO_RESP = {
    AllocLocation.VRAM: RESP_VRAM,
    AllocLocation.RAM: RESP_RAM,
    AllocLocation.EXTERNAL: RESP_EXTERNAL,
}


@dataclass
class MemoryBlock:
    """追蹤單一記憶體分配"""
    ptr: int                # 虛擬指標地址
    size_bytes: int
    location: AllocLocation
    device_id: int = 0
    is_pinned: bool = False
    refcount: int = 1
    tag: str = ""           # 用途標記 (e.g., "kv_cache", "weight_layer_42")
    alloc_time_ns: int = 0  # 分配時間 (time.time_ns)
    last_access_ns: int = 0 # 最後存取時間 (用於 LRU 驅逐)


@dataclass
class VRAMStats:
    """VRAM 使用統計"""
    physical_total_bytes: int = 0
    physical_used_bytes: int = 0
    extended_total_bytes: int = 0
    extended_used_bytes: int = 0
    ram_pool_bytes: int = 0
    ram_used_bytes: int = 0
    external_pool_bytes: int = 0
    external_used_bytes: int = 0
    intercept_count: int = 0
    redirect_count: int = 0

    @property
    def physical_free(self) -> int:
        return self.physical_total_bytes - self.physical_used_bytes

    @property
    def total_available(self) -> int:
        return (self.physical_total_bytes + self.ram_pool_bytes
                + self.external_pool_bytes)

    @property
    def total_used(self) -> int:
        return (self.physical_used_bytes + self.ram_used_bytes
                + self.external_used_bytes)


class CUDAInterceptor:
    """
    CUDA 記憶體分配攔截器。

    在 GPU 實體 VRAM 不足時，自動將分配請求重定向至
    RAM 緩衝池或外部儲存裝置，實現透明的記憶體擴展。
    """

    # ── Tag 優先級 (用於 TAG_PRIORITY 驅逐策略) ──
    # 數值越高 = 越不容易被驅逐
    TAG_PRIORITY_MAP = {
        "weight": 10,       # 模型權重：讀取頻率低，安全驅逐
        "activation": 30,   # 激活值：中間計算，可重算
        "kv_cache": 50,     # KV-cache：每個 token 都需要，盡量保留
        "optimizer": 20,    # 優化器狀態：訓練用，推理不需要
    }
    TAG_PRIORITY_DEFAULT = 25

    def __init__(
        self,
        vram_total_bytes: int,
        ram_pool_bytes: int = 0,
        external_pool_bytes: int = 0,
        alloc_threshold_bytes: int = 256 * 1024 * 1024,  # 256MB
        report_extended: bool = True,
        eviction_policy: EvictionPolicy = EvictionPolicy.TAG_PRIORITY,
        enable_shm_bridge: bool = False,
    ):
        self._vram_total = vram_total_bytes
        self._ram_pool = ram_pool_bytes
        self._ext_pool = external_pool_bytes
        self._threshold = alloc_threshold_bytes
        self._report_extended = report_extended
        self._eviction_policy = eviction_policy

        self._lock = threading.Lock()
        self._allocations: Dict[int, MemoryBlock] = {}
        self._next_virtual_ptr = 0x7F00_0000_0000  # 虛擬地址空間起始
        self._stats = VRAMStats(
            physical_total_bytes=vram_total_bytes,
            ram_pool_bytes=ram_pool_bytes,
            external_pool_bytes=external_pool_bytes,
        )

        # 回調函式：由 device driver 提供實際的讀寫實作
        self._on_external_alloc: Optional[Callable[[int], int]] = None
        self._on_external_free: Optional[Callable[[int], None]] = None
        self._on_ram_alloc: Optional[Callable[[int], int]] = None
        self._on_ram_free: Optional[Callable[[int], None]] = None

        # 驅逐回調：將 VRAM 區塊遷移到 RAM 或 External
        self._on_evict: Optional[Callable[[MemoryBlock, AllocLocation], bool]] = None

        self._is_os_windows = platform.system().lower() == "windows"
        self._active = False

        # Shared Memory IPC Bridge (用於 C shim 通訊)
        self._shm_bridge: Optional[SharedMemoryBridge] = None
        self._shm_poll_thread: Optional[threading.Thread] = None
        if enable_shm_bridge:
            self._shm_bridge = SharedMemoryBridge(self)

        logger.info(
            "CUDAInterceptor initialized: VRAM=%.1fGB, RAM_pool=%.1fGB, "
            "External=%.1fGB, eviction=%s",
            vram_total_bytes / (1024**3),
            ram_pool_bytes / (1024**3),
            external_pool_bytes / (1024**3),
            eviction_policy.name,
        )

    # ── Public API ──

    def activate(self) -> None:
        """啟動攔截器，開始監控 CUDA 記憶體分配"""
        self._active = True
        if self._shm_bridge:
            self._shm_bridge.open()
            self._shm_bridge.sync_stats(self._stats, self._threshold, active=True)
            self._shm_poll_thread = threading.Thread(
                target=self._shm_poll_loop, daemon=True, name="shm-poll",
            )
            self._shm_poll_thread.start()
        logger.info("CUDA interceptor activated (threshold=%dMB, shm=%s)",
                     self._threshold // (1024 * 1024),
                     "on" if self._shm_bridge else "off")

    def deactivate(self) -> None:
        """停用攔截器，釋放所有重定向的記憶體"""
        self._active = False
        if self._shm_bridge:
            self._shm_bridge.sync_stats(self._stats, self._threshold, active=False)
            self._shm_bridge.close()
        with self._lock:
            for blk in list(self._allocations.values()):
                if blk.location != AllocLocation.VRAM:
                    self._free_block(blk)
            self._allocations.clear()
        logger.info("CUDA interceptor deactivated, all redirected memory freed")

    def register_callbacks(
        self,
        external_alloc: Optional[Callable[[int], int]] = None,
        external_free: Optional[Callable[[int], None]] = None,
        ram_alloc: Optional[Callable[[int], int]] = None,
        ram_free: Optional[Callable[[int], None]] = None,
    ) -> None:
        """註冊外部儲存和 RAM 的記憶體分配/釋放回調"""
        self._on_external_alloc = external_alloc
        self._on_external_free = external_free
        self._on_ram_alloc = ram_alloc
        self._on_ram_free = ram_free

    def register_evict_callback(
        self,
        on_evict: Callable[[MemoryBlock, AllocLocation], bool],
    ) -> None:
        """
        註冊驅逐回調：將 VRAM 區塊的資料遷移到目標層。

        Args:
            on_evict: (block, dest_location) → success
                      呼叫者負責將 block 的資料複製到 dest_location，
                      成功回傳 True。
        """
        self._on_evict = on_evict

    def intercept_malloc(self, size_bytes: int, tag: str = "") -> Tuple[int, AllocLocation]:
        """
        攔截 cudaMalloc 請求。

        決策邏輯：
        1. 若 VRAM 有足夠空間 → 分配在 VRAM
        2. 若 VRAM 不足 → 嘗試驅逐舊區塊騰出空間
        3. 驅逐後仍不足 → 嘗試 RAM pool
        4. 若 RAM 也不足 → 嘗試外部儲存
        5. 全部不足 → 拋出 MemoryError

        Returns:
            (virtual_ptr, location) 虛擬指標與實際存放位置
        """
        if not self._active:
            raise RuntimeError("Interceptor not active")

        self._stats.intercept_count += 1

        with self._lock:
            location = self._decide_location(size_bytes)

            # VRAM 不足時嘗試驅逐
            if location != AllocLocation.VRAM:
                evicted = self._try_evict_for(size_bytes)
                if evicted > 0:
                    # 驅逐成功，重新評估
                    location = self._decide_location(size_bytes)

            ptr = self._allocate_block(size_bytes, location, tag)
            return ptr, location

    def intercept_free(self, ptr: int) -> None:
        """攔截 cudaFree 請求"""
        with self._lock:
            blk = self._allocations.get(ptr)
            if blk is None:
                logger.warning("Free request for unknown ptr 0x%X", ptr)
                return
            self._free_block(blk)
            del self._allocations[ptr]

    def query_mem_info(self) -> Tuple[int, int]:
        """
        攔截 cuMemGetInfo，回報擴展後的記憶體資訊。

        Returns:
            (free_bytes, total_bytes)
        """
        if self._report_extended:
            total = self._stats.total_available
            used = self._stats.total_used
        else:
            total = self._stats.physical_total_bytes
            used = self._stats.physical_used_bytes
        return (total - used, total)

    def query_device_total_mem(self) -> int:
        """攔截 cuDeviceTotalMem，回報擴展後的總容量"""
        if self._report_extended:
            return self._stats.total_available
        return self._stats.physical_total_bytes

    @property
    def stats(self) -> VRAMStats:
        return self._stats

    @property
    def allocations(self) -> Dict[int, MemoryBlock]:
        return dict(self._allocations)

    def get_allocation_summary(self) -> Dict[str, int]:
        """取得各位置的分配統計"""
        summary = {loc.value: 0 for loc in AllocLocation}
        for blk in self._allocations.values():
            summary[blk.location.value] += blk.size_bytes
        return summary

    # ── Internal ──

    def _decide_location(self, size_bytes: int) -> AllocLocation:
        """決定記憶體分配位置"""
        vram_free = self._stats.physical_total_bytes - self._stats.physical_used_bytes
        ram_free = self._stats.ram_pool_bytes - self._stats.ram_used_bytes
        ext_free = self._stats.external_pool_bytes - self._stats.external_used_bytes

        # 小分配優先放 VRAM
        if size_bytes < self._threshold and vram_free >= size_bytes:
            return AllocLocation.VRAM

        # VRAM 有空間就用 VRAM
        if vram_free >= size_bytes:
            return AllocLocation.VRAM

        # VRAM 不足，嘗試 RAM
        if ram_free >= size_bytes:
            self._stats.redirect_count += 1
            logger.debug("Redirecting %dMB to RAM (VRAM full)",
                         size_bytes // (1024 * 1024))
            return AllocLocation.RAM

        # RAM 也不足，嘗試外部裝置
        if ext_free >= size_bytes:
            self._stats.redirect_count += 1
            logger.debug("Redirecting %dMB to external storage",
                         size_bytes // (1024 * 1024))
            return AllocLocation.EXTERNAL

        raise MemoryError(
            f"Cannot allocate {size_bytes / (1024**3):.2f}GB: "
            f"VRAM free={vram_free / (1024**3):.2f}GB, "
            f"RAM free={ram_free / (1024**3):.2f}GB, "
            f"External free={ext_free / (1024**3):.2f}GB"
        )

    def touch(self, ptr: int) -> None:
        """更新區塊的最後存取時間（供 LRU 驅逐參考）"""
        blk = self._allocations.get(ptr)
        if blk:
            blk.last_access_ns = time.time_ns()

    def _allocate_block(self, size: int, location: AllocLocation, tag: str) -> int:
        """分配記憶體區塊並回傳虛擬指標"""
        ptr = self._next_virtual_ptr
        self._next_virtual_ptr += size + 4096  # 4KB alignment padding

        # 呼叫對應的 backend 進行實際分配
        if location == AllocLocation.EXTERNAL and self._on_external_alloc:
            self._on_external_alloc(size)
        elif location == AllocLocation.RAM and self._on_ram_alloc:
            self._on_ram_alloc(size)

        now = time.time_ns()
        block = MemoryBlock(
            ptr=ptr,
            size_bytes=size,
            location=location,
            tag=tag,
            alloc_time_ns=now,
            last_access_ns=now,
        )
        self._allocations[ptr] = block

        # 更新統計
        if location == AllocLocation.VRAM:
            self._stats.physical_used_bytes += size
        elif location == AllocLocation.RAM:
            self._stats.ram_used_bytes += size
        elif location == AllocLocation.EXTERNAL:
            self._stats.external_used_bytes += size

        self._stats.extended_used_bytes = self._stats.total_used

        logger.debug(
            "Allocated %dMB at 0x%X [%s] tag=%s",
            size // (1024 * 1024), ptr, location.value, tag,
        )
        return ptr

    def _free_block(self, blk: MemoryBlock) -> None:
        """釋放記憶體區塊"""
        if blk.location == AllocLocation.EXTERNAL and self._on_external_free:
            self._on_external_free(blk.ptr)
        elif blk.location == AllocLocation.RAM and self._on_ram_free:
            self._on_ram_free(blk.ptr)

        if blk.location == AllocLocation.VRAM:
            self._stats.physical_used_bytes -= blk.size_bytes
        elif blk.location == AllocLocation.RAM:
            self._stats.ram_used_bytes -= blk.size_bytes
        elif blk.location == AllocLocation.EXTERNAL:
            self._stats.external_used_bytes -= blk.size_bytes

        self._stats.extended_used_bytes = self._stats.total_used

    # ── Eviction Engine ──

    def _try_evict_for(self, needed_bytes: int) -> int:
        """
        嘗試從 VRAM 驅逐區塊以騰出 needed_bytes 空間。

        根據 self._eviction_policy 決定驅逐順序。
        回傳實際釋放的 bytes 數。
        """
        if not self._on_evict:
            return 0

        vram_free = self._stats.physical_total_bytes - self._stats.physical_used_bytes
        deficit = needed_bytes - vram_free
        if deficit <= 0:
            return 0

        candidates = self._rank_eviction_candidates()
        evicted_total = 0

        for blk in candidates:
            if blk.is_pinned:
                continue

            # 選擇驅逐目標層：優先 RAM，RAM 滿則 External
            ram_free = self._stats.ram_pool_bytes - self._stats.ram_used_bytes
            if ram_free >= blk.size_bytes:
                dest = AllocLocation.RAM
            else:
                ext_free = self._stats.external_pool_bytes - self._stats.external_used_bytes
                if ext_free >= blk.size_bytes:
                    dest = AllocLocation.EXTERNAL
                else:
                    continue  # 無處可去

            ok = self._on_evict(blk, dest)
            if not ok:
                continue

            # 更新統計：VRAM 減少，目標層增加
            self._stats.physical_used_bytes -= blk.size_bytes
            if dest == AllocLocation.RAM:
                self._stats.ram_used_bytes += blk.size_bytes
            else:
                self._stats.external_used_bytes += blk.size_bytes
            blk.location = dest

            evicted_total += blk.size_bytes
            logger.info(
                "Evicted %dMB (tag=%s) from VRAM → %s",
                blk.size_bytes // (1024 * 1024), blk.tag, dest.value,
            )

            if evicted_total >= deficit:
                break

        self._stats.extended_used_bytes = self._stats.total_used
        return evicted_total

    def _rank_eviction_candidates(self) -> List[MemoryBlock]:
        """根據驅逐策略排序 VRAM 區塊"""
        vram_blocks = [
            b for b in self._allocations.values()
            if b.location == AllocLocation.VRAM and not b.is_pinned
        ]

        if self._eviction_policy == EvictionPolicy.LRU:
            vram_blocks.sort(key=lambda b: b.last_access_ns)

        elif self._eviction_policy == EvictionPolicy.TAG_PRIORITY:
            # 優先級低的先驅逐，同優先級則 LRU
            def tag_key(b: MemoryBlock) -> Tuple[int, int]:
                base_tag = b.tag.split("_")[0] if b.tag else ""
                priority = self.TAG_PRIORITY_MAP.get(base_tag, self.TAG_PRIORITY_DEFAULT)
                return (priority, b.last_access_ns)
            vram_blocks.sort(key=tag_key)

        elif self._eviction_policy == EvictionPolicy.SIZE_FIRST:
            vram_blocks.sort(key=lambda b: -b.size_bytes)

        return vram_blocks

    # ── SHM Poll Loop (服務 C shim 的分配請求) ──

    def _shm_poll_loop(self) -> None:
        """輪詢 shared memory 中 C shim 送來的分配請求"""
        bridge = self._shm_bridge
        if not bridge:
            return

        while self._active:
            try:
                req = bridge.read_request()
                if req is not None:
                    size, tag = req
                    try:
                        ptr, loc = self.intercept_malloc(size, tag)
                        bridge.write_response(_LOCATION_TO_RESP[loc], ptr)
                    except MemoryError:
                        bridge.write_response(RESP_OOM, 0)
                    bridge.sync_stats(self._stats, self._threshold, active=True)
                else:
                    time.sleep(0.0001)  # 100μs poll interval
            except Exception as e:
                logger.error("SHM poll error: %s", e)
                time.sleep(0.01)

    # ── LD_PRELOAD shim 產生器 (Linux) ──

    @staticmethod
    def generate_preload_shim_source() -> str:
        """
        產生 Linux LD_PRELOAD 共享函式庫的 C 源碼框架。

        功能：
          1. 攔截 cudaMalloc / cudaFree
          2. 透過 POSIX shared memory 與 Python 控制器通訊
          3. VRAM 滿時使用 cudaMallocManaged (Unified Memory) 作為 fallback
          4. 回報擴展後的 VRAM 容量

        實際編譯需要 CUDA Toolkit headers。
        """
        return '''\
/* libsdvram_cuda.so - CUDA Memory Interception Shim
 * Compile: gcc -shared -fPIC -o libsdvram_cuda.so shim.c -ldl -lcuda -lcudart -lrt -lpthread
 * Usage:   LD_PRELOAD=./libsdvram_cuda.so python3 your_app.py
 *
 * IPC Protocol:
 *   Shared memory region "/sdvram_ctrl" (4096 bytes)
 *   Python 控制器建立，C shim 以 O_RDWR 開啟。
 *   佈局見 Python 側 SHM_HDR_FMT 定義。
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <pthread.h>
#include <cuda.h>
#include <cuda_runtime.h>

/* ── Real function pointers ── */
typedef cudaError_t (*real_cudaMalloc_t)(void**, size_t);
typedef cudaError_t (*real_cudaFree_t)(void*);
typedef CUresult    (*real_cuMemGetInfo_t)(size_t*, size_t*);
typedef CUresult    (*real_cuDeviceTotalMem_t)(size_t*, CUdevice);

static real_cudaMalloc_t       real_cudaMalloc       = NULL;
static real_cudaFree_t         real_cudaFree         = NULL;
static real_cuMemGetInfo_t     real_cuMemGetInfo      = NULL;
static real_cuDeviceTotalMem_t real_cuDeviceTotalMem  = NULL;

/* ── Shared memory IPC ── */
#define SHM_NAME   "/sdvram_ctrl"
#define SHM_SIZE   4096
#define SHM_MAGIC  0x5644524D  /* "VDRM" */

/* Offsets (must match Python SHM_HDR_FMT) */
#define OFF_MAGIC       0
#define OFF_VERSION     4
#define OFF_VRAM_TOTAL  8
#define OFF_VRAM_USED   16
#define OFF_RAM_POOL    24
#define OFF_RAM_USED    32
#define OFF_EXT_POOL    40
#define OFF_EXT_USED    48
#define OFF_THRESHOLD   56
#define OFF_FLAGS       60   /* bit0: active, bit1: request_pending */
#define OFF_REQ_SIZE    64
#define OFF_REQ_TAG_LEN 72
#define OFF_REQ_TAG     76   /* 64 bytes */
#define OFF_RESP_CODE   140  /* 0=pending, 1=VRAM, 2=RAM, 3=EXTERNAL, 0xFF=OOM */
#define OFF_RESP_PTR    144

/* Response codes */
#define RESP_PENDING  0
#define RESP_VRAM     1
#define RESP_RAM      2
#define RESP_EXTERNAL 3
#define RESP_OOM      0xFF

static volatile uint8_t *shm_ptr  = NULL;
static int               shm_fd   = -1;
static pthread_mutex_t   shm_lock = PTHREAD_MUTEX_INITIALIZER;

/* ── Redirected allocation tracking ── */
#define MAX_REDIRECTED 4096
struct redirected_alloc {
    void  *managed_ptr;   /* cudaMallocManaged pointer */
    void  *original_req;  /* what the caller sees */
    size_t size;
    int    in_use;
};
static struct redirected_alloc redirected[MAX_REDIRECTED];

static int shm_open_ctrl(void) {
    shm_fd = shm_open(SHM_NAME, O_RDWR, 0600);
    if (shm_fd < 0) return -1;
    shm_ptr = mmap(NULL, SHM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd, 0);
    if (shm_ptr == MAP_FAILED) { shm_ptr = NULL; close(shm_fd); return -1; }
    uint32_t magic = *(uint32_t *)(shm_ptr + OFF_MAGIC);
    if (magic != SHM_MAGIC) {
        fprintf(stderr, "[SD-VRAM Shim] SHM magic mismatch: 0x%X\\n", magic);
        munmap((void*)shm_ptr, SHM_SIZE); shm_ptr = NULL;
        close(shm_fd); return -1;
    }
    return 0;
}

/* Ask Python controller to decide allocation location */
static int shm_request_alloc(size_t size, const char *tag) {
    if (!shm_ptr) return RESP_PENDING;

    pthread_mutex_lock(&shm_lock);

    /* Write request */
    *(uint64_t *)(shm_ptr + OFF_REQ_SIZE) = size;
    size_t tag_len = tag ? strlen(tag) : 0;
    if (tag_len > 63) tag_len = 63;
    *(uint32_t *)(shm_ptr + OFF_REQ_TAG_LEN) = (uint32_t)tag_len;
    if (tag_len > 0) memcpy((void*)(shm_ptr + OFF_REQ_TAG), tag, tag_len);
    ((uint8_t*)(shm_ptr + OFF_REQ_TAG))[tag_len] = 0;

    /* Clear response and set request_pending flag */
    *(uint32_t *)(shm_ptr + OFF_RESP_CODE) = RESP_PENDING;
    uint32_t flags = *(uint32_t *)(shm_ptr + OFF_FLAGS);
    flags |= 0x02;  /* bit1: request_pending */
    *(uint32_t *)(shm_ptr + OFF_FLAGS) = flags;
    __sync_synchronize();  /* memory barrier */

    /* Spin-wait for Python response (timeout: 100ms) */
    int resp = RESP_PENDING;
    for (int i = 0; i < 10000; i++) {
        __sync_synchronize();
        resp = *(uint32_t *)(shm_ptr + OFF_RESP_CODE);
        if (resp != RESP_PENDING) break;
        usleep(10);  /* 10μs */
    }

    pthread_mutex_unlock(&shm_lock);
    return resp;
}

static int find_free_slot(void) {
    for (int i = 0; i < MAX_REDIRECTED; i++)
        if (!redirected[i].in_use) return i;
    return -1;
}

__attribute__((constructor))
static void init(void) {
    real_cudaMalloc       = dlsym(RTLD_NEXT, "cudaMalloc");
    real_cudaFree         = dlsym(RTLD_NEXT, "cudaFree");
    real_cuMemGetInfo     = dlsym(RTLD_NEXT, "cuMemGetInfo_v2");
    real_cuDeviceTotalMem = dlsym(RTLD_NEXT, "cuDeviceTotalMem_v2");
    memset(redirected, 0, sizeof(redirected));

    if (shm_open_ctrl() == 0)
        fprintf(stderr, "[SD-VRAM Shim] IPC connected to Python controller.\\n");
    else
        fprintf(stderr, "[SD-VRAM Shim] No IPC (standalone mode).\\n");
    fprintf(stderr, "[SD-VRAM Shim] Loaded. Intercepting CUDA memory APIs.\\n");
}

__attribute__((destructor))
static void fini(void) {
    if (shm_ptr) { munmap((void*)shm_ptr, SHM_SIZE); shm_ptr = NULL; }
    if (shm_fd >= 0) { close(shm_fd); shm_fd = -1; }
}

cudaError_t cudaMalloc(void **devPtr, size_t size) {
    /* 1. 嘗試正常 VRAM 分配 */
    cudaError_t err = real_cudaMalloc(devPtr, size);
    if (err == cudaSuccess)
        return err;

    /* 2. VRAM 不足 — 諮詢 Python 控制器 */
    fprintf(stderr, "[SD-VRAM Shim] VRAM full, requesting redirect for %zu bytes\\n", size);

    int resp = shm_request_alloc(size, NULL);

    if (resp == RESP_OOM || resp == RESP_PENDING) {
        /* Python 也無法分配，回傳原始錯誤 */
        return err;
    }

    /* 3. Fallback: 使用 cudaMallocManaged (Unified Memory)
     *    CUDA runtime 會自動在 GPU/CPU 之間遷移資料。
     *    對於 LLM 推理，這讓 GPU 能透明地存取超出 VRAM 的資料。
     */
    void *managed = NULL;
    cudaError_t merr = cudaMallocManaged(&managed, size, cudaMemAttachGlobal);
    if (merr != cudaSuccess) {
        fprintf(stderr, "[SD-VRAM Shim] cudaMallocManaged failed: %d\\n", merr);
        return merr;
    }

    /* 4. 追蹤重定向分配 */
    int slot = find_free_slot();
    if (slot >= 0) {
        redirected[slot].managed_ptr  = managed;
        redirected[slot].original_req = managed;  /* same ptr for Unified Memory */
        redirected[slot].size         = size;
        redirected[slot].in_use       = 1;
    }

    /* 5. 設定存取提示：偏好 GPU 存取（減少 CPU→GPU 遷移延遲） */
    int device;
    cudaGetDevice(&device);
    cudaMemAdvise(managed, size, cudaMemAdviseSetPreferredLocation, device);
    cudaMemPrefetchAsync(managed, size, device, 0);

    *devPtr = managed;
    fprintf(stderr, "[SD-VRAM Shim] Redirected %zu bytes via Unified Memory\\n", size);
    return cudaSuccess;
}

cudaError_t cudaFree(void *devPtr) {
    /* 檢查是否為重定向的分配 */
    for (int i = 0; i < MAX_REDIRECTED; i++) {
        if (redirected[i].in_use && redirected[i].managed_ptr == devPtr) {
            redirected[i].in_use = 0;
            fprintf(stderr, "[SD-VRAM Shim] Freed redirected %zu bytes\\n",
                    redirected[i].size);
            /* cudaFree 可以處理 Managed memory */
            break;
        }
    }
    return real_cudaFree(devPtr);
}

CUresult cuMemGetInfo_v2(size_t *free, size_t *total) {
    CUresult r = real_cuMemGetInfo(free, total);
    if (shm_ptr) {
        uint32_t flags = *(volatile uint32_t *)(shm_ptr + OFF_FLAGS);
        if (flags & 0x01) {  /* active */
            uint64_t vt = *(volatile uint64_t *)(shm_ptr + OFF_VRAM_TOTAL);
            uint64_t vu = *(volatile uint64_t *)(shm_ptr + OFF_VRAM_USED);
            uint64_t rp = *(volatile uint64_t *)(shm_ptr + OFF_RAM_POOL);
            uint64_t ru = *(volatile uint64_t *)(shm_ptr + OFF_RAM_USED);
            uint64_t ep = *(volatile uint64_t *)(shm_ptr + OFF_EXT_POOL);
            uint64_t eu = *(volatile uint64_t *)(shm_ptr + OFF_EXT_USED);
            *total = vt + rp + ep;
            *free  = (vt - vu) + (rp - ru) + (ep - eu);
        }
    }
    return r;
}

CUresult cuDeviceTotalMem_v2(size_t *bytes, CUdevice dev) {
    CUresult r = real_cuDeviceTotalMem(bytes, dev);
    if (shm_ptr) {
        uint32_t flags = *(volatile uint32_t *)(shm_ptr + OFF_FLAGS);
        if (flags & 0x01) {
            uint64_t vt = *(volatile uint64_t *)(shm_ptr + OFF_VRAM_TOTAL);
            uint64_t rp = *(volatile uint64_t *)(shm_ptr + OFF_RAM_POOL);
            uint64_t ep = *(volatile uint64_t *)(shm_ptr + OFF_EXT_POOL);
            *bytes = vt + rp + ep;
        }
    }
    return r;
}
'''


class SharedMemoryBridge:
    """
    POSIX Shared Memory IPC Bridge。

    Python 控制器端：建立 shared memory 區域，
    C shim 端以 O_RDWR 開啟讀寫。

    佈局對齊 SHM_HDR_FMT，供 C shim 和 Python 同時存取。
    Windows 上使用 mmap 對應到檔案（因為 Windows 無 POSIX shm）。
    """

    # Offsets (必須與 C shim 一致)
    OFF_MAGIC = 0
    OFF_VERSION = 4
    OFF_VRAM_TOTAL = 8
    OFF_VRAM_USED = 16
    OFF_RAM_POOL = 24
    OFF_RAM_USED = 32
    OFF_EXT_POOL = 40
    OFF_EXT_USED = 48
    OFF_THRESHOLD = 56
    OFF_FLAGS = 60
    OFF_REQ_SIZE = 64
    OFF_REQ_TAG_LEN = 72
    OFF_REQ_TAG = 76
    OFF_RESP_CODE = 140
    OFF_RESP_PTR = 144

    def __init__(self, interceptor: CUDAInterceptor):
        self._interceptor = interceptor
        self._mm: Optional[mmap.mmap] = None
        self._fd = None
        self._shm_path: Optional[str] = None
        self._is_windows = platform.system().lower() == "windows"

    def open(self) -> None:
        """建立或開啟 shared memory 區域"""
        if self._is_windows:
            # Windows: 使用 temp 目錄中的檔案作為 mmap backing
            self._shm_path = os.path.join(tempfile.gettempdir(), SHM_NAME)
            fd = os.open(self._shm_path, os.O_CREAT | os.O_RDWR | os.O_BINARY)
            try:
                # 確保檔案大小足夠
                if os.fstat(fd).st_size < SHM_SIZE:
                    os.write(fd, b"\x00" * SHM_SIZE)
                self._mm = mmap.mmap(fd, SHM_SIZE)
            finally:
                os.close(fd)
        else:
            # Linux/macOS: POSIX shared memory
            import posix_ipc  # type: ignore
            try:
                shm = posix_ipc.SharedMemory(
                    "/" + SHM_NAME, posix_ipc.O_CREX, size=SHM_SIZE,
                )
            except posix_ipc.ExistentialError:
                shm = posix_ipc.SharedMemory("/" + SHM_NAME)
            self._mm = mmap.mmap(shm.fd, SHM_SIZE)
            shm.close_fd()

        # 寫入 magic 和 version
        self._write_u32(self.OFF_MAGIC, SHM_MAGIC)
        self._write_u32(self.OFF_VERSION, SHM_VERSION)
        logger.info("SharedMemoryBridge opened (%s)",
                     "file-backed" if self._is_windows else "POSIX shm")

    def close(self) -> None:
        """關閉 shared memory"""
        if self._mm:
            # 清除 active flag
            self._write_u32(self.OFF_FLAGS, 0)
            self._mm.close()
            self._mm = None
        if self._is_windows and self._shm_path:
            try:
                os.unlink(self._shm_path)
            except OSError:
                pass

    def sync_stats(self, stats: VRAMStats, threshold: int, active: bool) -> None:
        """將 Python 端的統計同步到 shared memory"""
        if not self._mm:
            return
        self._write_u64(self.OFF_VRAM_TOTAL, stats.physical_total_bytes)
        self._write_u64(self.OFF_VRAM_USED, stats.physical_used_bytes)
        self._write_u64(self.OFF_RAM_POOL, stats.ram_pool_bytes)
        self._write_u64(self.OFF_RAM_USED, stats.ram_used_bytes)
        self._write_u64(self.OFF_EXT_POOL, stats.external_pool_bytes)
        self._write_u64(self.OFF_EXT_USED, stats.external_used_bytes)
        self._write_u32(self.OFF_THRESHOLD, threshold)
        flags = 0x01 if active else 0x00
        self._write_u32(self.OFF_FLAGS, flags)

    def read_request(self) -> Optional[Tuple[int, str]]:
        """
        讀取 C shim 寫入的分配請求。

        Returns:
            (size_bytes, tag) 或 None 如果沒有待處理的請求。
        """
        if not self._mm:
            return None

        flags = self._read_u32(self.OFF_FLAGS)
        if not (flags & 0x02):  # bit1: request_pending
            return None

        size = self._read_u64(self.OFF_REQ_SIZE)
        tag_len = self._read_u32(self.OFF_REQ_TAG_LEN)
        tag = ""
        if tag_len > 0:
            self._mm.seek(self.OFF_REQ_TAG)
            raw = self._mm.read(min(tag_len, 63))
            tag = raw.rstrip(b"\x00").decode("utf-8", errors="replace")

        # 清除 request_pending flag
        flags &= ~0x02
        self._write_u32(self.OFF_FLAGS, flags)

        return (size, tag)

    def write_response(self, resp_code: int, ptr: int) -> None:
        """寫入分配結果供 C shim 讀取"""
        if not self._mm:
            return
        self._write_u64(self.OFF_RESP_PTR, ptr)
        self._write_u32(self.OFF_RESP_CODE, resp_code)  # 最後寫，作為 ready flag

    # ── Low-level helpers ──

    def _write_u32(self, offset: int, value: int) -> None:
        self._mm.seek(offset)
        self._mm.write(struct.pack("<I", value))

    def _write_u64(self, offset: int, value: int) -> None:
        self._mm.seek(offset)
        self._mm.write(struct.pack("<Q", value))

    def _read_u32(self, offset: int) -> int:
        self._mm.seek(offset)
        return struct.unpack("<I", self._mm.read(4))[0]

    def _read_u64(self, offset: int) -> int:
        self._mm.seek(offset)
        return struct.unpack("<Q", self._mm.read(8))[0]
