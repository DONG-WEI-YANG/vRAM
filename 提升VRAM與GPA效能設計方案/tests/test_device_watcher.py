"""Tests for DeviceWatcher — real-time device detection."""

import unittest
from microsystems.core.device_watcher import (
    DeviceEvent, DeviceChangeInfo, diff_snapshots,
)
from microsystems.core.device_watcher import ExpansionAction, evaluate_expansion_policy


class TestDiffSnapshots(unittest.TestCase):
    """Test the pure snapshot diff logic."""

    def test_no_change(self):
        prev = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        curr = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertEqual(arrived, [])
        self.assertEqual(removed, [])

    def test_single_arrival(self):
        prev = {"E": {"bus_type": "USB"}}
        curr = {"E": {"bus_type": "USB"}, "G": {"bus_type": "NVMe"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertEqual(arrived, ["G"])
        self.assertEqual(removed, [])

    def test_single_removal(self):
        prev = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        curr = {"E": {"bus_type": "USB"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertEqual(arrived, [])
        self.assertEqual(removed, ["F"])

    def test_simultaneous_arrival_and_removal(self):
        prev = {"E": {"bus_type": "USB"}, "F": {"bus_type": "SD"}}
        curr = {"E": {"bus_type": "USB"}, "G": {"bus_type": "NVMe"}}
        arrived, removed = diff_snapshots(prev, curr)
        self.assertIn("G", arrived)
        self.assertIn("F", removed)

    def test_empty_prev(self):
        arrived, removed = diff_snapshots({}, {"E": {"bus_type": "USB"}})
        self.assertEqual(arrived, ["E"])
        self.assertEqual(removed, [])

    def test_empty_both(self):
        arrived, removed = diff_snapshots({}, {})
        self.assertEqual(arrived, [])
        self.assertEqual(removed, [])


class TestDeviceChangeInfo(unittest.TestCase):
    def test_fields(self):
        info = DeviceChangeInfo(
            event=DeviceEvent.ARRIVED,
            drive_letter="G",
            device_info=None,
            timestamp=1000.0,
        )
        self.assertEqual(info.event, DeviceEvent.ARRIVED)
        self.assertEqual(info.drive_letter, "G")


class TestExpansionPolicy(unittest.TestCase):
    """Test smart expansion: auto / prompt / ignore."""

    def test_nvme_enclosure_auto(self):
        action = evaluate_expansion_policy("nvme_enclosure", "NVMe")
        self.assertEqual(action, ExpansionAction.AUTO_EXPAND)

    def test_sd_express_auto(self):
        action = evaluate_expansion_policy("sd_express", "NVMe")
        self.assertEqual(action, ExpansionAction.AUTO_EXPAND)

    def test_usb_ssd_prompt(self):
        action = evaluate_expansion_policy("usb_ssd", "USB")
        self.assertEqual(action, ExpansionAction.PROMPT_USER)

    def test_usb_drive_prompt(self):
        action = evaluate_expansion_policy("usb_drive", "USB")
        self.assertEqual(action, ExpansionAction.PROMPT_USER)

    def test_sd_card_prompt(self):
        action = evaluate_expansion_policy("sd_card", "SD")
        self.assertEqual(action, ExpansionAction.PROMPT_USER)

    def test_hdd_ignore(self):
        action = evaluate_expansion_policy("hdd", "USB")
        self.assertEqual(action, ExpansionAction.IGNORE)

    def test_unknown_type_ignore(self):
        action = evaluate_expansion_policy("unknown", "")
        self.assertEqual(action, ExpansionAction.IGNORE)


if __name__ == "__main__":
    unittest.main()
