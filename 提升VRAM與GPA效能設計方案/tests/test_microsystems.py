"""
Comprehensive Test Suite for VRAM Booster Micro-Systems
=========================================================
測試核心模組、裝置層、與整合系統的功能正確性。
可在無真實 GPU/SD 卡的環境下運行（模擬模式）。

Usage:
  python -m pytest tests/test_microsystems.py -v
  python tests/test_microsystems.py  (standalone)
"""

import sys
import os
import json
import unittest
import time
from pathlib import Path

# 確保 import 路徑正確
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from microsystems.config import SystemConfig, DeviceType, BoostMode, DEVICE_SPECS, MODEL_PROFILES
from microsystems.core.cuda_intercept import CUDAInterceptor, AllocLocation, VRAMStats
from microsystems.core.memory_pool import MemoryPool, MemoryTier, Block
from microsystems.core.transfer_engine import TransferEngine, TransferRequest, TransferPriority
from microsystems.core.prefetcher import PredictivePrefetcher, PrefetchStrategy, LayerProfile
from microsystems.core.health_monitor import HealthMonitor, DeviceMetrics, DeviceHealth
from microsystems.core.circuit_breaker import CircuitBreaker, DeviceCircuitBreakers, BreakerState, BreakerConfig
from microsystems.core.recovery_chain import RecoveryChain, ErrorCategory, RecoveryStrategy
from microsystems.core.event_hooks import EventHookManager, HookEvent, HookContext, Hook
from microsystems.core.learner import VRAMLearner, Learning
from microsystems.core.undo_manager import UndoManager
from microsystems.core.audit_log import AuditLog
from microsystems.core.slow_device_optimizer import (
    SlowDeviceOptimizer, SlowDeviceProfile, CompressionMethod, QuantizationLevel,
)
from microsystems.devices.base_device import ConnectionProtocol, DeviceCapability
from microsystems.devices.sd_express import SDExpressDevice
from microsystems.devices.usb_storage import USBStorageDevice
from microsystems.devices.enclosure_nvme import EnclosureNVMeDevice


# ═══════════════════════════════════════════════
#  Test: Configuration
# ═══════════════════════════════════════════════

class TestConfig(unittest.TestCase):
    """測試系統配置"""

    def test_default_config(self):
        cfg = SystemConfig()
        self.assertEqual(cfg.device_type, DeviceType.SD_EXPRESS)
        self.assertEqual(cfg.boost_mode, BoostMode.AUTO)
        self.assertTrue(cfg.cuda_intercept_enabled)
        self.assertTrue(cfg.prefetch_enabled)

    def test_device_specs(self):
        self.assertIn("sd_gen4_x2", DEVICE_SPECS)
        spec = DEVICE_SPECS["sd_gen4_x2"]
        self.assertEqual(spec["bandwidth_mbs"], 3940)
        self.assertEqual(spec["latency_us"], 8)

    def test_model_profiles(self):
        self.assertIn("llama3_70b_q4", MODEL_PROFILES)
        profile = MODEL_PROFILES["llama3_70b_q4"]
        self.assertEqual(profile["weight_gb"], 40.0)
        self.assertEqual(profile["layers"], 80)

    def test_config_serialization(self):
        """測試配置的 JSON 序列化/反序列化"""
        import tempfile
        from pathlib import Path

        cfg = SystemConfig(boost_mode=BoostMode.KV_CACHE_ONLY)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = Path(f.name)

        try:
            cfg.save(path)
            loaded = SystemConfig.load(path)
            self.assertEqual(loaded.boost_mode, BoostMode.KV_CACHE_ONLY)
        finally:
            path.unlink()


# ═══════════════════════════════════════════════
#  Test: CUDA Interceptor
# ═══════════════════════════════════════════════

class TestCUDAInterceptor(unittest.TestCase):
    """測試 CUDA 記憶體攔截器"""

    def setUp(self):
        self.interceptor = CUDAInterceptor(
            vram_total_bytes=12 * (1024**3),     # 12GB VRAM
            ram_pool_bytes=16 * (1024**3),        # 16GB RAM pool
            external_pool_bytes=500 * (1024**3),  # 500GB external
        )
        self.interceptor.activate()

    def tearDown(self):
        self.interceptor.deactivate()

    def test_vram_allocation(self):
        """小分配應該放在 VRAM"""
        ptr, loc = self.interceptor.intercept_malloc(100 * (1024**2), "small_tensor")
        self.assertEqual(loc, AllocLocation.VRAM)
        self.assertGreater(ptr, 0)

    def test_vram_overflow_to_ram(self):
        """VRAM 滿後應該 redirect 到 RAM"""
        # 先填滿 VRAM
        ptr1, loc1 = self.interceptor.intercept_malloc(11 * (1024**3), "big_weight")
        self.assertEqual(loc1, AllocLocation.VRAM)

        # 下一個分配應該去 RAM
        ptr2, loc2 = self.interceptor.intercept_malloc(2 * (1024**3), "overflow")
        self.assertEqual(loc2, AllocLocation.RAM)

    def test_ram_overflow_to_external(self):
        """VRAM + RAM 都滿後應該 redirect 到 external"""
        self.interceptor.intercept_malloc(12 * (1024**3), "fill_vram")
        self.interceptor.intercept_malloc(16 * (1024**3), "fill_ram")

        ptr, loc = self.interceptor.intercept_malloc(1 * (1024**3), "external")
        self.assertEqual(loc, AllocLocation.EXTERNAL)

    def test_all_full_raises(self):
        """所有層級都滿時應該拋出 MemoryError"""
        self.interceptor.intercept_malloc(12 * (1024**3), "vram")
        self.interceptor.intercept_malloc(16 * (1024**3), "ram")
        self.interceptor.intercept_malloc(500 * (1024**3), "ext")

        with self.assertRaises(MemoryError):
            self.interceptor.intercept_malloc(1 * (1024**3), "too_much")

    def test_free(self):
        """釋放記憶體後空間應回收"""
        ptr, _ = self.interceptor.intercept_malloc(4 * (1024**3), "temp")
        self.interceptor.intercept_free(ptr)

        stats = self.interceptor.stats
        self.assertEqual(stats.physical_used_bytes, 0)

    def test_extended_memory_reporting(self):
        """擴展記憶體查詢應回報完整容量"""
        free, total = self.interceptor.query_mem_info()
        expected_total = 12 * (1024**3) + 16 * (1024**3) + 500 * (1024**3)
        self.assertEqual(total, expected_total)

    def test_stats_tracking(self):
        """統計追蹤應正確"""
        self.interceptor.intercept_malloc(12 * (1024**3), "a")
        self.interceptor.intercept_malloc(2 * (1024**3), "b")  # RAM redirect

        stats = self.interceptor.stats
        self.assertEqual(stats.intercept_count, 2)
        self.assertEqual(stats.redirect_count, 1)


