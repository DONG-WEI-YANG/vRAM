"""
VRAM Booster 核心引擎 — 整合偵測、記憶體管理、擴展啟動
製作者：Peter Yang
"""

import time
import json
import os
from typing import Optional, Callable

from ..detectors.system_scanner import SystemScanner, SystemReport
from ..memory.tier_manager import TierManager


class VRAMBooster:
    """
    SD-VRAM Booster 核心引擎
    負責整個「插入 → 偵測 → 詢問 → 擴展」流程。
    """

    def __init__(self, on_status_change: Optional[Callable] = None):
        self.scanner = SystemScanner()
        self.tier_manager = TierManager()
        self._report: Optional[SystemReport] = None
        self._is_boosted = False
        self._on_status_change = on_status_change
        self._log: list = []

    def scan_system(self) -> SystemReport:
        """Step 1: 掃描系統"""
        self._log_event("開始系統掃描...")
        self._report = self.scanner.scan()

        self._log_event(f"OS: {self._report.os_info.summary}")
        for gpu in self._report.gpus:
            self._log_event(f"GPU: {gpu.summary}")
        for sd in self._report.sd_cards:
            self._log_event(f"SD Card: {sd.summary}")

        if self._report.can_boost:
            self._log_event(f"✓ {self._report.boost_reason}")
        else:
            self._log_event(f"✗ {self._report.boost_reason}")

        # 初始化記憶體層級
        self.tier_manager.initialize(self._report.gpus, self._report.sd_cards)

        self._notify_status()
        return self._report

    def activate_boost(self, sd_card_index: int = 0,
                       size_gb: Optional[float] = None) -> dict:
        """Step 2: 啟動 VRAM 擴展"""
        if not self._report:
            return {"success": False, "message": "請先執行系統掃描"}

        expandable = self._report.sd_cards
        if not expandable:
            return {"success": False, "message": "無可用的 SD 卡"}

        if sd_card_index >= len(expandable):
            sd_card_index = 0

        card = expandable[sd_card_index]

        # 預設使用 SD 卡可用空間的 90%
        if size_gb is None:
            size_gb = card.usable_for_vram_gb

        self._log_event(f"正在啟動 VRAM 擴展: {card.model_name} ({size_gb:.1f} GB)...")

        # 確定掛載點
        mount_point = card.mount_point
        if not mount_point:
            return {"success": False, "message": "SD 卡未掛載，請先掛載 SD 卡"}

        result = self.tier_manager.activate_sd_expansion(mount_point, size_gb)

        if result["success"]:
            self._is_boosted = True
            self._log_event(f"✓ {result['message']}")
        else:
            self._log_event(f"✗ {result['message']}")

        self._notify_status()
        return result

    def deactivate_boost(self) -> dict:
        """停用 VRAM 擴展"""
        self._log_event("正在停用 VRAM 擴展...")
        result = self.tier_manager.deactivate()

        if result["success"]:
            self._is_boosted = False
            self._log_event("✓ VRAM 擴展已安全停用")
        else:
            self._log_event(f"✗ {result['message']}")

        self._notify_status()
        return result

    def get_full_status(self) -> dict:
        """取得完整狀態"""
        return {
            "is_boosted": self._is_boosted,
            "system_report": self._report.to_dict() if self._report else None,
            "memory_tiers": self.tier_manager.get_status(),
            "log": self._log[-50:],  # 最近 50 筆日誌
        }

    def calculate_context_window_boost(self, model_name: str = "Llama-3-8B") -> dict:
        """計算 Context Window 提升預估"""
        # 模型 KV Cache 參數 (每 token bytes, FP16)
        model_params = {
            "Llama-3-8B": {"kv_per_token": 131072, "weight_gb": 4.5, "label": "Llama-3 8B (Q4)"},
            "Llama-3-70B": {"kv_per_token": 327680, "weight_gb": 40.0, "label": "Llama-3 70B (Q4)"},
            "Qwen-2.5-32B": {"kv_per_token": 262144, "weight_gb": 18.0, "label": "Qwen-2.5 32B (Q4)"},
            "Mistral-7B": {"kv_per_token": 131072, "weight_gb": 4.0, "label": "Mistral 7B (Q4)"},
        }

        params = model_params.get(model_name, model_params["Llama-3-8B"])
        kv_per_token = params["kv_per_token"]
        weight_gb = params["weight_gb"]

        # 計算純 VRAM context
        vram_gb = self._report.total_vram_mb / 1024 if self._report else 12
        overhead_gb = 1.0
        available_vram = max(0, vram_gb - weight_gb - overhead_gb)
        vram_context = int(available_vram * (1024**3) / kv_per_token)

        # 計算擴展後 context
        sd_gb = self._report.expandable_vram_gb if self._report else 0
        total_available = available_vram + sd_gb
        boosted_context = int(total_available * (1024**3) / kv_per_token)

        boost_ratio = boosted_context / max(vram_context, 1)

        return {
            "model": params["label"],
            "vram_only_context": vram_context,
            "boosted_context": boosted_context,
            "boost_ratio": round(boost_ratio, 1),
            "vram_gb": round(vram_gb, 1),
            "sd_expansion_gb": round(sd_gb, 1),
        }

    @property
    def log(self) -> list:
        return self._log

    def _log_event(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self._log.append(entry)

    def _notify_status(self):
        if self._on_status_change:
            try:
                self._on_status_change(self.get_full_status())
            except Exception:
                pass
