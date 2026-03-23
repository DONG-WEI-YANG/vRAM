"""
JSONL Audit Log for VRAM Operations
======================================
借鑑自 OSS120BCLI audit.py。

所有記憶體操作的不可竄改（append-only）記錄。
每行一個 JSON 物件，方便 grep / jq 查詢。

用途：
  1. 效能調優 — 分析各操作的延遲分佈
  2. 故障診斷 — 重建事故時序
  3. 商業合規 — 證明資源使用紀錄
  4. 學習輸入 — 提供 learner 的原始數據

格式：
  {"ts":"2026-03-23T14:30:00","op":"allocate","block":"layer_42","tier":"VRAM",
   "size_mb":256,"duration_ms":0.3,"success":true,"device":"sd_gen4x2"}

自動輪替：當日誌超過 max_size_mb 時，rename 為 .1 並開啟新檔。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path(".vram/audit.jsonl")


class AuditLog:
    """
    Append-only JSONL 審計日誌。

    使用方式：
        log = AuditLog()
        log.record("allocate", block_id="layer_0", tier="VRAM", size_mb=256, success=True)
        log.record("migrate", block_id="layer_0", from_tier="VRAM", to_tier="RAM")

        entries = log.query(limit=10, operation="allocate")
    """

    _instance: Optional["AuditLog"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        path: Optional[Path] = None,
        max_size_mb: float = 10.0,
        enabled: bool = True,
    ):
        self._path = path or DEFAULT_LOG_PATH
        self._max_size = int(max_size_mb * 1024 * 1024)
        self._enabled = enabled
        self._lock = threading.Lock()
        self._fd = None
        self._total_entries = 0

        if self._enabled:
            self._open()

    @classmethod
    def get_instance(cls, **kwargs) -> "AuditLog":
        """Singleton 存取"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
            return cls._instance

    def record(self, operation: str, **kwargs) -> None:
        """
        記錄一筆操作。

        Args:
            operation: 操作類型 (allocate, free, migrate, transfer, activate, deactivate, error, recovery)
            **kwargs: 任意額外欄位 (block_id, tier, size_mb, duration_ms, success, device, ...)
        """
        if not self._enabled:
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "op": operation,
            **{k: v for k, v in kwargs.items() if v is not None},
        }

        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            try:
                if self._fd is None:
                    self._open()

                self._fd.write(line + "\n")
                self._fd.flush()
                self._total_entries += 1

                # 檢查是否需要輪替
                if self._fd.tell() > self._max_size:
                    self._rotate()

            except Exception as e:
                logger.error("Audit log write failed: %s", e)

    def record_allocate(
        self, block_id: str, tier: str, size_mb: float,
        success: bool, duration_ms: float = 0, device: str = "",
    ) -> None:
        self.record("allocate", block_id=block_id, tier=tier,
                     size_mb=round(size_mb, 2), success=success,
                     duration_ms=round(duration_ms, 3), device=device)

    def record_free(self, block_id: str, tier: str, size_mb: float) -> None:
        self.record("free", block_id=block_id, tier=tier, size_mb=round(size_mb, 2))

    def record_migrate(
        self, block_id: str, from_tier: str, to_tier: str,
        size_mb: float, duration_ms: float, success: bool,
    ) -> None:
        self.record("migrate", block_id=block_id, from_tier=from_tier,
                     to_tier=to_tier, size_mb=round(size_mb, 2),
                     duration_ms=round(duration_ms, 3), success=success)

    def record_transfer(
        self, block_id: str, direction: str, size_mb: float,
        throughput_mbs: float, duration_ms: float,
    ) -> None:
        self.record("transfer", block_id=block_id, direction=direction,
                     size_mb=round(size_mb, 2),
                     throughput_mbs=round(throughput_mbs, 1),
                     duration_ms=round(duration_ms, 3))

    def record_error(self, operation: str, error: str, device: str = "") -> None:
        self.record("error", failed_op=operation, error=error, device=device)

    def record_recovery(self, strategy: str, success: bool, duration_ms: float) -> None:
        self.record("recovery", strategy=strategy, success=success,
                     duration_ms=round(duration_ms, 3))

    def record_session(
        self, action: str, system: str, device: str = "",
        total_gb: float = 0, duration_s: float = 0,
    ) -> None:
        self.record(action, system=system, device=device,
                     total_gb=round(total_gb, 2), duration_s=round(duration_s, 1))

    # ── Query ──

    def query(
        self,
        limit: int = 20,
        operation: str = "",
        since: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        查詢 audit log。

        Args:
            limit: 最大回傳筆數
            operation: 篩選操作類型（空字串 = 全部）
            since: Unix timestamp，只回傳此時間之後的記錄
        """
        entries = []

        if not self._path.exists():
            return entries

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if operation and entry.get("op") != operation:
                            continue
                        if since:
                            ts_str = entry.get("ts", "")
                            entry_time = datetime.fromisoformat(ts_str).timestamp() if ts_str else 0
                            if entry_time < since:
                                continue
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue

            # 回傳最後 N 筆
            return entries[-limit:]

        except Exception as e:
            logger.error("Audit log query failed: %s", e)
            return []

    def get_operation_stats(self) -> Dict[str, Dict[str, Any]]:
        """取得各操作類型的統計"""
        stats: Dict[str, Dict[str, Any]] = {}
        entries = self.query(limit=10000)

        for entry in entries:
            op = entry.get("op", "unknown")
            if op not in stats:
                stats[op] = {"count": 0, "successes": 0, "failures": 0, "total_ms": 0}

            stats[op]["count"] += 1
            if entry.get("success") is True:
                stats[op]["successes"] += 1
            elif entry.get("success") is False:
                stats[op]["failures"] += 1

            duration = entry.get("duration_ms", 0)
            stats[op]["total_ms"] += duration

        # 計算平均
        for op, s in stats.items():
            s["avg_ms"] = s["total_ms"] / s["count"] if s["count"] > 0 else 0

        return stats

    @property
    def total_entries(self) -> int:
        return self._total_entries

    @property
    def log_path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if self._fd:
                self._fd.close()
                self._fd = None

    # ── Internal ──

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = open(self._path, "a", encoding="utf-8")
        except OSError as e:
            logger.error("Cannot open audit log %s: %s", self._path, e)
            self._enabled = False

    def _rotate(self) -> None:
        """日誌輪替"""
        if self._fd:
            self._fd.close()
            self._fd = None

        # Rename current → .1, .1 → .2, etc.
        for i in range(5, 0, -1):
            old = self._path.with_suffix(f".{i}.jsonl")
            new = self._path.with_suffix(f".{i+1}.jsonl")
            if old.exists():
                if i == 5:
                    old.unlink()  # 刪除最舊的
                else:
                    old.rename(new)

        if self._path.exists():
            self._path.rename(self._path.with_suffix(".1.jsonl"))

        self._open()
        logger.info("Audit log rotated")

    def __del__(self):
        self.close()