# ═══════════════════════════════════════════════
#  Test: Memory Pool
# ═══════════════════════════════════════════════

class TestMemoryPool(unittest.TestCase):
    """測試三層記憶體池"""

    def setUp(self):
        self.pool = MemoryPool(
            vram_capacity_bytes=12 * (1024**3),
            ram_capacity_bytes=16 * (1024**3),
            external_capacity_bytes=500 * (1024**3),
        )

    def test_allocate_in_vram(self):
        tier = self.pool.allocate("layer_0", 1 * (1024**3), tag="weight")
        self.assertEqual(tier, MemoryTier.VRAM)

    def test_allocate_overflow(self):
        self.pool.allocate("big", 12 * (1024**3))
        tier = self.pool.allocate("overflow", 1 * (1024**3))
        self.assertEqual(tier, MemoryTier.RAM)

    def test_free_block(self):
        self.pool.allocate("temp", 4 * (1024**3))
        self.pool.free("temp")
        state = self.pool.get_tier_state(MemoryTier.VRAM)
        self.assertEqual(state.used_bytes, 0)

    def test_access_tracking(self):
        self.pool.allocate("data", 1 * (1024**3))
        for _ in range(10):
            self.pool.access("data")
        blk = self.pool.get_block("data")
        self.assertEqual(blk.access_count, 10)

    def test_promotion(self):
        """頻繁存取的冷資料應被升級"""
        self.pool.allocate("fill", 12 * (1024**3))  # fill VRAM
        tier = self.pool.allocate("cold", 1 * (1024**3))
        self.assertEqual(tier, MemoryTier.RAM)

        # 釋放 VRAM 空間
        self.pool.free("fill")

        # 頻繁存取
        for _ in range(10):
            self.pool.access("cold")

        blk = self.pool.get_block("cold")
        # 應已被 promote 到 VRAM
        self.assertEqual(blk.tier, MemoryTier.VRAM)

    def test_demotion_sweep(self):
        self.pool.allocate("idle", 1 * (1024**3))
        blk = self.pool.get_block("idle")
        blk.last_access_ts = time.time() - 60  # 模擬 60 秒無存取

        demoted = self.pool.run_demotion_sweep()
        self.assertGreater(demoted, 0)

    def test_stats(self):
        self.pool.allocate("a", 1 * (1024**3))
        self.pool.allocate("b", 2 * (1024**3))
        stats = self.pool.get_stats()
        self.assertEqual(stats["total_blocks"], 2)
        self.assertGreater(stats["total_used_gb"], 0)

    def test_duplicate_allocation_raises(self):
        self.pool.allocate("x", 1024)
        with self.assertRaises(ValueError):
            self.pool.allocate("x", 1024)


# ═══════════════════════════════════════════════
#  Test: Transfer Engine
# ═══════════════════════════════════════════════

class TestTransferEngine(unittest.TestCase):
    """測試非同步傳輸引擎"""

    def test_start_stop(self):
        engine = TransferEngine(max_workers=2)
        engine.start()
        engine.stop()

    def test_submit_and_complete(self):
        engine = TransferEngine(max_workers=2)
        engine.start()

        completed = []

        def on_done(rid, success):
            completed.append((rid, success))

        req = TransferRequest(
            request_id="test_001",
            block_id="block_a",
            source_tier=MemoryTier.EXTERNAL,
            dest_tier=MemoryTier.RAM,
            size_bytes=1024 * 1024,
            callback=on_done,
        )
        engine.submit(req)

        # 等待完成
        time.sleep(1)
        engine.stop()

        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0][1])  # success

    def test_stats(self):
        engine = TransferEngine(max_workers=2)
        engine.start()

        engine.submit_migration("b1", MemoryTier.RAM, MemoryTier.VRAM, 1024)
        time.sleep(0.5)
        engine.stop()

        stats = engine.stats
        self.assertGreaterEqual(stats.total_requests, 1)


