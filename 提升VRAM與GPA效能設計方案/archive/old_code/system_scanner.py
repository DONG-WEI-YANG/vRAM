"""
系統掃描器 — 整合 OS、GPU、SD 卡偵測結果
製作者：Peter Yang
"""

import time
import json
from dataclasses import dataclass, field
from typing import List, Optional

from .os_detector import OSDetector, OSInfo
from .gpu_detector import GPUDetector, GPUInfo
from .sd_detector import SDCardDetector, SDCardInfo


@dataclass
class SystemReport:
    """系統掃描報告"""
    scan_time: str
    os_info: Optional[OSInfo]
    gpus: List[GPUInfo]
    sd_cards: List[SDCardInfo]
    total_vram_mb: int
    total_free_vram_mb: int
    expandable_vram_gb: float   # SD 卡可提供的擴展空間
    can_boost: bool             # 是否可以進行 VRAM 擴展
    boost_reason: str           # 可/不可擴展的原因

    def to_dict(self) -> dict:
        return {
            "scan_time": self.scan_time,
            "os": self.os_info.to_dict() if self.os_info else None,
            "gpus": [g.to_dict() for g in self.gpus],
            "sd_cards": [s.to_dict() for s in self.sd_cards],
            "total_vram_mb": self.total_vram_mb,
            "total_free_vram_mb": self.total_free_vram_mb,
            "expandable_vram_gb": self.expandable_vram_gb,
            "can_boost": self.can_boost,
            "boost_reason": self.boost_reason,
        }

    def to_json(self, indent=2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


class SystemScanner:
    """
    系統掃描器
    一鍵掃描所有硬體，產生完整的系統報告。
    """

    def __init__(self):
        self.os_detector = OSDetector()
        self.gpu_detector = GPUDetector()
        self.sd_detector = SDCardDetector()
        self._report: Optional[SystemReport] = None

    def scan(self) -> SystemReport:
        """執行完整系統掃描"""
        scan_time = time.strftime("%Y-%m-%d %H:%M:%S")

        # 1. 偵測 OS
        os_info = self.os_detector.detect()

        # 2. 偵測 GPU
        gpus = self.gpu_detector.detect()

        # 3. 偵測 SD 卡
        sd_cards = self.sd_detector.detect()

        # 4. 計算擴展能力
        total_vram = self.gpu_detector.total_vram_mb
        free_vram = self.gpu_detector.total_free_vram_mb

        expandable_cards = self.sd_detector.vram_expandable_cards
        expandable_gb = sum(c.usable_for_vram_gb for c in expandable_cards)

        # 5. 判斷是否可以擴展
        can_boost, reason = self._evaluate_boost(os_info, gpus, sd_cards, expandable_cards)

        self._report = SystemReport(
            scan_time=scan_time,
            os_info=os_info,
            gpus=gpus,
            sd_cards=sd_cards,
            total_vram_mb=total_vram,
            total_free_vram_mb=free_vram,
            expandable_vram_gb=round(expandable_gb, 1),
            can_boost=can_boost,
            boost_reason=reason,
        )
        return self._report

    @property
    def report(self) -> Optional[SystemReport]:
        return self._report

    def _evaluate_boost(self, os_info, gpus, sd_cards, expandable_cards) -> tuple:
        """評估是否可以進行 VRAM 擴展"""
        issues = []

        if not gpus:
            issues.append("未偵測到 GPU")

        if not sd_cards:
            issues.append("未偵測到 SD 卡")
        elif not expandable_cards:
            modes = [c.mode.value for c in sd_cards]
            issues.append(f"SD 卡頻寬不足 (目前: {', '.join(modes)})")

        if os_info and not os_info.sd_express_capable:
            issues.append(f"系統可能不支援 SD Express ({os_info.os_name})")

        if issues:
            return False, "；".join(issues)

        # 計算預估效能
        best_card = max(expandable_cards, key=lambda c: c.max_bandwidth_mbs)
        gpu_name = gpus[0].name if gpus else "GPU"
        return True, (
            f"可擴展！{gpu_name} + {best_card.model_name} "
            f"({best_card.capacity_gb:.0f}GB, {best_card.mode.value})"
        )
