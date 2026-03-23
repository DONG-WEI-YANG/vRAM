"""
External HDD VRAM System — 外接硬碟也能用（但只能做特定事）
==========================================================
HDD 的物理限制決定了它只能做兩件事：
  1. 模型權重的一次性大塊載入（startup 時）
  2. KV Cache 的定期 checkpoint（批次寫入）

絕對不能做的：
  - 即時 KV Cache 讀寫（隨機 I/O = 0.5 MB/s = 災難）
  - 頻繁的層級遷移（每次遷移都是隨機存取）

策略：超大 RAM buffer (512MB) + 壓縮 + 超大順序塊 (4MB)
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Dict, Any, List

from ..core.memory_pool import MemoryPool, MemoryTier
from ..core.cuda_intercept import CUDAInterceptor
from ..core.transfer_engine import TransferEngine
from ..core.health_monitor import HealthMonitor
from ..core.slow_device_optimizer import SlowDeviceOptimizer, SlowDeviceProfile
from ..devices.hdd_external import HDDExternalDevice
from ..devices.base_device import DeviceInfo
from ..config import SystemConfig, MODEL_PROFILES

logger = logging.getLogger(__name__)


class HDDVRAMSystem:

    VERSION = "0.2.0"
    SYSTEM_NAME = "HDD-VRAM Booster"

    def __init__(self, config: Optional[SystemConfig] = None):
        self._config = config or SystemConfig()
        self._device = HDDExternalDevice()
        self._optimizer: Optional[SlowDeviceOptimizer] = None
        self._pool: Optional[MemoryPool] = None
        self._interceptor: Optional[CUDAInterceptor] = None
        self._transfer: Optional[TransferEngine] = None
        self._monitor: Optional[HealthMonitor] = None

        self._is_active = False
        self._detected_devices: List[DeviceInfo] = []
        self._selected_device: Optional[DeviceInfo] = None
        self._vram_bytes: int = 0
        self._ram_bytes: int = 0
        self._start_time: float = 0.0
        self._device_profile: str = ""

    def scan(self) -> Dict[str, Any]:
        self._vram_bytes = self._detect_gpu_vram()
        self._ram_bytes = self._detect_system_ram()
        self._detected_devices = self._device.detect()

        return {
            "system": self.SYSTEM_NAME,
            "gpu_vram_gb": self._vram_bytes / (1024**3),
            "hdd_devices_found": len(self._detected_devices),
            "hdd_devices": [
                {"id": d.device_id, "name": d.device_name,
                 "capacity_gb": d.capacity_gb, "path": d.device_path}
                for d in self._detected_devices
            ],
            "warning": "HDD is sequential-only. Random I/O will be extremely slow.",
        }

    def activate(self, device_index: int = 0) -> bool:
        if self._is_active:
            return True
        if not self._detected_devices:
            return False

        if device_index < len(self._detected_devices):
            self._selected_device = self._detected_devices[device_index]

        if not self._device.initialize(self._selected_device):
            return False

        # HDD 用最保守的 profile
        seq_read = self._device._measured_seq_read
        self._device_profile = SlowDeviceProfile.auto_detect_profile(seq_read, is_rotational=True)
        self._optimizer = SlowDeviceProfile.create_optimizer(self._device_profile)

        eff = SlowDeviceProfile.estimate_effective_bandwidth(self._device_profile)
        effective_bw = eff["effective_read_mbs"]

        ext_bytes = 0
        if self._selected_device:
            import shutil
            from pathlib import Path
            try:
                ext_bytes = shutil.disk_usage(Path(self._selected_device.device_path)).free
            except Exception:
                ext_bytes = int(self._selected_device.capacity_bytes * 0.8)

        # HDD 需要更大的 RAM buffer（512MB 以上）
        ram_pool = min(self._ram_bytes // 2, 16 * (1024**3))

        self._pool = MemoryPool(
            vram_capacity_bytes=self._vram_bytes,
            ram_capacity_bytes=ram_pool,
            external_capacity_bytes=ext_bytes,
            external_bandwidth_mbs=effective_bw,
            external_latency_us=8000,  # HDD 典型延遲 8ms
            demotion_idle_s=120.0,     # HDD 更不願意 demote（寫入太慢）
        )

        self._transfer = TransferEngine(max_workers=1)  # HDD 只能單執行緒
        self._transfer.start()

        self._interceptor = CUDAInterceptor(
            vram_total_bytes=self._vram_bytes,
            ram_pool_bytes=ram_pool,
            external_pool_bytes=ext_bytes,
        )
        self._interceptor.activate()

        self._monitor = HealthMonitor(check_interval_s=15.0)
        self._monitor.register_device(self._selected_device.device_id, self._device.get_metrics)
        self._monitor.start()

        self._is_active = True
        self._start_time = time.time()

        total_gb = (self._vram_bytes + ram_pool + ext_bytes) / (1024**3)
        logger.info(
            "=== %s ACTIVE ===\n"
            "  Device:    %s\n"
            "  Profile:   %s (SEQUENTIAL ONLY)\n"
            "  Raw: %.0f MB/s → Effective: %.0f MB/s (%.1fx)\n"
            "  Workers:   1 (HDD single-thread)\n"
            "  Total:     %.1fGB\n"
            "  WARNING:   Do NOT use for real-time KV Cache I/O.\n"
            "             Best for: model weight preload + context checkpoint.",
            self.SYSTEM_NAME, self._selected_device.device_name,
            self._device_profile,
            seq_read, effective_bw, eff["total_multiplier"],
            total_gb,
        )
        return True

    def deactivate(self) -> None:
        if not self._is_active:
            return
        if self._optimizer:
            self._optimizer.flush_all(lambda bid, off, data: self._device.write_block(bid, off, data))
        if self._monitor:
            self._monitor.stop()
        if self._interceptor:
            self._interceptor.deactivate()
        if self._transfer:
            self._transfer.stop()
        self._device.shutdown()
        self._is_active = False

    def status(self) -> Dict[str, Any]:
        if not self._is_active:
            return {"active": False, "system": self.SYSTEM_NAME}
        return {
            "active": True, "system": self.SYSTEM_NAME,
            "profile": self._device_profile,
            "optimizer": self._optimizer.get_summary() if self._optimizer else {},
            "pool": self._pool.get_stats() if self._pool else {},
            "sequential_only": True,
        }

    def estimate_performance(self, model_key: str = "llama3_8b_q4") -> Dict[str, Any]:
        profile = MODEL_PROFILES.get(model_key, MODEL_PROFILES["llama3_8b_q4"])
        eff = SlowDeviceProfile.estimate_effective_bandwidth(self._device_profile or "hdd_5400rpm")

        return {
            "model": profile["name"],
            "effective_bw_mbs": eff["effective_read_mbs"],
            "multiplier": eff["total_multiplier"],
            "best_use": eff["best_use"],
            "warning": "HDD: model preload only, NOT real-time inference offload",
            "preload_time_s": round(profile["weight_gb"] * 1024 / eff["effective_read_mbs"], 1),
        }

    @staticmethod
    def _detect_gpu_vram() -> int:
        import subprocess
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0: return int(r.stdout.strip().split("\n")[0]) * 1024 * 1024
        except Exception: pass
        return 12 * (1024**3)

    @staticmethod
    def _detect_system_ram() -> int:
        try:
            import platform as pf
            if pf.system().lower() != "windows":
                with open("/proc/meminfo") as f:
                    for l in f:
                        if l.startswith("MemTotal"): return int(l.split()[1]) * 1024
            else:
                import subprocess
                r = subprocess.run(["powershell", "-Command", "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0: return int(r.stdout.strip())
        except Exception: pass
        return 32 * (1024**3)