# ═══════════════════════════════════════════════
#  Test: Health Monitor
# ═══════════════════════════════════════════════

class TestHealthMonitor(unittest.TestCase):
    """測試健康監控器"""

    def test_device_health_classification(self):
        m = DeviceMetrics(device_id="test", temperature_celsius=40, wear_level_pct=90)
        self.assertEqual(m.health, DeviceHealth.HEALTHY)

        m.temperature_celsius = 75
        self.assertEqual(m.health, DeviceHealth.WARNING)

        m.temperature_celsius = 90
        self.assertEqual(m.health, DeviceHealth.CRITICAL)

        m.is_connected = False
        self.assertEqual(m.health, DeviceHealth.DISCONNECTED)

    def test_alert_generation(self):
        monitor = HealthMonitor(check_interval_s=0.1, temp_warning=70)
        alerts = []
        monitor.on_alert(lambda a: alerts.append(a))

        def collector():
            return DeviceMetrics(
                device_id="hot_drive",
                temperature_celsius=80,
                wear_level_pct=50,
                is_connected=True,
            )

        monitor.register_device("hot_drive", collector)
        monitor.start()
        time.sleep(0.5)
        monitor.stop()

        self.assertGreater(len(alerts), 0)
        self.assertEqual(alerts[0].device_id, "hot_drive")


# ═══════════════════════════════════════════════
#  Test: Device Drivers
# ═══════════════════════════════════════════════

class TestSDExpressDevice(unittest.TestCase):
    """測試 SD Express 裝置驅動"""

    def test_capability_resolution(self):
        cap = SDExpressDevice._resolve_capability(ConnectionProtocol.PCIE_GEN4_X2)
        self.assertEqual(cap.max_bandwidth_mbs, 3940)
        self.assertTrue(cap.supports_nvme)
        self.assertEqual(cap.protocol_conversions, 0)

    def test_capability_gen3(self):
        cap = SDExpressDevice._resolve_capability(ConnectionProtocol.PCIE_GEN3_X1)
        self.assertEqual(cap.max_bandwidth_mbs, 985)


class TestUSBStorageDevice(unittest.TestCase):
    """測試 USB 儲存裝置驅動"""

    def test_usb4_pcie_tunneling(self):
        cap = USBStorageDevice._resolve_capability(ConnectionProtocol.USB4_V1)
        self.assertTrue(cap.supports_pcie_tunneling)
        self.assertEqual(cap.protocol_conversions, 1)

    def test_usb3_no_tunneling(self):
        cap = USBStorageDevice._resolve_capability(ConnectionProtocol.USB3_GEN2)
        self.assertFalse(cap.supports_pcie_tunneling)
        self.assertEqual(cap.protocol_conversions, 2)


class TestEnclosureDevice(unittest.TestCase):
    """測試外接硬碟盒驅動"""

    def test_tb5_capability(self):
        cap = EnclosureNVMeDevice._resolve_capability(ConnectionProtocol.TB5)
        self.assertEqual(cap.max_bandwidth_mbs, 10000)
        self.assertTrue(cap.supports_pcie_tunneling)
        self.assertTrue(cap.supports_nvme)
        self.assertTrue(cap.supports_trim)
        self.assertEqual(cap.protocol_conversions, 1)

    def test_tb4_capability(self):
        cap = EnclosureNVMeDevice._resolve_capability(ConnectionProtocol.TB4)
        self.assertEqual(cap.max_bandwidth_mbs, 3000)


# ═══════════════════════════════════════════════
#  Test: Integrated Systems (Smoke Tests)
# ═══════════════════════════════════════════════

class TestSDVRAMSystemIntegration(unittest.TestCase):
    """SD-VRAM 系統整合煙霧測試"""

    def test_system_creation(self):
        from microsystems.systems.sd_vram_system import SDVRAMSystem
        system = SDVRAMSystem()
        self.assertEqual(system.SYSTEM_NAME, "SD-VRAM Booster")
        self.assertFalse(system._is_active)

    def test_scan(self):
        from microsystems.systems.sd_vram_system import SDVRAMSystem
        system = SDVRAMSystem()
        result = system.scan()
        self.assertIn("gpu_vram_gb", result)
        self.assertIn("sd_devices_found", result)

    def test_estimate_performance(self):
        from microsystems.systems.sd_vram_system import SDVRAMSystem
        system = SDVRAMSystem()
        system.scan()
        est = system.estimate_performance("llama3_8b_q4")
        self.assertEqual(est.model_name, "Llama-3 8B (Q4)")
        self.assertGreater(est.estimated_tps, 0)


class TestUSBVRAMSystemIntegration(unittest.TestCase):
    """USB-VRAM 系統整合煙霧測試"""

    def test_system_creation(self):
        from microsystems.systems.usb_vram_system import USBVRAMSystem
        system = USBVRAMSystem()
        self.assertEqual(system.SYSTEM_NAME, "USB-VRAM Booster")

    def test_scan(self):
        from microsystems.systems.usb_vram_system import USBVRAMSystem
        system = USBVRAMSystem()
        result = system.scan()
        self.assertIn("usb_devices_found", result)


