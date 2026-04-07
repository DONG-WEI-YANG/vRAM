"""
Device Detection Integration Tests
====================================
測試 BusType 遷移後的核心行為：

  1. USBLegacyDevice._detect_windows()  — 4-step BusType 流程
  2. hotplug_launcher.get_my_drive()    — 路徑偵測 + BusType Fallback
  3. hotplug_launcher.detect_mode()     — host vs device 模式判斷

Usage:
  python -m pytest tests/test_device_detection.py -v
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from microsystems.devices.usb_legacy import USBLegacyDevice
import microsystems.hotplug_launcher as launcher


# ═══════════════════════════════════════════════════════════
#  Test: USBLegacyDevice._detect_windows() — BusType 4-step
# ═══════════════════════════════════════════════════════════

class TestUSBLegacyDetectWindows(unittest.TestCase):
    """
    USBLegacyDevice._detect_windows() 的 4-step BusType 流程測試。

    ps_query 呼叫順序：
      call 1 (label=usb_legacy_disks)      → USB 磁碟編號清單
      call 2 (label=usb_legacy_partitions) → 磁碟代號 → 磁碟編號映射
      call 3 (label=usb_legacy_volumes)    → Volume 詳細資訊
    """

    def _make_device(self) -> USBLegacyDevice:
        dev = USBLegacyDevice()
        dev._is_windows = True
        return dev

    # ── Bug Fix: USB SSD (DriveType=Fixed) 現在能被偵測 ──

    @patch("microsystems.core.device_query.ps_query")
    def test_usb_ssd_fixed_drivetype_detected(self, mock_ps):
        """
        核心 Bug Fix 測試：
        Samsung T5 等 USB SSD 回報 DriveType=Fixed，舊邏輯 (DriveType=Removable) 會漏掉。
        新邏輯用 BusType=USB（來自 Get-Disk），完全不看 DriveType。
        """
        mock_ps.side_effect = [
            [{"Number": 2}],
            [{"DriveLetter": "E", "DiskNumber": 2}],
            [{"DriveLetter": "E", "FileSystemLabel": "T5",
              "Size": 500_000_000_000, "DriveType": "Fixed"}],
        ]
        result = self._make_device()._detect_windows()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].device_path, "E:\\")
        self.assertIn("E", result[0].device_name)

    @patch("microsystems.core.device_query.ps_query")
    def test_removable_drivetype_also_detected(self, mock_ps):
        """DriveType=Removable 的傳統 USB 隨身碟仍然正常偵測"""
        mock_ps.side_effect = [
            [{"Number": 3}],
            [{"DriveLetter": "F", "DiskNumber": 3}],
            [{"DriveLetter": "F", "FileSystemLabel": "KINGSTON",
              "Size": 32_000_000_000, "DriveType": "Removable"}],
        ]
        result = self._make_device()._detect_windows()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].device_path, "F:\\")

    # ── 排除邏輯 ──

    @patch("microsystems.core.device_query.ps_query")
    def test_non_usb_disk_excluded(self, mock_ps):
        """
        同一個 Volume 清單中，只有掛在 USB 磁碟上的磁碟代號被收入。
        D: (NVMe 系統碟，disk #0) 不會被收入，E: (USB，disk #2) 才會。
        """
        mock_ps.side_effect = [
            [{"Number": 2}],                                     # USB disks: {2}
            [{"DriveLetter": "D", "DiskNumber": 0},              # D → disk 0 (NVMe)
             {"DriveLetter": "E", "DiskNumber": 2}],             # E → disk 2 (USB)
            [{"DriveLetter": "D", "FileSystemLabel": "Data",
              "Size": 1_000_000_000_000, "DriveType": "Fixed"},
             {"DriveLetter": "E", "FileSystemLabel": "USB",
              "Size": 500_000_000_000, "DriveType": "Fixed"}],
        ]
        result = self._make_device()._detect_windows()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].device_path, "E:\\")

    @patch("microsystems.core.device_query.ps_query")
    def test_no_usb_disks_early_exit(self, mock_ps):
        """
        Step 1 找不到 USB 磁碟 → 立即返回空清單。
        ps_query 只被呼叫 1 次（不繼續執行 Step 2/3）。
        """
        mock_ps.return_value = None   # Get-Disk BusType=USB 無結果

        result = self._make_device()._detect_windows()

        self.assertEqual(result, [])
        self.assertEqual(mock_ps.call_count, 1)  # 只有 step 1 被呼叫

    @patch("microsystems.core.device_query.ps_query")
    def test_zero_size_volume_skipped(self, mock_ps):
        """Volume.Size <= 0 的磁碟不加入（未格式化或佔位 partition）"""
        mock_ps.side_effect = [
            [{"Number": 2}],
            [{"DriveLetter": "E", "DiskNumber": 2}],
            [{"DriveLetter": "E", "FileSystemLabel": "", "Size": 0, "DriveType": "Removable"}],
        ]
        result = self._make_device()._detect_windows()
        self.assertEqual(result, [])

    @patch("microsystems.core.device_query.ps_query")
    def test_partition_without_usb_disk_excluded(self, mock_ps):
        """
        Volume 存在，但它對應的磁碟編號不在 USB 磁碟集合中 → 排除。
        模擬 partition map 查不到對應磁碟的情境。
        """
        mock_ps.side_effect = [
            [{"Number": 5}],                             # USB disk: {5}
            [{"DriveLetter": "G", "DiskNumber": 7}],     # G → disk 7（不是 USB）
            [{"DriveLetter": "G", "FileSystemLabel": "X",
              "Size": 64_000_000_000, "DriveType": "Removable"}],
        ]
        result = self._make_device()._detect_windows()
        self.assertEqual(result, [])

    # ── 多裝置 & 正規化 ──

    @patch("microsystems.core.device_query.ps_query")
    def test_multiple_usb_devices_all_detected(self, mock_ps):
        """多個 USB 裝置同時連接，全部被偵測"""
        mock_ps.side_effect = [
            [{"Number": 1}, {"Number": 3}],
            [{"DriveLetter": "E", "DiskNumber": 1},
             {"DriveLetter": "F", "DiskNumber": 3}],
            [{"DriveLetter": "E", "FileSystemLabel": "USB1",
              "Size": 64_000_000_000, "DriveType": "Removable"},
             {"DriveLetter": "F", "FileSystemLabel": "USB2",
              "Size": 128_000_000_000, "DriveType": "Fixed"}],
        ]
        result = self._make_device()._detect_windows()

        self.assertEqual(len(result), 2)
        paths = {r.device_path for r in result}
        self.assertEqual(paths, {"E:\\", "F:\\"})

    @patch("microsystems.core.device_query.ps_query")
    def test_drive_letter_normalized_to_uppercase(self, mock_ps):
        """
        PowerShell 有時回傳小寫磁碟代號。
        device_path 和 device_id 必須使用大寫。
        """
        mock_ps.side_effect = [
            [{"Number": 2}],
            [{"DriveLetter": "e", "DiskNumber": 2}],   # 小寫 "e"
            [{"DriveLetter": "e", "FileSystemLabel": "test",
              "Size": 32_000_000_000, "DriveType": "Removable"}],
        ]
        result = self._make_device()._detect_windows()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].device_path, "E:\\")
        self.assertIn("E", result[0].device_id)

    @patch("microsystems.core.device_query.ps_query")
    def test_label_used_in_device_name(self, mock_ps):
        """FileSystemLabel 出現在 device_name 中"""
        mock_ps.side_effect = [
            [{"Number": 2}],
            [{"DriveLetter": "E", "DiskNumber": 2}],
            [{"DriveLetter": "E", "FileSystemLabel": "MySamsung",
              "Size": 500_000_000_000, "DriveType": "Fixed"}],
        ]
        result = self._make_device()._detect_windows()
        self.assertIn("MySamsung", result[0].device_name)

    @patch("microsystems.core.device_query.ps_query")
    def test_ps_query_exception_returns_empty(self, mock_ps):
        """ps_query 拋出例外 → _detect_windows 返回空清單（不 crash）"""
        mock_ps.side_effect = Exception("PowerShell unavailable")
        result = self._make_device()._detect_windows()
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════════════════
#  Test: hotplug_launcher.get_my_drive()
# ═══════════════════════════════════════════════════════════

class TestGetMyDrive(unittest.TestCase):
    """
    get_my_drive() 的兩個路徑：
      A. 從 exe 路徑直接提取磁碟代號（非系統碟）
      B. Fallback：用 BusType 掃描外接裝置
    """

    @patch.dict(os.environ, {"SystemDrive": "C:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.os.path.abspath",
           return_value="E:\\VRAM_Booster.exe")
    def test_exe_on_external_drive_returns_letter(self, mock_abs, mock_plat):
        """
        exe 在 E: (非系統碟) → 直接回傳 "E"，不需要 BusType fallback。
        """
        result = launcher.get_my_drive()
        self.assertEqual(result, "E")

    @patch.dict(os.environ, {"SystemDrive": "D:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.os.path.abspath",
           return_value="D:\\Users\\user\\VRAM_Booster.exe")
    def test_system_drive_detection_uses_env_var(self, mock_abs, mock_plat):
        """
        系統碟由 SystemDrive 環境變數決定，不硬編碼 "C:"。
        此測試中系統碟是 D:，exe 在 D: → 不應直接回傳，改走 fallback。
        """
        with patch("microsystems.core.device_query.get_external_drive_letters",
                   return_value=[{"letter": "E", "bus_type": "USB"}]):
            result = launcher.get_my_drive()
        self.assertEqual(result, "E")

    @patch.dict(os.environ, {"SystemDrive": "C:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.os.path.abspath",
           return_value="C:\\Users\\user\\VRAM_Booster.exe")
    @patch("microsystems.core.device_query.get_external_drive_letters",
           return_value=[{"letter": "F", "bus_type": "USB"},
                         {"letter": "G", "bus_type": "SD"}])
    def test_bustype_fallback_returns_first_external_drive(self, mock_ext,
                                                            mock_abs, mock_plat):
        """
        exe 在系統碟 C: → BusType fallback → 回傳第一個外接裝置的代號。
        """
        result = launcher.get_my_drive()
        self.assertEqual(result, "F")

    @patch.dict(os.environ, {"SystemDrive": "C:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.os.path.abspath",
           return_value="C:\\Users\\user\\VRAM_Booster.exe")
    @patch("microsystems.core.device_query.get_external_drive_letters",
           return_value=[])
    def test_no_external_drives_returns_none(self, mock_ext, mock_abs, mock_plat):
        """
        BusType fallback 也找不到外接裝置 → 回傳 None。
        """
        result = launcher.get_my_drive()
        self.assertIsNone(result)

    @patch.dict(os.environ, {"SystemDrive": "C:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.os.path.abspath",
           return_value="C:\\Users\\user\\VRAM_Booster.exe")
    @patch("microsystems.core.device_query.get_external_drive_letters",
           side_effect=RuntimeError("WMI crash"))
    def test_bustype_fallback_exception_returns_none(self, mock_ext,
                                                      mock_abs, mock_plat):
        """
        BusType scan 拋出例外 → logger.warning，回傳 None（不 crash）。
        """
        result = launcher.get_my_drive()
        self.assertIsNone(result)

    # ── TODO (你的任務) ──────────────────────────────────────
    # 下面這個測試模擬 PyInstaller 打包後的情境（sys.frozen=True）。
    # 設計重點：打包後 sys.executable 可能指向 %TEMP%，但 sys.argv[0]
    # 保留了 exe 被呼叫時的原始路徑（外接磁碟上的真實位置）。
    #
    # 請實作這個測試：
    #   - sys.frozen = True
    #   - sys.executable = "C:\\Users\\user\\AppData\\Local\\Temp\\VRAM_Booster.exe"
    #                       （PyInstaller onefile 解壓縮到暫存目錄）
    #   - sys.argv[0]     = "E:\\VRAM_Booster.exe"（原始呼叫路徑，外接磁碟）
    #   - SystemDrive     = "C:"
    #
    # 預期結果：get_my_drive() 回傳 "E"（從 sys.argv[0] 取得）
    #
    # 提示：
    #   - patch.object(sys, 'frozen', True, create=True)
    #   - patch.object(sys, 'executable', ...)
    #   - patch.object(sys, 'argv', [...])
    #   - os.path.abspath 需要 return actual values (用 side_effect=[...])

    def test_frozen_exe_argv_takes_priority_over_temp_path(self):
        # TODO: 你來實作
        pass


# ═══════════════════════════════════════════════════════════
#  Test: hotplug_launcher.detect_mode()
# ═══════════════════════════════════════════════════════════

class TestDetectMode(unittest.TestCase):
    """detect_mode() 決定程式以 'host' 或 'device' 模式啟動"""

    @patch.dict(os.environ, {"SystemDrive": "C:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.get_my_drive", return_value="E")
    def test_external_drive_is_device_mode(self, mock_drive, mock_plat):
        """exe 在外接磁碟 E: → 'device' 模式"""
        result = launcher.detect_mode()
        self.assertEqual(result, "device")

    @patch.dict(os.environ, {"SystemDrive": "C:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.get_my_drive", return_value="C")
    def test_system_drive_is_host_mode(self, mock_drive, mock_plat):
        """exe 在系統碟 C: → 'host' 模式"""
        result = launcher.detect_mode()
        self.assertEqual(result, "host")

    @patch("microsystems.hotplug_launcher.get_my_drive", return_value=None)
    def test_no_drive_detected_is_host_mode(self, mock_drive):
        """get_my_drive() 回傳 None → 'host' 模式（安全 fallback）"""
        result = launcher.detect_mode()
        self.assertEqual(result, "host")

    @patch.dict(os.environ, {"SystemDrive": "D:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.get_my_drive", return_value="D")
    def test_host_mode_respects_non_c_system_drive(self, mock_drive, mock_plat):
        """
        SystemDrive 不一定是 C:（企業環境或多重開機）。
        當 get_my_drive 回傳 D: 且 SystemDrive=D: → 'host' 模式。
        """
        result = launcher.detect_mode()
        self.assertEqual(result, "host")

    @patch.dict(os.environ, {"SystemDrive": "C:"})
    @patch("microsystems.hotplug_launcher.platform.system", return_value="Windows")
    @patch("microsystems.hotplug_launcher.get_my_drive", return_value="D")
    def test_device_mode_when_drive_differs_from_system(self, mock_drive, mock_plat):
        """
        SystemDrive=C:，exe 磁碟=D: → 'device' 模式。
        確認比較邏輯用的是 SystemDrive env var，不是硬編碼 "C:"。
        """
        result = launcher.detect_mode()
        self.assertEqual(result, "device")


if __name__ == "__main__":
    unittest.main(verbosity=2)
