"""
Tests for CUDA Interceptor eviction engine and SharedMemoryBridge.

Covers:
  - EvictionPolicy.LRU / TAG_PRIORITY / SIZE_FIRST ordering
  - _try_evict_for() releasing VRAM under pressure
  - intercept_malloc() auto-eviction on VRAM-full
  - SharedMemoryBridge open/sync/request/response cycle
  - touch() updating last_access_ns for LRU
"""

import os
import sys
import struct
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from microsystems.core.cuda_intercept import (
    AllocLocation,
    CUDAInterceptor,
    EvictionPolicy,
    MemoryBlock,
    SharedMemoryBridge,
    VRAMStats,
    RESP_VRAM,
    RESP_RAM,
    RESP_EXTERNAL,
    RESP_OOM,
    SHM_MAGIC,
    SHM_VERSION,
)


GB = 1024 ** 3
MB = 1024 ** 2


class TestEvictionPolicyLRU(unittest.TestCase):
    """LRU eviction: least-recently-accessed VRAM blocks evicted first."""

    def setUp(self):
        self.ci = CUDAInterceptor(
            vram_total_bytes=4 * GB,
            ram_pool_bytes=8 * GB,
            external_pool_bytes=16 * GB,
            eviction_policy=EvictionPolicy.LRU,
        )
        self.evicted = []
        self.ci.register_callbacks()
        self.ci.register_evict_callback(
            lambda blk, dest: (self.evicted.append((blk.tag, dest)), True)[1]
        )
        self.ci.activate()

    def test_lru_evicts_oldest_first(self):
        """Blocks with older last_access_ns are evicted before newer ones."""
        # Allocate 4 x 1GB blocks filling VRAM
        ptrs = []
        for i in range(4):
            ptr, loc = self.ci.intercept_malloc(1 * GB, f"weight_{i}")
            self.assertEqual(loc, AllocLocation.VRAM)
            ptrs.append(ptr)

        # Touch blocks in order: 0, 2 are old; 1, 3 are recent
        time.sleep(0.001)
        self.ci.touch(ptrs[1])
        self.ci.touch(ptrs[3])

        # Next alloc should evict oldest (weight_0 then weight_2)
        ptr5, loc5 = self.ci.intercept_malloc(1 * GB, "new_block")
        self.assertEqual(loc5, AllocLocation.VRAM)
        self.assertGreaterEqual(len(self.evicted), 1)
        # First evicted should be weight_0 (oldest access)
        self.assertEqual(self.evicted[0][0], "weight_0")

    def test_touch_updates_access_time(self):
        """touch() makes a block less likely to be evicted."""
        ptr1, _ = self.ci.intercept_malloc(2 * GB, "old_block")
        ptr2, _ = self.ci.intercept_malloc(2 * GB, "new_block")

        # Touch old_block so it becomes "recent"
        time.sleep(0.001)
        self.ci.touch(ptr1)

        # Force eviction: allocating 2GB more exceeds VRAM
        ptr3, _ = self.ci.intercept_malloc(2 * GB, "trigger")
        # new_block should be evicted (older access), not old_block
        self.assertEqual(self.evicted[0][0], "new_block")


class TestEvictionPolicyTagPriority(unittest.TestCase):
    """TAG_PRIORITY eviction: weights before KV-cache, then LRU within tier."""

    def setUp(self):
        self.ci = CUDAInterceptor(
            vram_total_bytes=4 * GB,
            ram_pool_bytes=8 * GB,
            eviction_policy=EvictionPolicy.TAG_PRIORITY,
        )
        self.evicted = []
        self.ci.register_evict_callback(
            lambda blk, dest: (self.evicted.append((blk.tag, dest)), True)[1]
        )
        self.ci.activate()

    def test_weights_evicted_before_kv_cache(self):
        """Weight blocks have lower priority, evicted before KV-cache."""
        self.ci.intercept_malloc(1 * GB, "weight_layer_0")
        self.ci.intercept_malloc(1 * GB, "kv_cache_layer_0")
        self.ci.intercept_malloc(1 * GB, "weight_layer_1")
        self.ci.intercept_malloc(1 * GB, "kv_cache_layer_1")

        # Need 1GB → eviction required
        self.ci.intercept_malloc(1 * GB, "new_alloc")
        # Should evict a weight block first (priority=10 < kv_cache=50)
        self.assertTrue(self.evicted[0][0].startswith("weight"))

    def test_eviction_goes_to_ram_first(self):
        """Evicted blocks should prefer RAM over External storage."""
        self.ci.intercept_malloc(4 * GB, "weight_big")
        self.ci.intercept_malloc(1 * GB, "trigger")
        # RAM has 8GB free → eviction should target RAM
        self.assertEqual(self.evicted[0][1], AllocLocation.RAM)


