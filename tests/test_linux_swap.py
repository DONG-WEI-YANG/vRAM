"""
Tests for Linux Multi-Device Swap Engine.

Actual swap operations require root privileges and Linux OS.
These tests verify initialization, logic, and error handling.
"""

import os
import platform
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '提升VRAM與GPA效能設計方案'))


class TestLinuxSwapEngine(unittest.TestCase):
    """Tests for LinuxSwapEngine."""

    def _get_engine(self):
        """Import and return a LinuxSwapEngine instance."""
        from microsystems.core.linux_swap import LinuxSwapEngine
        return LinuxSwapEngine()

    def test_engine_initialization(self):
        """Engine should initialize with empty state."""
        engine = self._get_engine()
        self.assertFalse(engine._active)
        self.assertEqual(len(engine._devices), 0)

    def test_constants(self):
        """Key constants should be set."""
        from microsystems.core.linux_swap import LinuxSwapEngine
        self.assertEqual(LinuxSwapEngine.SWAP_FILENAME, "vram_boost.swap")
        self.assertEqual(LinuxSwapEngine.SWAP_FILL_TIME_SECONDS, 600)
        self.assertGreater(LinuxSwapEngine.SWAP_PRIORITY, 0)

    def test_deactivate_when_not_active(self):
        """Deactivating when not active should be safe."""
        engine = self._get_engine()
        result = engine.deactivate()
        self.assertTrue(result.get("success"))

    def test_status_when_inactive(self):
        """Status when not active should return clean state."""
        engine = self._get_engine()
        st = engine.status()
        self.assertFalse(st.get("active"))
        self.assertEqual(st.get("device_count"), 0)

    def test_activate_nonexistent_mount_fails(self):
        """Activating on non-existent mount should fail gracefully."""
        engine = self._get_engine()
        result = engine.activate(
            mount_points=["/nonexistent/path"],
            use_percent=80
        )
        # Should not crash, may return success=False or skip the device
        self.assertIsInstance(result, dict)

    def test_benchmark_returns_positive(self):
        """Benchmark should return a positive number or fallback value."""
        engine = self._get_engine()
        # Use current directory as a valid path for benchmark
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            speed = engine._benchmark_random_write(td, test_size_mb=1)
            self.assertGreater(speed, 0)

    @patch('os.path.isdir', return_value=True)
    @patch('subprocess.run')
    def test_scan_mount_points_parses_lsblk(self, mock_run, mock_isdir):
        """Should parse lsblk JSON output to find removable mount points."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"blockdevices": [{"name": "sda", "mountpoint": null, "rm": true, "type": "disk", "children": [{"name": "sda1", "mountpoint": "/mnt/usb", "rm": true, "type": "part"}]}, {"name": "mmcblk0", "mountpoint": null, "rm": true, "type": "disk", "children": [{"name": "mmcblk0p1", "mountpoint": "/mnt/sd", "rm": true, "type": "part"}]}]}'
        )
        engine = self._get_engine()
        points = engine._scan_mount_points("/mnt/usb")
        # Should find at least /mnt/usb and /mnt/sd
        self.assertIn("/mnt/usb", points)
        self.assertIn("/mnt/sd", points)

    @patch('subprocess.run')
    def test_scan_mount_points_handles_empty(self, mock_run):
        """Should handle empty lsblk output gracefully."""
        mock_run.return_value = MagicMock(returncode=1, stdout='')
        engine = self._get_engine()
        points = engine._scan_mount_points("/mnt/usb")
        # Should at least return the primary mount point
        self.assertIn("/mnt/usb", points)

    def test_verify_swap_file_nonexistent(self):
        """Verifying non-existent file should return False."""
        engine = self._get_engine()
        result = engine._verify_swap_file("/nonexistent/swap")
        self.assertFalse(result)

    def test_check_device_present(self):
        """Existing path should be 'present', fake path should not."""
        engine = self._get_engine()
        import tempfile
        # Temp dir always exists on any OS
        self.assertTrue(engine._check_device_present(tempfile.gettempdir()))
        # Non-existent path should not be present
        self.assertFalse(engine._check_device_present("/nonexistent/xyz/abc"))


if __name__ == "__main__":
    unittest.main()
