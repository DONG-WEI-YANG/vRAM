"""
Real Memory Expansion Engine
==============================
真正的系統記憶體擴展 — 不是 GUI 模擬。

做什麼：
  Windows: 在儲存裝置上建立 pagefile (分頁檔)
  Linux:   在儲存裝置上建立 swap file

效果：
  系統可用記憶體立即增加 → 任何 AI 軟體自動受益
  Ollama/llama.cpp 載入大模型時，溢出的部分自動用 SD 卡

原理：
  AI 推理引擎 (Ollama/llama.cpp) 載入超過 RAM 的模型時：
    1. 模型權重先載入 RAM
    2. RAM 不夠 → OS 自動把最冷的頁面 swap 到 SD 卡
    3. GPU 需要某層權重 → OS 從 SD 卡讀回 RAM → 送給 GPU
    4. 整個過程對應用程式完全透明

這跟 NVIDIA GreenBoost 的「T3: NVMe swap」層是同樣的機制，
只是我們用 SD 卡/USB 碟代替 NVMe SSD。
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import shutil
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Callable

from .mmap_engine import MmapSwapEngine
from .safety_policy import SafetyPolicy, GLOBAL_POLICY_PATH
from .striped_swap import StripedSwapScheduler
from .vhd_pagefile import VhdPagefileEngine

logger = logging.getLogger(__name__)

# Windows: 隱藏子程序視窗
_NO_WINDOW = 0
if platform.system().lower() == "windows":
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # 0x08000000


def _run_hidden(cmd, **kwargs):
    """執行子程序，Windows 上不彈出視窗"""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if _NO_WINDOW:
        kwargs.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kwargs)  # noqa: used by _run_hidden only


# swap 大小 = 實測隨機寫入速度 × 可接受的最大寫滿秒數
# 原理：OS 高壓 swap 時可能寫滿整個 swap 空間，
#       寫滿時間過長 → 用戶感覺系統凍結。
SWAP_FILL_TIME_SECONDS = 600  # 10 分鐘內能寫滿 = 可接受的上限
SWAP_MIN_BYTES = 512 * (1024 ** 2)  # 最小 512 MB（太小沒意義）


class RealBoostEngine:
    """
    真實的記憶體擴展引擎。

    activate() → 在裝置上建立 swap/pagefile → 系統記憶體立即增加
    deactivate() → 移除 swap/pagefile → 恢復原狀
    status() → 回傳真實的記憶體使用量
    """

    SWAP_FILENAME = "vram_boost.swap"
    CONFIG_FILENAME = ".vram_boost_config.json"
    # 快速驗證用：寫入 256KB 測速，若與記錄差異超過此比例則重測
    SPEED_DRIFT_THRESHOLD = 0.3  # 30%

    def __init__(self):
        self._is_windows = platform.system().lower() == "windows"
        self._active = False
        self._swap_path: Optional[Path] = None
        self._swap_size_bytes: int = 0
        self._device_letter = ""
        self._original_mem: Dict[str, int] = {}
        self._measured_rand_write_mbs: float = 0.0
        # Multi-device state
        self._mmap_engines: list = []
        self._striped: Optional[StripedSwapScheduler] = None
        self._known_drives: set = set()
        self._system_pf_bytes: int = 0  # 啟動時的 Windows pagefile 大小
        self._hotdetect_thread: Optional[threading.Thread] = None
        self._hotdetect_stop = threading.Event()
        self._engine_swap_bytes: Dict[str, int] = {}  # engine.uid → swap bytes
        self._vhd_engine: Optional[VhdPagefileEngine] = None
        self._vhd_active: bool = False
        self._linux_swap_engine = None  # LinuxSwapEngine instance
        self._state_lock = threading.RLock()  # 保護所有共享狀態

    # ── 持久化設定：存在 SD 卡上，下次插入免重測 ──

    @staticmethod
    def _get_card_fingerprint(mount_path: str) -> str:
        """
        產生 SD 卡指紋：容量 + 磁碟標籤。
        不同的卡會有不同指紋，即使插在同一個讀卡機。
        """
        try:
            usage = shutil.disk_usage(mount_path)
            total_gb = round(usage.total / (1024 ** 3), 1)
        except OSError:
            total_gb = 0

        label = ""
        if platform.system().lower() == "windows":
            letter = mount_path.rstrip(":\\/ ")[0]
            try:
                r = _run_hidden(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Volume -DriveLetter {letter} -ErrorAction Stop).FileSystemLabel"],
                    timeout=5,
                )
                if r.returncode == 0:
                    label = r.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                pass
        else:
            # Linux: 用 lsblk 取得檔案系統 label
            try:
                r = _run_hidden(
                    ["lsblk", "-J", "-o", "MOUNTPOINT,LABEL"],
                    timeout=5,
                )
                if r.returncode == 0:
                    import json
                    data = json.loads(r.stdout)
                    for dev in data.get("blockdevices", []):
                        for part in dev.get("children", [dev]):
                            if part.get("mountpoint") == mount_path:
                                label = part.get("label", "") or ""
                                break
            except (subprocess.TimeoutExpired, OSError):
                pass

        return f"{total_gb}GB|{label}"

    def _load_cached_config(self, mount_path: str) -> Optional[Dict[str, Any]]:
        """讀取 SD 卡上的快取設定，並驗證指紋是否匹配"""
        config_path = Path(mount_path) / self.CONFIG_FILENAME
        if not config_path.exists():
            return None
        try:
            import json
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if "rand_write_mbs" not in data or "swap_size_bytes" not in data:
                return None

            # 指紋比對：設定檔可能是另一張卡留下的
            saved_fp = data.get("card_fingerprint", "")
            current_fp = self._get_card_fingerprint(mount_path)
            if saved_fp and saved_fp != current_fp:
                logger.info("Card fingerprint mismatch: saved=%s current=%s", saved_fp, current_fp)
                return None  # 不同的卡，不用快取

            return data
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def _save_config(self, mount_path: str, config: Dict[str, Any]) -> None:
        """將設定存到 SD 卡（含卡片指紋），下次插入可直接使用"""
        config["card_fingerprint"] = self._get_card_fingerprint(mount_path)
        config_path = Path(mount_path) / self.CONFIG_FILENAME
        try:
            import json
            config_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Config saved to %s", config_path)
        except OSError as e:
            logger.warning("Cannot save config: %s", e)

    def _quick_speed_check(self, mount_path: str) -> float:
        """快速驗證（256KB），確認裝置速度沒有大幅變化"""
        return self._benchmark_random_write(mount_path, test_size_mb=0.25)

    @staticmethod
    def _verify_swap_file(swap_path: Path) -> bool:
        """
        驗證 swap 檔案完整性：讀寫頭尾各 4KB。
        損壞或不可存取的檔案會被刪除，觸發重建。
        """
        try:
            size = swap_path.stat().st_size
            if size < 512 * (1024 ** 2):
                return False
            with open(swap_path, "r+b") as f:
                # 讀頭部
                f.seek(0)
                f.read(4096)
                # 讀尾部
                f.seek(max(0, size - 4096))
                f.read(4096)
            return True
        except OSError:
            logger.warning("Swap file integrity check failed, will recreate: %s", swap_path)
            try:
                swap_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def activate(self, drive_letter: str, use_percent: float = 80.0,
                 on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """
        在指定磁碟上建立 swap/pagefile，立即擴展系統記憶體。

        持久記憶：首次測速後將結果存在 SD 卡上，下次插入讀取快取，
        只做快速驗證（<1 秒），速度無明顯變化就跳過完整測速。

        Args:
            drive_letter: 磁碟代號 (e.g., "E")
            use_percent: 使用可用空間的百分比 (預設 80%)
            on_progress: 進度回報函式 (message: str) → None，供 GUI 更新狀態

        Returns: {"success": bool, "added_gb": float, "total_mem_gb": float, ...}
        """
        if self._active:
            return {"success": False, "error": "Already active"}

        def report(msg: str):
            if on_progress:
                on_progress(msg)
            logger.info(msg)

        # 記錄原始記憶體
        report("讀取系統記憶體...")
        self._original_mem = self.get_system_memory()
        self._device_letter = drive_letter

        mount = f"{drive_letter}:\\" if self._is_windows else drive_letter

        # 嘗試讀取上次的測速結果
        cached = self._load_cached_config(mount)
        need_full_benchmark = True

        if cached:
            cached_speed = cached.get("rand_write_mbs", 0)
            # 有快取就直接用，不做寫入驗證
            # SD 卡 I/O 可能因 FTL GC 而凍結數十秒，寫入測速反而造成卡頓
            self._measured_rand_write_mbs = cached_speed
            report(f"using cached speed: {cached_speed:.0f} MB/s")
            need_full_benchmark = False

        if need_full_benchmark:
            # 完整測速：4MB 隨機寫入
            report("測試裝置速度（約 3-5 秒）...")
            self._measured_rand_write_mbs = self._benchmark_random_write(mount)
            report(f"測速完成：隨機寫入 {self._measured_rand_write_mbs:.0f} MB/s")

        # 計算 swap 大小
        report("計算最佳 swap 大小...")

        # ── Safety Policy validation ──
        try:
            capacity_gb = shutil.disk_usage(mount).total / (1024 ** 3)
            device_config_path = Path(mount) / self.CONFIG_FILENAME
            policy = SafetyPolicy.load_merged_policy(
                GLOBAL_POLICY_PATH, device_config_path,
                capacity_gb=capacity_gb,
                speed_mbs=self._measured_rand_write_mbs,
            )

            # Clamp requested size to policy limits
            requested_gb = capacity_gb * (use_percent / 100)
            requested_gb = min(requested_gb, policy.pagefile_max_gb)
            requested_gb = max(requested_gb, policy.pagefile_min_gb)

            ok, reason = SafetyPolicy.validate_activation(
                capacity_gb, requested_gb, policy)
            if not ok:
                logger.warning("Safety policy rejected activation: %s", reason)
                report(f"Policy rejected: {reason}")
                return {"success": False, "error": f"Safety policy: {reason}"}

            # Recalculate use_percent from clamped value
            use_percent = (requested_gb / capacity_gb) * 100
            report(f"Safety policy: {requested_gb:.1f} GB "
                   f"(reserve {policy.device_reserved_gb:.0f} GB on device)")
        except Exception as e:
            logger.warning("Safety policy check skipped: %s", e)

        if self._is_windows:
            result = self._activate_windows(drive_letter, use_percent, report)
        else:
            result = self._activate_linux(drive_letter, use_percent, report)

        # 成功後存設定到 SD 卡，下次免重測
        if result.get("success"):
            self._save_config(mount, {
                "rand_write_mbs": self._measured_rand_write_mbs,
                "swap_size_bytes": self._swap_size_bytes,
                "swap_filename": self.SWAP_FILENAME,
                "drive_letter": drive_letter,
            })

        return result

    @staticmethod
    def _benchmark_random_write(mount_path: str, test_size_mb: float = 4) -> float:
        """
        用隨機 4KB 寫入測試裝置速度，模擬 swap/pagefile 的真實 I/O 模式。
        Returns: 隨機寫入速度 (MB/s)
        """
        test_file = Path(mount_path) / ".vram_speed_test"
        block_4kb = os.urandom(4096)
        total_bytes = test_size_mb * 1024 * 1024
        num_writes = total_bytes // 4096

        try:
            # 先建立測試檔案
            with open(test_file, "wb") as f:
                f.seek(total_bytes - 1)
                f.write(b"\0")

            # 隨機位置寫入 4KB 區塊
            import random
            offsets = [random.randint(0, num_writes - 1) * 4096 for _ in range(num_writes)]

            start = time.perf_counter()
            with open(test_file, "r+b", buffering=0) as f:
                for off in offsets:
                    f.seek(off)
                    f.write(block_4kb)
                f.flush()
                os.fsync(f.fileno())
            elapsed = time.perf_counter() - start

            speed_mbs = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            return speed_mbs

        except OSError as e:
            logger.warning("Speed test failed: %s", e)
            return 5.0  # 保守預設：假設很慢
        finally:
            try:
                test_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _cap_swap_by_speed(self, swap_bytes: int) -> Tuple[int, str]:
        """
        根據實測隨機寫入速度動態計算 swap 大小上限。

        公式：max_swap = 速度 (MB/s) × 可接受寫滿時間 (秒)
        例：5 MB/s × 600s = 3 GB, 50 MB/s × 600s = 30 GB, 200 MB/s × 600s = 120 GB

        Returns: (capped_bytes, warning_message or "")
        """
        speed = self._measured_rand_write_mbs
        max_bytes = int(speed * (1024 ** 2) * SWAP_FILL_TIME_SECONDS)
        max_bytes = max(max_bytes, SWAP_MIN_BYTES)

        if swap_bytes <= max_bytes:
            return swap_bytes, ""

        capped_gb = max_bytes / (1024 ** 3)
        original_gb = swap_bytes / (1024 ** 3)
        warning = (
            f"裝置隨機寫入 {speed:.0f} MB/s → swap 上限 {capped_gb:.1f}GB "
            f"(原 {original_gb:.0f}GB，依速度自動調配)"
        )
        logger.warning(warning)
        return max_bytes, warning

    def deactivate(self) -> Dict[str, Any]:
        """移除 swap/pagefile，恢復原狀"""
        if not self._active:
            return {"success": True, "note": "Not active"}

        if self._is_windows:
            return self._deactivate_windows()
        else:
            return self._deactivate_linux()

    def status(self) -> Dict[str, Any]:
        """取得真實的系統記憶體狀態"""
        mem = self.get_system_memory()

        # 計算真實可用的外接 swap（只算連線中的裝置）
        real_swap_bytes = 0
        with self._state_lock:
            engines = list(getattr(self, '_mmap_engines', []))
            if not engines and hasattr(self, '_mmap_engine') and self._mmap_engine:
                engines = [self._mmap_engine]
        for eng in engines:
            st = eng.status()
            if not st.get("degraded", False):
                real_swap_bytes += self._engine_swap_bytes.get(eng.uid, 0)

        result = {
            "active": self._active,
            "swap_path": str(self._swap_path) if self._swap_path else None,
            "swap_size_gb": self._swap_size_bytes / (1024 ** 3),
            "real_swap_gb": real_swap_bytes / (1024 ** 3),
            "physical_ram_gb": mem.get("physical_total", 0) / (1024 ** 3),
            "available_ram_gb": mem.get("physical_available", 0) / (1024 ** 3),
            "swap_total_gb": mem.get("swap_total", 0) / (1024 ** 3),
            "swap_used_gb": mem.get("swap_used", 0) / (1024 ** 3),
            "total_usable_gb": (mem.get("physical_total", 0) + real_swap_bytes) / (1024 ** 3),
        }

        # Windows pagefile 資訊：讓呼叫者知道系統已有的 swap
        sys_pf = getattr(self, '_system_pf_bytes', 0)
        if sys_pf > 0:
            result["system_pagefile_gb"] = sys_pf / (1024 ** 3)
            result["total_with_system_gb"] = (mem.get("physical_total", 0) + sys_pf + real_swap_bytes) / (1024 ** 3)

        # VHD pagefile 狀態
        if getattr(self, '_vhd_active', False) and self._vhd_engine:
            vhd_st = self._vhd_engine.status()
            result["vhd_active"] = True
            result["vhd_devices"] = vhd_st.get("devices", [])
            result["method"] = "vhd_pagefile"
            result["system_wide"] = True
        else:
            result["vhd_active"] = False
            result["method"] = "mmap_swap" if engines else "none"
            result["system_wide"] = False

        # Linux 多裝置 swap 狀態
        if getattr(self, '_linux_swap_engine', None):
            linux_st = self._linux_swap_engine.status()
            result["linux_swap_active"] = linux_st.get("active", False)
            result["linux_devices"] = linux_st.get("devices", [])
            if linux_st.get("active"):
                result["method"] = "linux_parallel_swap"
                result["system_wide"] = True

        # 多裝置聚合狀態
        if engines:
            total_mapped = 0
            total_evicted = 0
            any_degraded = False
            devices = []
            for eng in engines:
                st = eng.status()
                total_mapped += st.get("mapped_blocks", 0)
                total_evicted += st.get("evicted_blocks", 0)
                if st.get("degraded"):
                    any_degraded = True
                devices.append({
                    "path": st.get("swap_path", ""),
                    "state": st.get("device_state", "unknown"),
                    "degraded": st.get("degraded", False),
                    "blocks": st.get("total_blocks", 0),
                })
            result["device_count"] = len(engines)
            result["devices"] = devices
            result["mapped_blocks"] = total_mapped
            result["evicted_blocks"] = total_evicted
            result["degraded"] = any_degraded
        return result

    @staticmethod
    def get_system_memory() -> Dict[str, int]:
        """
        取得系統記憶體真實數據。
        Windows 用 ctypes (GlobalMemoryStatusEx) — 零成本，不啟動子程序。
        Linux 用 /proc/meminfo — 同樣輕量。
        """
        mem = {"physical_total": 0, "physical_available": 0, "swap_total": 0, "swap_used": 0}

        if platform.system().lower() == "windows":
            try:
                import ctypes
                import ctypes.wintypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.wintypes.DWORD),
                        ("dwMemoryLoad", ctypes.wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))

                mem["physical_total"] = stat.ullTotalPhys
                mem["physical_available"] = stat.ullAvailPhys
                mem["swap_total"] = stat.ullTotalPageFile - stat.ullTotalPhys
                swap_free = stat.ullAvailPageFile - stat.ullAvailPhys
                mem["swap_used"] = max(0, mem["swap_total"] - swap_free)
            except (OSError, AttributeError, ValueError):
                pass
        else:
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        parts = line.split()
                        if parts[0] == "MemTotal:":
                            mem["physical_total"] = int(parts[1]) * 1024
                        elif parts[0] == "MemAvailable:":
                            mem["physical_available"] = int(parts[1]) * 1024
                        elif parts[0] == "SwapTotal:":
                            mem["swap_total"] = int(parts[1]) * 1024
                        elif parts[0] == "SwapFree:":
                            swap_free = int(parts[1]) * 1024
                            mem["swap_used"] = mem["swap_total"] - swap_free
            except (OSError, ValueError):
                pass

        return mem

    # GPU 資訊快取：nvidia-smi 呼叫較重，快取 30 秒
    _gpu_cache: Optional[Dict[str, Any]] = None
    _gpu_cache_ts: float = 0.0
    _GPU_CACHE_TTL: float = 30.0

    @classmethod
    def get_gpu_info(cls) -> Dict[str, Any]:
        """取得 GPU 資訊（快取 30 秒，避免頻繁呼叫 nvidia-smi）"""
        now = time.time()
        if cls._gpu_cache and (now - cls._gpu_cache_ts) < cls._GPU_CACHE_TTL:
            return cls._gpu_cache

        info = {"name": "Unknown", "vram_total_mb": 0, "vram_used_mb": 0, "vram_free_mb": 0}
        try:
            r = _run_hidden(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
                 "--format=csv,noheader,nounits"],
                timeout=10,
            )
            if r.returncode == 0:
                parts = r.stdout.strip().split(",")
                if len(parts) >= 4:
                    info["name"] = parts[0].strip()
                    info["vram_total_mb"] = int(parts[1].strip())
                    info["vram_used_mb"] = int(parts[2].strip())
                    info["vram_free_mb"] = int(parts[3].strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        cls._gpu_cache = info
        cls._gpu_cache_ts = now
        return info

    # ── Windows: Multi-Device Mmap Swap ──

    def _scan_external_drives(self, primary_letter: str) -> list:
        """掃描所有可用的外接裝置（排除 C: 和 D: 系統碟）"""
        drives = [primary_letter]
        # 直接用 os.path.exists 掃描所有磁碟代號，不依賴 PowerShell
        for letter in "EFGHIJKLMNOPQRSTUVWXYZ":
            if letter in drives or letter in ("C", "D"):
                continue
            if os.path.exists(f"{letter}:\\"):
                try:
                    usage = shutil.disk_usage(f"{letter}:\\")
                    if usage.total > 1024 ** 3:  # > 1GB
                        drives.append(letter)
                except OSError:
                    pass
        return drives

    def _activate_windows(self, letter: str, use_pct: float,
                          report=None) -> Dict[str, Any]:
        """
        Windows 記憶體擴展：VHD pagefile 優先，mmap fallback。

        策略：
        1. 嘗試 VHD Bridge → 真正的 Windows pagefile（所有程式受益）
        2. VHD 失敗 → fallback 到 mmap swap（只有本 process 受益）
        """
        report = report or (lambda msg: None)

        # 掃描所有外接裝置
        drives = self._scan_external_drives(letter)
        report(f"found {len(drives)} device(s): {', '.join(d + ':' for d in drives)}")

        # ── 嘗試 VHD Bridge（主策略）──
        report("trying VHD pagefile (system-wide benefit)...")
        try:
            self._vhd_engine = VhdPagefileEngine()
            vhd_result = self._vhd_engine.activate(
                drive_letters=drives,
                use_percent=use_pct,
                on_progress=report,
            )

            if vhd_result.get("success"):
                self._vhd_active = True
                self._active = True
                self._swap_size_bytes = int(vhd_result.get("added_gb", 0) * (1024**3))

                # Assess system pagefile
                self._system_pf_bytes = self._get_system_pagefile_bytes()

                # Start hot-detect for new devices
                self._known_drives = set(drives)
                self._hotdetect_stop.clear()
                self._hotdetect_thread = threading.Thread(
                    target=self._hot_detect_loop, daemon=True, name="hotplug-detect")
                self._hotdetect_thread.start()
                report("hot-detect started (30s interval)")

                after_mem = self.get_system_memory()
                return {
                    "success": True,
                    "method": "vhd_pagefile",
                    "system_wide": True,
                    "device_count": vhd_result.get("device_count", 0),
                    "devices": vhd_result.get("devices", []),
                    "added_gb": vhd_result.get("added_gb", 0),
                    "system_pagefile_gb": round(self._system_pf_bytes / (1024**3), 1),
                    "total_usable_gb": round(
                        after_mem.get("physical_total", 0) / (1024**3) +
                        vhd_result.get("added_gb", 0) +
                        self._system_pf_bytes / (1024**3), 1),
                    "needs_reboot": False,
                }
        except Exception as e:
            logger.warning("VHD pagefile failed: %s, falling back to mmap", e)
            report(f"VHD failed ({e}), trying mmap fallback...")
            self._vhd_engine = None
            self._vhd_active = False

        # ── Fallback: mmap swap（原有邏輯）──
        report("using mmap swap (process-level only)...")
        return self._activate_windows_mmap(letter, use_pct, report)

    def _activate_windows_mmap(self, letter: str, use_pct: float,
                               report=None) -> Dict[str, Any]:
        """Fallback: mmap-based swap (only benefits this process)."""
        report = report or (lambda msg: None)

        # 掃描所有外接裝置
        drives = self._scan_external_drives(letter)
        report(f"found {len(drives)} device(s): {', '.join(d + ':' for d in drives)}")

        self._mmap_engines: list = []
        total_swap_bytes = 0
        device_details = []

        for drv in drives:
            mount = f"{drv}:\\"
            try:
                usage = shutil.disk_usage(mount)
            except OSError:
                report(f"{drv}: skipped (inaccessible)")
                continue

            if usage.free < 512 * (1024 ** 2):
                report(f"{drv}: skipped (< 512MB free)")
                continue

            # 每個裝置獨立測速（有快取就用快取，取歷史最佳）
            cached = self._load_cached_config(mount)
            cached_speed = cached.get("effective_speed_mbs", 0) if cached else 0

            report(f"{drv}: benchmarking...")
            speed = self._benchmark_random_write(mount, test_size_mb=4)
            seq_speed = self._benchmark_sequential_write(mount)
            measured = max(speed, seq_speed * 0.7) if seq_speed > speed * 3 else speed

            # 取歷史最佳和本次測量的較大值，避免單次波動縮小 swap
            effective_speed = max(measured, cached_speed)
            if effective_speed > measured:
                report(f"{drv}: using cached speed {effective_speed:.0f} MB/s (measured {measured:.0f})")

            # 存回快取（含等效速度）
            self._save_config(mount, {
                "rand_write_mbs": speed,
                "seq_write_mbs": seq_speed,
                "effective_speed_mbs": effective_speed,
                "drive_letter": drv,
            })

            # Striped 模式：個別裝置 cap 放寬到 2x（平行分攤）
            # 但不是無限 — 防止單裝置被斷線後壓垮
            STRIPED_MULTIPLIER = 2.0  # 比單裝置寬鬆 2 倍
            device_cap = int(effective_speed * (1024 ** 2) * SWAP_FILL_TIME_SECONDS * STRIPED_MULTIPLIER)
            device_cap = max(device_cap, SWAP_MIN_BYTES)
            wanted = min(int(usage.free * (use_pct / 100)), device_cap)
            swap_bytes = (wanted // (1024 * 1024)) * (1024 * 1024)
            if swap_bytes < 512 * (1024 ** 2):
                report(f"{drv}: skipped (swap too small)")
                continue

            swap_gb = swap_bytes / (1024 ** 3)
            report(f"{drv}: {effective_speed:.0f} MB/s → {swap_gb:.1f}GB swap")

            engine = MmapSwapEngine()
            result = engine.activate(
                device_path=mount,
                size_bytes=swap_bytes,
                on_progress=lambda msg, d=drv: report(f"{d}: {msg}"),
                on_state_change=lambda state, d=drv: self._on_device_state_changed(d, state),
            )

            if result.get("success"):
                self._mmap_engines.append(engine)
                self._engine_swap_bytes[engine.uid] = swap_bytes
                total_swap_bytes += swap_bytes
                device_details.append({
                    "drive": drv,
                    "speed_mbs": round(effective_speed, 1),
                    "swap_gb": round(swap_gb, 1),
                    "blocks": result.get("total_blocks", 0),
                })
            else:
                report(f"{drv}: failed ({result.get('error', '?')})")

        if not self._mmap_engines:
            return {"success": False, "error": "No devices activated"}

        # ── 建立 Striped Scheduler（平行 I/O 調度） ──
        self._striped = StripedSwapScheduler()
        for eng, detail in zip(self._mmap_engines, device_details):
            self._striped.add_device(eng, detail["speed_mbs"])

        # ── 合計速度 cap：用所有裝置的合計速度做總上限 ──
        striped_speed = self._striped.total_speed_mbs
        combined_cap = int(striped_speed * (1024 ** 2) * SWAP_FILL_TIME_SECONDS)
        if total_swap_bytes > combined_cap:
            logger.info("Striped cap: %.1fGB → %.1fGB (combined %.0f MB/s × %ds)",
                         total_swap_bytes / (1024**3), combined_cap / (1024**3),
                         striped_speed, SWAP_FILL_TIME_SECONDS)
            total_swap_bytes = combined_cap

        self._swap_size_bytes = total_swap_bytes
        self._active = True
        self._known_drives = set(drv for drv in drives if any(
            d["drive"] == drv for d in device_details))

        # ── 評估 Windows pagefile 是否足夠 ──
        self._system_pf_bytes = self._get_system_pagefile_bytes()
        pf_gb = self._system_pf_bytes / (1024**3)
        ram_gb = self._original_mem.get("physical_total", 0) / (1024**3)
        if self._system_pf_bytes > 0:
            report(f"Windows pagefile: {pf_gb:.0f}GB (auto), our mmap: +{total_swap_bytes/(1024**3):.1f}GB on external")
        else:
            report(f"No Windows pagefile detected, external swap is primary expansion")

        # ── 背景熱偵測：每 30 秒掃描新裝置 ──
        self._hotdetect_stop.clear()
        self._hotdetect_thread = threading.Thread(
            target=self._hot_detect_loop, daemon=True,
            name="hotplug-detect")
        self._hotdetect_thread.start()
        report("hot-detect started (30s interval)")

        after_mem = self.get_system_memory()
        total_gb = total_swap_bytes / (1024 ** 3)

        return {
            "success": True,
            "method": "striped_mmap_swap" if len(self._mmap_engines) > 1 else "mmap_swap",
            "system_wide": False,
            "system_pagefile_gb": round(self._system_pf_bytes / (1024**3), 1),
            "device_count": len(self._mmap_engines),
            "devices": device_details,
            "added_gb": round(total_gb, 1),
            "total_usable_gb": round(
                after_mem.get("physical_total", 0) / (1024**3) + total_gb, 1),
            "combined_speed_mbs": round(striped_speed, 1),
            "rand_write_mbs": round(self._measured_rand_write_mbs, 1),
            "needs_reboot": False,
        }

    # ── Hot-Detect: 背景偵測新裝置 ──

    def _hot_detect_loop(self):
        """每 30 秒掃描新裝置，自動加入 striped group。"""
        while not self._hotdetect_stop.wait(timeout=30):
            if not self._active:
                break
            try:
                self._hot_detect_scan()
            except Exception as e:
                logger.debug("Hot-detect error: %s", e)

    def _hot_detect_scan(self):
        """掃描是否有新裝置可加入。"""
        current = self._scan_external_drives(self._device_letter)
        new_drives = [d for d in current if d not in self._known_drives]

        if not new_drives:
            return

        for drv in new_drives:
            mount = f"{drv}:\\"
            try:
                usage = shutil.disk_usage(mount)
            except OSError:
                continue
            if usage.free < 512 * (1024 ** 2):
                continue

            logger.info("Hot-detect: new device %s:", drv)

            if self._vhd_active and self._vhd_engine:
                # VHD 模式：加入新裝置的 VHD pagefile
                try:
                    result = self._vhd_engine.activate(
                        drive_letters=[drv], use_percent=80.0,
                        on_progress=lambda msg: logger.info("Hot-add %s: %s", drv, msg),
                    )
                    if result.get("success"):
                        with self._state_lock:
                            self._known_drives.add(drv)
                            self._swap_size_bytes += int(result.get("added_gb", 0) * (1024**3))
                        logger.info("Hot-added VHD pagefile on %s:", drv)
                except Exception as e:
                    logger.warning("Hot-add VHD failed for %s: %s", drv, e)
            else:
                # mmap fallback 模式：用原有邏輯
                # 測速
                cached = self._load_cached_config(mount)
                cached_speed = cached.get("effective_speed_mbs", 0) if cached else 0
                speed = self._benchmark_random_write(mount, test_size_mb=4)
                seq_speed = self._benchmark_sequential_write(mount)
                measured = max(speed, seq_speed * 0.7) if seq_speed > speed * 3 else speed
                effective_speed = max(measured, cached_speed)

                self._save_config(mount, {
                    "rand_write_mbs": speed,
                    "seq_write_mbs": seq_speed,
                    "effective_speed_mbs": effective_speed,
                    "drive_letter": drv,
                })

                # Per-device cap
                STRIPED_MULTIPLIER = 2.0
                device_cap = int(effective_speed * (1024 ** 2) * SWAP_FILL_TIME_SECONDS * STRIPED_MULTIPLIER)
                device_cap = max(device_cap, SWAP_MIN_BYTES)
                wanted = min(int(usage.free * 0.8), device_cap)
                swap_bytes = (wanted // (1024 * 1024)) * (1024 * 1024)

                if swap_bytes < 512 * (1024 ** 2):
                    continue

                # 建立 mmap swap
                engine = MmapSwapEngine()
                result = engine.activate(
                    device_path=mount, size_bytes=swap_bytes,
                    on_state_change=lambda state, d=drv: self._on_device_state_changed(d, state),
                )

                if result.get("success"):
                    with self._state_lock:
                        self._mmap_engines.append(engine)
                        self._engine_swap_bytes[engine.uid] = swap_bytes
                        self._swap_size_bytes += swap_bytes
                        self._known_drives.add(drv)

                        if self._striped:
                            self._striped.add_device(engine, effective_speed)

                    swap_gb = swap_bytes / (1024 ** 3)
                    logger.info("Hot-added %s: %.1f MB/s, +%.1fGB swap", drv, effective_speed, swap_gb)

    # ── 裝置狀態變化 ──

    def _on_device_state_changed(self, drive_letter: str, state: str) -> None:
        """
        由 MmapSwapEngine 的 on_state_change callback 觸發。
        state = "degraded" (裝置斷線) 或 "restored" (裝置恢復)
        """
        logger.info("Device %s: state → %s", drive_letter, state)

        with self._state_lock:
            if state == "degraded":
                # 重新計算：只算仍在連線中的裝置
                alive_bytes = 0
                for eng in list(self._mmap_engines):
                    st = eng.status()
                    if not st.get("degraded", False):
                        alive_bytes += self._engine_swap_bytes.get(eng.uid, 0)

                lost_bytes = self._swap_size_bytes - alive_bytes
                logger.info("Device %s: disconnected — lost %.1fGB, remaining %.1fGB",
                            drive_letter, lost_bytes / (1024**3), alive_bytes / (1024**3))

            elif state == "restored":
                # 裝置恢復：重新計算全量
                total_bytes = sum(
                    self._engine_swap_bytes.get(eng.uid, 0)
                    for eng in list(self._mmap_engines)
                    if not eng.status().get("degraded", False)
                )
                logger.info("Device %s: restored — total %.1fGB", drive_letter, total_bytes / (1024**3))

    @staticmethod
    def _get_system_pagefile_bytes() -> int:
        """取得 Windows 系統 pagefile 大小（不含物理 RAM）。"""
        if platform.system().lower() != "windows":
            return 0
        try:
            import ctypes
            import ctypes.wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.wintypes.DWORD),
                    ("dwMemoryLoad", ctypes.wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return max(0, stat.ullTotalPageFile - stat.ullTotalPhys)
        except (OSError, AttributeError):
            return 0

    @staticmethod
    def _benchmark_sequential_write(mount_path: str, size_mb: int = 8) -> float:
        """順序寫入測速（Write-Back Buffer 的等效速度基準），30 秒 timeout"""
        test_file = Path(mount_path) / ".vram_seq_test"
        chunk = os.urandom(1024 * 1024)  # 1MB
        try:
            start = time.perf_counter()
            with open(test_file, "wb", buffering=0) as f:
                for _ in range(size_mb):
                    f.write(chunk)
                    # 逐 MB 檢查 timeout，避免 fsync 卡死
                    if time.perf_counter() - start > 30:
                        logger.warning("Sequential write timeout at %s", mount_path)
                        break
                f.flush()
                # fsync 也加 timeout（用 thread）
                import threading
                done = threading.Event()
                def do_sync():
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                    done.set()
                t = threading.Thread(target=do_sync, daemon=True)
                t.start()
                done.wait(timeout=15)  # fsync 最多等 15 秒
            elapsed = time.perf_counter() - start
            return size_mb / elapsed if elapsed > 0 else 0
        except OSError:
            return 0
        finally:
            try:
                test_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _deactivate_windows(self) -> Dict[str, Any]:
        """關閉 VHD pagefile + mmap swap + striped scheduler + hot-detect。"""
        # 停止熱偵測
        self._hotdetect_stop.set()
        if self._hotdetect_thread and self._hotdetect_thread.is_alive():
            self._hotdetect_thread.join(timeout=15)
            if self._hotdetect_thread.is_alive():
                logger.warning("Hot-detect thread did not stop within 15s")
        self._hotdetect_thread = None

        # ── VHD pagefile 清理 ──
        if self._vhd_active and self._vhd_engine:
            try:
                self._vhd_engine.deactivate()
            except Exception as e:
                logger.warning("VHD deactivate error: %s", e)
            self._vhd_engine = None
            self._vhd_active = False

        # ── mmap 清理（fallback 時才有） ──
        if hasattr(self, '_striped') and self._striped:
            self._striped.close()
            self._striped = None
        for engine in getattr(self, '_mmap_engines', []):
            try:
                engine.deactivate()
            except (OSError, Exception) as e:
                logger.warning("Deactivate error: %s", e)
        self._mmap_engines = []
        self._engine_swap_bytes = {}
        self._known_drives = set()
        # Legacy single-engine cleanup
        if hasattr(self, '_mmap_engine') and self._mmap_engine:
            self._mmap_engine.deactivate()
            self._mmap_engine = None

        self._system_pf_bytes = 0

        self._active = False
        self._swap_path = None
        self._swap_size_bytes = 0
        return {"success": True}

    # ── Linux: Swap File ──

    def _activate_linux(self, mount_point: str, use_pct: float,
                        report=None) -> Dict[str, Any]:
        """
        Linux 多裝置平行 swap：掃描所有外接裝置，各自建立 swap file，
        用 swapon -p 同優先級實現 round-robin I/O。

        所有程式自動受益（kernel-level swap）。
        """
        report = report or (lambda msg: None)

        try:
            from .linux_swap import LinuxSwapEngine
        except ImportError as e:
            logger.warning("LinuxSwapEngine not available: %s", e)
            return self._activate_linux_single(mount_point, use_pct, report)

        # 掃描所有外接掛載點
        engine = LinuxSwapEngine()
        mount_points = engine._scan_mount_points(mount_point)
        if not mount_points:
            mount_points = [mount_point]
        report(f"found {len(mount_points)} mount point(s)")

        # 多裝置平行啟用
        result = engine.activate(
            mount_points=mount_points,
            use_percent=use_pct,
            on_progress=report,
        )

        if result.get("success"):
            self._linux_swap_engine = engine
            self._active = True
            self._swap_size_bytes = int(result.get("added_gb", 0) * (1024 ** 3))

            # 記錄系統 swap 狀態
            self._system_pf_bytes = self._get_system_pagefile_bytes()

            return {
                "success": True,
                "method": result.get("method", "linux_parallel_swap"),
                "system_wide": True,
                "device_count": result.get("device_count", 0),
                "devices": result.get("devices", []),
                "added_gb": result.get("added_gb", 0),
                "combined_speed_mbs": result.get("combined_speed_mbs", 0),
                "needs_reboot": False,
            }

        # Fallback: 單裝置模式
        logger.warning("Multi-device swap failed, falling back to single device")
        return self._activate_linux_single(mount_point, use_pct, report)

    def _activate_linux_single(self, mount_point: str, use_pct: float,
                                report=None) -> Dict[str, Any]:
        """Fallback: 單一裝置 swap（原始邏輯）"""
        report = report or (lambda msg: None)
        try:
            usage = shutil.disk_usage(mount_point)
        except OSError as e:
            return {"success": False, "error": str(e)}

        swap_path = Path(mount_point) / self.SWAP_FILENAME

        # 先算出速度公式的上限
        wanted_bytes = int(usage.free * (use_pct / 100))
        wanted_bytes, speed_warning_msg = self._cap_swap_by_speed(wanted_bytes)

        # 檢查上次的 swap 檔案是否可複用
        reuse = False
        if swap_path.exists() and self._verify_swap_file(swap_path):
            existing_size = swap_path.stat().st_size
            if existing_size <= wanted_bytes * 1.5:
                swap_bytes = existing_size
                reuse = True
                report(f"swap ({existing_size // (1024**3)}GB) reuse OK")
            else:
                report(f"swap too large ({existing_size // (1024**3)}GB > {wanted_bytes // (1024**3)}GB limit), rebuilding...")
                try:
                    swap_path.unlink()
                except OSError:
                    pass

        if not reuse:
            swap_bytes = wanted_bytes

        swap_mb = swap_bytes // (1024 * 1024)

        if swap_mb < 512:
            return {"success": False, "error": f"Not enough space: {swap_mb}MB"}

        try:
            if not reuse:
                report(f"building swap ({swap_mb // 1024}GB)...")
                _run_hidden(
                    ["dd", "if=/dev/zero", f"of={swap_path}", "bs=1M", f"count={swap_mb}"],
                    timeout=300, check=True,
                )
                report("設定 swap 權限...")
                _run_hidden(["chmod", "600", str(swap_path)], check=True)
                report("格式化 swap...")
                _run_hidden(["mkswap", str(swap_path)], check=True)

            report("啟用 swap...")
            _run_hidden(["swapon", str(swap_path)], check=True)

            self._swap_path = swap_path
            self._swap_size_bytes = swap_bytes
            self._active = True

            added_gb = swap_bytes / (1024 ** 3)
            method = "swap_reuse" if reuse else "swap_file"
            logger.info("Linux swap %s: %s (%.1fGB)",
                        "reused" if reuse else "created", swap_path, added_gb)

            result = {
                "success": True,
                "method": method,
                "reused": reuse,
                "swap_path": str(swap_path),
                "added_gb": round(added_gb, 1),
                "rand_write_mbs": round(self._measured_rand_write_mbs, 1),
                "needs_reboot": False,  # Linux swap 透過 swapon 立即生效
            }
            if speed_warning_msg:
                result["warning"] = speed_warning_msg
            return result

        except subprocess.CalledProcessError as e:
            return {"success": False, "error": str(e)}

    def _deactivate_linux(self) -> Dict[str, Any]:
        """取消所有 swap，保留檔案供下次快速啟動"""
        # 多裝置引擎清理
        if self._linux_swap_engine:
            try:
                self._linux_swap_engine.deactivate()
            except Exception as e:
                logger.warning("Linux swap engine deactivate error: %s", e)
            self._linux_swap_engine = None

        # 單裝置 fallback 清理
        if self._swap_path:
            try:
                _run_hidden(["swapoff", str(self._swap_path)], timeout=30)
                logger.info("Swap disabled, file kept for reuse: %s", self._swap_path)
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning("Swapoff error: %s", e)

        self._active = False
        self._swap_path = None
        self._swap_size_bytes = 0
        logger.info("Linux swap deactivated")
        return {"success": True}
