"""Tests for SafetyPolicy engine."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from microsystems.core.safety_policy import SafetyPolicy, PolicyLimits


class TestPolicyLimits(unittest.TestCase):
    """Test PolicyLimits dataclass."""

    def test_create_policy_limits(self):
        p = PolicyLimits(
            device_reserved_gb=4.0,
            pagefile_min_gb=1.0,
            pagefile_max_gb=25.0,
            system_ram_reserve_gb=6.4,
        )
        self.assertEqual(p.device_reserved_gb, 4.0)
        self.assertEqual(p.pagefile_min_gb, 1.0)
        self.assertEqual(p.pagefile_max_gb, 25.0)
        self.assertEqual(p.system_ram_reserve_gb, 6.4)


class TestSmartDefaults(unittest.TestCase):
    """Test compute_smart_defaults() tier logic."""

    def test_small_device_under_32gb(self):
        """< 32 GB: reserved=2, pf_min=0.5, pf_max=capacity*60%"""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=16.0, speed_mbs=500.0)
        self.assertEqual(p.device_reserved_gb, 2.0)
        self.assertEqual(p.pagefile_min_gb, 0.5)
        self.assertAlmostEqual(p.pagefile_max_gb, 16.0 * 0.6)

    def test_medium_device_32_to_128gb(self):
        """32-128 GB: reserved=4, pf_min=1, pf_max=capacity*70%"""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=64.0, speed_mbs=1000.0)
        self.assertEqual(p.device_reserved_gb, 4.0)
        self.assertEqual(p.pagefile_min_gb, 1.0)
        self.assertAlmostEqual(p.pagefile_max_gb, 64.0 * 0.7)

    def test_large_device_over_128gb(self):
        """128+ GB: reserved=8, pf_min=2, pf_max=capacity*80%"""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=256.0, speed_mbs=3000.0)
        self.assertEqual(p.device_reserved_gb, 8.0)
        self.assertEqual(p.pagefile_min_gb, 2.0)
        self.assertAlmostEqual(p.pagefile_max_gb, 256.0 * 0.8)

    def test_boundary_32gb_exact(self):
        """32 GB exactly falls into medium tier."""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=32.0, speed_mbs=500.0)
        self.assertEqual(p.device_reserved_gb, 4.0)
        self.assertEqual(p.pagefile_min_gb, 1.0)

    def test_boundary_128gb_exact(self):
        """128 GB exactly falls into large tier."""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=128.0, speed_mbs=1000.0)
        self.assertEqual(p.device_reserved_gb, 8.0)
        self.assertEqual(p.pagefile_min_gb, 2.0)

    def test_system_ram_reserve_is_set(self):
        """system_ram_reserve_gb should be > 0 (based on system RAM * 20%)."""
        p = SafetyPolicy.compute_smart_defaults(capacity_gb=64.0, speed_mbs=1000.0)
        self.assertGreater(p.system_ram_reserve_gb, 0.0)


if __name__ == "__main__":
    unittest.main()
