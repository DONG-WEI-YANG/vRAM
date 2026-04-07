"""
Device Query & Detection Pipeline Tests
=========================================
測試 BusType 驅動的裝置偵測邏輯。
不需要真實裝置 — 全部用 mock。

Usage:
  python -m pytest tests/test_device_query.py -v
"""

import sys
import os
import json
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from microsystems.core.device_query import (
    ps_query,
    _normalize_list,
    classify_device,
    get_external_drive_letters,
    diagnose_devices,
    EXTERNAL_BUS_TYPES,
)


# ═══════════════════════════════════════════════
#  Test: _normalize_list
# ═══════════════════════════════════════════════

class TestNormalizeList(unittest.TestCase):
    """PowerShell 單筆/多筆結果統一處理"""

    def test_none_returns_empty(self):
        self.assertEqual(_normalize_list(None), [])

    def test_dict_wraps_in_list(self):
        self.assertEqual(_normalize_list({"a": 1}), [{"a": 1}])

    def test_list_passes_through(self):
        data = [{"a": 1}, {"b": 2}]
        self.assertEqual(_normalize_list(data), data)

    def test_non_dict_non_list_returns_empty(self):
        self.assertEqual(_normalize_list("string"), [])
        self.assertEqual(_normalize_list(42), [])


# ═══════════════════════════════════════════════
#  Test: ps_query retry logic
# ═══════════════════════════════════════════════