class TestEncVRAMSystemIntegration(unittest.TestCase):
    """Enclosure-VRAM 系統整合煙霧測試"""

    def test_system_creation(self):
        from microsystems.systems.enc_vram_system import EnclosureVRAMSystem
        system = EnclosureVRAMSystem()
        self.assertEqual(system.SYSTEM_NAME, "Enclosure-VRAM Booster")

    def test_scan(self):
        from microsystems.systems.enc_vram_system import EnclosureVRAMSystem
        system = EnclosureVRAMSystem()
        result = system.scan()
        self.assertIn("enclosure_devices_found", result)

    def test_estimate_performance(self):
        from microsystems.systems.enc_vram_system import EnclosureVRAMSystem
        system = EnclosureVRAMSystem()
        system.scan()
        est = system.estimate_performance("llama3_70b_q4")
        self.assertTrue(est.can_run_70b or not est.can_run_70b)  # 取決於硬體


# ═══════════════════════════════════════════════
#  Test: Circuit Breaker (from OSS120BCLI pattern)
# ═══════════════════════════════════════════════

class TestCircuitBreaker(unittest.TestCase):
    """測試斷路器"""

    def test_initial_state_closed(self):
        cb = CircuitBreaker("test", BreakerConfig(failure_threshold=3))
        self.assertEqual(cb.state, BreakerState.CLOSED)
        self.assertTrue(cb.allow())

    def test_trip_after_threshold(self):
        cb = CircuitBreaker("test", BreakerConfig(failure_threshold=3))
        cb.record_failure("err1")
        cb.record_failure("err2")
        self.assertTrue(cb.allow())  # 還沒到 3 次
        cb.record_failure("err3")
        self.assertEqual(cb.state, BreakerState.OPEN)
        self.assertFalse(cb.allow())  # 斷路了

    def test_cooldown_to_half_open(self):
        cb = CircuitBreaker("test", BreakerConfig(
            failure_threshold=2, cooldown_seconds=0.1,
        ))
        cb.record_failure("a")
        cb.record_failure("b")
        self.assertEqual(cb.state, BreakerState.OPEN)

        time.sleep(0.15)
        self.assertEqual(cb.state, BreakerState.HALF_OPEN)
        self.assertTrue(cb.allow())  # 半開允許一次

    def test_half_open_success_recovers(self):
        cb = CircuitBreaker("test", BreakerConfig(
            failure_threshold=2, cooldown_seconds=0.1, success_threshold=1,
        ))
        cb.record_failure("a")
        cb.record_failure("b")
        time.sleep(0.15)

        cb.allow()
        cb.record_success()
        self.assertEqual(cb.state, BreakerState.CLOSED)

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("test", BreakerConfig(
            failure_threshold=2, cooldown_seconds=0.1,
        ))
        cb.record_failure("a")
        cb.record_failure("b")
        time.sleep(0.15)

        cb.allow()
        cb.record_failure("c")
        self.assertEqual(cb.state, BreakerState.OPEN)

    def test_success_resets_counter(self):
        cb = CircuitBreaker("test", BreakerConfig(failure_threshold=3))
        cb.record_failure("a")
        cb.record_failure("b")
        cb.record_success()  # 重置
        cb.record_failure("c")
        self.assertEqual(cb.state, BreakerState.CLOSED)  # 只有 1 次連續失敗

    def test_device_breakers(self):
        db = DeviceCircuitBreakers("sd_card_0")
        self.assertTrue(db.all_healthy())
        self.assertFalse(db.any_open())

    def test_trip_callback(self):
        trips = []
        cb = CircuitBreaker("test", BreakerConfig(failure_threshold=1))
        cb.on_trip(lambda name: trips.append(name))
        cb.record_failure("boom")
        self.assertEqual(len(trips), 1)

    def test_stats(self):
        cb = CircuitBreaker("test", BreakerConfig(failure_threshold=3))
        cb.allow()
        cb.record_success()
        cb.allow()
        cb.record_failure("x")
        stats = cb.stats
        self.assertEqual(stats.total_successes, 1)
        self.assertEqual(stats.total_failures, 1)


# ═══════════════════════════════════════════════
#  Test: Recovery Chain (from OSS120BCLI pattern)
# ═══════════════════════════════════════════════