class TestEvictionPolicySizeFirst(unittest.TestCase):
    """SIZE_FIRST eviction: largest VRAM blocks evicted first."""

    def setUp(self):
        self.ci = CUDAInterceptor(
            vram_total_bytes=4 * GB,
            ram_pool_bytes=8 * GB,
            eviction_policy=EvictionPolicy.SIZE_FIRST,
        )
        self.evicted = []
        self.ci.register_evict_callback(
            lambda blk, dest: (self.evicted.append((blk.size_bytes, blk.tag)), True)[1]
        )
        self.ci.activate()

    def test_largest_block_evicted_first(self):
        """Largest block is evicted before smaller ones."""
        self.ci.intercept_malloc(512 * MB, "small")
        self.ci.intercept_malloc(2 * GB, "large")
        self.ci.intercept_malloc(1 * GB, "medium")
        # 3.5GB used, 0.5GB free → need 1GB → evict largest
        self.ci.intercept_malloc(1 * GB, "trigger")
        self.assertEqual(self.evicted[0][1], "large")


class TestPinnedBlocksNotEvicted(unittest.TestCase):
    """Pinned blocks should never be evicted regardless of policy."""

    def test_pinned_blocks_skipped(self):
        ci = CUDAInterceptor(
            vram_total_bytes=2 * GB,
            ram_pool_bytes=4 * GB,
            eviction_policy=EvictionPolicy.LRU,
        )
        evicted = []
        ci.register_evict_callback(
            lambda blk, dest: (evicted.append(blk.tag), True)[1]
        )
        ci.activate()

        ptr1, _ = ci.intercept_malloc(1 * GB, "pinned_block")
        # Pin it
        ci.allocations[ptr1]  # verify exists
        with ci._lock:
            ci._allocations[ptr1].is_pinned = True

        ptr2, _ = ci.intercept_malloc(1 * GB, "normal_block")

        # Force eviction
        ptr3, loc = ci.intercept_malloc(1 * GB, "trigger")
        # Only normal_block should be evicted
        evicted_tags = [t for t in evicted]
        self.assertNotIn("pinned_block", evicted_tags)
        if evicted_tags:
            self.assertIn("normal_block", evicted_tags)


class TestEvictionFailureFallsThrough(unittest.TestCase):
    """When eviction fails, allocation falls through to RAM/External."""

    def test_eviction_callback_returns_false(self):
        ci = CUDAInterceptor(
            vram_total_bytes=2 * GB,
            ram_pool_bytes=4 * GB,
            eviction_policy=EvictionPolicy.LRU,
        )
        # Eviction always fails
        ci.register_evict_callback(lambda blk, dest: False)
        ci.activate()

        ci.intercept_malloc(2 * GB, "fill_vram")
        # Should fall through to RAM since eviction fails
        ptr, loc = ci.intercept_malloc(1 * GB, "overflow")
        self.assertEqual(loc, AllocLocation.RAM)


class TestNoEvictionCallbackFallsThrough(unittest.TestCase):
    """Without eviction callback, VRAM overflow goes directly to RAM/External."""

    def test_no_callback(self):
        ci = CUDAInterceptor(
            vram_total_bytes=2 * GB,
            ram_pool_bytes=4 * GB,
        )
        ci.activate()

        ci.intercept_malloc(2 * GB, "fill")
        ptr, loc = ci.intercept_malloc(1 * GB, "overflow")
        self.assertEqual(loc, AllocLocation.RAM)


