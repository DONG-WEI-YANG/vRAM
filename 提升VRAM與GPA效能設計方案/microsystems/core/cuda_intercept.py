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
import platform
import struct
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Dict, Tuple

logger = logging.getLogger(__name__)


class AllocLocation(Enum):
    VRAM = "vram"
    RAM = "ram"
    EXTERNAL = "external"


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

    def __init__(
        self,
        vram_total_bytes: int,
        ram_pool_bytes: int = 0,
        external_pool_bytes: int = 0,
        alloc_threshold_bytes: int = 256 * 1024 * 1024,  # 256MB
        report_extended: bool = True,
    ):
        self._vram_total = vram_total_bytes
        self._ram_pool = ram_pool_bytes
        self._ext_pool = external_pool_bytes
        self._threshold = alloc_threshold_bytes
        self._report_extended = report_extended

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

        self._is_os_windows = platform.system().lower() == "windows"
        self._active = False

        logger.info(
            "CUDAInterceptor initialized: VRAM=%.1fGB, RAM_pool=%.1fGB, External=%.1fGB",
            vram_total_bytes / (1024**3),
            ram_pool_bytes / (1024**3),
            external_pool_bytes / (1024**3),
        )

    # ── Public API ──

    def activate(self) -> None:
        """啟動攔截器，開始監控 CUDA 記憶體分配"""
        self._active = True
        logger.info("CUDA interceptor activated (threshold=%dMB)",
                     self._threshold // (1024 * 1024))

    def deactivate(self) -> None:
        """停用攔截器，釋放所有重定向的記憶體"""
        self._active = False
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

    def intercept_malloc(self, size_bytes: int, tag: str = "") -> Tuple[int, AllocLocation]:
        """
        攔截 cudaMalloc 請求。

        決策邏輯：
        1. 若 VRAM 有足夠空間 → 分配在 VRAM
        2. 若 VRAM 不足且 size > threshold → 嘗試 RAM pool
        3. 若 RAM 也不足 → 嘗試外部儲存
        4. 全部不足 → 拋出 MemoryError

        Returns:
            (virtual_ptr, location) 虛擬指標與實際存放位置
        """
        if not self._active:
            raise RuntimeError("Interceptor not active")

        self._stats.intercept_count += 1

        with self._lock:
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

    def _allocate_block(self, size: int, location: AllocLocation, tag: str) -> int:
        """分配記憶體區塊並回傳虛擬指標"""
        ptr = self._next_virtual_ptr
        self._next_virtual_ptr += size + 4096  # 4KB alignment padding

        # 呼叫對應的 backend 進行實際分配
        if location == AllocLocation.EXTERNAL and self._on_external_alloc:
            self._on_external_alloc(size)
        elif location == AllocLocation.RAM and self._on_ram_alloc:
            self._on_ram_alloc(size)

        block = MemoryBlock(
            ptr=ptr,
            size_bytes=size,
            location=location,
            tag=tag,
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

    # ── LD_PRELOAD shim 產生器 (Linux) ──

    @staticmethod
    def generate_preload_shim_source() -> str:
        """
        產生 Linux LD_PRELOAD 共享函式庫的 C 源碼框架。
        實際編譯需要 CUDA Toolkit headers。
        """
        return '''\
/* libsdvram_cuda.so - CUDA Memory Interception Shim
 * Compile: gcc -shared -fPIC -o libsdvram_cuda.so shim.c -ldl -lcuda
 * Usage:   LD_PRELOAD=./libsdvram_cuda.so python3 your_app.py
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <cuda.h>
#include <cuda_runtime.h>

typedef cudaError_t (*real_cudaMalloc_t)(void**, size_t);
typedef cudaError_t (*real_cudaFree_t)(void*);
typedef CUresult    (*real_cuMemGetInfo_t)(size_t*, size_t*);
typedef CUresult    (*real_cuDeviceTotalMem_t)(size_t*, CUdevice);

static real_cudaMalloc_t       real_cudaMalloc       = NULL;
static real_cudaFree_t         real_cudaFree         = NULL;
static real_cuMemGetInfo_t     real_cuMemGetInfo      = NULL;
static real_cuDeviceTotalMem_t real_cuDeviceTotalMem  = NULL;

static size_t vram_physical_total = 0;
static size_t extended_total      = 0;  /* set by IPC from Python */

__attribute__((constructor))
static void init(void) {
    real_cudaMalloc       = dlsym(RTLD_NEXT, "cudaMalloc");
    real_cudaFree         = dlsym(RTLD_NEXT, "cudaFree");
    real_cuMemGetInfo     = dlsym(RTLD_NEXT, "cuMemGetInfo_v2");
    real_cuDeviceTotalMem = dlsym(RTLD_NEXT, "cuDeviceTotalMem_v2");
    fprintf(stderr, "[SD-VRAM Shim] Loaded. Intercepting CUDA memory APIs.\\n");
}

cudaError_t cudaMalloc(void **devPtr, size_t size) {
    /* TODO: Check if VRAM is full, redirect to external pool via IPC */
    fprintf(stderr, "[SD-VRAM Shim] cudaMalloc(%zu bytes)\\n", size);
    return real_cudaMalloc(devPtr, size);
}

CUresult cuMemGetInfo_v2(size_t *free, size_t *total) {
    CUresult r = real_cuMemGetInfo(free, total);
    if (extended_total > 0) {
        *total = extended_total;
        /* free includes external pool free space */
    }
    return r;
}

CUresult cuDeviceTotalMem_v2(size_t *bytes, CUdevice dev) {
    CUresult r = real_cuDeviceTotalMem(bytes, dev);
    if (extended_total > 0) *bytes = extended_total;
    return r;
}
'''
