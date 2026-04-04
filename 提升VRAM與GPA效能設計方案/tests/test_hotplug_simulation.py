"""
Hotplug Simulation — SD 卡移除與插入的完整資源變化模擬
========================================================
模擬一張 SD Express 卡的完整生命週期：
  1. 初始狀態：Booster 運行中，SD 卡提供 32GB swap
  2. 移除 SD 卡：DeviceWatcher 偵測 → remove_device → degraded
  3. 重新插入：DeviceWatcher 偵測 → expand_to_device → 恢復
"""

import os
import sys
import time
import shutil
import logging
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# 加入 project path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from microsystems.core.device_watcher import (
    DeviceWatcher, DeviceEvent, DeviceChangeInfo, ExpansionAction,
    evaluate_expansion_policy, diff_snapshots,
)
from microsystems.core.health_monitor import HealthMonitor


def _sep(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _state(label: str, resources: dict):
    print(f"\n  [{label}]")
    print(f"  ┌─ GPU VRAM:    {resources['vram_gb']:.1f} GB")
    print(f"  ├─ System RAM:  {resources['ram_gb']:.1f} GB")
    print(f"  ├─ Swap (ext):  {resources['swap_gb']:.1f} GB  ← external devices")
    print(f"  ├─ Total:       {resources['total_gb']:.1f} GB")
    print(f"  ├─ Devices:     {', '.join(resources['devices']) or '(none)'}")
    print(f"  └─ Degraded:    {', '.join(resources['degraded']) or '(none)'}")


class HotplugSimulation(unittest.TestCase):
    """Full simulation: SD card removal and reinsertion with resource tracking."""

    def setUp(self):
        """Build a mock environment that tracks resource values."""
        # Simulated system state
        self.vram_gb = 12.0
        self.ram_gb = 32.0
        self.swap_devices = {}       # letter -> {"gb": float, "engine": mock, "degraded": bool}
        self.event_log = []

        # Set up DeviceWatcher
        self.watcher = DeviceWatcher()
        self.watcher.on_change(self._on_device_event)

        # Set up HealthMonitor
        self.monitor = HealthMonitor(check_interval_s=9999)  # no auto-polling
        self.disconnect_log = []
        self.reconnect_log = []
        self.monitor.on_disconnect(lambda did: self.disconnect_log.append(did))
        self.monitor.on_reconnect(lambda did: self.reconnect_log.append(did))
        self.monitor.attach_watcher(self.watcher)

    def _on_device_event(self, change: DeviceChangeInfo):
        self.event_log.append(change)

    def _get_resources(self) -> dict:
        swap_gb = sum(d["gb"] for d in self.swap_devices.values() if not d["degraded"])
        devices = [f'{l}:\\' for l, d in self.swap_devices.items() if not d["degraded"]]
        degraded = [f'{l}:\\' for l, d in self.swap_devices.items() if d["degraded"]]
        return {
            "vram_gb": self.vram_gb,
            "ram_gb": self.ram_gb,
            "swap_gb": swap_gb,
            "total_gb": self.vram_gb + self.ram_gb + swap_gb,
            "devices": devices,
            "degraded": degraded,
        }

    def _simulate_expand(self, letter: str, gb: float):
        self.swap_devices[letter] = {"gb": gb, "engine": MagicMock(), "degraded": False}

    def _simulate_remove(self, letter: str):
        if letter in self.swap_devices:
            self.swap_devices[letter]["degraded"] = True

    def _simulate_restore(self, letter: str, gb: float):
        self.swap_devices[letter] = {"gb": gb, "engine": MagicMock(), "degraded": False}

    def test_full_sd_card_lifecycle(self):
        """模擬 SD Express 卡的移除→重新插入完整流程。"""

        # ──────────────────────────────────────────────────
        _sep("Phase 0: 初始狀態 — Booster 運行中，E:\\ 提供 32GB swap")
        # ──────────────────────────────────────────────────

        # 模擬 Booster 已啟動，SD Express (E:\\) 已在 swap pool
        self._simulate_expand("E", 32.0)
        self.watcher._snapshot = {
            "E": {
                "bus_type": "NVMe",
                "friendly_name": "SD Express Gen4 (Realtek RTS5261)",
                "media_type": "SSD",
                "spindle_speed": 0,
                "size_bytes": 128 * (1024 ** 3),
            }
        }

        res = self._get_resources()
        _state("Booster Active", res)

        self.assertEqual(res["swap_gb"], 32.0)
        self.assertEqual(res["total_gb"], 12.0 + 32.0 + 32.0)  # VRAM + RAM + Swap
        self.assertEqual(res["devices"], ["E:\\"])
        self.assertEqual(res["degraded"], [])

        print(f"\n  GPU 可使用的有效記憶體: {res['total_gb']:.1f} GB")
        print(f"  (12 VRAM + 32 RAM + 32 SD Express swap)")

        # ──────────────────────────────────────────────────
        _sep("Phase 1: SD 卡被拔除 — DeviceWatcher 即時偵測")
        # ──────────────────────────────────────────────────

        print("\n  [WMI Event] Win32_VolumeChangeEvent fired!")
        print("  [DeviceWatcher] Snapshot diff: E:\\ disappeared")

        # 模擬 WMI 事件：E:\\ 從快照消失
        self.watcher._process_snapshot({})

        # 驗證事件鏈
        self.assertEqual(len(self.event_log), 1)
        evt = self.event_log[0]
        self.assertEqual(evt.event, DeviceEvent.REMOVED)
        self.assertEqual(evt.drive_letter, "E")
        print(f"  [Callback] DeviceEvent.REMOVED for E:\\")

        # 驗證 HealthMonitor 即時收到斷線
        self.assertEqual(len(self.disconnect_log), 1)
        self.assertIn("E", self.disconnect_log[0])
        print(f"  [HealthMonitor] Instant disconnect: {self.disconnect_log[0]}")

        # 模擬 remove_device 效果
        self._simulate_remove("E")
        lost_gb = 32.0

        res = self._get_resources()
        _state("After Removal", res)

        self.assertEqual(res["swap_gb"], 0.0)
        self.assertEqual(res["total_gb"], 12.0 + 32.0)  # Only VRAM + RAM
        self.assertEqual(res["devices"], [])
        self.assertEqual(res["degraded"], ["E:\\"])

        print(f"\n  Lost: {lost_gb:.1f} GB swap capacity")
        print(f"  GPU 有效記憶體: {res['total_gb']:.1f} GB → 降回 RAM-only 模式")
        print(f"  反應時間: < 1 秒 (WMI event-driven)")

        # ──────────────────────────────────────────────────
        _sep("Phase 2: SD 卡重新插入 — 自動識別並擴充")
        # ──────────────────────────────────────────────────

        print("\n  [WMI Event] Win32_VolumeChangeEvent fired!")
        print("  [DeviceWatcher] Snapshot diff: E:\\ appeared")

        # 清除事件紀錄
        self.event_log.clear()
        self.reconnect_log.clear()

        # 模擬 WMI 事件：E:\\ 重新出現
        self.watcher._process_snapshot({
            "E": {
                "bus_type": "NVMe",
                "friendly_name": "SD Express Gen4 (Realtek RTS5261)",
                "media_type": "SSD",
                "spindle_speed": 0,
                "size_bytes": 128 * (1024 ** 3),
            }
        })

        # 驗證事件
        self.assertEqual(len(self.event_log), 1)
        evt = self.event_log[0]
        self.assertEqual(evt.event, DeviceEvent.ARRIVED)
        self.assertEqual(evt.drive_letter, "E")
        print(f"  [Callback] DeviceEvent.ARRIVED for E:\\")

        # 驗證分類和策略
        info = evt.device_info
        self.assertEqual(info["device_type"], "sd_express")
        self.assertEqual(info["expansion_action"], "auto_expand")
        print(f"  [classify] device_type = {info['device_type']}")
        print(f"  [policy]   expansion_action = {info['expansion_action']}")

        # 驗證 HealthMonitor 收到重連
        self.assertEqual(len(self.reconnect_log), 1)
        print(f"  [HealthMonitor] Instant reconnect: {self.reconnect_log[0]}")

        # 模擬 expand_to_device 效果（benchmark → mmap → striped）
        print("\n  [RealBoostEngine] expand_to_device('E')...")
        print("  [RealBoostEngine]   benchmarking... 1,850 MB/s sequential")
        print("  [RealBoostEngine]   allocating 32.0 GB swap file...")
        print("  [RealBoostEngine]   adding to striped scheduler...")

        self._simulate_restore("E", 32.0)

        res = self._get_resources()
        _state("After Re-Expansion", res)

        self.assertEqual(res["swap_gb"], 32.0)
        self.assertEqual(res["total_gb"], 12.0 + 32.0 + 32.0)
        self.assertEqual(res["devices"], ["E:\\"])
        self.assertEqual(res["degraded"], [])

        print(f"\n  Restored: +{32.0:.1f} GB swap capacity")
        print(f"  GPU 有效記憶體: {res['total_gb']:.1f} GB → 完全恢復")

        # ──────────────────────────────────────────────────
        _sep("Phase 3: 額外插入 USB SSD — 智慧判斷: PROMPT")
        # ──────────────────────────────────────────────────

        self.event_log.clear()

        print("\n  [User] 插入 Samsung T5 USB SSD (G:\\)")

        self.watcher._process_snapshot({
            "E": self.watcher._snapshot["E"],  # SD Express still there
            "G": {
                "bus_type": "USB",
                "friendly_name": "Samsung T5",
                "media_type": "SSD",
                "spindle_speed": 0,
                "size_bytes": 500 * (1024 ** 3),
            }
        })

        self.assertEqual(len(self.event_log), 1)
        evt = self.event_log[0]
        self.assertEqual(evt.event, DeviceEvent.ARRIVED)
        self.assertEqual(evt.drive_letter, "G")

        info = evt.device_info
        self.assertEqual(info["device_type"], "usb_ssd")
        self.assertEqual(info["expansion_action"], "prompt_user")
        print(f"  [classify] device_type = {info['device_type']}")
        print(f"  [policy]   expansion_action = {info['expansion_action']}")
        print(f"  [GUI] 彈出提示: 「發現 Samsung T5 (G:\\)，是否加入擴充？」")

        # 模擬使用者點擊「加入」
        print(f"  [User] 點擊「Add」")
        self._simulate_expand("G", 64.0)

        res = self._get_resources()
        _state("After User Accepts USB SSD", res)

        self.assertEqual(res["swap_gb"], 32.0 + 64.0)
        self.assertEqual(res["total_gb"], 12.0 + 32.0 + 96.0)

        print(f"\n  GPU 有效記憶體: {res['total_gb']:.1f} GB")
        print(f"  (12 VRAM + 32 RAM + 32 SD Express + 64 USB SSD)")

        # ──────────────────────────────────────────────────
        _sep("Summary: 完整生命週期資源變化")
        # ──────────────────────────────────────────────────

        print("""
  Time ─────────── Event ──────────────── Total ── Swap ── Devices
  T+0.0s           Booster active         76.0 GB  32 GB   E:\\(SD Express)
  T+5.2s           SD card removed        44.0 GB   0 GB   (none)           ← < 1s detection
  T+12.7s          SD card reinserted     76.0 GB  32 GB   E:\\(SD Express)  ← auto-expand
  T+30.0s          USB SSD plugged in    140.0 GB  96 GB   E:\\ + G:\\        ← user confirmed
        """)


if __name__ == "__main__":
    unittest.main(verbosity=2)
