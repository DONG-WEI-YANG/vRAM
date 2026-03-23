"""
USB Legacy VRAM System — 普通 USB 隨身碟也能用
================================================
任何 USB 3.0+ 隨身碟或外接 SSD 都能使用。

依照實測頻寬自動分級：
  USB 2.0 隨身碟 (35 MB/s)  → INT4 量化 + 壓縮 = ~350 MB/s 等效 → context snapshot only
  USB 3.0 隨身碟 (300 MB/s) → INT8 量化 + 壓縮 = ~1500 MB/s 等效 → KV Cache 卸載
  USB 3.x SSD (800 MB/s)    → 壓縮 only = ~2000 MB/s 等效 → 完整模型卸載
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
from ..core.audit_log import AuditLog
from ..devices.usb_legacy import USBLegacyDevice
from ..devices.base_device import DeviceInfo
from ..config import SystemConfig, MODEL_PROFILES

logger = logging.getLogger(__name__)


class USBLegacySystem:

    VERSION = "0.2.0"
    SYSTEM_NAME = "USB-VRAM Booster (Legacy)"

    def __init__(self, config: Optional[SystemConfig] = None):
        self._config = config or SystemConfig()
        self._device = USBLegacyDevice()
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

        result = {
            "system": self.SYSTEM_NAME,
            "gpu_vram_gb": self._vram_bytes / (1024**3),
            "usb_devices_found": len(self._detected_devices),
            "usb_devices": [
                {"id": d.device_id, "name": d.device_name,
                 "capacity_gb": d.capacity_gb, "path": d.device_path}
                for d in self._detected_devices
            ],
        }

        if self._detected_devices:
            self._selected_device = max(self._detected_devices, key=lambda d: d.capacity_bytes)
            result["recommendation"] = f"Best: {self._selected_device.device_name}"

        return result

    def activate(self, device_index: int = 0) -> bool:
        if self._is_active:
            return True
        if not self._detected_devices:
            return False

        if device_index < len(self._detected_devices):
            self._selected_device = self._detected_devices[device_index]

        if not self._device.initialize(self._selected_device):
            return False

        # 根據實測速度選 profile
        read_mbs = self._device._measured_read_mbs
        self._device_profile = SlowDeviceProfile.auto_detect_profile(read_mbs, is_rotational=False)
        self._optimizer = SlowDeviceProfile.create_optimizer(self._device_profile)

        eff = SlowDeviceProfile.estimate_effective_bandwidth(self._device_profile)
        effective_bw = eff["effective_read_mbs"]

        ext_bytes = self._device.free_space_bytes if hasattr(self._device, 'free_space_bytes') else 0
        if ext_bytes == 0 and self._selected_device:
            import shutil
            from pathlib import Path
            try:
                ext_bytes = shutil.disk_usage(Path(self._selected_device.device_path)).free
            except Exception:
                ext_bytes = int(self._selected_device.capacity_bytes * 0.85)

        ram_pool = min(self._ram_bytes // 2, 16 * (1024**3))

        self._pool = MemoryPool(
            vram_capacity_bytes=self._vram_bytes,
            ram_capacity_bytes=ram_pool,
            external_capacity_bytes=ext_bytes,
            external_bandwidth_mbs=effective_bw,
        )

        self._transfer = TransferEngine(max_workers=2)
        self._transfer.start()

        self._interceptor = CUDAInterceptor(
            vram_total_bytes=self._vram_bytes,
            ram_pool_bytes=ram_pool,
            external_pool_bytes=ext_bytes,
        )
        self._interceptor.activate()

        self._monitor = HealthMonitor(check_interval_s=10.0)
        self._monitor.register_device(self._selected_device.device_id, self._device.get_metrics)
        self._monitor.start()

        self._is_active = True
        self._start_time = time.time()

        logger.info(
            "=== %s ACTIVE ===\n"
            "  Device:   %s\n"
            "  Profile:  %s\n"
            "  Raw: %.0f MB/s → Effective: %.0f MB/s (%.1fx)\n"
            "  Total:    %.1fGB (VRAM %.1f + RAM %.1f + USB %.1f)",
            self.SYSTEM_NAME, self._selected_device.device_name,
            self._device_profile,
            read_mbs, effective_bw, eff["total_multiplier"],
            (self._vram_bytes + ram_pool + ext_bytes) / (1024**3),
            self._vram_bytes / (1024**3), ram_pool / (1024**3), ext_bytes / (1024**3),
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
        }

    def estimate_performance(self, model_key: str = "llama3_8b_q4") -> Dict[str, Any]:
        profile = MODEL_PROFILES.get(model_key, MODEL_PROFILES["llama3_8b_q4"])
        eff = SlowDeviceProfile.estimate_effective_bandwidth(self._device_profile or "usb3_flash")
        model_gb = profile["weight_gb"]
        vram_gb = self._vram_bytes / (1024**3)
        avail = max(0, vram_gb - 1.5)
        overflow = max(0, model_gb - avail)
        eff_bw = eff["effective_read_mbs"] / 1024
        tps = 50.0 if overflow <= 0 else 1.0 / (avail / 336 + overflow / eff_bw)
        return {
            "model": profile["name"], "overflow_gb": overflow,
            "effective_bw_mbs": eff["effective_read_mbs"],
            "multiplier": eff["total_multiplier"],
            "estimated_tps": round(tps, 2), "best_use": eff["best_use"],
        }

    @staticmethod
    def _detect_gpu_vram() -> int:
        import subprocess
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return int(r.stdout.strip().split("\n")[0]) * 1024 * 1024
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
