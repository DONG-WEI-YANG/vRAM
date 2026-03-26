"""Tests for SafetyPolicy engine."""

import sys
import os
import unittest
import tempfile
import json
from pathlib import Path

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


class TestConfigMerge(unittest.TestCase):
    """Test load_merged_policy with global + device overrides."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.global_path = os.path.join(self.tmpdir, "safety_policy.json")
        self.device_path = os.path.join(self.tmpdir, ".vram_boost_config.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_config_files_uses_smart_defaults(self):
        """When no config files exist, smart defaults are used."""
        p = SafetyPolicy.load_merged_policy(
            Path(os.path.join(self.tmpdir, "nonexistent.json")),
            Path(os.path.join(self.tmpdir, "nonexistent2.json")),
            capacity_gb=64.0, speed_mbs=1000.0,
        )
        self.assertEqual(p.device_reserved_gb, 4.0)  # medium tier default
        self.assertEqual(p.pagefile_min_gb, 1.0)

    def test_global_override_replaces_smart_default(self):
        """Global config overrides smart defaults."""
        with open(self.global_path, "w") as f:
            json.dump({
                "version": 1,
                "global_defaults": {
                    "device_reserved_gb": 10,
                    "pagefile_min_gb": "auto",
                    "pagefile_max_gb": "auto",
                    "system_ram_reserve_pct": 20,
                }
            }, f)

        p = SafetyPolicy.load_merged_policy(
            Path(self.global_path), Path(self.device_path),
            capacity_gb=64.0, speed_mbs=1000.0,
        )
        self.assertEqual(p.device_reserved_gb, 10.0)  # overridden
        self.assertEqual(p.pagefile_min_gb, 1.0)       # "auto" = smart default

    def test_device_override_wins_over_global(self):
        """Device-level override takes precedence over global."""
        with open(self.global_path, "w") as f:
            json.dump({
                "version": 1,
                "global_defaults": {"device_reserved_gb": 10}
            }, f)

        with open(self.device_path, "w") as f:
            json.dump({
                "card_fingerprint": "64.0GB|TEST",
                "rand_write_mbs": 100,
                "swap_size_bytes": 0,
                "safety_override": {"device_reserved_gb": 3}
            }, f)

        p = SafetyPolicy.load_merged_policy(
            Path(self.global_path), Path(self.device_path),
            capacity_gb=64.0, speed_mbs=1000.0,
        )
        self.assertEqual(p.device_reserved_gb, 3.0)  # device wins

    def test_save_global_policy(self):
        """save_global_policy writes valid JSON."""
        SafetyPolicy.save_global_policy(
            Path(self.global_path),
            {"device_reserved_gb": 5, "pagefile_max_gb": 30},
        )
        with open(self.global_path) as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["global_defaults"]["device_reserved_gb"], 5)

    def test_save_device_override(self):
        """save_device_override merges into existing config."""
        # Pre-existing device config
        with open(self.device_path, "w") as f:
            json.dump({"card_fingerprint": "64.0GB|TEST", "rand_write_mbs": 100}, f)

        SafetyPolicy.save_device_override(
            Path(self.device_path),
            {"pagefile_max_gb": 20},
        )
        with open(self.device_path) as f:
            data = json.load(f)
        self.assertEqual(data["card_fingerprint"], "64.0GB|TEST")  # preserved
        self.assertEqual(data["safety_override"]["pagefile_max_gb"], 20)


class TestValidation(unittest.TestCase):
    """Test validate_activation()."""

    def setUp(self):
        self.policy = PolicyLimits(
            device_reserved_gb=4.0,
            pagefile_min_gb=1.0,
            pagefile_max_gb=25.0,
            system_ram_reserve_gb=6.4,
        )

    def test_valid_activation(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 20.0, self.policy)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_reject_below_min(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 0.5, self.policy)
        self.assertFalse(ok)
        self.assertIn("min", reason.lower())

    def test_reject_above_max(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 30.0, self.policy)
        self.assertFalse(ok)
        self.assertIn("max", reason.lower())

    def test_reject_insufficient_device_space(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 62.0, self.policy)
        self.assertFalse(ok)
        self.assertIn("reserved", reason.lower())

    def test_exact_boundary_max(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 25.0, self.policy)
        self.assertTrue(ok)

    def test_exact_boundary_min(self):
        ok, reason = SafetyPolicy.validate_activation(64.0, 1.0, self.policy)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
