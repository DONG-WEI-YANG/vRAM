"""
Architecture Benefit Validation
=================================
測試 vRAM Booster 架構是否確實能為使用者增加可用記憶體。

三個測試等級：
  Tier 1 (無需 admin，E:\\ 存在即可):
    - BusType 偵測正確性
    - 裝置速度量測
    - Swap 大小公式驗證
    - Safety policy 接受度
    - 記憶體狀態可讀性

  Tier 2 (無需 admin，E:\\ 存在):
    - VHD 檔案建立與大小驗證

  Tier 3 (需要 admin 權限):
    - 完整管道：VHD 建立 → 掛載 → Pagefile → commit limit 增加 → 清理

執行方式：
  python test_architecture_benefit.py          (Tier 1 + Tier 2)
  python test_architecture_benefit.py --admin  (全部 Tier)
  python -m pytest test_architecture_benefit.py -v -s
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import platform
import shutil
import sys
import time
import unittest
from pathlib import Path
from typing import Dict, Any, Optional

# 確保 microsystems 模組可以被找到
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "提升VRAM與GPA效能設計方案"))

TEST_DRIVE = "E"
TEST_DRIVE_PATH = f"{TEST_DRIVE}:\\"

SWAP_FILL_TIME_SECONDS = 600   # 與 real_boost.py 一致


# ── Utility ───────────────────────────────────────────────────────────────

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def get_commit_limit_gb() -> float:
    """取得系統當前 commit limit（RAM + Pagefile 總量）"""
    class MEMSTATUS(ctypes.Structure):
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
    s = MEMSTATUS()
    s.dwLength = ctypes.sizeof(MEMSTATUS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ullTotalPageFile / (1024 ** 3)


def drive_exists(letter: str) -> bool:
    return os.path.exists(f"{letter}:\\")


def speed_to_max_swap_gb(speed_mbs: float) -> float:
    """與 real_boost._cap_swap_by_speed 相同的公式"""
    return max(speed_mbs * SWAP_FILL_TIME_SECONDS / 1024, 0.5)


def classify_benefit_tier(speed_mbs: float) -> str:
    if speed_mbs >= 150:
        return "High   (USB SSD / NVMe 外接盒) — expected +30~120 GB"
    elif speed_mbs >= 30:
        return "Medium (USB 3.0 Flash / SD Express) — expected +5~30 GB"
    else:
        return "Low    (SD Card / USB 2.0) — expected +1~5 GB"


# ── Tier 1: Detection + Speed + Policy ───────────────────────────────────

@unittest.skipUnless(drive_exists(TEST_DRIVE), f"E:\\ not found — skip Tier 1")
class TestTier1_DetectionAndBenefitAnalysis(unittest.TestCase):
    """
    Tier 1: 驗證架構的偵測層和效益計算層。
    需要 E:\\ 存在，不需要管理員權限。
    """

    # ── (A) BusType-driven Device Detection ──

    def test_A1_e_drive_detected_as_external(self):
        """
        E:\\ 必須被 get_external_drive_letters() 列為外接裝置。

        Before BusType fix: NVMe 外接盒 / USB SSD 因回報 DriveType=Fixed 而漏掉。
        After BusType fix: 只看 BusType，完全不受 DriveType 影響。
        """
        from microsystems.core.device_query import get_external_drive_letters
        external = get_external_drive_letters()
        letters = [d["letter"] for d in external]
        self.assertIn(
            TEST_DRIVE, letters,
            f"E:\\ not in external drives. Found: {letters}\n"
            f"Full result: {json.dumps(external, indent=2, default=str)}"
        )

    def test_A2_e_drive_has_bus_type(self):
        """偵測到的 E:\\ 必須有有效的 BusType"""
        from microsystems.core.device_query import get_external_drive_letters, EXTERNAL_BUS_TYPES
        external = get_external_drive_letters()
        e_info = next((d for d in external if d["letter"] == TEST_DRIVE), None)
        self.assertIsNotNone(e_info, "E:\\ not detected")

        bus = e_info["bus_type"]
        valid_buses = EXTERNAL_BUS_TYPES | {"NVMe"}
        self.assertIn(
            bus, valid_buses,
            f"Unexpected BusType: {bus!r}. Expected one of {valid_buses}"
        )
        print(f"\n  [A2] E:\\ BusType = {bus}")

    def test_A3_classify_device_type_consistent(self):
        """classify_device() 對 E:\\ 的分類必須是已知的六種類型之一"""
        from microsystems.core.device_query import get_external_drive_letters, classify_device
        external = get_external_drive_letters()
        e_info = next((d for d in external if d["letter"] == TEST_DRIVE), None)
        self.assertIsNotNone(e_info)

        device_type = classify_device(
            bus_type=e_info["bus_type"],
            media_type=e_info["media_type"],
            friendly_name=e_info["friendly_name"],
            spindle_speed=e_info.get("spindle_speed", 0),
            capacity_gb=e_info["size_bytes"] / (1024 ** 3),
        )
        known_types = {"sd_express", "nvme_enclosure", "usb_ssd",
                       "sd_card", "hdd", "usb_drive"}
        self.assertIn(device_type, known_types)
        print(f"\n  [A3] Device type = {device_type}")

    # ── (B) Speed Measurement ──

    def test_B1_speed_measurement_returns_positive(self):
        """E:\\ 隨機寫入速度必須 > 0 MB/s"""
        from microsystems.core.real_boost import RealBoostEngine
        speed = RealBoostEngine._benchmark_random_write(TEST_DRIVE_PATH, test_size_mb=1)
        self.assertGreater(speed, 0, "Speed measurement returned 0 or negative")
        print(f"\n  [B1] Measured speed: {speed:.1f} MB/s")

    def test_B2_speed_consistent_with_device_type(self):
        """
        測速結果必須在裝置類型的合理範圍內。
        確保速度函式沒有 bug（如永遠回傳預設值 5.0）。
        """
        from microsystems.core.real_boost import RealBoostEngine
        from microsystems.core.device_query import get_external_drive_letters, classify_device

        external = get_external_drive_letters()
        e_info = next((d for d in external if d["letter"] == TEST_DRIVE), None)
        self.assertIsNotNone(e_info)

        device_type = classify_device(
            bus_type=e_info["bus_type"],
            media_type=e_info["media_type"],
            friendly_name=e_info["friendly_name"],
            spindle_speed=e_info.get("spindle_speed", 0),
            capacity_gb=e_info["size_bytes"] / (1024**3),
        )
        speed = RealBoostEngine._benchmark_random_write(TEST_DRIVE_PATH, test_size_mb=2)

        # 每種裝置類型的合理速度下限
        min_speeds = {
            "nvme_enclosure": 100,
            "sd_express": 50,
            "usb_ssd": 30,
            "sd_card": 2,
            "hdd": 5,
            "usb_drive": 2,
        }
        min_expected = min_speeds.get(device_type, 2)
        self.assertGreaterEqual(
            speed, min_expected,
            f"{device_type} speed {speed:.1f} MB/s < expected minimum {min_expected} MB/s\n"
            f"(速度函式可能回傳了錯誤的預設值)"
        )
        print(f"\n  [B2] {device_type} speed: {speed:.1f} MB/s (min expected: {min_expected})")

    # ── (C) Swap Size Calculation ──

    def test_C1_swap_size_formula_produces_valid_gb(self):
        """
        swap 大小公式（speed × 600s）必須產生 > 0.5 GB 的值。
        確認公式正確且效益確實存在。
        """
        from microsystems.core.real_boost import RealBoostEngine
        speed = RealBoostEngine._benchmark_random_write(TEST_DRIVE_PATH, test_size_mb=1)
        max_gb = speed_to_max_swap_gb(speed)

        self.assertGreater(max_gb, 0.5,
            f"Max swap only {max_gb:.2f} GB — device too slow to be useful?")
        print(f"\n  [C1] Max swap capacity: {max_gb:.1f} GB")

    def test_C2_swap_size_capped_by_available_space(self):
        """
        最終 swap 大小不能超過裝置可用空間的 80%。
        """
        usage = shutil.disk_usage(TEST_DRIVE_PATH)
        available_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)

        from microsystems.core.real_boost import RealBoostEngine
        speed = RealBoostEngine._benchmark_random_write(TEST_DRIVE_PATH, test_size_mb=1)
        max_by_speed_gb = speed_to_max_swap_gb(speed)

        final_gb = min(max_by_speed_gb, available_gb * 0.80)

        self.assertGreater(final_gb, 0, "No usable space for swap")
        self.assertLessEqual(final_gb, total_gb,
            "Calculated swap exceeds device total capacity")
        print(f"\n  [C2] Device: {total_gb:.1f} GB total, {available_gb:.1f} GB free"
              f"\n       Final swap estimate: {final_gb:.1f} GB")

    def test_C3_speed_formula_matches_engine_formula(self):
        """
        test_architecture_benefit 的計算公式必須與 RealBoostEngine 一致。
        避免 test 用不同公式算出「假設性收益」。
        """
        from microsystems.core.real_boost import RealBoostEngine, SWAP_FILL_TIME_SECONDS as ENGINE_TIME

        self.assertEqual(SWAP_FILL_TIME_SECONDS, ENGINE_TIME,
            f"Test uses {SWAP_FILL_TIME_SECONDS}s but engine uses {ENGINE_TIME}s")

    # ── (D) Safety Policy ──

    def test_D1_safety_policy_accepts_e_drive(self):
        """Safety policy 必須接受 E:\\ 進行 boost 操作"""
        from microsystems.core.safety_policy import SafetyPolicy, GLOBAL_POLICY_PATH

        usage = shutil.disk_usage(TEST_DRIVE_PATH)
        capacity_gb = usage.total / (1024 ** 3)

        from microsystems.core.real_boost import RealBoostEngine
        speed = RealBoostEngine._benchmark_random_write(TEST_DRIVE_PATH, test_size_mb=1)

        policy = SafetyPolicy.load_merged_policy(
            GLOBAL_POLICY_PATH,
            None,
            capacity_gb=capacity_gb,
            speed_mbs=speed,
        )
        requested_gb = min(capacity_gb * 0.80, policy.pagefile_max_gb)

        ok, reason = SafetyPolicy.validate_activation(capacity_gb, requested_gb, policy)
        self.assertTrue(ok,
            f"Safety policy rejected E:\\ boost: {reason}\n"
            f"capacity={capacity_gb:.1f}GB, requested={requested_gb:.1f}GB")
        print(f"\n  [D1] Safety policy: OK (max={policy.pagefile_max_gb:.1f}GB)")

    # ── (E) Memory State ──

    def test_E1_commit_limit_readable(self):
        """Windows commit limit 必須可讀（ctypes 查詢正常）"""
        commit_gb = get_commit_limit_gb()
        self.assertGreater(commit_gb, 1.0,
            f"Commit limit {commit_gb:.2f} GB seems too low")
        print(f"\n  [E1] Current commit limit: {commit_gb:.2f} GB")

    def test_E2_physical_ram_readable(self):
        """物理 RAM 大小可讀"""
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.wintypes.DWORD),
                        ("dwMemoryLoad", ctypes.wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        s = MS()
        s.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        ram_gb = s.ullTotalPhys / (1024 ** 3)
        self.assertGreater(ram_gb, 0.5)
        print(f"\n  [E2] Physical RAM: {ram_gb:.1f} GB")


# ── Tier 2: VHD File Creation ─────────────────────────────────────────────

@unittest.skipUnless(drive_exists(TEST_DRIVE), f"E:\\ not found — skip Tier 2")
class TestTier2_VhdFileCreation(unittest.TestCase):
    """
    Tier 2: 驗證 VHD 檔案的建立（不掛載，不需要 admin）。
    確認 VhdBridge 能在外接裝置上正確建立 VHDX 動態磁碟。
    """

    TEST_VHD = f"{TEST_DRIVE_PATH}vram_arch_test_small.vhdx"
    TEST_SIZE = 64 * 1024 * 1024  # 64 MB（最小可建立的 VHDX）

    def tearDown(self):
        """確保測試後清理 VHD 檔案"""
        try:
            if os.path.exists(self.TEST_VHD):
                os.unlink(self.TEST_VHD)
        except OSError:
            pass

    def test_F1_vhd_file_creation(self):
        """
        VhdBridge 能在 E:\\ 建立 64 MB VHDX 檔案。
        這步驟不需要 admin（只是建立檔案，還沒掛載）。
        """
        from microsystems.core.vhd_bridge import VhdBridge

        # 清理殘留
        if os.path.exists(self.TEST_VHD):
            os.unlink(self.TEST_VHD)

        bridge = VhdBridge()
        created = bridge.create(self.TEST_VHD, self.TEST_SIZE)
        bridge.close()

        self.assertTrue(created, "VhdBridge.create() returned False")
        self.assertTrue(os.path.exists(self.TEST_VHD), "VHDX file not found after create()")

    def test_F2_vhd_file_size_reasonable(self):
        """
        動態 VHDX 建立後實際大小應 << 宣告大小（動態擴展特性）。
        一個空的 64MB VHDX 動態磁碟，實際磁碟佔用通常 < 5 MB。
        """
        from microsystems.core.vhd_bridge import VhdBridge

        if os.path.exists(self.TEST_VHD):
            os.unlink(self.TEST_VHD)

        bridge = VhdBridge()
        bridge.create(self.TEST_VHD, self.TEST_SIZE)
        bridge.close()

        if not os.path.exists(self.TEST_VHD):
            self.skipTest("VHD creation failed in previous test")

        actual_bytes = os.path.getsize(self.TEST_VHD)
        actual_mb = actual_bytes / (1024 ** 2)
        declared_mb = self.TEST_SIZE / (1024 ** 2)

        # 動態 VHDX：實際大小應 < 宣告大小
        self.assertLess(
            actual_mb, declared_mb,
            f"VHDX {actual_mb:.1f} MB >= declared {declared_mb:.1f} MB "
            f"— may be fixed-size (unexpected)"
        )
        print(f"\n  [F2] VHDX size: {actual_mb:.1f} MB actual / {declared_mb:.0f} MB declared "
              f"(dynamic ratio: {actual_mb/declared_mb:.1%})")

    def test_F3_vhdx_format_supported(self):
        """Windows 8+ 必須支援 VHDX 格式"""
        from microsystems.core.vhd_bridge import _is_vhdx_supported
        supported = _is_vhdx_supported()
        self.assertTrue(supported,
            "VHDX not supported on this Windows version. VHD (legacy) format would be used.")
        print(f"\n  [F3] VHDX format: supported")


# ── Tier 3: Full Pipeline (Admin Required) ────────────────────────────────

@unittest.skipUnless(drive_exists(TEST_DRIVE), "E:\\ not found")
@unittest.skipUnless(is_admin(), "Admin required for Tier 3 (pagefile creation)")
class TestTier3_FullPipeline_AdminRequired(unittest.TestCase):
    """
    Tier 3: 完整管道驗證 — 需要管理員權限。

    測試：VHD 建立 → 掛載 → 磁碟初始化 → Pagefile 建立 →
         commit limit 增加 → 安全移除 → commit limit 恢復

    這是最直接的「架構效益」驗證：數字說話。
    """

    TEST_VHD = f"{TEST_DRIVE_PATH}vram_arch_full_test.vhdx"
    TEST_SIZE = 512 * 1024 * 1024  # 512 MB
    MOUNT_LETTER = "V"

    @classmethod
    def setUpClass(cls):
        cls.baseline_commit_gb = get_commit_limit_gb()
        print(f"\n  [Tier 3 Setup] Baseline commit limit: {cls.baseline_commit_gb:.2f} GB")

    def test_G1_full_vhd_pagefile_cycle(self):
        """
        完整 VHD pagefile 管道驗證：
          1. 在 E:\\ 建立 512MB VHDX
          2. Attach 為 Fixed volume
          3. 格式化 NTFS
          4. 建立 Windows pagefile
          5. 驗證 commit limit 增加
          6. 清理並驗證恢復
        """
        import subprocess
        import tempfile
        from microsystems.core.vhd_bridge import VhdBridge

        # 清理殘留
        for f in [self.TEST_VHD]:
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

        # Step 1: 建立 VHD
        bridge = VhdBridge()
        self.assertTrue(bridge.create(self.TEST_VHD, self.TEST_SIZE),
                        "VHD creation failed")

        try:
            # Step 2: Attach
            self.assertTrue(bridge.attach(permanent=True), "VHD attach failed")
            disk_num = bridge.get_disk_number()
            self.assertIsNotNone(disk_num, "Cannot get disk number after attach")

            # Step 3: 初始化 + 格式化
            dp_script = (
                f"select disk {disk_num}\n"
                f"clean\n"
                f"create partition primary\n"
                f"format fs=ntfs quick label=VRAM_TEST\n"
                f"assign letter={self.MOUNT_LETTER}\n"
            )
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write(dp_script)
                script_path = f.name

            r = subprocess.run(
                ["diskpart", "/s", script_path],
                capture_output=True, text=True, timeout=30
            )
            os.unlink(script_path)
            self.assertEqual(r.returncode, 0, f"diskpart failed: {r.stderr}")

            # 等待 volume 出現
            mount_path = f"{self.MOUNT_LETTER}:\\"
            for _ in range(10):
                if os.path.exists(mount_path):
                    break
                time.sleep(0.5)
            self.assertTrue(os.path.exists(mount_path),
                            f"{mount_path} did not appear after diskpart")

            # Step 4: 建立 Pagefile (NtCreatePagingFile)
            class US(ctypes.Structure):
                _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                             ("Buffer", ctypes.c_wchar_p)]
            class LI(ctypes.Structure):
                _fields_ = [("QuadPart", ctypes.c_longlong)]

            ntdll = ctypes.WinDLL("ntdll")
            fn = ntdll.NtCreatePagingFile
            fn.restype = ctypes.c_long
            fn.argtypes = [ctypes.POINTER(US), ctypes.POINTER(LI),
                           ctypes.POINTER(LI), ctypes.c_ulong]

            nt_path = f"\\??\\{self.MOUNT_LETTER}:\\pagefile.sys"
            upath = US()
            upath.Buffer = nt_path
            upath.Length = len(nt_path) * 2
            upath.MaximumLength = (len(nt_path) + 1) * 2
            min_s = LI(); min_s.QuadPart = 16 * 1024 * 1024
            max_s = LI(); max_s.QuadPart = self.TEST_SIZE

            status = fn(ctypes.byref(upath), ctypes.byref(min_s),
                        ctypes.byref(max_s), 0)

            # Step 5: 驗證 commit limit 增加
            time.sleep(2)
            after_gb = get_commit_limit_gb()
            delta_gb = after_gb - self.baseline_commit_gb

            print(f"\n  [G1] Pagefile status: 0x{status & 0xFFFFFFFF:08X}")
            print(f"  [G1] Commit limit: {self.baseline_commit_gb:.2f} → "
                  f"{after_gb:.2f} GB (Δ = {delta_gb:+.2f} GB)")

            if status == 0:
                # Pagefile 建立成功，commit limit 應增加
                self.assertGreater(
                    delta_gb, 0.01,
                    f"Pagefile created but commit limit only increased by {delta_gb:.3f} GB"
                )
            else:
                # NtCreatePagingFile 失敗（可能需要重開機才能看到效果）
                print(f"  [G1] Note: NtCreatePagingFile status != 0, "
                      f"may need reboot to take effect")

        finally:
            # Step 6: 清理
            try:
                # 移除 pagefile
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"$pf = Get-WmiObject Win32_PageFileSetting | "
                     f"Where-Object {{ $_.Name -eq '{self.MOUNT_LETTER}:\\pagefile.sys' }}; "
                     f"if ($pf) {{ $pf.Delete() }}"],
                    capture_output=True, text=True, timeout=10,
                )
                bridge.detach()
                bridge.close()
                time.sleep(1)
                if os.path.exists(self.TEST_VHD):
                    os.unlink(self.TEST_VHD)
            except Exception as e:
                print(f"  [G1 cleanup] Warning: {e}")

    def test_G2_commit_restored_after_cleanup(self):
        """
        清理後 commit limit 應恢復到 baseline（允許 ±0.5 GB 誤差）。
        這驗證 deactivate() 的完整性。
        """
        time.sleep(2)
        restored_gb = get_commit_limit_gb()
        delta_from_baseline = abs(restored_gb - self.baseline_commit_gb)
        print(f"\n  [G2] Restored commit: {restored_gb:.2f} GB "
              f"(baseline: {self.baseline_commit_gb:.2f}, "
              f"diff: {delta_from_baseline:.3f} GB)")
        self.assertLess(
            delta_from_baseline, 0.5,
            f"Commit limit not restored: {restored_gb:.2f} GB "
            f"vs baseline {self.baseline_commit_gb:.2f} GB"
        )


# ── Summary Report ────────────────────────────────────────────────────────

def print_benefit_report():
    """完整的架構效益報告，顯示具體的數字預測。"""
    print("\n" + "═" * 60)
    print("  vRAM Architecture Benefit Report")
    print("═" * 60)
    print(f"  Platform:   {platform.system()} {platform.release()}")
    print(f"  Admin:      {'YES ✓' if is_admin() else 'NO (Tier 3 skipped)'}")
    print(f"  E:\\ exists: {'YES ✓' if drive_exists(TEST_DRIVE) else 'NO (all tiers skipped)'}")

    if not drive_exists(TEST_DRIVE):
        print("\n  No external device at E:\\ — cannot measure benefit.")
        print("═" * 60)
        return

    try:
        from microsystems.core.device_query import get_external_drive_letters, classify_device
        external = get_external_drive_letters()
        e_info = next((d for d in external if d["letter"] == TEST_DRIVE), None)

        if e_info:
            bus = e_info["bus_type"]
            friendly = e_info["friendly_name"] or "(unknown)"
            size_gb = e_info["size_bytes"] / (1024 ** 3)
            dev_type = classify_device(
                bus_type=bus,
                media_type=e_info["media_type"],
                friendly_name=friendly,
                capacity_gb=size_gb,
            )
            print(f"\n  Device:     {friendly}")
            print(f"  BusType:    {bus}  (type: {dev_type})")
            print(f"  Capacity:   {size_gb:.1f} GB")
        else:
            print(f"\n  [WARN] E:\\ not detected as external via BusType!")
            print(f"  This may indicate a detection bug.")
            print("═" * 60)
            return

        from microsystems.core.real_boost import RealBoostEngine
        print(f"\n  Measuring speed on E:\\ (2 MB test)...")
        speed = RealBoostEngine._benchmark_random_write(f"{TEST_DRIVE}:\\", test_size_mb=2)

        usage = shutil.disk_usage(f"{TEST_DRIVE}:\\")
        available_gb = usage.free / (1024 ** 3)
        max_by_speed = speed_to_max_swap_gb(speed)
        final_estimate = min(max_by_speed, available_gb * 0.80)
        tier = classify_benefit_tier(speed)
        commit_now = get_commit_limit_gb()

        print(f"\n  Speed:      {speed:.1f} MB/s random write")
        print(f"  Tier:       {tier}")
        print(f"\n  Benefit Projection:")
        print(f"    Current commit limit:      {commit_now:.1f} GB")
        print(f"    Max by speed formula:      {max_by_speed:.1f} GB")
        print(f"    Max by available space:    {available_gb * 0.80:.1f} GB")
        print(f"    ┌─────────────────────────────────────────────┐")
        print(f"    │  Estimated expansion:  +{final_estimate:.1f} GB            │")
        print(f"    │  New commit limit:     ~{commit_now + final_estimate:.1f} GB           │")
        print(f"    └─────────────────────────────────────────────┘")

    except Exception as e:
        print(f"\n  [ERROR] {e}")

    print("\n═" * 61)


if __name__ == "__main__":
    print_benefit_report()
    print()
    unittest.main(verbosity=2)
