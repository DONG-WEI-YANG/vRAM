"""
Two-Tier Learning System for VRAM Optimization
================================================
借鑑自 OSS120BCLI learner.py 的雙層學習機制。

學習什麼：
  - 最佳 tier 分配策略（哪些層放 VRAM、哪些放 SD 卡）
  - 預取命中率最高的 lookahead 深度
  - 各模型在各 GPU 上的實際 tokens/s
  - 失敗→恢復的成功模式
  - 各裝置的實際頻寬/延遲（vs 理論值）

兩層架構：
  Project-level (.vram/learnings.json)
    → 快速、專案特定（例如：「這個專案主要跑 Llama-3 70B」）
    → 當場生效

  Global (~/.vram/learnings.json)
    → 跨專案累積（例如：「RTX 4070 + SD Gen4 x2 跑 70B 最佳分配是 12:5」）
    → 出現 3 次以上自動 promote

學習觸發時機：
  - activate() 完成後：記錄初始配置
  - deactivate() 時：記錄整場 session 的效能數據
  - recovery_chain 執行後：記錄失敗→恢復模式
  - 每 N 分鐘：記錄即時效能快照
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# 預設路徑
PROJECT_LEARNINGS = Path(".vram/learnings.json")
GLOBAL_LEARNINGS = Path.home() / ".vram" / "learnings.json"
PROMOTE_THRESHOLD = 3  # 出現 N 個專案後 promote 到 global


@dataclass
class Learning:
    """單一學習記錄"""
    key: str                    # 唯一識別 (e.g., "rtx4070_sd_gen4x2_llama3_70b_q4")
    category: str               # "tier_strategy" | "prefetch" | "performance" | "recovery" | "bandwidth"
    data: Dict[str, Any]        # 學習到的數據
    confidence: float = 0.5     # 0.0 ~ 1.0，隨驗證次數提升
    occurrences: int = 1        # 出現次數
    projects: List[str] = field(default_factory=list)  # 在哪些專案中出現
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "Learning":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class VRAMLearner:
    """
    雙層學習系統。

    使用方式：
        learner = VRAMLearner(project_id="my_project")

        # 記錄學習
        learner.learn(
            key="rtx4070_sd_gen4x2_llama3_70b_q4",
            category="tier_strategy",
            data={"vram_gb": 12, "ram_gb": 5, "sd_gb": 23, "tps": 3.2},
        )

        # 查詢建議
        hints = learner.get_hints("rtx4070", "llama3_70b_q4")
    """

    def __init__(
        self,
        project_id: str = "default",
        project_path: Optional[Path] = None,
        global_path: Optional[Path] = None,
    ):
        self._project_id = project_id
        self._project_path = project_path or PROJECT_LEARNINGS
        self._global_path = global_path or GLOBAL_LEARNINGS

        self._project_learnings: Dict[str, Learning] = {}
        self._global_learnings: Dict[str, Learning] = {}

        self._load()

    # ── Core API ──

    def learn(self, key: str, category: str, data: Dict[str, Any]) -> None:
        """
        記錄一筆學習。

        如果相同 key 已存在：
          - 更新 data（合併新數據）
          - 提升 confidence
          - 增加 occurrences
          - 檢查是否應 promote 到 global
        """
        now = time.time()

        # 更新 project-level
        if key in self._project_learnings:
            existing = self._project_learnings[key]
            existing.data.update(data)
            existing.occurrences += 1
            existing.confidence = min(1.0, existing.confidence + 0.1)
            existing.updated_at = now
            if self._project_id not in existing.projects:
                existing.projects.append(self._project_id)
        else:
            self._project_learnings[key] = Learning(
                key=key,
                category=category,
                data=data,
                confidence=0.5,
                projects=[self._project_id],
                created_at=now,
                updated_at=now,
            )

        # 檢查是否該 promote 到 global
        learning = self._project_learnings[key]
        if len(learning.projects) >= PROMOTE_THRESHOLD:
            self._promote_to_global(learning)

        self._save()
        logger.debug("Learned: %s [%s] (conf=%.2f, occ=%d)",
                      key, category, learning.confidence, learning.occurrences)

    def get_hints(self, gpu_key: str = "", model_key: str = "") -> List[Dict[str, Any]]:
        """
        查詢與當前情境相關的學習建議。

        Returns: 按 confidence 排序的建議列表
        """
        hints = []
        query = f"{gpu_key}_{model_key}".lower()

        # 先查 global（跨專案驗證過的更可靠）
        for key, learning in self._global_learnings.items():
            if not query or query in key.lower() or any(q in key.lower() for q in query.split("_") if q):
                hints.append({
                    "source": "global",
                    "key": key,
                    "category": learning.category,
                    "data": learning.data,
                    "confidence": learning.confidence,
                    "occurrences": learning.occurrences,
                })

        # 再查 project-level
        for key, learning in self._project_learnings.items():
            if not query or query in key.lower() or any(q in key.lower() for q in query.split("_") if q):
                # 不重複加入已在 global 中的
                if key not in self._global_learnings:
                    hints.append({
                        "source": "project",
                        "key": key,
                        "category": learning.category,
                        "data": learning.data,
                        "confidence": learning.confidence,
                        "occurrences": learning.occurrences,
                    })

        # 按 confidence 降序排列
        hints.sort(key=lambda h: h["confidence"], reverse=True)
        return hints

    def learn_session(
        self,
        gpu_name: str,
        device_type: str,
        device_protocol: str,
        model_key: str,
        tier_distribution: Dict[str, float],
        actual_tps: float,
        context_tokens: int,
        prefetch_hit_rate: float,
        session_duration_s: float,
        errors: int = 0,
    ) -> None:
        """
        記錄完整 session 的效能數據。
        在 deactivate() 時呼叫。
        """
        base_key = f"{gpu_name}_{device_type}_{device_protocol}_{model_key}".lower().replace(" ", "_")

        # 1. 記錄 tier 策略
        self.learn(
            key=f"{base_key}_tier",
            category="tier_strategy",
            data={
                **tier_distribution,
                "tps": actual_tps,
                "context_tokens": context_tokens,
            },
        )

        # 2. 記錄預取效果
        self.learn(
            key=f"{base_key}_prefetch",
            category="prefetch",
            data={
                "hit_rate_pct": prefetch_hit_rate,
                "duration_s": session_duration_s,
            },
        )

        # 3. 記錄實際效能
        self.learn(
            key=f"{base_key}_perf",
            category="performance",
            data={
                "actual_tps": actual_tps,
                "context_tokens": context_tokens,
                "errors": errors,
                "session_duration_s": session_duration_s,
            },
        )

    def learn_recovery(self, error_category: str, strategy: str, success: bool) -> None:
        """記錄失敗→恢復模式"""
        key = f"recovery_{error_category}_{strategy}"
        existing = self._project_learnings.get(key)

        if existing:
            stats = existing.data
            stats["attempts"] = stats.get("attempts", 0) + 1
            stats["successes"] = stats.get("successes", 0) + (1 if success else 0)
            stats["success_rate"] = stats["successes"] / stats["attempts"]
            self.learn(key=key, category="recovery", data=stats)
        else:
            self.learn(
                key=key,
                category="recovery",
                data={"attempts": 1, "successes": 1 if success else 0, "success_rate": 1.0 if success else 0.0},
            )

    def learn_bandwidth(self, device_id: str, protocol: str, measured_mbs: float, latency_us: float) -> None:
        """記錄裝置的實測頻寬/延遲"""
        key = f"bw_{device_id}_{protocol}".lower().replace(" ", "_")
        self.learn(
            key=key,
            category="bandwidth",
            data={
                "measured_bandwidth_mbs": measured_mbs,
                "measured_latency_us": latency_us,
                "protocol": protocol,
            },
        )

    def get_best_tier_strategy(self, gpu_key: str, model_key: str) -> Optional[Dict]:
        """
        查詢此 GPU + 模型組合的最佳 tier 分配策略。
        Returns: {"vram_gb": ..., "ram_gb": ..., "sd_gb": ..., "tps": ...} or None
        """
        hints = self.get_hints(gpu_key, model_key)
        for h in hints:
            if h["category"] == "tier_strategy" and h["confidence"] >= 0.6:
                return h["data"]
        return None

    def decay_confidence(self, rate: float = 0.01) -> None:
        """
        信心度衰減。定期呼叫以淘汰過時的學習。
        """
        for learnings in [self._project_learnings, self._global_learnings]:
            for learning in learnings.values():
                learning.confidence = max(0.1, learning.confidence - rate)
        self._save()

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "project_learnings": len(self._project_learnings),
            "global_learnings": len(self._global_learnings),
            "categories": self._count_categories(),
        }

    # ── Internal ──

    def _promote_to_global(self, learning: Learning) -> None:
        """將學習推廣到 global 層"""
        key = learning.key
        if key in self._global_learnings:
            # 合併
            glob = self._global_learnings[key]
            glob.data.update(learning.data)
            glob.occurrences += learning.occurrences
            glob.confidence = min(1.0, max(glob.confidence, learning.confidence) + 0.1)
            for p in learning.projects:
                if p not in glob.projects:
                    glob.projects.append(p)
            glob.updated_at = time.time()
        else:
            # 複製到 global
            self._global_learnings[key] = Learning(
                key=key,
                category=learning.category,
                data=dict(learning.data),
                confidence=min(1.0, learning.confidence + 0.1),
                occurrences=learning.occurrences,
                projects=list(learning.projects),
                created_at=learning.created_at,
                updated_at=time.time(),
            )

        logger.info("Promoted learning to global: %s (projects=%d)",
                     key, len(learning.projects))

    def _count_categories(self) -> Dict[str, int]:
        cats: Dict[str, int] = {}
        for learnings in [self._project_learnings, self._global_learnings]:
            for l in learnings.values():
                cats[l.category] = cats.get(l.category, 0) + 1
        return cats

    def _load(self) -> None:
        self._project_learnings = self._load_file(self._project_path)
        self._global_learnings = self._load_file(self._global_path)

    def _save(self) -> None:
        self._save_file(self._project_path, self._project_learnings)
        self._save_file(self._global_path, self._global_learnings)

    @staticmethod
    def _load_file(path: Path) -> Dict[str, Learning]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {k: Learning.from_dict(v) for k, v in data.items()}
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to load learnings from %s: %s", path, e)
            return {}

    @staticmethod
    def _save_file(path: Path, learnings: Dict[str, Learning]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.to_dict() for k, v in learnings.items()}
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to save learnings to %s: %s", path, e)
