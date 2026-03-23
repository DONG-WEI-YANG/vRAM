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

logger = logging.getLogger(__name__)

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

    def _load_cached_config(self, mount_path: str) -> Optional[Dict[str, Any]]:
        """讀取 SD 卡上的快取設定"""
        config_path = Path(mount_path) / self.CONFIG_FILENAME
        if not config_path.exists():
            return None
        try:
            import json
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if "rand_write_mbs" in data and "swap_size_bytes" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def _save_config(self, mount_path: str, config: Dict[str, Any]) -> None:
        """將設定存到 SD 卡，下次插入可直接使用"""
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
            report(f"讀取上次記錄：{cached_speed:.0f} MB/s，快速驗證中...")

            # 快速驗證：256KB 測速 < 1 秒
            quick_speed = self._quick_speed_check(mount)
            drift = abs(quick_speed - cached_speed) / max(cached_speed, 1)

            if drift <= self.SPEED_DRIFT_THRESHOLD:
                # 速度穩定，使用快取
                self._measured_rand_write_mbs = cached_speed
                report(f"速度穩定（{quick_speed:.0f} ≈ {cached_speed:.0f} MB/s），使用上次設定")
                need_full_benchmark = False
            else:
                report(f"速度變化較大（{quick_speed:.0f} vs {cached_speed:.0f} MB/s），重新完整測速...")

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
        return {
            "active": self._active,
            "swap_path": str(self._swap_path) if self._swap_path else None,
            "swap_size_gb": self._swap_size_bytes / (1024 ** 3),
            "physical_ram_gb": mem.get("physical_total", 0) / (1024 ** 3),
            "available_ram_gb": mem.get("physical_available", 0) / (1024 ** 3),
            "swap_total_gb": mem.get("swap_total", 0) / (1024 ** 3),
            "swap_used_gb": mem.get("swap_used", 0) / (1024 ** 3),
            "total_usable_gb": (mem.get("physical_total", 0) + mem.get("swap_total", 0)) / (1024 ** 3),
        }

    @staticmethod
    def get_system_memory() -> Dict[str, int]:
        """取得系統記憶體真實數據"""
        mem = {"physical_total": 0, "physical_available": 0, "swap_total": 0, "swap_used": 0}

        if platform.system().lower() == "windows":
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_OperatingSystem | "
                     "Select-Object TotalVisibleMemorySize,FreePhysicalMemory,"
                     "TotalVirtualMemorySize,FreeVirtualMemory | ConvertTo-Json"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    import json
                    d = json.loads(r.stdout)
                    mem["physical_total"] = d.get("TotalVisibleMemorySize", 0) * 1024
                    mem["physical_available"] = d.get("FreePhysicalMemory", 0) * 1024
                    total_virtual = d.get("TotalVirtualMemorySize", 0) * 1024
                    mem["swap_total"] = total_virtual - mem["physical_total"]
                    free_virtual = d.get("FreeVirtualMemory", 0) * 1024
                    mem["swap_used"] = mem["swap_total"] - (free_virtual - mem["physical_available"])
            except Exception:
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
            except Exception:
                pass

        return mem

    @staticmethod
    def get_gpu_info() -> Dict[str, Any]:
        """取得 GPU 真實資訊"""
        info = {"name": "Unknown", "vram_total_mb": 0, "vram_used_mb": 0, "vram_free_mb": 0}
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
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
        return info

    # ── Windows: Pagefile ──

    def _activate_windows(self, letter: str, use_pct: float,
                          report=None) -> Dict[str, Any]:
        """在 Windows 上建立 pagefile"""
        report = report or (lambda msg: None)
        mount = f"{letter}:\\"
        try:
            usage = shutil.disk_usage(mount)
        except OSError as e:
            return {"success": False, "error": f"Cannot access {mount}: {e}"}

        # 計算 swap 大小 (可用空間的 use_pct%)
        swap_bytes = int(usage.free * (use_pct / 100))

        # 根據裝置速度限制 swap 大小
        swap_bytes, speed_warning = self._cap_swap_by_speed(swap_bytes)

        swap_mb = swap_bytes // (1024 * 1024)

        if swap_mb < 512:
            return {"success": False, "error": f"Not enough space: {swap_mb}MB < 512MB minimum"}

        swap_path = f"{letter}:\\{self.SWAP_FILENAME}"
        report(f"建立 pagefile ({swap_mb // 1024}GB)...")

        try:
            # 用 wmic 建立 pagefile (需要管理員權限)
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"$pf = Get-WmiObject Win32_PageFileSetting -Filter \"Name='{swap_path.replace(chr(92), chr(92)+chr(92))}'\" -ErrorAction SilentlyContinue; "
                 f"if (-not $pf) {{ "
                 f"  $pf = ([WmiClass]'Win32_PageFileSetting').CreateInstance(); "
                 f"  $pf.Name = '{swap_path}'; "
                 f"  $pf.InitialSize = {swap_mb}; "
                 f"  $pf.MaximumSize = {swap_mb}; "
                 f"  $pf.Put() "
                 f"}}"],
                capture_output=True, text=True, timeout=30,
            )

            if r.returncode != 0:
                return {
                    "success": False,
                    "error": "需要管理員權限才能建立 pagefile。請以系統管理員身份執行。",
                    "rand_write_mbs": round(self._measured_rand_write_mbs, 1),
                }

            self._swap_path = Path(swap_path)
            self._swap_size_bytes = swap_bytes
            self._active = True

            after_mem = self.get_system_memory()
            added_gb = swap_bytes / (1024 ** 3)

            logger.info("Windows pagefile created: %s (%.1fGB)", swap_path, added_gb)

            result = {
                "success": True,
                "method": "pagefile",
                "swap_path": swap_path,
                "added_gb": round(added_gb, 1),
                "total_usable_gb": round(after_mem.get("physical_total", 0) / (1024**3) + added_gb, 1),
                "rand_write_mbs": round(self._measured_rand_write_mbs, 1),
            }
            if speed_warning:
                result["warning"] = speed_warning
            return result

        except Exception as e:
            return {"success": False, "error": str(e)}

    # 已移除 _activate_windows_fallback：
    # 舊的 fallback 建立稀疏檔案但從未 mmap()，報告成功卻實際增加 0 bytes 記憶體。
    # pagefile 建立需要管理員權限，沒有安全的非特權替代方案。

    def _deactivate_windows(self) -> Dict[str, Any]:
        """移除 Windows pagefile/swap"""
        if self._swap_path:
            try:
                # 嘗試移除 WMI pagefile
                swap_str = str(self._swap_path).replace("\\", "\\\\")
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"$pf = Get-WmiObject Win32_PageFileSetting -Filter \"Name='{swap_str}'\" -ErrorAction SilentlyContinue; "
                     "if ($pf) { $pf.Delete() }"],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

            # 刪除檔案
            try:
                self._swap_path.unlink(missing_ok=True)
            except OSError:
                pass

        self._active = False
        self._swap_path = None
        self._swap_size_bytes = 0
        logger.info("Windows swap deactivated")
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

        swap_bytes = int(usage.free * (use_pct / 100))

        # 根據裝置速度限制 swap 大小
        swap_bytes, speed_warning = self._cap_swap_by_speed(swap_bytes)

        swap_mb = swap_bytes // (1024 * 1024)
        swap_path = Path(mount_point) / self.SWAP_FILENAME

        if swap_mb < 512:
            return {"success": False, "error": f"Not enough space: {swap_mb}MB"}

        try:
            report(f"建立 swap 檔案 ({swap_mb // 1024}GB)，請稍候...")
            subprocess.run(
                ["dd", "if=/dev/zero", f"of={swap_path}", "bs=1M", f"count={swap_mb}"],
                capture_output=True, timeout=300, check=True,
            )
            report("設定 swap 權限...")
            subprocess.run(["chmod", "600", str(swap_path)], check=True)
            report("格式化 swap...")
            subprocess.run(["mkswap", str(swap_path)], capture_output=True, check=True)
            report("啟用 swap...")
            subprocess.run(["swapon", str(swap_path)], capture_output=True, check=True)

            self._swap_path = swap_path
            self._swap_size_bytes = swap_bytes
            self._active = True

            added_gb = swap_bytes / (1024 ** 3)
            logger.info("Linux swap activated: %s (%.1fGB)", swap_path, added_gb)

            result = {
                "success": True,
                "method": "swap_file",
                "swap_path": str(swap_path),
                "added_gb": round(added_gb, 1),
                "rand_write_mbs": round(self._measured_rand_write_mbs, 1),
            }
            if speed_warning:
                result["warning"] = speed_warning
            return result

        except subprocess.CalledProcessError as e:
            return {"success": False, "error": str(e)}

    def _deactivate_linux(self) -> Dict[str, Any]:
        if self._swap_path:
            try:
                subprocess.run(["swapoff", str(self._swap_path)], capture_output=True, timeout=30)
                self._swap_path.unlink(missing_ok=True)
            except Exception:
                pass

        self._active = False
        self._swap_path = None
        self._swap_size_bytes = 0
        logger.info("Linux swap deactivated")
        return {"success": True}