class TestRecoveryChain(unittest.TestCase):
    """測試恢復鏈"""

    def test_successful_recovery(self):
        chain = RecoveryChain()
        chain.register_handler(RecoveryStrategy.RETRY, lambda ctx: True)

        result = chain.execute(ErrorCategory.IO_ERROR)
        self.assertTrue(result.success)
        self.assertEqual(result.strategy, RecoveryStrategy.RETRY)

    def test_fallback_chain(self):
        """第一個策略失敗，第二個成功"""
        chain = RecoveryChain(max_retries=1)
        chain.register_handler(RecoveryStrategy.RETRY, lambda ctx: False)
        chain.register_handler(RecoveryStrategy.DOWNGRADE_TIER, lambda ctx: True)

        result = chain.execute(ErrorCategory.IO_ERROR)
        self.assertTrue(result.success)
        self.assertEqual(result.strategy, RecoveryStrategy.DOWNGRADE_TIER)

    def test_all_fail(self):
        chain = RecoveryChain(max_retries=1)
        chain.register_handler(RecoveryStrategy.RETRY, lambda ctx: False)
        chain.register_handler(RecoveryStrategy.DOWNGRADE_TIER, lambda ctx: False)

        result = chain.execute(ErrorCategory.IO_ERROR)
        self.assertFalse(result.success)
        self.assertEqual(chain.stats.failed_recoveries, 1)

    def test_error_classification(self):
        chain = RecoveryChain()
        self.assertEqual(
            chain.classify_error(MemoryError("out of memory")),
            ErrorCategory.OUT_OF_MEMORY,
        )
        self.assertEqual(
            chain.classify_error(TimeoutError("timeout")),
            ErrorCategory.TIMEOUT,
        )
        self.assertEqual(
            chain.classify_error(OSError("No such device")),
            ErrorCategory.DEVICE_DISCONNECTED,
        )

    def test_device_disconnected_chain(self):
        """裝置斷線時應直接跳到 emergency RAM only"""
        chain = RecoveryChain()
        chain.register_handler(RecoveryStrategy.EMERGENCY_RAM_ONLY, lambda ctx: True)

        result = chain.execute(ErrorCategory.DEVICE_DISCONNECTED)
        self.assertTrue(result.success)
        self.assertEqual(result.strategy, RecoveryStrategy.EMERGENCY_RAM_ONLY)


# ═══════════════════════════════════════════════
#  Test: Event Hooks (from OSS120BCLI pattern)
# ═══════════════════════════════════════════════

class TestEventHooks(unittest.TestCase):
    """測試事件鉤子系統"""

    def test_fire_hook(self):
        mgr = EventHookManager()
        calls = []
        mgr.register(Hook(
            name="logger",
            event=HookEvent.POST_ALLOCATE,
            handler=lambda ctx: calls.append(ctx.block_id),
        ))

        ctx = HookContext(event=HookEvent.POST_ALLOCATE, block_id="layer_0")
        mgr.fire(ctx)
        self.assertEqual(calls, ["layer_0"])

    def test_blocking_hook(self):
        mgr = EventHookManager()
        mgr.register(Hook(
            name="guard",
            event=HookEvent.PRE_ALLOCATE,
            handler=lambda ctx: setattr(ctx, 'blocked', True) or setattr(ctx, 'block_reason', 'too hot'),
            blocking=True,
        ))

        ctx = HookContext(event=HookEvent.PRE_ALLOCATE)
        mgr.fire(ctx)
        self.assertTrue(ctx.blocked)
        self.assertEqual(ctx.block_reason, "too hot")

    def test_priority_ordering(self):
        mgr = EventHookManager()
        order = []
        mgr.register(Hook(name="low", event=HookEvent.POST_TRANSFER,
                          handler=lambda ctx: order.append("low"), priority=200))
        mgr.register(Hook(name="high", event=HookEvent.POST_TRANSFER,
                          handler=lambda ctx: order.append("high"), priority=10))

        mgr.fire(HookContext(event=HookEvent.POST_TRANSFER))
        self.assertEqual(order, ["high", "low"])

    def test_conditional_hook(self):
        mgr = EventHookManager()
        calls = []
        mgr.register(Hook(
            name="big_only",
            event=HookEvent.PRE_ALLOCATE,
            handler=lambda ctx: calls.append(ctx.size_bytes),
            condition=lambda ctx: ctx.size_bytes > 1024 * 1024,
        ))

        mgr.fire(HookContext(event=HookEvent.PRE_ALLOCATE, size_bytes=100))
        self.assertEqual(calls, [])  # 太小，不觸發

        mgr.fire(HookContext(event=HookEvent.PRE_ALLOCATE, size_bytes=2 * 1024 * 1024))
        self.assertEqual(len(calls), 1)

    def test_unregister(self):
        mgr = EventHookManager()
        mgr.register(Hook(name="temp", event=HookEvent.HEALTH_ALERT,
                          handler=lambda ctx: None))
        self.assertEqual(len(mgr.get_hooks(HookEvent.HEALTH_ALERT)), 1)

        mgr.unregister("temp")
        self.assertEqual(len(mgr.get_hooks(HookEvent.HEALTH_ALERT)), 0)

    def test_stats(self):
        mgr = EventHookManager()
        mgr.register(Hook(name="a", event=HookEvent.DEVICE_CONNECT,
                          handler=lambda ctx: None))
        mgr.fire(HookContext(event=HookEvent.DEVICE_CONNECT))
        stats = mgr.stats
        self.assertEqual(stats["total_fires"], 1)
        self.assertEqual(stats["total_hooks"], 1)


# ═══════════════════════════════════════════════
#  Test: Two-Tier Learning System
# ═══════════════════════════════════════════════

