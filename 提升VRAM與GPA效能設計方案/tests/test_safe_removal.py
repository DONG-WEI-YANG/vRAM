"""Tests for SafeRemovalManager."""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from microsystems.core.safe_removal import (
    SafeRemovalManager, RemovalState, PreflightResult, DrainProgress,
)
from microsystems.core.safety_policy import PolicyLimits


class TestRemovalState(unittest.TestCase):
    """Test RemovalState enum values."""

    def test_states_exist(self):
        self.assertEqual(RemovalState.IDLE.value, "idle")
        self.assertEqual(RemovalState.PREFLIGHT.value, "preflight")
        self.assertEqual(RemovalState.DRAINING.value, "draining")
        self.assertEqual(RemovalState.DETACHING.value, "detaching")
        self.assertEqual(RemovalState.READY.value, "ready")
        self.assertEqual(RemovalState.FORCE_EJECT.value, "force_eject")


class TestPreflightResult(unittest.TestCase):
    """Test PreflightResult dataclass."""

    def test_can_remove_immediately(self):
        r = PreflightResult(
            can_remove_immediately=True,
            current_usage_mb=0,
            system_ram_available_mb=16000,
            can_absorb=True,
            estimated_drain_seconds=0,
            warnings=[],
        )
        self.assertTrue(r.can_remove_immediately)
        self.assertEqual(r.warnings, [])

    def test_needs_drain(self):
        r = PreflightResult(
            can_remove_immediately=False,
            current_usage_mb=1200,
            system_ram_available_mb=8000,
            can_absorb=True,
            estimated_drain_seconds=27.0,
            warnings=[],
        )
        self.assertFalse(r.can_remove_immediately)
        self.assertEqual(r.current_usage_mb, 1200)


class TestSafeRemovalManager(unittest.TestCase):
    """Test SafeRemovalManager state machine."""

    def setUp(self):
        self.policy = PolicyLimits(
            device_reserved_gb=4.0,
            pagefile_min_gb=1.0,
            pagefile_max_gb=25.0,
            system_ram_reserve_gb=6.4,
        )
        self.manager = SafeRemovalManager()

    def test_initial_state_is_idle(self):
        self.assertEqual(self.manager.state("E"), RemovalState.IDLE)

    def test_preflight_with_zero_usage(self):
        """When pagefile usage is 0, can_remove_immediately should be True."""
        mock_engine = MagicMock()
        mock_engine.get_pagefile_usage.return_value = {
            "allocated_mb": 8192,
            "current_usage_mb": 0,
            "peak_usage_mb": 500,
            "safe_to_remove": True,
        }

        self.manager.set_vhd_engine(mock_engine)
        result = self.manager.preflight_check("G", self.policy)

        self.assertTrue(result.can_remove_immediately)
        self.assertEqual(result.current_usage_mb, 0)
        self.assertEqual(self.manager.state("G"), RemovalState.PREFLIGHT)

    def test_preflight_with_active_usage(self):
        """When pagefile has usage, can_remove_immediately should be False."""
        mock_engine = MagicMock()
        mock_engine.get_pagefile_usage.return_value = {
            "allocated_mb": 8192,
            "current_usage_mb": 1200,
            "peak_usage_mb": 2000,
            "safe_to_remove": False,
        }

        self.manager.set_vhd_engine(mock_engine)
        result = self.manager.preflight_check("G", self.policy)

        self.assertFalse(result.can_remove_immediately)
        self.assertEqual(result.current_usage_mb, 1200)

    def test_preflight_warns_low_ram(self):
        """When system RAM is below reserve, a warning should be added."""
        mock_engine = MagicMock()
        mock_engine.get_pagefile_usage.return_value = {
            "allocated_mb": 8192,
            "current_usage_mb": 500,
            "peak_usage_mb": 1000,
            "safe_to_remove": False,
        }

        self.manager.set_vhd_engine(mock_engine)

        with patch("microsystems.core.safe_removal._get_available_ram_mb",
                   return_value=4000):  # 4 GB < 6.4 GB reserve
            result = self.manager.preflight_check("G", self.policy)

        self.assertTrue(len(result.warnings) > 0)
        self.assertIn("RAM", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