class TestPsQuery(unittest.TestCase):
    """PowerShell 查詢重試邏輯"""

    @patch("microsystems.core.device_query.subprocess.run")
    def test_success_first_try(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"DeviceId": 0}]',
        )
        result = ps_query("Get-Disk", label="test")
        self.assertEqual(result, [{"DeviceId": 0}])
        self.assertEqual(mock_run.call_count, 1)

    @patch("microsystems.core.device_query.subprocess.run")
    def test_retry_on_failure_then_succeed(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="error"),
            MagicMock(returncode=0, stdout='{"Number": 2}'),
        ]
        result = ps_query("Get-Disk", retries=2, label="test")
        self.assertEqual(result, {"Number": 2})
        self.assertEqual(mock_run.call_count, 2)

    @patch("microsystems.core.device_query.subprocess.run")
    def test_all_retries_fail(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="fail")
        result = ps_query("Get-Disk", retries=3, label="test")
        self.assertIsNone(result)
        self.assertEqual(mock_run.call_count, 3)

    @patch("microsystems.core.device_query.subprocess.run")
    def test_empty_stdout_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = ps_query("Get-Disk", label="test")
        self.assertIsNone(result)

    @patch("microsystems.core.device_query.subprocess.run")
    def test_invalid_json_retries(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="not json"),
            MagicMock(returncode=0, stdout='{"ok": true}'),
        ]
        result = ps_query("Get-Disk", retries=2, label="test")
        self.assertEqual(result, {"ok": True})

    @patch("microsystems.core.device_query.subprocess.run")
    def test_timeout_retries(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = [
            sp.TimeoutExpired("cmd", 15),
            MagicMock(returncode=0, stdout='[{"id": 1}]'),
        ]
        result = ps_query("Get-Disk", retries=2, label="test")
        self.assertEqual(result, [{"id": 1}])

    @patch("microsystems.core.device_query.subprocess.run")
    def test_os_error_no_retry(self, mock_run):
        mock_run.side_effect = OSError("powershell not found")
        result = ps_query("Get-Disk", retries=3, label="test")
        self.assertIsNone(result)
        self.assertEqual(mock_run.call_count, 1)  # OSError 不重試


# ═══════════════════════════════════════════════
#  Test: classify_device
# ═══════════════════════════════════════════════

class TestClassifyDevice(unittest.TestCase):
    """裝置分類邏輯 — BusType 驅動"""

    def test_usb_flash_drive(self):
        """USB 隨身碟（小容量、非 SSD）→ usb_drive"""
        result = classify_device("USB", "", "Kingston DataTraveler", 0, 32)
        self.assertEqual(result, "usb_drive")

    def test_usb_ssd_by_media_type(self):
        """USB SSD（MediaType=SSD）→ usb_ssd"""
        result = classify_device("USB", "SSD", "Samsung T5", 0, 500)
        self.assertEqual(result, "usb_ssd")

    def test_usb_ssd_by_name(self):
        """USB SSD（名稱含 SSD）→ usb_ssd"""
        result = classify_device("USB", "", "WD My Passport SSD", 0, 500)
        self.assertEqual(result, "usb_ssd")

    def test_usb_ssd_by_capacity(self):
        """USB 大容量（> 200GB）→ usb_ssd"""
        result = classify_device("USB", "", "Generic USB Drive", 0, 256)
        self.assertEqual(result, "usb_ssd")

    def test_usb_hdd(self):
        """USB HDD（有旋轉速度）→ hdd"""
        result = classify_device("USB", "HDD", "Seagate Expansion", 5400, 1000)
        self.assertEqual(result, "hdd")

    def test_usb_hdd_by_spindle(self):
        """USB HDD（SpindleSpeed > 0）→ hdd"""
        result = classify_device("USB", "", "Toshiba Canvio", 7200, 2000)
        self.assertEqual(result, "hdd")

    def test_sd_bus(self):
        """SD 匯流排 → sd_card"""
        result = classify_device("SD", "", "SD Memory Card", 0, 128)
        self.assertEqual(result, "sd_card")

    def test_usb_card_reader(self):
        """USB 讀卡器（名稱含 card）→ sd_card"""
        result = classify_device("USB", "", "Realtek USB SD Card Reader", 0, 64)
        self.assertEqual(result, "sd_card")

    def test_nvme_enclosure_generic(self):
        """NVMe（無 SD 關鍵字）→ nvme_enclosure（最常見）"""
        result = classify_device("NVMe", "SSD", "Samsung 980 PRO", 0, 1000)
        self.assertEqual(result, "nvme_enclosure")

    def test_nvme_sd_express_by_name(self):
        """NVMe + SD Express 關鍵字 → sd_express"""
        result = classify_device("NVMe", "SSD", "Realtek RTS5261 SD Express", 0, 256)
        self.assertEqual(result, "sd_express")

    def test_nvme_sd_express_controller(self):
        """NVMe + 已知 SD Express 控制器 → sd_express"""
        result = classify_device("NVMe", "", "Genesys GL9767", 0, 128)
        self.assertEqual(result, "sd_express")

    def test_thunderbolt(self):
        """Thunderbolt 匯流排 → nvme_enclosure"""
        result = classify_device("Thunderbolt", "SSD", "OWC Envoy Pro", 0, 2000)
        self.assertEqual(result, "nvme_enclosure")

    def test_usb4(self):
        """USB4 匯流排 → nvme_enclosure"""
        result = classify_device("USB4", "SSD", "Plugable TBT4", 0, 1000)
        self.assertEqual(result, "nvme_enclosure")

    def test_unknown_bus_defaults_usb_drive(self):
        """未知匯流排 → usb_drive"""
        result = classify_device("RAID", "", "SomeDevice", 0, 100)
        self.assertEqual(result, "usb_drive")

    def test_card_reader_in_name(self):
        """名稱含 'card reader' → sd_card"""
        result = classify_device("USB", "", "Generic Card Reader", 0, 32)
        self.assertEqual(result, "sd_card")

    # ── Bug #3 修復驗證 ──

    def test_nvme_fixed_drive_type_still_detected(self):
        """
        關鍵測試：NVMe 外接盒回報 Fixed 也能被正確分類。
        舊邏輯依賴 DriveType=Removable，這會漏掉。
        classify_device 完全不看 DriveType，只看 BusType。
        """
        result = classify_device("NVMe", "SSD", "Samsung 990 PRO", 0, 2000)
        self.assertEqual(result, "nvme_enclosure")

    def test_usb_ssd_fixed_drive_type_still_detected(self):
        """
        關鍵測試：USB SSD（如 Samsung T5）回報 DriveType=Fixed。
        classify_device 用 BusType=USB + MediaType=SSD 判斷。
        """
        result = classify_device("USB", "SSD", "Samsung Portable SSD T5", 0, 500)
        self.assertEqual(result, "usb_ssd")


# ═══════════════════════════════════════════════
#  Test: get_external_drive_letters (mocked)
# ═══════════════════════════════════════════════

class TestGetExternalDriveLetters(unittest.TestCase):
    """外接磁碟偵測 — 模擬 Windows 環境"""

    @patch("microsystems.core.device_query.platform.system", return_value="Windows")
    @patch("microsystems.core.device_query.get_drive_letter_to_disk_map")
    @patch("microsystems.core.device_query.get_physical_disks")
    @patch("microsystems.core.device_query._get_system_drive", return_value="C")
    def test_usb_ssd_fixed_detected(self, mock_sys, mock_disks, mock_map, mock_plat):
        """
        核心 Bug #1 測試：USB SSD 回報 Fixed 也能被偵測。
        """
        mock_disks.return_value = [
            {"DeviceId": "0", "BusType": "NVMe", "MediaType": "SSD",
             "FriendlyName": "System NVMe", "Size": 512_000_000_000,
             "IsSystem": True, "CanPool": False, "SpindleSpeed": 0},
            {"DeviceId": "2", "BusType": "USB", "MediaType": "SSD",
             "FriendlyName": "Samsung T5", "Size": 500_107_862_016,
             "IsSystem": False, "CanPool": True, "SpindleSpeed": 0},
        ]
        mock_map.return_value = {"C": 0, "E": 2}

        result = get_external_drive_letters()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["letter"], "E")
        self.assertEqual(result[0]["bus_type"], "USB")
        self.assertEqual(result[0]["friendly_name"], "Samsung T5")

    @patch("microsystems.core.device_query.platform.system", return_value="Windows")
    @patch("microsystems.core.device_query.get_drive_letter_to_disk_map")
    @patch("microsystems.core.device_query.get_physical_disks")
    @patch("microsystems.core.device_query._get_system_drive", return_value="C")
    def test_system_nvme_excluded(self, mock_sys, mock_disks, mock_map, mock_plat):
        """系統 NVMe SSD 不會被誤認為外接裝置"""
        mock_disks.return_value = [
            {"DeviceId": "0", "BusType": "NVMe", "MediaType": "SSD",
             "FriendlyName": "WD Black SN850", "Size": 1_000_000_000_000,
             "IsSystem": True, "CanPool": False, "SpindleSpeed": 0},
        ]
        mock_map.return_value = {"C": 0}

        result = get_external_drive_letters()
        self.assertEqual(len(result), 0)

    @patch("microsystems.core.device_query.platform.system", return_value="Windows")
    @patch("microsystems.core.device_query.get_drive_letter_to_disk_map")
    @patch("microsystems.core.device_query.get_physical_disks")
    @patch("microsystems.core.device_query._get_system_drive", return_value="C")
    def test_multiple_external_devices(self, mock_sys, mock_disks, mock_map, mock_plat):
        """多個外接裝置同時偵測"""
        mock_disks.return_value = [
            {"DeviceId": "0", "BusType": "NVMe", "MediaType": "SSD",
             "FriendlyName": "System", "Size": 512e9,
             "IsSystem": True, "CanPool": False, "SpindleSpeed": 0},
            {"DeviceId": "1", "BusType": "USB", "MediaType": "SSD",
             "FriendlyName": "Samsung T5", "Size": 500e9,
             "IsSystem": False, "CanPool": True, "SpindleSpeed": 0},
            {"DeviceId": "2", "BusType": "SD", "MediaType": "",
             "FriendlyName": "SD Card", "Size": 128e9,
             "IsSystem": False, "CanPool": True, "SpindleSpeed": 0},
            {"DeviceId": "3", "BusType": "USB", "MediaType": "HDD",
             "FriendlyName": "Seagate Expansion", "Size": 2000e9,
             "IsSystem": False, "CanPool": True, "SpindleSpeed": 5400},
        ]
        mock_map.return_value = {"C": 0, "E": 1, "F": 2, "G": 3}

        result = get_external_drive_letters()
        self.assertEqual(len(result), 3)
        letters = {r["letter"] for r in result}
        self.assertEqual(letters, {"E", "F", "G"})

    @patch("microsystems.core.device_query.platform.system", return_value="Windows")
    @patch("microsystems.core.device_query.get_drive_letter_to_disk_map")
    @patch("microsystems.core.device_query.get_physical_disks")
    @patch("microsystems.core.device_query._get_system_drive", return_value="C")
    def test_nvme_enclosure_detected(self, mock_sys, mock_disks, mock_map, mock_plat):
        """NVMe 外接盒（非系統碟）被偵測為外接裝置"""
        mock_disks.return_value = [
            {"DeviceId": "0", "BusType": "NVMe", "MediaType": "SSD",
             "FriendlyName": "System NVMe", "Size": 512e9,
             "IsSystem": True, "CanPool": False, "SpindleSpeed": 0},
            {"DeviceId": "1", "BusType": "NVMe", "MediaType": "SSD",
             "FriendlyName": "Samsung 990 PRO", "Size": 1000e9,
             "IsSystem": False, "CanPool": True, "SpindleSpeed": 0},
        ]
        mock_map.return_value = {"C": 0, "D": 1}

        result = get_external_drive_letters()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["letter"], "D")
        self.assertEqual(result[0]["bus_type"], "NVMe")

    @patch("microsystems.core.device_query.platform.system", return_value="Windows")
    @patch("microsystems.core.device_query.get_drive_letter_to_disk_map")
    @patch("microsystems.core.device_query.get_physical_disks")
    @patch("microsystems.core.device_query._get_system_drive", return_value="C")
    def test_sata_internal_excluded(self, mock_sys, mock_disks, mock_map, mock_plat):
        """內建 SATA 磁碟不會被收入外接裝置"""
        mock_disks.return_value = [
            {"DeviceId": "0", "BusType": "NVMe", "MediaType": "SSD",
             "FriendlyName": "System", "Size": 512e9,
             "IsSystem": True, "CanPool": False, "SpindleSpeed": 0},
            {"DeviceId": "1", "BusType": "SATA", "MediaType": "HDD",
             "FriendlyName": "Internal HDD", "Size": 2000e9,
             "IsSystem": False, "CanPool": True, "SpindleSpeed": 7200},
        ]
        mock_map.return_value = {"C": 0, "D": 1}

        result = get_external_drive_letters()
        self.assertEqual(len(result), 0)  # SATA 不在 EXTERNAL_BUS_TYPES

    @patch("microsystems.core.device_query.platform.system", return_value="Windows")
    @patch("microsystems.core.device_query.get_physical_disks")
    def test_empty_disks_returns_empty(self, mock_disks, mock_plat):
        """沒有實體磁碟 → 空清單"""
        mock_disks.return_value = []
        result = get_external_drive_letters()
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════
#  Test: diagnose_devices (smoke test)
# ═══════════════════════════════════════════════