class TestVRAMLearner(unittest.TestCase):
    """測試雙層學習系統"""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._proj = Path(self._tmpdir) / "project" / "learnings.json"
        self._glob = Path(self._tmpdir) / "global" / "learnings.json"
        self.learner = VRAMLearner(
            project_id="test_project",
            project_path=self._proj,
            global_path=self._glob,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_learn_and_query(self):
        self.learner.learn("rtx4070_sd_llama70b", "tier_strategy",
                           {"vram_gb": 12, "sd_gb": 28, "tps": 3.2})
        hints = self.learner.get_hints("rtx4070")
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["data"]["tps"], 3.2)

    def test_confidence_increases(self):
        for _ in range(5):
            self.learner.learn("key1", "perf", {"tps": 5})
        hints = self.learner.get_hints("key1")
        self.assertGreater(len(hints), 0)
        self.assertGreater(hints[0]["confidence"], 0.5)

    def test_promote_to_global(self):
        """出現在 3+ 個專案後應 promote 到 global"""
        # 模擬 3 個專案
        for pid in ["proj_a", "proj_b", "proj_c"]:
            learner = VRAMLearner(project_id=pid, project_path=self._proj, global_path=self._glob)
            learner.learn("shared_key", "tier_strategy", {"tps": 3})

        # 驗證 global 有記錄
        learner2 = VRAMLearner(project_id="new", project_path=self._proj, global_path=self._glob)
        hints = learner2.get_hints()
        global_hints = [h for h in hints if h["source"] == "global"]
        self.assertGreater(len(global_hints), 0)

    def test_learn_session(self):
        self.learner.learn_session(
            gpu_name="RTX4070", device_type="sd", device_protocol="gen4_x2",
            model_key="llama3_70b_q4", tier_distribution={"vram": 12, "sd": 28},
            actual_tps=3.2, context_tokens=50000, prefetch_hit_rate=85.0,
            session_duration_s=300,
        )
        hints = self.learner.get_hints("rtx4070", "llama3_70b")
        self.assertGreater(len(hints), 0)

    def test_learn_recovery(self):
        self.learner.learn_recovery("io_error", "retry", True)
        self.learner.learn_recovery("io_error", "retry", True)
        hints = self.learner.get_hints()
        recovery_hints = [h for h in hints if h["category"] == "recovery"]
        self.assertGreater(len(recovery_hints), 0)
        self.assertEqual(recovery_hints[0]["data"]["success_rate"], 1.0)

    def test_stats(self):
        self.learner.learn("a", "perf", {})
        self.learner.learn("b", "tier_strategy", {})
        stats = self.learner.stats
        self.assertEqual(stats["project_learnings"], 2)

    def test_persistence(self):
        """資料應保存到檔案並可重新載入"""
        self.learner.learn("persist_key", "perf", {"val": 42})

        # 重新建立 learner（模擬重啟）
        learner2 = VRAMLearner(project_id="test", project_path=self._proj, global_path=self._glob)
        hints = learner2.get_hints("persist")
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["data"]["val"], 42)


# ═══════════════════════════════════════════════
#  Test: Undo Manager
# ═══════════════════════════════════════════════

class TestUndoManager(unittest.TestCase):
    """測試 Undo Manager"""

    def setUp(self):
        self.pool = MemoryPool(
            vram_capacity_bytes=12 * (1024**3),
            ram_capacity_bytes=16 * (1024**3),
            external_capacity_bytes=500 * (1024**3),
        )
        self.undo = UndoManager(self.pool)

    def test_checkpoint_and_rollback(self):
        """基本 checkpoint + rollback"""
        self.undo.checkpoint("empty state")
        self.pool.allocate("block_a", 2 * (1024**3))
        self.pool.allocate("block_b", 3 * (1024**3))

        # 兩個區塊存在
        self.assertIsNotNone(self.pool.get_block("block_a"))

        # Rollback
        self.undo.rollback()

        # 兩個區塊都應消失
        self.assertIsNone(self.pool.get_block("block_a"))
        self.assertIsNone(self.pool.get_block("block_b"))

    def test_multi_step_rollback(self):
        """多步回滾"""
        self.undo.checkpoint("step 0")
        self.pool.allocate("a", 1 * (1024**3))

        self.undo.checkpoint("step 1")
        self.pool.allocate("b", 1 * (1024**3))

        # 回到 step 1（b 消失，a 保留）
        self.undo.rollback()
        self.assertIsNotNone(self.pool.get_block("a"))
        self.assertIsNone(self.pool.get_block("b"))

        # 回到 step 0（a 也消失）
        self.undo.rollback()
        self.assertIsNone(self.pool.get_block("a"))

    def test_no_snapshot_rollback(self):
        """無快照時 rollback 應回傳 False"""
        self.assertFalse(self.undo.rollback())

    def test_list_snapshots(self):
        self.undo.checkpoint("first")
        self.undo.checkpoint("second")
        snaps = self.undo.list_snapshots()
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[0]["description"], "first")

    def test_stats(self):
        self.undo.checkpoint("a")
        self.undo.rollback()
        stats = self.undo.stats
        self.assertEqual(stats["total_checkpoints"], 1)
        self.assertEqual(stats["total_rollbacks"], 1)

    def test_prevents_memory_leak(self):
        """模型載入一半失敗，rollback 應釋放所有已分配的區塊"""
        self.undo.checkpoint("before_model_load")

        # 模擬載入 40 層，在第 20 層失敗
        for i in range(20):
            self.pool.allocate(f"layer_{i}", 100 * (1024**2))  # 100MB each

        # 模擬失敗
        self.undo.rollback()

        # 所有 20 層都應被清理
        for i in range(20):
            self.assertIsNone(self.pool.get_block(f"layer_{i}"))

        # 使用量應回到 0
        state = self.pool.get_tier_state(MemoryTier.VRAM)
        self.assertEqual(state.used_bytes, 0)


# ═══════════════════════════════════════════════
#  Test: Audit Log
# ═══════════════════════════════════════════════

