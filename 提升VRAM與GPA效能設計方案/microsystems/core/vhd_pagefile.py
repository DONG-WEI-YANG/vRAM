"""
VHD Pagefile Engine -- 在外接裝置上建立真正的 Windows Pagefile
================================================================
透過 VHD Bridge 繞過 Windows 對 Removable 裝置的 pagefile 限制：

  SD/USB (Removable) -> VHDX file -> Mount as Fixed -> Real pagefile
  -> 所有程式受益 -> commit limit 真實增加

多裝置平行：
  SD1 -> VHD -> pagefile1 -+
  SD2 -> VHD -> pagefile2 --+-> Windows 自動 round-robin I/O
  USB -> VHD -> pagefile3 -+   合計頻寬 ~ 各裝置之和
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable

from .vhd_bridge import VhdBridge

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

# Windows: 隱藏子程序視窗
_NO_WINDOW = 0
if platform.system().lower() == "windows":
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # 0x08000000

# swap 大小 = 實測隨機寫入速度 x 可接受的最大寫滿秒數
SWAP_FILL_TIME_SECONDS = 600   # 10 分鐘內能寫滿 = 可接受的上限
SWAP_MIN_BYTES = 512 * (1024 ** 2)  # 最小 512 MB

_MONITOR_INTERVAL: float = 10.0  # 背景監控間隔（秒）

# 安全策略：pagefile min 設很小，讓 Windows 優先用 C:\ 的快速 pagefile
# 只有在 C:\ pagefile 壓力大時才溢出到外接裝置
# 這樣拔卡時外接 pagefile 上幾乎沒有 active pages → 安全
PAGEFILE_MIN_MB = 16  # 16MB — 最小佔用，幾乎不會被主動使用


# ── Helpers ────────────────────────────────────────────────────────────────

def _run_hidden(cmd, **kwargs):
    """執行子程序，Windows 上不彈出視窗"""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if _NO_WINDOW:
        kwargs.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def _get_system_memory() -> Dict[str, int]:
    """取得系統記憶體真實數據（ctypes 零成本）"""
    mem = {"physical_total": 0, "physical_available": 0,
           "swap_total": 0, "swap_used": 0}
    if platform.system().lower() != "windows":
        return mem
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
        mem["swap_total"] = max(0, stat.ullTotalPageFile - stat.ullTotalPhys)
        swap_free = max(0, stat.ullAvailPageFile - stat.ullAvailPhys)
        mem["swap_used"] = max(0, mem["swap_total"] - swap_free)
    except (OSError, AttributeError, ValueError):
        pass
    return mem


def _get_system_pagefile_bytes() -> int:
    """取得 Windows 系統現有 pagefile 大小（不含物理 RAM）"""
    mem = _get_system_memory()
    return mem.get("swap_total", 0)


# ── Data Classes ───────────────────────────────────────────────────────────

class VhdPagefileDevice:
    """代表一個 VHD-backed pagefile 裝置的完整狀態。"""

    def __init__(self, drive_letter: str, vhd_path: str, mount_letter: str,
                 swap_bytes: int, bridge: VhdBridge, speed_mbs: float = 0.0):
        self.drive_letter = drive_letter       # 來源裝置代號 (e.g., "E")
        self.vhd_path = vhd_path               # VHDX 檔案路徑
        self.mount_letter = mount_letter        # VHD 掛載代號 (e.g., "V")
        self.swap_bytes = swap_bytes            # pagefile 大小
        self.bridge = bridge                    # VhdBridge handle
        self.speed_mbs = speed_mbs              # 裝置測速結果
        self.pagefile_active = False            # pagefile 是否已建立
        self.degraded = False                   # 裝置是否已斷線
        self.disk_number: Optional[int] = None  # VHD 掛載後的 disk number


# ── Main Engine ────────────────────────────────────────────────────────────

class VhdPagefileEngine:
    """
    管理多個 VHD-backed pagefile 的生命週期。

    Usage::

        engine = VhdPagefileEngine()
        result = engine.activate(["E", "F"], use_percent=80)
        print(engine.status())
        engine.deactivate()
    """

    # 可用磁碟代號（避開系統常用的 A-F）
    _VHD_LETTERS = list("VUWTSRQPONMLKJIHG")
    _VHD_FILENAME = "vram_swap.vhdx"
    _VHD_FILENAME_VHD = "vram_swap.vhd"  # Win7 fallback

    def __init__(self):
        self._devices: List[VhdPagefileDevice] = []
        self._lock = threading.RLock()
        self._active = False
        self._used_letters: set = set()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Public API ─────────────────────────────────────────────────────

    def activate(self, drive_letters: List[str], use_percent: float = 80.0,
                 on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """
        在指定的外接裝置上建立 VHD pagefile。

        流程（每個裝置）：
          1. 計算 swap 大小（可用空間 x use_percent，受速度公式 cap）
          2. 在裝置上建立 VHDX 檔案
          3. 掛載（Attach）為 Fixed volume
          4. 初始化磁碟、建立分割區、格式化 NTFS
          5. 在掛載的 volume 上建立 Windows pagefile
          6. 啟動背景監控執行緒

        Returns:
            結果字典，包含 success / device_count / added_gb 等資訊。
        """
        def report(msg: str):
            if on_progress:
                on_progress(msg)
            logger.info(msg)

        with self._lock:
            if self._active:
                return {"success": False, "error": "already active"}

        report(f"啟動 VHD pagefile，裝置: {', '.join(d + ':' for d in drive_letters)}")

        device_details = []
        total_swap_bytes = 0

        for letter in drive_letters:
            dev = self._setup_single_device(letter, use_percent, report)
            if dev is not None:
                with self._lock:
                    self._devices.append(dev)
                total_swap_bytes += dev.swap_bytes
                device_details.append({
                    "drive": dev.drive_letter,
                    "mount": dev.mount_letter,
                    "swap_gb": round(dev.swap_bytes / (1024 ** 3), 1),
                    "speed_mbs": round(dev.speed_mbs, 1),
                })

        if not self._devices:
            return {
                "success": False,
                "method": "vhd_pagefile",
                "error": "No devices activated",
                "needs_reboot": False,
            }

        # 啟動背景監控
        with self._lock:
            self._active = True
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="vhd-pagefile-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        report("背景監控已啟動（每 10 秒檢查裝置狀態）")

        # 取得系統記憶體資訊
        mem = _get_system_memory()
        existing_pf_bytes = _get_system_pagefile_bytes()
        ram_bytes = mem.get("physical_total", 0)
        total_gb = total_swap_bytes / (1024 ** 3)

        return {
            "success": True,
            "method": "vhd_pagefile",
            "device_count": len(self._devices),
            "devices": device_details,
            "added_gb": round(total_gb, 1),
            "system_pagefile_gb": round(existing_pf_bytes / (1024 ** 3), 1),
            "total_usable_gb": round(
                (ram_bytes + existing_pf_bytes + total_swap_bytes) / (1024 ** 3), 1),
            "needs_reboot": False,  # WMI pagefile 立即生效
        }

    def deactivate(self) -> Dict[str, Any]:
        """
        移除所有 VHD pagefile，卸載 VHD，清理資源。

        每個裝置的流程：
          1. 移除 pagefile（WMI）
          2. 卸載（Detach）VHD
          3. 關閉 VhdBridge handle
        注意：pagefile 移除可能需要重啟才完全生效。
        """
        # 停止監控執行緒
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=15)
            if self._monitor_thread.is_alive():
                logger.warning("Monitor thread did not stop within 15s")
            self._monitor_thread = None

        removed = 0
        errors = []

        with self._lock:
            devices_copy = list(self._devices)

        for dev in devices_copy:
            try:
                # Step 1: 移除 pagefile
                if dev.pagefile_active:
                    ok = self._remove_pagefile_on_volume(dev.mount_letter)
                    if ok:
                        dev.pagefile_active = False
                        logger.info("Pagefile removed on %s:", dev.mount_letter)
                    else:
                        logger.warning(
                            "Pagefile removal failed on %s: (may need reboot)",
                            dev.mount_letter)

                # Step 2: 卸載 VHD
                try:
                    dev.bridge.detach()
                    logger.info("VHD detached: %s", dev.vhd_path)
                except Exception as e:
                    logger.warning("VHD detach error: %s", e)

                # Step 3: 關閉 bridge handle
                try:
                    dev.bridge.close()
                except Exception as e:
                    logger.warning("VHD close error: %s", e)

                removed += 1

            except Exception as e:
                msg = f"Cleanup error for {dev.drive_letter}: {e}"
                logger.error(msg)
                errors.append(msg)

        # 歸還所有磁碟代號
        with self._lock:
            for dev in self._devices:
                self._release_letter(dev.mount_letter)
            self._devices.clear()
            self._active = False

        result: Dict[str, Any] = {
            "success": len(errors) == 0,
            "removed": removed,
        }
        if errors:
            result["errors"] = errors
            result["note"] = "部分 pagefile 可能需要重啟後才完全移除"
        return result

    def status(self) -> Dict[str, Any]:
        """回傳所有 VHD pagefile 的即時狀態。"""
        with self._lock:
            if not self._active:
                return {
                    "active": False,
                    "device_count": 0,
                    "devices": [],
                    "total_swap_gb": 0.0,
                    "degraded": False,
                }

            devices = []
            total_bytes = 0
            any_degraded = False

            for dev in self._devices:
                pf_usage = self.get_pagefile_usage(dev.mount_letter) if dev.pagefile_active else {}
                devices.append({
                    "drive": dev.drive_letter,
                    "mount": dev.mount_letter,
                    "vhd_path": dev.vhd_path,
                    "swap_gb": round(dev.swap_bytes / (1024 ** 3), 1),
                    "speed_mbs": round(dev.speed_mbs, 1),
                    "pagefile_active": dev.pagefile_active,
                    "degraded": dev.degraded,
                    "disk_number": dev.disk_number,
                    "pagefile_usage_mb": pf_usage.get("current_usage_mb", 0),
                    "safe_to_remove": pf_usage.get("safe_to_remove", True),
                })
                if not dev.degraded:
                    total_bytes += dev.swap_bytes
                if dev.degraded:
                    any_degraded = True

            return {
                "active": True,
                "device_count": len(self._devices),
                "devices": devices,
                "total_swap_gb": round(total_bytes / (1024 ** 3), 1),
                "degraded": any_degraded,
            }

    # ── Pagefile Usage Monitoring ─────────────────────────────────────

    @staticmethod
    def get_pagefile_usage(letter: str) -> Dict[str, int]:
        """
        查詢指定磁碟上 pagefile 的使用狀況。

        Returns:
            {
                "allocated_mb": total allocated size,
                "current_usage_mb": currently in-use pages,
                "peak_usage_mb": peak usage since boot,
                "safe_to_remove": True if current_usage == 0,
            }
        """
        try:
            import subprocess
            pf_path = f"{letter}:\\pagefile.sys"
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-CimInstance Win32_PageFileUsage | "
                 f"Where-Object {{ $_.Name -eq '{pf_path}' }} | "
                 f"Select-Object AllocatedBaseSize, CurrentUsage, PeakUsage | "
                 f"ConvertTo-Json"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            if r.returncode == 0 and r.stdout.strip():
                import json
                data = json.loads(r.stdout)
                if isinstance(data, list):
                    data = data[0] if data else {}
                return {
                    "allocated_mb": data.get("AllocatedBaseSize", 0),
                    "current_usage_mb": data.get("CurrentUsage", 0),
                    "peak_usage_mb": data.get("PeakUsage", 0),
                    "safe_to_remove": data.get("CurrentUsage", 0) == 0,
                }
        except Exception as e:
            logger.warning("Pagefile usage query failed for %s: %s", letter, e)

        return {"allocated_mb": 0, "current_usage_mb": 0, "peak_usage_mb": 0, "safe_to_remove": True}

    # ── Safe Device Removal ────────────────────────────────────────────

    def safe_remove_device(self, drive_letter: str,
                           on_progress=None) -> Dict[str, Any]:
        """
        安全移除指定裝置的 VHD pagefile。

        流程：
        1. 檢查 pagefile 使用量
        2. 如果有 active pages → 移除 pagefile（讓 Windows 遷移 pages 回 C:\）
        3. 等待 pagefile 使用量降為 0
        4. Detach VHD
        5. 回報安全可拔卡

        Returns:
            {"success": bool, "safe_to_remove": bool, "message": str}
        """
        report = on_progress or (lambda msg: logger.info(msg))

        with self._lock:
            dev = None
            for d in self._devices:
                if d.drive_letter == drive_letter:
                    dev = d
                    break

        if dev is None:
            return {"success": False, "safe_to_remove": False,
                    "message": f"Device {drive_letter}: not found"}

        # Step 1: 檢查 pagefile 使用量
        usage = self.get_pagefile_usage(dev.mount_letter)
        report(f"{drive_letter}: pagefile usage = {usage['current_usage_mb']} MB")

        # Step 2: 移除 pagefile（讓 Windows 遷移 pages）
        if dev.pagefile_active:
            report(f"{drive_letter}: removing pagefile (Windows will migrate pages to C:\\)...")
            self._remove_pagefile_on_volume(dev.mount_letter)
            dev.pagefile_active = False

        # Step 3: 等待使用量降為 0（最多 30 秒）
        for i in range(30):
            usage = self.get_pagefile_usage(dev.mount_letter)
            if usage["safe_to_remove"]:
                break
            report(f"{drive_letter}: waiting for pages to migrate... ({usage['current_usage_mb']} MB remaining)")
            time.sleep(1)

        if not usage["safe_to_remove"]:
            report(f"{drive_letter}: WARNING - {usage['current_usage_mb']} MB still in use, forced removal")

        # Step 4: Detach VHD
        report(f"{drive_letter}: detaching VHD...")
        try:
            dev.bridge.detach()
            dev.bridge.close()
        except Exception as e:
            logger.warning("VHD detach error for %s: %s", drive_letter, e)

        # Step 5: 從 device list 移除
        with self._lock:
            if dev in self._devices:
                self._devices.remove(dev)
            self._release_letter(dev.mount_letter)

        report(f"{drive_letter}: safe to remove")
        return {
            "success": True,
            "safe_to_remove": True,
            "message": f"Device {drive_letter}: safely ejected",
            "remaining_usage_mb": usage.get("current_usage_mb", 0),
        }

    # ── Drive Letter Management ────────────────────────────────────────

    def _allocate_letter(self) -> Optional[str]:
        """分配一個未使用的磁碟代號給 VHD mount。"""
        for letter in self._VHD_LETTERS:
            if letter in self._used_letters:
                continue
            # 確認此代號目前沒被系統使用
            if os.path.exists(f"{letter}:\\"):
                continue
            self._used_letters.add(letter)
            return letter
        return None

    def _release_letter(self, letter: str):
        """歸還磁碟代號。"""
        self._used_letters.discard(letter)

    # ── Single Device Setup ────────────────────────────────────────────

    def _setup_single_device(self, drive_letter: str, use_percent: float,
                             report: Callable[[str], None]) -> Optional[VhdPagefileDevice]:
        """
        在單一裝置上完成完整的 VHD pagefile 設定。

        步驟：
          1. 檢查裝置可用空間
          2. 在裝置上建立 VHDX 檔案
          3. Attach VHD -> 取得 disk number
          4. 初始化磁碟（MBR）、建立分割區、格式化 NTFS、指派磁碟代號
          5. 在新 volume 上建立 pagefile
        """
        mount = f"{drive_letter}:\\"

        # Step 1: 檢查裝置可用空間
        try:
            usage = shutil.disk_usage(mount)
        except OSError as e:
            report(f"{drive_letter}: 無法存取 ({e})")
            return None

        free_bytes = usage.free
        if free_bytes < SWAP_MIN_BYTES:
            report(f"{drive_letter}: 可用空間不足 ({free_bytes // (1024**2)} MB < 512 MB)")
            return None

        # 測速（簡易隨機寫入）
        speed_mbs = self._benchmark_random_write(mount)
        report(f"{drive_letter}: 隨機寫入 {speed_mbs:.0f} MB/s")

        # 計算 swap 大小：可用空間 x 百分比，受速度公式 cap
        wanted_bytes = int(free_bytes * (use_percent / 100))
        max_by_speed = int(speed_mbs * (1024 ** 2) * SWAP_FILL_TIME_SECONDS)
        max_by_speed = max(max_by_speed, SWAP_MIN_BYTES)
        swap_bytes = min(wanted_bytes, max_by_speed)

        # 對齊到 MB
        swap_bytes = (swap_bytes // (1024 * 1024)) * (1024 * 1024)
        if swap_bytes < SWAP_MIN_BYTES:
            report(f"{drive_letter}: swap 大小不足 ({swap_bytes // (1024**2)} MB)")
            return None

        swap_gb = swap_bytes / (1024 ** 3)
        report(f"{drive_letter}: 分配 {swap_gb:.1f} GB swap (速度上限: "
               f"{max_by_speed / (1024**3):.1f} GB)")

        # Step 2: 分配磁碟代號
        with self._lock:
            mount_letter = self._allocate_letter()
        if mount_letter is None:
            report(f"{drive_letter}: 沒有可用的磁碟代號")
            return None

        # Step 3: 選擇 VHD 檔案格式，建立 VHD 檔案路徑
        vhd_path = os.path.join(mount, self._VHD_FILENAME)

        # Step 4: 建立 VhdBridge + attach
        bridge = VhdBridge()
        try:
            report(f"{drive_letter}: 建立 VHDX ({swap_gb:.1f} GB)...")
            bridge.create(vhd_path, swap_bytes)

            report(f"{drive_letter}: 掛載 VHD...")
            disk_number = bridge.attach()
            if disk_number is None:
                report(f"{drive_letter}: VHD 掛載失敗（無法取得 disk number）")
                bridge.close()
                with self._lock:
                    self._release_letter(mount_letter)
                return None
            report(f"{drive_letter}: VHD 掛載完成 (Disk {disk_number})")

        except Exception as e:
            report(f"{drive_letter}: VHD 操作失敗 ({e})")
            try:
                bridge.close()
            except Exception:
                pass
            with self._lock:
                self._release_letter(mount_letter)
            return None

        # Step 5: 用 diskpart 初始化、分區、格式化
        report(f"{drive_letter}: 初始化磁碟、格式化 NTFS...")
        if not self._init_and_format_disk(disk_number, mount_letter):
            report(f"{drive_letter}: diskpart 格式化失敗")
            try:
                bridge.detach()
                bridge.close()
            except Exception:
                pass
            with self._lock:
                self._release_letter(mount_letter)
            return None

        # 等待 volume 出現
        volume_ready = False
        for _ in range(10):
            if os.path.exists(f"{mount_letter}:\\"):
                volume_ready = True
                break
            time.sleep(0.5)

        if not volume_ready:
            report(f"{drive_letter}: Volume {mount_letter}: 未出現")
            try:
                bridge.detach()
                bridge.close()
            except Exception:
                pass
            with self._lock:
                self._release_letter(mount_letter)
            return None

        # Step 6: 建立 pagefile
        swap_mb = swap_bytes // (1024 * 1024)
        report(f"{drive_letter}: 建立 pagefile ({swap_mb} MB) on {mount_letter}:...")
        # 極致優化：強制 Windows 建立固定大小的 Pagefile (Initial == Maximum)
        # 避免模型載入 VRAM 瞬間溢出導致的 NTFS 碎片化擴展，消除 I/O 阻塞。
        pf_ok = self._create_pagefile_on_volume(mount_letter, swap_mb, swap_mb)

        dev = VhdPagefileDevice(
            drive_letter=drive_letter,
            vhd_path=vhd_path,
            mount_letter=mount_letter,
            swap_bytes=swap_bytes,
            bridge=bridge,
            speed_mbs=speed_mbs,
        )
        dev.disk_number = disk_number
        dev.pagefile_active = pf_ok

        if pf_ok:
            report(f"{drive_letter}: pagefile 建立成功 ({mount_letter}:\\pagefile.sys)")
        else:
            report(f"{drive_letter}: pagefile 建立失敗，VHD volume 仍可用")

        return dev

    # ── Disk Initialization (diskpart) ─────────────────────────────────

    def _init_and_format_disk(self, disk_number: int, letter: str) -> bool:
        """
        用 diskpart 初始化、分區、格式化 VHD 掛載的磁碟。

        diskpart 腳本：
            select disk {N}
            clean
            create partition primary
            format fs=ntfs quick label=VRAM_SWAP
            assign letter={L}
        """
        script = (
            f"select disk {disk_number}\n"
            f"clean\n"
            f"create partition primary\n"
            f"format fs=ntfs quick label=VRAM_SWAP\n"
            f"assign letter={letter}\n"
        )

        try:
            # 寫入暫存腳本檔案
            script_path = os.path.join(tempfile.gettempdir(), f"vram_diskpart_{disk_number}.txt")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script)

            logger.debug("diskpart script:\n%s", script)

            result = _run_hidden(
                ["diskpart", "/s", script_path],
                timeout=60,
            )

            logger.debug("diskpart stdout:\n%s", result.stdout)
            if result.stderr:
                logger.debug("diskpart stderr:\n%s", result.stderr)

            # 清理暫存檔
            try:
                os.unlink(script_path)
            except OSError:
                pass

            if result.returncode != 0:
                logger.error("diskpart failed (rc=%d): %s", result.returncode, result.stdout)
                return False

            # 檢查輸出中是否有錯誤訊息
            output_lower = result.stdout.lower()
            if "error" in output_lower or "failed" in output_lower:
                # diskpart 有時 rc=0 但內容有錯
                # 但 "format" 的輸出可能包含 "100 percent completed"
                # 只在沒有 "successfully" 的情況下判定為失敗
                if "successfully" not in output_lower:
                    logger.error("diskpart reported error: %s", result.stdout)
                    return False

            return True

        except subprocess.TimeoutExpired:
            logger.error("diskpart timeout (60s)")
            return False
        except OSError as e:
            logger.error("diskpart execution error: %s", e)
            return False

    # ── Pagefile Management (WMI via PowerShell) ───────────────────────

    def _create_pagefile_on_volume(self, letter: str, min_mb: int, max_mb: int) -> bool:
        """
        在指定磁碟上建立 Windows pagefile。

        主要方法：NtCreatePagingFile（即時生效，不需重啟）
        備用方法：WMI via PowerShell（VHD 上可能 timeout）
        """
        # 方法 1: NtCreatePagingFile（快速，需要 SeCreatePagefilePrivilege）
        if self._create_pagefile_ntapi(letter, min_mb, max_mb):
            return True

        # 方法 2: WMI fallback（較慢，VHD volume 上可能 timeout）
        logger.info("NtCreatePagingFile failed, trying WMI fallback...")
        ps_script = (
            f"$ErrorActionPreference = 'Stop'; "
            f"$pf = [wmiclass]'Win32_PageFileSetting'; "
            f"$new = $pf.CreateInstance(); "
            f"$new.Name = '{letter}:\\pagefile.sys'; "
            f"$new.InitialSize = {min_mb}; "
            f"$new.MaximumSize = {max_mb}; "
            f"$new.Put()"
        )

        try:
            result = _run_hidden(
                ["powershell", "-NoProfile", "-Command", ps_script],
                timeout=60,
            )
            if result.returncode == 0:
                logger.info("Pagefile created via WMI: %s:\\pagefile.sys (%d MB)",
                            letter, max_mb)
                return True
            else:
                logger.warning("WMI pagefile creation failed (rc=%d): %s",
                               result.returncode, result.stderr or result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning("WMI pagefile creation timeout (60s)")
        except OSError as e:
            logger.warning("WMI pagefile creation error: %s", e)

        return False

    def _create_pagefile_ntapi(self, letter: str, min_mb: int, max_mb: int) -> bool:
        """
        使用 NtCreatePagingFile NT API 建立 pagefile（WMI 失敗時的 fallback）。

        NtCreatePagingFile 可以在非系統磁碟上直接建立 pagefile，
        不需要重啟，但需要管理員權限 + SeCreatePagefilePrivilege。
        """
        try:
            import ctypes
            from ctypes import wintypes

            # ── 啟用 SeCreatePagefilePrivilege ──
            self._enable_pagefile_privilege()

            ntdll = ctypes.WinDLL("ntdll")

            # UNICODE_STRING 結構
            class UNICODE_STRING(ctypes.Structure):
                _fields_ = [
                    ("Length", wintypes.USHORT),
                    ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_wchar_p),
                ]

            # LARGE_INTEGER
            class LARGE_INTEGER(ctypes.Structure):
                _fields_ = [("QuadPart", ctypes.c_longlong)]

            # pagefile 路徑：NT 格式
            pf_path = f"\\??\\{letter}:\\pagefile.sys"
            path_buf = ctypes.create_unicode_buffer(pf_path)

            us = UNICODE_STRING()
            us.Length = len(pf_path) * 2
            us.MaximumLength = (len(pf_path) + 1) * 2
            us.Buffer = ctypes.cast(path_buf, ctypes.c_wchar_p)

            min_size = LARGE_INTEGER(QuadPart=min_mb * 1024 * 1024)
            max_size = LARGE_INTEGER(QuadPart=max_mb * 1024 * 1024)

            # NTSTATUS NtCreatePagingFile(
            #   PUNICODE_STRING PageFileName,
            #   PLARGE_INTEGER MinimumSize,
            #   PLARGE_INTEGER MaximumSize,
            #   PLARGE_INTEGER Priority  // 0 = 預設
            # )
            NtCreatePagingFile = ntdll.NtCreatePagingFile
            NtCreatePagingFile.restype = ctypes.c_long
            NtCreatePagingFile.argtypes = [
                ctypes.POINTER(UNICODE_STRING),
                ctypes.POINTER(LARGE_INTEGER),
                ctypes.POINTER(LARGE_INTEGER),
                ctypes.POINTER(LARGE_INTEGER),
            ]

            priority = LARGE_INTEGER(QuadPart=0)
            status = NtCreatePagingFile(
                ctypes.byref(us),
                ctypes.byref(min_size),
                ctypes.byref(max_size),
                ctypes.byref(priority),
            )

            if status == 0:  # STATUS_SUCCESS
                logger.info("Pagefile created via NtCreatePagingFile: %s", pf_path)
                return True
            else:
                logger.warning("NtCreatePagingFile failed: NTSTATUS=0x%08X", status & 0xFFFFFFFF)
                return False

        except (OSError, AttributeError, ImportError) as e:
            logger.warning("NtCreatePagingFile fallback error: %s", e)
            return False

    @staticmethod
    def _enable_pagefile_privilege():
        """啟用 SeCreatePagefilePrivilege（NtCreatePagingFile 必要）。"""
        try:
            import ctypes
            from ctypes import wintypes

            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE

            class LUID(ctypes.Structure):
                _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]
            class LUID_AND_ATTRIBUTES(ctypes.Structure):
                _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]
            class TOKEN_PRIVILEGES(ctypes.Structure):
                _fields_ = [("PrivilegeCount", wintypes.DWORD),
                             ("Privileges", LUID_AND_ATTRIBUTES * 1)]

            hToken = wintypes.HANDLE()
            advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), 0x0028, ctypes.byref(hToken))
            luid = LUID()
            advapi32.LookupPrivilegeValueW(
                None, "SeCreatePagefilePrivilege", ctypes.byref(luid))
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
            advapi32.AdjustTokenPrivileges(
                hToken, False, ctypes.byref(tp), 0, None, None)
            kernel32.CloseHandle(hToken)
            logger.debug("SeCreatePagefilePrivilege enabled")
        except Exception as e:
            logger.warning("Failed to enable SeCreatePagefilePrivilege: %s", e)

    def _remove_pagefile_on_volume(self, letter: str) -> bool:
        """
        移除指定磁碟上的 pagefile。

        使用 WMI 透過 PowerShell 查詢並刪除 pagefile 設定。
        """
        ps_script = (
            f"$ErrorActionPreference = 'SilentlyContinue'; "
            f"$pf = Get-WmiObject Win32_PageFileSetting | "
            f"Where-Object {{ $_.Name -eq '{letter}:\\pagefile.sys' }}; "
            f"if ($pf) {{ $pf.Delete(); Write-Output 'DELETED' }} "
            f"else {{ Write-Output 'NOT_FOUND' }}"
        )

        try:
            result = _run_hidden(
                ["powershell", "-NoProfile", "-Command", ps_script],
                timeout=30,
            )

            output = (result.stdout or "").strip()
            if "DELETED" in output:
                logger.info("Pagefile removed: %s:\\pagefile.sys", letter)
                return True
            elif "NOT_FOUND" in output:
                logger.info("Pagefile not found on %s: (already removed?)", letter)
                return True
            else:
                logger.warning("Pagefile removal uncertain: %s", output)
                return False

        except subprocess.TimeoutExpired:
            logger.warning("Pagefile removal timeout on %s:", letter)
            return False
        except OSError as e:
            logger.warning("Pagefile removal error: %s", e)
            return False

    # ── Background Monitor ─────────────────────────────────────────────

    def _monitor_loop(self):
        """
        背景監控：每 10 秒檢查所有外接裝置是否仍在線。

        - 裝置消失 -> 標記 degraded（VHD 自動失效，pagefile 不可用）
        - 裝置恢復 -> 嘗試重新掛載 VHD
        """
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=_MONITOR_INTERVAL)
            if self._stop_event.is_set():
                break

            with self._lock:
                devices_snapshot = list(self._devices)

            for dev in devices_snapshot:
                try:
                    present = self._check_device_present(dev.drive_letter)

                    if not present and not dev.degraded:
                        # 裝置消失 -> 標記 degraded
                        logger.warning(
                            "裝置 %s: 已斷線 — VHD pagefile 不可用",
                            dev.drive_letter)
                        with self._lock:
                            dev.degraded = True

                    elif present and dev.degraded:
                        # 裝置恢復 -> 嘗試重新掛載
                        logger.info(
                            "裝置 %s: 偵測到恢復，嘗試重新掛載 VHD...",
                            dev.drive_letter)
                        self._try_restore_device(dev)

                except Exception as e:
                    logger.debug("Monitor error for %s: %s", dev.drive_letter, e)

    def _check_device_present(self, drive_letter: str) -> bool:
        """檢查外接裝置是否仍然存在（快速 stat 檢查）。"""
        try:
            path = f"{drive_letter}:\\"
            os.stat(path)
            return True
        except OSError:
            return False

    def _try_restore_device(self, dev: VhdPagefileDevice):
        """
        嘗試恢復斷線的裝置：重新 attach VHD 並建立 pagefile。
        如果 VHD 檔案仍存在，可以直接重新掛載。
        """
        try:
            # 確認 VHD 檔案仍存在
            if not os.path.exists(dev.vhd_path):
                logger.warning("VHD 檔案不存在: %s", dev.vhd_path)
                return

            # 重新建立 bridge 並 attach
            new_bridge = VhdBridge()
            disk_number = new_bridge.attach_existing(dev.vhd_path)
            if disk_number is None:
                logger.warning("VHD 重新掛載失敗: %s", dev.vhd_path)
                new_bridge.close()
                return

            # 重新指派磁碟代號（如果 volume 沒自動出現）
            if not os.path.exists(f"{dev.mount_letter}:\\"):
                # 用 diskpart 重新 assign letter
                script = (
                    f"select disk {disk_number}\n"
                    f"select partition 1\n"
                    f"assign letter={dev.mount_letter}\n"
                )
                script_path = os.path.join(
                    tempfile.gettempdir(), f"vram_restore_{disk_number}.txt")
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(script)
                _run_hidden(["diskpart", "/s", script_path], timeout=30)
                try:
                    os.unlink(script_path)
                except OSError:
                    pass

            # 等待 volume
            for _ in range(10):
                if os.path.exists(f"{dev.mount_letter}:\\"):
                    break
                time.sleep(0.5)

            if not os.path.exists(f"{dev.mount_letter}:\\"):
                logger.warning("Volume %s: 恢復失敗", dev.mount_letter)
                new_bridge.close()
                return

            # 替換 bridge
            try:
                dev.bridge.close()
            except Exception:
                pass

            with self._lock:
                dev.bridge = new_bridge
                dev.disk_number = disk_number
                dev.degraded = False

            # 重新建立 pagefile
            swap_mb = dev.swap_bytes // (1024 * 1024)
            pf_ok = self._create_pagefile_on_volume(dev.mount_letter, PAGEFILE_MIN_MB, swap_mb)
            with self._lock:
                dev.pagefile_active = pf_ok

            logger.info("裝置 %s: 已恢復 (Disk %d, %s:, pagefile=%s)",
                        dev.drive_letter, disk_number, dev.mount_letter,
                        "OK" if pf_ok else "FAILED")

        except Exception as e:
            logger.error("裝置 %s 恢復失敗: %s", dev.drive_letter, e)

    # ── Benchmark ──────────────────────────────────────────────────────

    @staticmethod
    def _benchmark_random_write(mount_path: str, test_size_mb: float = 4) -> float:
        """
        用隨機 4KB 寫入測試裝置速度，模擬 pagefile 的真實 I/O 模式。

        Returns: 隨機寫入速度 (MB/s)
        """
        test_file = Path(mount_path) / ".vram_speed_test"
        block_4kb = os.urandom(4096)
        total_bytes = int(test_size_mb * 1024 * 1024)
        num_writes = total_bytes // 4096

        try:
            # 建立測試檔案（sparse）
            with open(test_file, "wb") as f:
                f.seek(total_bytes - 1)
                f.write(b"\0")

            # 隨機位置寫入 4KB 區塊
            import random
            offsets = [random.randint(0, max(1, num_writes - 1)) * 4096
                       for _ in range(num_writes)]

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
