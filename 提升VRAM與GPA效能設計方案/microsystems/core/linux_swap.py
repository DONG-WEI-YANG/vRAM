"""
Linux Multi-Device Swap Engine
================================
在多個外接裝置上建立 swap file，透過 swapon -p 同優先級平行 I/O。

Linux 不需要 VHD 繞路：
  swapon -p -1 /mnt/sd1/vram_boost.swap   # SD card 1
  swapon -p -1 /mnt/sd2/vram_boost.swap   # SD card 2
  swapon -p -1 /mnt/usb/vram_boost.swap   # USB drive
  → kernel round-robin across all → 合計頻寬 ≈ 各裝置之和
  → 所有程式自動受益（kernel-level swap）

安全策略（與 Windows VHD 版一致）：
  - 低優先級 (-1)：比系統內建 swap 低，溢出時才使用外接
  - 拔卡時外接 swap 上幾乎無 active pages → 安全
  - get_swap_usage() 可查詢 /proc/swaps 確認是否 safe_to_remove
  - safe_remove_device() 先 swapoff（kernel 遷移 pages）再安全移除
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 常數 ─────────────────────────────────────────────────────────────────

# swap 大小 = 實測隨機寫入速度 × 可接受的最大寫滿秒數
# 原理：OS 高壓 swap 時可能寫滿整個 swap 空間，
#       寫滿時間過長 → 用戶感覺系統凍結。
SWAP_FILL_TIME_SECONDS = 600       # 10 分鐘內能寫滿 = 可接受的上限
SWAP_MIN_BYTES = 512 * (1024 ** 2) # 最小 512 MB（太小沒意義）

# swap 檔案中 magic bytes 的位置與內容
# Linux swap header: offset 4086 起的 10 bytes = b"SWAPSPACE2"
_SWAP_MAGIC_OFFSET = 4086
_SWAP_MAGIC = b"SWAPSPACE2"

# 健康監控間隔
_MONITOR_INTERVAL_SECONDS = 10


# ── 資料類別 ──────────────────────────────────────────────────────────────


class SwapDevice:
    """Represents one swap file on one external device."""

    def __init__(
        self,
        mount_point: str,
        swap_path: str,
        size_bytes: int,
        speed_mbs: float,
    ):
        self.mount_point = mount_point
        self.swap_path = swap_path
        self.size_bytes = size_bytes
        self.speed_mbs = speed_mbs
        self.active = False
        self.degraded = False
        self.reused = False


# ── 子程序執行輔助 ────────────────────────────────────────────────────────


def _run_cmd(
    cmd: List[str],
    timeout: int = 60,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """
    執行子程序，統一 timeout 與錯誤處理。

    不使用 _run_hidden（那是 Windows 專用），直接用 subprocess.run。
    """
    logger.debug("執行命令: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
        return result
    except subprocess.TimeoutExpired:
        logger.error("命令逾時 (%ds): %s", timeout, " ".join(cmd))
        raise
    except subprocess.CalledProcessError as exc:
        logger.error(
            "命令失敗 (rc=%d): %s\nstderr: %s",
            exc.returncode, " ".join(cmd), exc.stderr,
        )
        raise


# ── 主引擎 ────────────────────────────────────────────────────────────────


class LinuxSwapEngine:
    """
    多裝置平行 swap 引擎。

    Usage::

        engine = LinuxSwapEngine()
        result = engine.activate(["/mnt/sd1", "/mnt/usb"], use_percent=80)
        print(engine.status())
        engine.deactivate()
    """

    SWAP_FILENAME = "vram_boost.swap"
    SWAP_FILL_TIME_SECONDS = SWAP_FILL_TIME_SECONDS
    SWAP_MIN_BYTES = SWAP_MIN_BYTES
    # Linux swap priority: 高數字 = 優先使用
    # 系統預設 swap (C:\ 等效) 通常 priority >= 0
    # 我們設 -1 → 比系統低 → 溢出時才用外接
    # 多個外接裝置之間用相同 priority → 彼此 round-robin
    SWAP_PRIORITY = -1  # 比系統低，溢出時才使用

    def __init__(self):
        self._devices: List[SwapDevice] = []
        self._lock = threading.RLock()
        self._active = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────────

    def activate(
        self,
        mount_points: List[str],
        use_percent: float = 80.0,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """
        在多個掛載點上建立並啟用 swap file。

        對每個掛載點依序執行：
          1. 檢查剩餘空間
          2. 隨機 4KB 寫入測速
          3. 依速度公式計算 swap 大小
          4. 檢查是否可重用既有 swap 檔案
          5. 建立 swap 檔案 (fallocate 或 dd)
          6. mkswap + chmod 600
          7. swapon -p SWAP_PRIORITY (低優先級 = 溢出時才用外接)
          8. 啟動監控執行緒

        Args:
            mount_points: 外接裝置掛載點路徑列表
            use_percent:  使用每個裝置可用空間的百分比 (0-100)
            on_progress:  進度回呼函式，接收狀態字串

        Returns:
            結果字典，含 success / added_gb / device_count 等資訊
        """
        with self._lock:
            if self._active:
                return {
                    "success": False,
                    "error": "引擎已在運作中，請先 deactivate()",
                }

            def _report(msg: str):
                logger.info(msg)
                if on_progress:
                    try:
                        on_progress(msg)
                    except Exception:
                        pass

            self._devices.clear()
            succeeded: List[Dict[str, Any]] = []
            errors: List[str] = []

            for mount in mount_points:
                _report(f"處理裝置: {mount}")
                try:
                    dev = self._setup_single_device(mount, use_percent, _report)
                    if dev is not None:
                        self._devices.append(dev)
                        succeeded.append({
                            "mount": dev.mount_point,
                            "swap_gb": round(dev.size_bytes / (1024 ** 3), 2),
                            "speed_mbs": round(dev.speed_mbs, 1),
                            "reused": dev.reused,
                        })
                        _report(
                            f"  ✓ {mount}: "
                            f"{dev.size_bytes / (1024**3):.1f} GB, "
                            f"{dev.speed_mbs:.1f} MB/s"
                            f"{' (重用)' if dev.reused else ''}"
                        )
                except Exception as exc:
                    msg = f"裝置 {mount} 設定失敗: {exc}"
                    logger.error(msg, exc_info=True)
                    errors.append(msg)
                    _report(f"  ✗ {mount}: {exc}")

            if not self._devices:
                return {
                    "success": False,
                    "method": "linux_parallel_swap",
                    "system_wide": True,
                    "device_count": 0,
                    "devices": [],
                    "added_gb": 0,
                    "priority": self.SWAP_PRIORITY,
                    "combined_speed_mbs": 0,
                    "needs_reboot": False,
                    "errors": errors,
                }

            # 啟動裝置監控
            self._active = True
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="linux-swap-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

            total_bytes = sum(d.size_bytes for d in self._devices)
            combined_speed = sum(d.speed_mbs for d in self._devices)

            result = {
                "success": True,
                "method": "linux_parallel_swap",
                "system_wide": True,
                "device_count": len(self._devices),
                "devices": succeeded,
                "added_gb": round(total_bytes / (1024 ** 3), 2),
                "priority": self.SWAP_PRIORITY,
                "combined_speed_mbs": round(combined_speed, 1),
                "needs_reboot": False,
            }
            if errors:
                result["warnings"] = errors

            _report(
                f"完成：{len(self._devices)} 個裝置, "
                f"共 {total_bytes / (1024**3):.1f} GB, "
                f"合計 {combined_speed:.1f} MB/s"
            )
            return result

    def deactivate(self) -> Dict[str, Any]:
        """
        swapoff 所有裝置，保留檔案以便下次重用。

        Returns:
            結果字典，含 success 與各裝置 swapoff 狀態
        """
        with self._lock:
            if not self._active and not self._devices:
                return {"success": True, "note": "引擎未在運作中"}

            # 停止監控執行緒
            self._stop_event.set()
            if self._monitor_thread and self._monitor_thread.is_alive():
                self._monitor_thread.join(timeout=15)
            self._monitor_thread = None

            results = []
            for dev in self._devices:
                if not dev.active:
                    results.append({
                        "mount": dev.mount_point,
                        "swapoff": True,
                        "note": "已處於非活動狀態",
                    })
                    continue

                try:
                    r = _run_cmd(
                        ["swapoff", dev.swap_path],
                        timeout=30,
                    )
                    ok = r.returncode == 0
                    dev.active = not ok
                    results.append({
                        "mount": dev.mount_point,
                        "swapoff": ok,
                        "stderr": r.stderr.strip() if not ok else "",
                    })
                    if ok:
                        logger.info("swapoff 成功: %s", dev.swap_path)
                    else:
                        logger.warning(
                            "swapoff 失敗 (rc=%d): %s — %s",
                            r.returncode, dev.swap_path, r.stderr.strip(),
                        )
                except Exception as exc:
                    logger.error("swapoff 例外: %s — %s", dev.swap_path, exc)
                    results.append({
                        "mount": dev.mount_point,
                        "swapoff": False,
                        "error": str(exc),
                    })

            self._active = False
            # 不清除 _devices，保留資訊供 status() 查看

            all_ok = all(r.get("swapoff", False) for r in results)
            return {
                "success": all_ok,
                "devices": results,
            }

    def status(self) -> Dict[str, Any]:
        """
        回傳所有 swap 裝置的即時狀態。

        Returns:
            狀態字典，含 active、device_count 與各裝置詳細資訊
        """
        with self._lock:
            devices_info = []
            for dev in self._devices:
                usage = self.get_swap_usage(dev.swap_path)
                devices_info.append({
                    "mount_point": dev.mount_point,
                    "swap_path": dev.swap_path,
                    "size_gb": round(dev.size_bytes / (1024 ** 3), 2),
                    "speed_mbs": round(dev.speed_mbs, 1),
                    "active": dev.active,
                    "degraded": dev.degraded,
                    "reused": dev.reused,
                    "used_kb": usage["used_kb"],
                    "safe_to_remove": usage["safe_to_remove"],
                })

            total_bytes = sum(d.size_bytes for d in self._devices)
            active_count = sum(1 for d in self._devices if d.active)
            degraded_count = sum(1 for d in self._devices if d.degraded)
            combined_speed = sum(
                d.speed_mbs for d in self._devices if d.active and not d.degraded
            )

            return {
                "engine": "LinuxSwapEngine",
                "active": self._active,
                "device_count": len(self._devices),
                "active_count": active_count,
                "degraded_count": degraded_count,
                "total_gb": round(total_bytes / (1024 ** 3), 2),
                "combined_speed_mbs": round(combined_speed, 1),
                "priority": self.SWAP_PRIORITY,
                "devices": devices_info,
            }

    # ── 安全移除 ──────────────────────────────────────────────────────

    @staticmethod
    def get_swap_usage(swap_path: str) -> Dict[str, int]:
        """
        查詢指定 swap 檔案的使用狀況。

        讀取 /proc/swaps 解析。

        Returns:
            {"size_kb": N, "used_kb": N, "safe_to_remove": bool}
        """
        try:
            with open("/proc/swaps") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == swap_path:
                        size_kb = int(parts[2])
                        used_kb = int(parts[3])
                        return {
                            "size_kb": size_kb,
                            "used_kb": used_kb,
                            "safe_to_remove": used_kb == 0,
                        }
        except (OSError, ValueError) as e:
            logger.debug("Swap usage query failed for %s: %s", swap_path, e)

        return {"size_kb": 0, "used_kb": 0, "safe_to_remove": True}

    def safe_remove_device(self, mount_point: str,
                           on_progress=None) -> Dict[str, Any]:
        """
        安全移除指定裝置的 swap。

        流程：
        1. 檢查 swap 使用量
        2. swapoff（kernel 會自動遷移 pages 回 RAM 或其他 swap）
        3. 回報安全可拔裝置
        """
        report = on_progress or (lambda msg: logger.info(msg))

        with self._lock:
            dev = None
            for d in self._devices:
                if d.mount_point == mount_point:
                    dev = d
                    break

        if dev is None:
            return {"success": False, "safe_to_remove": False,
                    "message": f"Device {mount_point}: not found"}

        # 檢查使用量
        usage = self.get_swap_usage(dev.swap_path)
        report(f"{mount_point}: swap usage = {usage['used_kb']} KB")

        # swapoff（kernel 自動遷移 active pages）
        if dev.active:
            report(f"{mount_point}: swapoff (kernel migrating pages)...")
            try:
                r = subprocess.run(
                    ["swapoff", dev.swap_path],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0:
                    dev.active = False
                    report(f"{mount_point}: swap removed, safe to eject")
                else:
                    report(f"{mount_point}: swapoff failed: {r.stderr.strip()}")
                    return {"success": False, "safe_to_remove": False,
                            "message": f"swapoff failed: {r.stderr.strip()}"}
            except subprocess.TimeoutExpired:
                report(f"{mount_point}: swapoff timeout (60s)")
                return {"success": False, "safe_to_remove": False,
                        "message": "swapoff timeout"}

        # 從 device list 移除
        with self._lock:
            if dev in self._devices:
                self._devices.remove(dev)

        return {"success": True, "safe_to_remove": True,
                "message": f"Device {mount_point}: safely ejected"}

    # ── 單裝置設定 ────────────────────────────────────────────────────

    def _setup_single_device(
        self,
        mount_point: str,
        use_percent: float,
        report: Callable[[str], None],
    ) -> Optional[SwapDevice]:
        """
        在單一裝置上建立並啟用 swap file。

        流程：
          1. 檢查磁碟使用量
          2. 隨機 4KB 寫入測速
          3. 依速度公式限制 swap 大小
          4. 檢查既有 swap 檔是否可重用
          5. 建立 swap 檔 (fallocate 優先, dd 備援)
          6. chmod 600
          7. mkswap
          8. swapon -p {SWAP_PRIORITY}

        Returns:
            SwapDevice 物件，失敗時回傳 None
        """
        # 1. 檢查磁碟使用量
        try:
            usage = shutil.disk_usage(mount_point)
        except OSError as exc:
            report(f"  無法讀取磁碟空間: {mount_point} — {exc}")
            return None

        free_bytes = usage.free
        report(
            f"  磁碟空間: 總計 {usage.total / (1024**3):.1f} GB, "
            f"可用 {free_bytes / (1024**3):.1f} GB"
        )

        # 2. 測速（隨機 4KB 寫入）
        report("  測速中（隨機 4KB 寫入）...")
        speed_mbs = self._benchmark_random_write(mount_point, test_size_mb=4)
        report(f"  隨機寫入速度: {speed_mbs:.1f} MB/s")

        # 3. 計算 swap 大小
        desired_bytes = int(free_bytes * (use_percent / 100.0))
        # 依速度上限：speed × fill_time
        speed_cap_bytes = int(speed_mbs * (1024 ** 2) * self.SWAP_FILL_TIME_SECONDS)
        speed_cap_bytes = max(speed_cap_bytes, self.SWAP_MIN_BYTES)

        swap_bytes = min(desired_bytes, speed_cap_bytes)
        swap_bytes = max(swap_bytes, self.SWAP_MIN_BYTES)

        # 不能超過可用空間（留 100 MB 緩衝）
        max_safe = max(free_bytes - 100 * (1024 ** 2), 0)
        if swap_bytes > max_safe:
            swap_bytes = max_safe

        if swap_bytes < self.SWAP_MIN_BYTES:
            report(
                f"  可用空間不足: 需要 {self.SWAP_MIN_BYTES / (1024**2):.0f} MB, "
                f"只有 {max_safe / (1024**2):.0f} MB"
            )
            return None

        if desired_bytes > speed_cap_bytes:
            report(
                f"  速度限制: {speed_mbs:.0f} MB/s → "
                f"swap 上限 {speed_cap_bytes / (1024**3):.1f} GB "
                f"(原 {desired_bytes / (1024**3):.1f} GB)"
            )

        swap_path = os.path.join(mount_point, self.SWAP_FILENAME)

        # 4. 檢查是否可重用既有 swap 檔案
        reused = False
        if os.path.isfile(swap_path):
            existing_size = os.path.getsize(swap_path)
            # 大小在 50% ~ 120% 之間且檔案格式正確就重用
            size_ok = (existing_size >= swap_bytes * 0.5 and
                       existing_size <= swap_bytes * 1.2)
            if size_ok and self._verify_swap_file(swap_path):
                report(
                    f"  重用既有 swap 檔: {existing_size / (1024**3):.1f} GB"
                )
                swap_bytes = existing_size
                reused = True
            else:
                # 大小不合或格式不對，刪除重建
                report("  既有 swap 檔不符合條件，將重建")
                try:
                    os.remove(swap_path)
                except OSError as exc:
                    logger.warning("刪除舊 swap 檔失敗: %s — %s", swap_path, exc)

        # 5. 建立 swap 檔案
        if not reused:
            report(f"  建立 swap 檔: {swap_bytes / (1024**3):.1f} GB ...")
            if not self._create_swap_file(swap_path, swap_bytes, report):
                return None

        # 6. chmod 600（安全性要求）
        try:
            os.chmod(swap_path, 0o600)
        except OSError as exc:
            logger.warning("chmod 600 失敗: %s — %s", swap_path, exc)
            # 非致命，繼續

        # 7. mkswap（重用時跳過）
        if not reused:
            report("  格式化 swap (mkswap)...")
            r = _run_cmd(["mkswap", swap_path], timeout=120)
            if r.returncode != 0:
                report(f"  mkswap 失敗: {r.stderr.strip()}")
                return None

        # 8. swapon -p SWAP_PRIORITY
        report(f"  啟用 swap (swapon -p {self.SWAP_PRIORITY})...")
        r = _run_cmd(
            ["swapon", "-p", str(self.SWAP_PRIORITY), swap_path],
            timeout=30,
        )
        if r.returncode != 0:
            report(f"  swapon 失敗: {r.stderr.strip()}")
            return None

        dev = SwapDevice(
            mount_point=mount_point,
            swap_path=swap_path,
            size_bytes=swap_bytes,
            speed_mbs=speed_mbs,
        )
        dev.active = True
        dev.reused = reused
        return dev

    # ── Swap 檔案建立 ─────────────────────────────────────────────────

    def _create_swap_file(
        self,
        swap_path: str,
        size_bytes: int,
        report: Callable[[str], None],
    ) -> bool:
        """
        建立 swap 檔案。
        優先用 fallocate（瞬間完成，無 I/O），失敗時退回 dd。

        Args:
            swap_path:  swap 檔案完整路徑
            size_bytes: 檔案大小 (bytes)
            report:     進度回呼

        Returns:
            True = 建立成功
        """
        # 嘗試 fallocate（某些檔案系統不支援，例如 ZFS、某些 tmpfs）
        if self._try_fallocate(swap_path, size_bytes):
            report(f"  fallocate 成功: {size_bytes / (1024**3):.1f} GB")
            return True

        # fallocate 失敗，退回 dd
        report("  fallocate 不支援，改用 dd（較慢）...")
        return self._create_with_dd(swap_path, size_bytes, report)

    @staticmethod
    def _try_fallocate(swap_path: str, size_bytes: int) -> bool:
        """
        用 fallocate 建立預配置檔案（瞬間完成）。
        某些檔案系統不支援，回傳 False 時呼叫端應退回 dd。
        """
        try:
            r = _run_cmd(
                ["fallocate", "-l", str(size_bytes), swap_path],
                timeout=30,
            )
            if r.returncode == 0 and os.path.isfile(swap_path):
                actual = os.path.getsize(swap_path)
                if actual >= size_bytes:
                    return True
            # fallocate 指令回傳錯誤
            logger.info("fallocate 未成功 (rc=%d): %s", r.returncode, r.stderr.strip())
        except Exception as exc:
            logger.info("fallocate 不可用: %s", exc)

        # 清理可能產生的不完整檔案
        try:
            if os.path.isfile(swap_path):
                os.remove(swap_path)
        except OSError:
            pass
        return False

    @staticmethod
    def _create_with_dd(
        swap_path: str,
        size_bytes: int,
        report: Callable[[str], None],
    ) -> bool:
        """
        用 dd if=/dev/zero 建立 swap 檔案。
        比 fallocate 慢（需要實際寫入），但通用性高。
        """
        mb = size_bytes // (1024 ** 2)
        if mb < 1:
            mb = 1

        try:
            # dd 可能需要較長時間，大檔案給充足 timeout
            # 假設最慢 1 MB/s → timeout = mb + 120 秒緩衝
            timeout_s = mb + 120
            r = _run_cmd(
                [
                    "dd", "if=/dev/zero", f"of={swap_path}",
                    "bs=1M", f"count={mb}",
                    "conv=fsync",  # 確保寫入完成
                ],
                timeout=timeout_s,
            )
            if r.returncode != 0:
                report(f"  dd 失敗: {r.stderr.strip()}")
                return False

            # 驗證檔案大小
            if os.path.isfile(swap_path):
                actual = os.path.getsize(swap_path)
                if actual >= size_bytes * 0.95:  # 允許微小差異（bs 對齊）
                    return True
                report(
                    f"  dd 建立的檔案過小: 期望 {size_bytes}, 實際 {actual}"
                )
            return False

        except subprocess.TimeoutExpired:
            report("  dd 操作逾時")
            return False
        except Exception as exc:
            report(f"  dd 例外: {exc}")
            return False

    # ── 測速 ──────────────────────────────────────────────────────────

    @staticmethod
    def _benchmark_random_write(mount_path: str, test_size_mb: float = 4) -> float:
        """
        用隨機 4KB 寫入測試裝置速度，模擬 swap 的真實 I/O 模式。

        Args:
            mount_path:   掛載點路徑
            test_size_mb: 測試資料總量 (MB)

        Returns:
            隨機寫入速度 (MB/s)
        """
        test_file = os.path.join(mount_path, ".vram_speed_test")
        block_4kb = os.urandom(4096)
        total_bytes = int(test_size_mb * 1024 * 1024)
        num_writes = max(total_bytes // 4096, 1)

        try:
            # 先建立測試檔案
            with open(test_file, "wb") as f:
                f.seek(total_bytes - 1)
                f.write(b"\0")

            # 隨機位置寫入 4KB 區塊
            offsets = [
                random.randint(0, num_writes - 1) * 4096
                for _ in range(num_writes)
            ]

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

        except OSError as exc:
            logger.warning("測速失敗: %s", exc)
            return 5.0  # 保守預設：假設很慢
        finally:
            try:
                os.remove(test_file)
            except OSError:
                pass

    # ── Swap 檔案驗證 ─────────────────────────────────────────────────

    @staticmethod
    def _verify_swap_file(swap_path: str) -> bool:
        """
        驗證既有 swap 檔案是否為有效的 Linux swap 格式。

        檢查方法：讀取 offset 4086 處是否為 "SWAPSPACE2" magic bytes。
        若讀取失敗則退回 ``file`` 命令判斷。

        Args:
            swap_path: swap 檔案路徑

        Returns:
            True = 有效的 swap 檔案
        """
        # 方法一：直接讀取 magic bytes
        try:
            with open(swap_path, "rb") as f:
                f.seek(_SWAP_MAGIC_OFFSET)
                magic = f.read(len(_SWAP_MAGIC))
                if magic == _SWAP_MAGIC:
                    return True
        except OSError as exc:
            logger.debug("直接讀取 swap magic 失敗: %s", exc)

        # 方法二：用 file 命令（排除路徑本身含 "swap" 的誤判）
        try:
            r = _run_cmd(["file", swap_path], timeout=10)
            if r.returncode == 0:
                # 只看 file 輸出中冒號後的描述部分，避免路徑名誤判
                parts = r.stdout.split(":", 1)
                desc = parts[1].lower() if len(parts) > 1 else ""
                if "swap" in desc and "cannot open" not in desc:
                    return True
        except Exception as exc:
            logger.debug("file 命令失敗: %s", exc)

        return False

    # ── 掛載點掃描 ────────────────────────────────────────────────────

    @staticmethod
    def _scan_mount_points(primary: str = "") -> List[str]:
        """
        掃描所有外接（可卸除式）裝置掛載點。

        使用 lsblk -J 取得裝置資訊，篩選 RM=1（removable）
        且有有效掛載點的裝置。

        Args:
            primary: 主要掛載點（放在結果最前面，若有的話）

        Returns:
            掛載點路徑列表
        """
        mounts: List[str] = []

        try:
            r = _run_cmd(
                ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,RM,SIZE,TYPE"],
                timeout=10,
            )
            if r.returncode != 0:
                logger.warning("lsblk 失敗: %s", r.stderr.strip())
                return [primary] if primary else []

            data = json.loads(r.stdout)
            devices = data.get("blockdevices", [])

            for dev in devices:
                _collect_removable_mounts(dev, mounts)

        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("解析 lsblk 輸出失敗: %s", exc)
        except Exception as exc:
            logger.warning("掃描掛載點失敗: %s", exc)

        # 去重
        seen = set()
        unique: List[str] = []
        for m in mounts:
            if m not in seen:
                seen.add(m)
                unique.append(m)

        # 若有指定 primary，確保它在最前面
        if primary:
            if primary in unique:
                unique.remove(primary)
            unique.insert(0, primary)

        return unique

    # ── 監控迴圈 ──────────────────────────────────────────────────────

    def _monitor_loop(self):
        """
        背景監控執行緒：每 10 秒檢查一次裝置狀態。

        - 裝置消失（掛載點不可存取） → 標記 degraded
        - 裝置恢復 → 嘗試重新 swapon
        """
        logger.info("swap 監控執行緒啟動")
        while not self._stop_event.wait(timeout=_MONITOR_INTERVAL_SECONDS):
            with self._lock:
                for dev in self._devices:
                    present = self._check_device_present(dev.mount_point)

                    if present and dev.degraded:
                        # 裝置恢復 → 嘗試重新啟用
                        logger.info("裝置恢復: %s — 嘗試重新 swapon", dev.mount_point)
                        try:
                            r = _run_cmd(
                                [
                                    "swapon", "-p", str(self.SWAP_PRIORITY),
                                    dev.swap_path,
                                ],
                                timeout=15,
                            )
                            if r.returncode == 0:
                                dev.degraded = False
                                dev.active = True
                                logger.info(
                                    "重新啟用成功: %s", dev.swap_path,
                                )
                            else:
                                logger.warning(
                                    "重新 swapon 失敗: %s — %s",
                                    dev.swap_path, r.stderr.strip(),
                                )
                        except Exception as exc:
                            logger.warning(
                                "重新 swapon 例外: %s — %s",
                                dev.swap_path, exc,
                            )

                    elif not present and not dev.degraded and dev.active:
                        # 裝置消失 → 標記 degraded
                        logger.warning("裝置遺失: %s — 標記 degraded", dev.mount_point)
                        dev.degraded = True
                        dev.active = False

        logger.info("swap 監控執行緒結束")

    @staticmethod
    def _check_device_present(mount_point: str) -> bool:
        """
        檢查掛載點是否仍可存取。

        用 os.statvfs 確認掛載點存在且為有效的檔案系統。

        Args:
            mount_point: 掛載點路徑

        Returns:
            True = 掛載點可存取
        """
        try:
            if hasattr(os, 'statvfs'):
                # Linux: 用 statvfs 確認是真實掛載的檔案系統
                st = os.statvfs(mount_point)
                return st.f_blocks > 0
            else:
                # Windows fallback: 用 os.stat 確認路徑存在
                os.stat(mount_point)
                return True
        except OSError:
            return False

    # ── 魔法方法 ──────────────────────────────────────────────────────

    def __repr__(self) -> str:
        active_count = sum(1 for d in self._devices if d.active)
        total_count = len(self._devices)
        return (
            f"<LinuxSwapEngine devices={total_count} "
            f"active={active_count} running={self._active}>"
        )

    def __enter__(self) -> "LinuxSwapEngine":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.deactivate()


# ── 輔助函式 ──────────────────────────────────────────────────────────────


def _collect_removable_mounts(
    device: Dict[str, Any],
    result: List[str],
) -> None:
    """
    遞迴收集一個 lsblk 裝置及其子裝置的可卸除式掛載點。

    Args:
        device: lsblk JSON 中的單一裝置物件
        result: 收集結果列表（就地修改）
    """
    # RM 欄位: "1" 或 True 表示可卸除式裝置
    rm = device.get("rm")
    is_removable = rm in (True, "1", 1)

    mount = device.get("mountpoint")
    dev_type = device.get("type", "")

    if is_removable and mount and dev_type in ("part", "disk"):
        # 確認掛載點存在且可存取
        if os.path.isdir(mount):
            result.append(mount)

    # 遞迴檢查子裝置（分割區）
    children = device.get("children", [])
    for child in children:
        # 子裝置繼承父裝置的 RM 狀態
        if "rm" not in child:
            child["rm"] = rm
        _collect_removable_mounts(child, result)


# ── Standalone Test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=== LinuxSwapEngine 自我測試 ===")
    print()

    engine = LinuxSwapEngine()
    print(f"引擎物件: {engine!r}")
    print()

    # 掃描外接裝置
    print("掃描可卸除式裝置掛載點...")
    mounts = LinuxSwapEngine._scan_mount_points()
    if mounts:
        print(f"  找到 {len(mounts)} 個掛載點:")
        for m in mounts:
            print(f"    {m}")
    else:
        print("  未找到可卸除式裝置")
    print()

    # 狀態查詢（未啟用時）
    print("引擎狀態:")
    status = engine.status()
    for k, v in status.items():
        if k != "devices":
            print(f"  {k}: {v}")
    print()

    print("=== 測試完成 ===")
    print("（實際 activate 需要 root 權限和外接裝置，此處僅測試掃描與初始化）")