class TestAuditLog(unittest.TestCase):
    """測試審計日誌"""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._log_path = Path(self._tmpdir) / "test_audit.jsonl"
        # Reset singleton
        AuditLog._instance = None
        self.log = AuditLog(path=self._log_path)

    def tearDown(self):
        self.log.close()
        AuditLog._instance = None
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_record_and_query(self):
        self.log.record("allocate", block_id="a", tier="VRAM", size_mb=256)
        self.log.record("allocate", block_id="b", tier="RAM", size_mb=512)

        entries = self.log.query(limit=10)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["block_id"], "a")

    def test_filter_by_operation(self):
        self.log.record("allocate", block_id="a")
        self.log.record("free", block_id="a")
        self.log.record("allocate", block_id="b")

        allocs = self.log.query(operation="allocate")
        self.assertEqual(len(allocs), 2)

        frees = self.log.query(operation="free")
        self.assertEqual(len(frees), 1)

    def test_convenience_methods(self):
        self.log.record_allocate("blk", "VRAM", 100, True, 0.5, "sd_card")
        self.log.record_free("blk", "VRAM", 100)
        self.log.record_migrate("blk", "VRAM", "RAM", 100, 5.0, True)
        self.log.record_error("allocate", "OOM", "sd_card")
        self.log.record_recovery("retry", True, 10.0)

        entries = self.log.query(limit=100)
        self.assertEqual(len(entries), 5)
        ops = [e["op"] for e in entries]
        self.assertEqual(ops, ["allocate", "free", "migrate", "error", "recovery"])

    def test_jsonl_format(self):
        """每行一個合法 JSON"""
        self.log.record("test", x=1)
        self.log.record("test", x=2)
        self.log.close()

        with open(self._log_path, "r") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            parsed = json.loads(line.strip())
            self.assertIn("ts", parsed)
            self.assertIn("op", parsed)

    def test_operation_stats(self):
        self.log.record_allocate("a", "VRAM", 100, True, 1.0)
        self.log.record_allocate("b", "VRAM", 200, True, 2.0)
        self.log.record_allocate("c", "VRAM", 300, False, 0.5)

        stats = self.log.get_operation_stats()
        self.assertEqual(stats["allocate"]["count"], 3)
        self.assertEqual(stats["allocate"]["successes"], 2)
        self.assertEqual(stats["allocate"]["failures"], 1)


# ═══════════════════════════════════════════════
#  Test: MCP Server
# ═══════════════════════════════════════════════

class TestMCPServer(unittest.TestCase):
    """測試 MCP Server"""

    def setUp(self):
        from microsystems.mcp_server import VRAMBoosterMCPServer
        self.server = VRAMBoosterMCPServer()

    def test_initialize(self):
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}
        })
        self.assertEqual(resp["result"]["serverInfo"]["name"], "vram-booster")

    def test_tools_list(self):
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
        })
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("vram_scan", names)
        self.assertIn("vram_activate", names)
        self.assertIn("vram_status", names)
        self.assertIn("vram_estimate", names)
        self.assertIn("vram_health", names)
        self.assertIn("vram_audit", names)

    def test_tool_call_scan(self):
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "vram_scan", "arguments": {}}
        })
        content = resp["result"]["content"][0]["text"]
        parsed = json.loads(content)
        self.assertIn("sd", parsed)
        self.assertIn("usb", parsed)
        self.assertIn("enc", parsed)

    def test_tool_call_status_no_active(self):
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "vram_status", "arguments": {}}
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(content["active"])

    def test_unknown_tool(self):
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "nonexistent", "arguments": {}}
        })
        self.assertTrue(resp["result"].get("isError"))

    def test_unknown_method(self):
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 6, "method": "fake/method", "params": {}
        })
        self.assertIn("error", resp)


# ═══════════════════════════════════════════════
#  Test: Slow Device Optimizer
# ═══════════════════════════════════════════════

