"""
Tests for VHD Bridge and VHD Pagefile Engine.

Most operations require admin privileges and real devices, so we test:
- Class initialization and configuration
- Version detection (VHD vs VHDX)
- Drive letter allocation logic
- Error handling for unavailable devices
- Mock-based workflow verification
"""

import os
import platform
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure the package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '提升VRAM與GPA效能設計方案'))


class TestVhdBridge(unittest.TestCase):
    """Tests for the low-level VHD ctypes wrapper."""

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_virtdisk_dll_available(self):
        """virtdisk.dll should be loadable on all Windows versions."""
        from microsystems.core.vhd_bridge import VhdBridge
        bridge = VhdBridge()
        # Should not raise - dll is loaded in __init__

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_vhdx_supported_on_modern_windows(self):
        """VHDX should be supported on Windows 8+."""
        from microsystems.core.vhd_bridge import VhdBridge
        ver = sys.getwindowsversion()
        if ver.major >= 10:
            self.assertTrue(VhdBridge.is_vhdx_supported())

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_create_without_path_fails(self):
        """Creating VHD with invalid path should return False."""
        from microsystems.core.vhd_bridge import VhdBridge
        bridge = VhdBridge()
        # Non-existent drive path
        result = bridge.create("Z:\\nonexistent\\test.vhdx", 512 * 1024 * 1024)
        self.assertFalse(result)

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_attach_without_open_fails(self):
        """Attaching without first creating/opening should return False."""
        from microsystems.core.vhd_bridge import VhdBridge
        bridge = VhdBridge()
        result = bridge.attach()
        self.assertFalse(result)

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_detach_without_attach_is_safe(self):
        """Detaching when not attached should be safe (no crash)."""
        from microsystems.core.vhd_bridge import VhdBridge
        bridge = VhdBridge()
        bridge.detach()  # Should not raise

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_cleanup_is_safe_when_not_active(self):
        """Cleanup should be safe to call anytime."""
        from microsystems.core.vhd_bridge import VhdBridge
        bridge = VhdBridge()
        bridge.cleanup()  # Should not raise


class TestVhdPagefileEngine(unittest.TestCase):
    """Tests for the high-level VHD Pagefile Engine."""

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_engine_initialization(self):
        """Engine should initialize cleanly."""
        from microsystems.core.vhd_pagefile import VhdPagefileEngine
        engine = VhdPagefileEngine()
        self.assertFalse(engine._active)
        self.assertEqual(len(engine._devices), 0)

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_letter_allocation(self):
        """Should allocate available drive letters."""
        from microsystems.core.vhd_pagefile import VhdPagefileEngine
        engine = VhdPagefileEngine()
        letter = engine._allocate_letter()
        self.assertIsNotNone(letter)
        self.assertIn(letter, "VUWTSRQPONMLKJIHG")
        # Second allocation should give different letter
        letter2 = engine._allocate_letter()
        self.assertNotEqual(letter, letter2)
        # Release and reallocate
        engine._release_letter(letter)
        letter3 = engine._allocate_letter()
        self.assertEqual(letter, letter3)

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_activate_nonexistent_drive_fails_gracefully(self):
        """Activating on a non-existent drive should fail gracefully."""
        from microsystems.core.vhd_pagefile import VhdPagefileEngine
        engine = VhdPagefileEngine()
        result = engine.activate(drive_letters=["Z"], use_percent=80)
        self.assertFalse(result.get("success"))

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_deactivate_when_not_active_is_safe(self):
        """Deactivating when not active should be safe."""
        from microsystems.core.vhd_pagefile import VhdPagefileEngine
        engine = VhdPagefileEngine()
        result = engine.deactivate()
        self.assertTrue(result.get("success"))

    @unittest.skipUnless(platform.system() == "Windows", "Windows only")
    def test_status_when_inactive(self):
        """Status when not active should return clean state."""
        from microsystems.core.vhd_pagefile import VhdPagefileEngine
        engine = VhdPagefileEngine()
        st = engine.status()
        self.assertFalse(st.get("active"))
        self.assertEqual(st.get("device_count"), 0)


if __name__ == "__main__":
    unittest.main()