class TestDiagnostics(unittest.TestCase):
    """診斷工具基本煙霧測試"""

    @patch("microsystems.core.device_query.platform.system", return_value="Windows")
    @patch("microsystems.core.device_query.ps_query", return_value=None)
    @patch("microsystems.core.device_query.get_external_drive_letters", return_value=[])
    @patch("microsystems.core.device_query.get_physical_disks", return_value=[])
    @patch("microsystems.core.device_query.get_drive_letter_to_disk_map", return_value={})
    def test_diagnose_returns_dict(self, *mocks):
        result = diagnose_devices()
        self.assertIsInstance(result, dict)
        self.assertIn("timestamp", result)
        self.assertIn("external_drives", result)
        self.assertIn("steps", result)
        self.assertIn("errors", result)

    def test_external_bus_types_correct(self):
        """確認 EXTERNAL_BUS_TYPES 包含正確的匯流排類型"""
        self.assertIn("USB", EXTERNAL_BUS_TYPES)
        self.assertIn("SD", EXTERNAL_BUS_TYPES)
        self.assertIn("Thunderbolt", EXTERNAL_BUS_TYPES)
        self.assertIn("USB4", EXTERNAL_BUS_TYPES)
        # SATA, RAID, NVMe 不在其中（NVMe 需要額外判斷）
        self.assertNotIn("SATA", EXTERNAL_BUS_TYPES)
        self.assertNotIn("RAID", EXTERNAL_BUS_TYPES)


if __name__ == "__main__":
    unittest.main()