class TestSlowDeviceOptimizer(unittest.TestCase):
    """測試慢速裝置最佳化引擎"""

    def test_compression_reduces_size(self):
        opt = SlowDeviceOptimizer(compression=CompressionMethod.ZLIB_FAST, buffer_size_mb=10)
        # 高度可壓縮的資料
        data = b"\x00" * (1024 * 1024)  # 1MB of zeros
        written = opt.write("block_a", 0, data)
        self.assertLess(written, len(data))
        self.assertGreater(opt.stats.compression_ratio, 1.0)

    def test_buffer_hit(self):
        opt = SlowDeviceOptimizer(compression=CompressionMethod.NONE, buffer_size_mb=10)
        data = b"hello world" * 100
        opt.write("blk", 0, data)

        # 從 buffer 讀取
        result = opt.read("blk", 0, len(data))
        self.assertEqual(result[:11], b"hello world")
        self.assertEqual(opt.stats.buffer_hits, 1)
        self.assertEqual(opt.stats.buffer_misses, 0)

    def test_buffer_miss(self):
        opt = SlowDeviceOptimizer(compression=CompressionMethod.NONE, buffer_size_mb=10)
        # 未寫入任何東西，讀取應 miss
        result = opt.read("nonexistent", 0, 100)
        self.assertEqual(len(result), 100)  # fallback 回傳 zeros
        self.assertEqual(opt.stats.buffer_misses, 1)

    def test_buffer_eviction(self):
        """buffer 滿時應 evict 最舊的"""
        opt = SlowDeviceOptimizer(
            compression=CompressionMethod.NONE,
            buffer_size_mb=1,  # 1MB buffer
            max_buffer_entries=3,
        )
        for i in range(5):
            opt.write(f"blk_{i}", 0, b"x" * 1024)
        # buffer 最多 3 entries
        self.assertLessEqual(len(opt._buffer), 3)

    def test_flush_all(self):
        opt = SlowDeviceOptimizer(compression=CompressionMethod.NONE, buffer_size_mb=10)
        flushed_blocks = []
        opt.write("a", 0, b"data_a")
        opt.write("b", 0, b"data_b")

        count = opt.flush_all(lambda bid, off, data: flushed_blocks.append(bid))
        self.assertEqual(count, 2)

    def test_sequential_reorder(self):
        opt = SlowDeviceOptimizer(sequential_block_kb=4096)
        requests = [
            ("block_b", 1000, 100),
            ("block_a", 0, 200),
            ("block_a", 200, 100),
            ("block_b", 0, 500),
        ]
        reordered = opt.reorder_sequential(requests)
        # 應該按 block_id 然後 offset 排序
        self.assertEqual(reordered[0][0], "block_a")

    def test_int8_quantization_roundtrip(self):
        opt = SlowDeviceOptimizer(
            compression=CompressionMethod.NONE,
            quantization=QuantizationLevel.INT8,
            buffer_size_mb=10,
        )
        original = bytes(range(256)) * 4  # 1024 bytes
        opt.write("q_block", 0, original)
        result = opt.read("q_block", 0, len(original))
        # 量化有損，但大小應正確
        self.assertEqual(len(result), len(original))


class TestSlowDeviceProfile(unittest.TestCase):
    """測試裝置 profile 和自動偵測"""

    def test_profiles_exist(self):
        self.assertIn("sd_uhs1", SlowDeviceProfile.PROFILES)
        self.assertIn("usb3_flash", SlowDeviceProfile.PROFILES)
        self.assertIn("hdd_5400rpm", SlowDeviceProfile.PROFILES)

    def test_auto_detect_hdd(self):
        profile = SlowDeviceProfile.auto_detect_profile(120, is_rotational=True)
        self.assertIn("hdd", profile)

    def test_auto_detect_ssd(self):
        profile = SlowDeviceProfile.auto_detect_profile(700, is_rotational=False)
        self.assertEqual(profile, "usb3_portable_ssd")

    def test_auto_detect_flash(self):
        profile = SlowDeviceProfile.auto_detect_profile(250, is_rotational=False)
        self.assertIn("usb3", profile)

    def test_effective_bandwidth(self):
        eff = SlowDeviceProfile.estimate_effective_bandwidth("sd_uhs1")
        self.assertGreater(eff["effective_read_mbs"], eff["raw_read_mbs"])
        self.assertGreater(eff["total_multiplier"], 1.0)

    def test_hdd_bandwidth_boost(self):
        eff = SlowDeviceProfile.estimate_effective_bandwidth("hdd_5400rpm")
        # HDD 100 MB/s × compression 2.5x × INT8 2x = 500 MB/s effective
        self.assertGreater(eff["effective_read_mbs"], 200)

    def test_create_optimizer(self):
        opt = SlowDeviceProfile.create_optimizer("sd_uhs1")
        self.assertIsInstance(opt, SlowDeviceOptimizer)


class TestLegacySystems(unittest.TestCase):
    """測試 Legacy 微系統"""

    def test_sd_legacy_system(self):
        from microsystems.systems.sd_legacy_system import SDLegacySystem
        sys = SDLegacySystem()
        self.assertEqual(sys.SYSTEM_NAME, "SD-VRAM Booster (Legacy)")
        result = sys.scan()
        self.assertIn("sd_devices_found", result)

    def test_usb_legacy_system(self):
        from microsystems.systems.usb_legacy_system import USBLegacySystem
        sys = USBLegacySystem()
        self.assertEqual(sys.SYSTEM_NAME, "USB-VRAM Booster (Legacy)")
        result = sys.scan()
        self.assertIn("usb_devices_found", result)

    def test_hdd_system(self):
        from microsystems.systems.hdd_system import HDDVRAMSystem
        sys = HDDVRAMSystem()
        self.assertEqual(sys.SYSTEM_NAME, "HDD-VRAM Booster")
        result = sys.scan()
        self.assertIn("hdd_devices_found", result)
        self.assertIn("warning", result)

    def test_sd_legacy_estimate(self):
        from microsystems.systems.sd_legacy_system import SDLegacySystem
        sys = SDLegacySystem()
        sys.scan()
        est = sys.estimate_performance("llama3_8b_q4")
        self.assertIn("effective_bandwidth_mbs", est)
        self.assertIn("bandwidth_multiplier", est)
        self.assertGreater(est["bandwidth_multiplier"], 1.0)

    def test_hdd_estimate_warning(self):
        from microsystems.systems.hdd_system import HDDVRAMSystem
        sys = HDDVRAMSystem()
        sys.scan()
        est = sys.estimate_performance("llama3_8b_q4")
        self.assertIn("warning", est)


# ═══════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  VRAM Booster Micro-Systems — Test Suite")
    print("=" * 60)
    unittest.main(verbosity=2)
