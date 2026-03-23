"""
Undo Manager for VRAM Allocations
====================================
借鑑自 OSS120BCLI undo_manager.py。

在每次重大記憶體操作前拍快照，支援多步回滾。
防止模型載入失敗留下殘餘分配導致記憶體洩漏。

快照內容：
  - 所有區塊的 block_id, size, tier, tag
  - 各層使用量
  - CUDA interceptor 統計

使用場景：
  1. activate() 前拍快照 → 失敗時 rollback 到空白狀態
  2. 批次 allocate 前拍快照 → 載入模型一半失敗時全部 rollback
  3. migrate 前拍快照 → 遷移失敗時恢復原位
"""

from __future__ import annotations

import copy
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable

from .memory_pool import MemoryPool, MemoryTier, Block

logger = logging.getLogger(__name__)


@dataclass
class PoolSnapshot:
    """記憶體池快照"""
    snapshot_id: str
    timestamp: float
    description: str

    # 區塊狀態
    blocks: Dict[str, Dict[str, Any]]       # block_id → {size, tier, tag, ...}
    tier_used: Dict[str, int]                # tier_name → used_bytes

    # 統計
    total_blocks: int = 0
    total_used_bytes: int = 0

    def summary(self) -> str:
        return (f"Snapshot[{self.snapshot_id}] {self.description}: "
                f"{self.total_blocks} blocks, "
                f"{self.total_used_bytes / (1024**3):.2f}GB used")


class UndoManager:
    """
    記憶體分配的 Undo 管理器。

    使用方式：
        undo = UndoManager(pool)

        undo.checkpoint("before_model_load")
        try:
            pool.allocate("layer_0", 1GB)
            pool.allocate("layer_1", 1GB)
            ...
        except MemoryError:
            undo.rollback()  # 回復到 checkpoint 狀態

        undo.checkpoint("before_migrate")
        ...
        undo.rollback()  # 回復到 before_migrate
        undo.rollback()  # 再回復到 before_model_load
    """

    MAX_SNAPSHOTS = 10

    def __init__(self, pool: MemoryPool):
        self._pool = pool
        self._snapshots: deque[PoolSnapshot] = deque(maxlen=self.MAX_SNAPSHOTS)
        self._counter = 0

        # 回調：rollback 時需要執行的外部清理（如關閉裝置區塊）
        self._on_rollback: Optional[Callable[[PoolSnapshot], None]] = None

        # 統計
        self._total_checkpoints = 0
        self._total_rollbacks = 0

    def on_rollback(self, callback: Callable[[PoolSnapshot], None]) -> None:
        """註冊 rollback 回調"""
        self._on_rollback = callback

    def checkpoint(self, description: str = "") -> str:
        """
        拍攝當前記憶體池的快照。

        Returns: snapshot_id
        """
        self._counter += 1
        snap_id = f"snap_{self._counter:04d}"

        # 序列化所有區塊
        blocks = {}
        tier_used = {}

        for tier in MemoryTier:
            state = self._pool.get_tier_state(tier)
            tier_used[tier.name] = state.used_bytes

            for blk in self._pool.get_blocks_in_tier(tier):
                blocks[blk.block_id] = {
                    "size_bytes": blk.size_bytes,
                    "tier": blk.tier.name,
                    "data_tag": blk.data_tag,
                    "is_pinned": blk.is_pinned,
                    "access_count": blk.access_count,
                }

        snapshot = PoolSnapshot(
            snapshot_id=snap_id,
            timestamp=time.time(),
            description=description,
            blocks=blocks,
            tier_used=tier_used,
            total_blocks=len(blocks),
            total_used_bytes=sum(tier_used.values()),
        )

        self._snapshots.append(snapshot)
        self._total_checkpoints += 1

        logger.info("Checkpoint: %s", snapshot.summary())
        return snap_id

    def rollback(self, snapshot_id: Optional[str] = None) -> bool:
        """
        回滾到指定快照（或最近一個）。

        執行步驟：
        1. 找到目標快照
        2. 釋放快照中不存在但現在存在的區塊
        3. 恢復快照中存在但現在已釋放的區塊
        4. 修正層級使用量
        """
        if not self._snapshots:
            logger.warning("No snapshots available for rollback")
            return False

        # 找到目標快照
        if snapshot_id:
            target = None
            for snap in reversed(self._snapshots):
                if snap.snapshot_id == snapshot_id:
                    target = snap
                    break
            if not target:
                logger.error("Snapshot %s not found", snapshot_id)
                return False
        else:
            target = self._snapshots.pop()

        logger.info("Rolling back to: %s", target.summary())

        # 1. 找出差異
        current_blocks = set()
        for tier in MemoryTier:
            for blk in self._pool.get_blocks_in_tier(tier):
                current_blocks.add(blk.block_id)

        snapshot_blocks = set(target.blocks.keys())

        to_free = current_blocks - snapshot_blocks      # 新增的要釋放
        to_restore = snapshot_blocks - current_blocks    # 丟失的要恢復

        # 2. 釋放快照後新增的區塊
        freed_count = 0
        for block_id in to_free:
            self._pool.free(block_id)
            freed_count += 1

        # 3. 恢復快照中存在但現在丟失的區塊
        restored_count = 0
        for block_id in to_restore:
            blk_data = target.blocks[block_id]
            try:
                target_tier = MemoryTier[blk_data["tier"]]
                self._pool.allocate(
                    block_id,
                    blk_data["size_bytes"],
                    tag=blk_data.get("data_tag", ""),
                    preferred_tier=target_tier,
                )
                restored_count += 1
            except (MemoryError, ValueError) as e:
                logger.warning("Cannot restore block '%s': %s", block_id, e)

        # 4. 觸發外部回調
        if self._on_rollback:
            try:
                self._on_rollback(target)
            except Exception as e:
                logger.error("Rollback callback error: %s", e)

        self._total_rollbacks += 1

        # 移除目標快照及其後的所有快照
        if snapshot_id:
            while self._snapshots and self._snapshots[-1].snapshot_id != snapshot_id:
                self._snapshots.pop()
            if self._snapshots:
                self._snapshots.pop()

        logger.info("Rollback complete: freed %d blocks, restored %d blocks",
                     freed_count, restored_count)
        return True

    def peek(self) -> Optional[PoolSnapshot]:
        """查看最近的快照（不移除）"""
        if self._snapshots:
            return self._snapshots[-1]
        return None

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """列出所有快照"""
        return [
            {
                "id": s.snapshot_id,
                "description": s.description,
                "timestamp": s.timestamp,
                "blocks": s.total_blocks,
                "used_gb": s.total_used_bytes / (1024**3),
            }
            for s in self._snapshots
        ]

    def clear(self) -> None:
        """清除所有快照"""
        self._snapshots.clear()

    @property
    def depth(self) -> int:
        return len(self._snapshots)

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "snapshots": len(self._snapshots),
            "max_snapshots": self.MAX_SNAPSHOTS,
            "total_checkpoints": self._total_checkpoints,
            "total_rollbacks": self._total_rollbacks,
        }