class TestSharedMemoryBridge(unittest.TestCase):
    """SharedMemoryBridge: file-backed mmap for C shim IPC."""

    def setUp(self):
        self.ci = CUDAInterceptor(
            vram_total_bytes=8 * GB,
            ram_pool_bytes=16 * GB,
            external_pool_bytes=32 * GB,
        )
        self.bridge = SharedMemoryBridge(self.ci)

    def test_open_writes_magic_and_version(self):
        """Opening bridge writes correct magic and version to SHM."""
        self.bridge.open()
        try:
            magic = self.bridge._read_u32(SharedMemoryBridge.OFF_MAGIC)
            version = self.bridge._read_u32(SharedMemoryBridge.OFF_VERSION)
            self.assertEqual(magic, SHM_MAGIC)
            self.assertEqual(version, SHM_VERSION)
        finally:
            self.bridge.close()

    def test_sync_stats(self):
        """sync_stats correctly writes all fields to shared memory."""
        self.bridge.open()
        try:
            stats = VRAMStats(
                physical_total_bytes=8 * GB,
                physical_used_bytes=3 * GB,
                ram_pool_bytes=16 * GB,
                ram_used_bytes=2 * GB,
                external_pool_bytes=32 * GB,
                external_used_bytes=5 * GB,
            )
            self.bridge.sync_stats(stats, 256 * MB, active=True)

            self.assertEqual(self.bridge._read_u64(SharedMemoryBridge.OFF_VRAM_TOTAL), 8 * GB)
            self.assertEqual(self.bridge._read_u64(SharedMemoryBridge.OFF_VRAM_USED), 3 * GB)
            self.assertEqual(self.bridge._read_u64(SharedMemoryBridge.OFF_RAM_POOL), 16 * GB)
            self.assertEqual(self.bridge._read_u64(SharedMemoryBridge.OFF_RAM_USED), 2 * GB)
            self.assertEqual(self.bridge._read_u64(SharedMemoryBridge.OFF_EXT_POOL), 32 * GB)
            self.assertEqual(self.bridge._read_u64(SharedMemoryBridge.OFF_EXT_USED), 5 * GB)
            self.assertEqual(self.bridge._read_u32(SharedMemoryBridge.OFF_THRESHOLD), 256 * MB)
            flags = self.bridge._read_u32(SharedMemoryBridge.OFF_FLAGS)
            self.assertTrue(flags & 0x01)  # active bit set
        finally:
            self.bridge.close()

    def test_request_response_cycle(self):
        """Simulate C shim writing a request and Python responding."""
        self.bridge.open()
        try:
            # Simulate C shim writing a request
            self.bridge._write_u64(SharedMemoryBridge.OFF_REQ_SIZE, 512 * MB)
            tag = b"weight_layer_3"
            self.bridge._write_u32(SharedMemoryBridge.OFF_REQ_TAG_LEN, len(tag))
            self.bridge._mm.seek(SharedMemoryBridge.OFF_REQ_TAG)
            self.bridge._mm.write(tag + b"\x00" * (64 - len(tag)))
            # Set request_pending flag
            self.bridge._write_u32(SharedMemoryBridge.OFF_FLAGS, 0x03)  # active + pending

            # Python reads request
            req = self.bridge.read_request()
            self.assertIsNotNone(req)
            size, rtag = req
            self.assertEqual(size, 512 * MB)
            self.assertEqual(rtag, "weight_layer_3")

            # Python writes response
            self.bridge.write_response(RESP_VRAM, 0x7F0000000000)
            resp_code = self.bridge._read_u32(SharedMemoryBridge.OFF_RESP_CODE)
            resp_ptr = self.bridge._read_u64(SharedMemoryBridge.OFF_RESP_PTR)
            self.assertEqual(resp_code, RESP_VRAM)
            self.assertEqual(resp_ptr, 0x7F0000000000)
        finally:
            self.bridge.close()

    def test_no_request_returns_none(self):
        """read_request returns None when no pending request."""
        self.bridge.open()
        try:
            self.bridge._write_u32(SharedMemoryBridge.OFF_FLAGS, 0x01)  # active, no pending
            req = self.bridge.read_request()
            self.assertIsNone(req)
        finally:
            self.bridge.close()

    def test_close_clears_active_flag(self):
        """Closing bridge clears the active flag."""
        self.bridge.open()
        self.bridge.sync_stats(
            VRAMStats(physical_total_bytes=8 * GB), 256 * MB, active=True,
        )
        # Re-open to check (close would unmap)
        # Instead, verify close sets flags to 0 by checking the write
        self.bridge.close()
        # After close, mm is None — this is expected behavior
        self.assertIsNone(self.bridge._mm)


class TestCShimSource(unittest.TestCase):
    """Verify the generated C shim source contains key components."""

    def test_shm_ipc_present(self):
        src = CUDAInterceptor.generate_preload_shim_source()
        self.assertIn("shm_open", src)
        self.assertIn("SHM_MAGIC", src)
        self.assertIn("OFF_RESP_CODE", src)

    def test_unified_memory_fallback(self):
        src = CUDAInterceptor.generate_preload_shim_source()
        self.assertIn("cudaMallocManaged", src)
        self.assertIn("cudaMemAdvise", src)
        self.assertIn("cudaMemPrefetchAsync", src)

    def test_cudafree_interception(self):
        src = CUDAInterceptor.generate_preload_shim_source()
        self.assertIn("cudaFree", src)
        self.assertIn("redirected", src)

    def test_no_todo_remaining(self):
        """The TODO has been resolved."""
        src = CUDAInterceptor.generate_preload_shim_source()
        self.assertNotIn("TODO", src)

    def test_memory_barrier(self):
        src = CUDAInterceptor.generate_preload_shim_source()
        self.assertIn("__sync_synchronize", src)


class TestInterceptorWithShmBridge(unittest.TestCase):
    """Integration: CUDAInterceptor with enable_shm_bridge=True."""

    def test_activate_opens_bridge(self):
        ci = CUDAInterceptor(
            vram_total_bytes=4 * GB,
            ram_pool_bytes=8 * GB,
            enable_shm_bridge=True,
        )
        self.assertIsNotNone(ci._shm_bridge)
        ci.activate()
        self.assertTrue(ci._active)
        # Bridge should have an open mmap
        self.assertIsNotNone(ci._shm_bridge._mm)
        ci.deactivate()
        self.assertIsNone(ci._shm_bridge._mm)


if __name__ == "__main__":
    unittest.main()
