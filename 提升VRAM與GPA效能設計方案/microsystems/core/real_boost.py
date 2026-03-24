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
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Callable

from .mmap_engine import MmapSwapEngine
from .striped_swap import StripedSwapScheduler

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
        result = {
            "active": self._active,
            "swap_path": str(self._swap_path) if self._swap_path else None,
            "swap_size_gb": self._swap_size_bytes / (1024 ** 3),
            "physical_ram_gb": mem.get("physical_total", 0) / (1024 ** 3),
            "available_ram_gb": mem.get("physical_available", 0) / (1024 ** 3),
            "swap_total_gb": mem.get("swap_total", 0) / (1024 ** 3),
            "swap_used_gb": mem.get("swap_used", 0) / (1024 ** 3),
            "total_usable_gb": (mem.get("physical_total", 0) + mem.get("swap_total", 0)) / (1024 ** 3),
        }
        # 多裝置聚合狀態
        engines = getattr(self, '_mmap_engines', [])
        # Legacy single-engine fallback
        if not engines and hasattr(self, '_mmap_engine') and self._mmap_engine:
            engines = [self._mmap_engine]
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
        多裝置 mmap swap：掃描所有外接裝置，各自建立 mmap swap。
        零 C:\\ 佔用，資訊透過 GUI 顯示。
        """
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

            # 用等效速度計算 swap 上限
            old_speed = self._measured_rand_write_mbs
            self._measured_rand_write_mbs = effective_speed
            wanted = int(usage.free * (use_pct / 100))
            wanted, _ = self._cap_swap_by_speed(wanted)
            self._measured_rand_write_mbs = old_speed  # restore

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
            )

            if result.get("success"):
                self._mmap_engines.append(engine)
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

        self._swap_size_bytes = total_swap_bytes
        self._active = True

        after_mem = self.get_system_memory()
        total_gb = total_swap_bytes / (1024 ** 3)

        striped_speed = self._striped.total_speed_mbs

        return {
            "success": True,
            "method": "striped_mmap_swap" if len(self._mmap_engines) > 1 else "mmap_swap",
            "device_count": len(self._mmap_engines),
            "devices": device_details,
            "added_gb": round(total_gb, 1),
            "total_usable_gb": round(
                after_mem.get("physical_total", 0) / (1024**3) + total_gb, 1),
            "combined_speed_mbs": round(striped_speed, 1),
            "rand_write_mbs": round(self._measured_rand_write_mbs, 1),
            "needs_reboot": False,
        }

    @staticmethod
    def _benchmark_sequential_write(mount_path: str, size_mb: int = 16) -> float:
        """順序寫入測速（Write-Back Buffer 的等效速度基準）"""
        test_file = Path(mount_path) / ".vram_seq_test"
        chunk = os.urandom(1024 * 1024)  # 1MB
        try:
            start = time.perf_counter()
            with open(test_file, "wb", buffering=0) as f:
                for _ in range(size_mb):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
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
        """關閉所有裝置的 mmap swap + striped scheduler。"""
        if hasattr(self, '_striped') and self._striped:
            self._striped.close()
            self._striped = None
        for engine in getattr(self, '_mmap_engines', []):
            try:
                engine.deactivate()
            except (OSError, Exception) as e:
                logger.warning("Deactivate error: %s", e)
        self._mmap_engines = []
        # Legacy single-engine cleanup
        if hasattr(self, '_mmap_engine') and self._mmap_engine:
            self._mmap_engine.deactivate()
            self._mmap_engine = None
        self._active = False
        self._swap_path = None
        self._swap_size_bytes = 0
        return {"success": True}

    # ── Linux: Swap File ──

    def _activate_linux(self, mount_point: str, use_pct: float,
                        report=None) -> Dict[str, Any]:
        """在 Linux 上建立 swap file"""
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
        """取消 swap，但保留檔案供下次快速啟動"""
        if self._swap_path:
            try:
                _run_hidden(["swapoff", str(self._swap_path)], timeout=30)
                # 保留 swap 檔案在 SD 卡上，下次插入可直接複用
                logger.info("Swap disabled, file kept for reuse: %s", self._swap_path)
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning("Swapoff error: %s", e)

        self._active = False
        self._swap_path = None
        self._swap_size_bytes = 0
        logger.info("Linux swap deactivated")
        return {"success": True}
