"""
Tiered Memory Pool Manager
===========================
管理三層記憶體階層的資料分配與遷移：
  L1: GPU VRAM (熱資料) — 當前運算層的權重與中間結果
  L2: System RAM (溫資料) — 即將被存取的鄰近層權重
  L3: External Storage (冷資料) — KV Cache、非活躍模型層

資料會根據存取頻率在層級間動態遷移（promotion / demotion）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, Dict, List, Callable, Any

logger = logging.getLogger(__name__)


class MemoryTier(IntEnum):
    """記憶體層級，數值越小速度越快"""
    VRAM = 0
    RAM = 1
    EXTERNAL = 2


@dataclass
class Block:
    """記憶體區塊元資料"""
    block_id: str
    size_bytes: int
    tier: MemoryTier
    data_tag: str = ""          # 語意標記 (e.g., "layer_32_weight")
    access_count: int = 0
    last_access_ts: float = 0.0
    is_dirty: bool = False      # 是否有未同步的修改
    is_pinned: bool = False     # 是否固定不可遷移

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


@dataclass
class TierState:
    """單一層級的狀態"""
    tier: MemoryTier
    capacity_bytes: int
    used_bytes: int = 0
    bandwidth_mbs: float = 0.0
    latency_us: float = 0.0
    block_count: int = 0

    @property
    def free_bytes(self) -> int:
        return self.capacity_bytes - self.used_bytes

    @property
    def utilization_pct(self) -> float:
        if self.capacity_bytes == 0:
            return 0.0
        return (self.used_bytes / self.capacity_bytes) * 100


class MemoryPool:
    """
    三層記憶體池管理器。

    負責：
    1. 在三個層級間分配記憶體區塊
    2. 根據存取模式進行 promotion/demotion
    3. 提供統一的讀寫介面（隱藏底層位置）
    4. 追蹤容量使用與效能指標
    """

    def __init__(
        self,
        vram_capacity_bytes: int,
        ram_capacity_bytes: int,
        external_capacity_bytes: int,
        vram_bandwidth_mbs: float = 336_000.0,
        ram_bandwidth_mbs: float = 44_800.0,
        external_bandwidth_mbs: float = 3940.0,
        vram_latency_us: float = 0.01,
        ram_latency_us: float = 0.1,
        external_latency_us: float = 10.0,
        promotion_threshold: int = 5,     # 存取 N 次後升級
        demotion_idle_s: float = 30.0,    # 閒置 N 秒後降級
    ):
        self._lock = threading.Lock()
        self._blocks: Dict[str, Block] = {}

        # LRU 追蹤（每層各一個）
        self._lru: Dict[MemoryTier, OrderedDict] = {
            MemoryTier.VRAM: OrderedDict(),
            MemoryTier.RAM: OrderedDict(),
            MemoryTier.EXTERNAL: OrderedDict(),
        }

        self._tiers: Dict[MemoryTier, TierState] = {
            MemoryTier.VRAM: TierState(
                tier=MemoryTier.VRAM,
                capacity_bytes=vram_capacity_bytes,
                bandwidth_mbs=vram_bandwidth_mbs,
                latency_us=vram_latency_us,
            ),
            MemoryTier.RAM: TierState(
                tier=MemoryTier.RAM,
                capacity_bytes=ram_capacity_bytes,
                bandwidth_mbs=ram_bandwidth_mbs,
                latency_us=ram_latency_us,
            ),
            MemoryTier.EXTERNAL: TierState(
                tier=MemoryTier.EXTERNAL,
                capacity_bytes=external_capacity_bytes,
                bandwidth_mbs=external_bandwidth_mbs,
                latency_us=external_latency_us,
            ),
        }

        self._promote_threshold = promotion_threshold
        self._demote_idle_s = demotion_idle_s

        # 實際資料搬移回調（由 TransferEngine 提供）
        self._on_migrate: Optional[Callable[[str, MemoryTier, MemoryTier], bool]] = None

        # 統計
        self._total_promotions = 0
        self._total_demotions = 0
        self._total_allocs = 0
        self._total_frees = 0

        logger.info(
            "MemoryPool created: VRAM=%.1fGB RAM=%.1fGB External=%.1fGB",
            vram_capacity_bytes / (1024**3),
            ram_capacity_bytes / (1024**3),
            external_capacity_bytes / (1024**3),
        )

    def register_migrate_callback(self, fn: Callable[[str, MemoryTier, MemoryTier], bool]) -> None:
        """註冊資料遷移回調。fn(block_id, from_tier, to_tier) -> success"""
        self._on_migrate = fn

    # ── Allocation ──

    def allocate(self, block_id: str, size_bytes: int, tag: str = "",
                 preferred_tier: Optional[MemoryTier] = None) -> MemoryTier:
        """
        分配一個記憶體區塊。

        優先嘗試 preferred_tier，若無空間則往下尋找。
        Returns: 實際分配的層級
        """
        with self._lock:
            if block_id in self._blocks:
                raise ValueError(f"Block '{block_id}' already exists")

            tier_order = [MemoryTier.VRAM, MemoryTier.RAM, MemoryTier.EXTERNAL]
            if preferred_tier is not None:
                tier_order = [preferred_tier] + [
                    t for t in tier_order if t != preferred_tier
                ]

            for tier in tier_order:
                state = self._tiers[tier]
                if state.free_bytes >= size_bytes:
                    return self._do_allocate(block_id, size_bytes, tier, tag)

            # 嘗試 evict 最冷的區塊來騰出空間
            for tier in tier_order:
                if self._try_evict(tier, size_bytes):
                    return self._do_allocate(block_id, size_bytes, tier, tag)

            raise MemoryError(
                f"Cannot allocate {size_bytes/(1024**2):.1f}MB for '{block_id}': "
                f"all tiers full"
            )

    def free(self, block_id: str) -> None:
        """釋放指定區塊"""
        with self._lock:
            blk = self._blocks.pop(block_id, None)
            if blk is None:
                return
            self._tiers[blk.tier].used_bytes -= blk.size_bytes
            self._tiers[blk.tier].block_count -= 1
            self._lru[blk.tier].pop(block_id, None)
            self._total_frees += 1

    def access(self, block_id: str) -> MemoryTier:
        """
        記錄一次存取。更新 LRU 順序並檢查是否需要 promotion。
        Returns: 區塊當前所在的層級
        """
        with self._lock:
            blk = self._blocks.get(block_id)
            if blk is None:
                raise KeyError(f"Block '{block_id}' not found")

            blk.access_count += 1
            blk.last_access_ts = time.time()

            # 更新 LRU（移到最近存取的位置）
            lru = self._lru[blk.tier]
            lru.move_to_end(block_id)

            # 檢查是否需要升級
            if (blk.tier > MemoryTier.VRAM
                    and blk.access_count >= self._promote_threshold
                    and not blk.is_pinned):
                target = MemoryTier(blk.tier - 1)
                self._try_promote(block_id, target)

            return blk.tier

    # ── Migration ──

    def promote(self, block_id: str, target_tier: MemoryTier) -> bool:
        """手動將區塊升級到更快的層級"""
        with self._lock:
            return self._try_promote(block_id, target_tier)

    def demote(self, block_id: str, target_tier: MemoryTier) -> bool:
        """手動將區塊降級到更慢的層級"""
        with self._lock:
            return self._try_demote(block_id, target_tier)

    def run_demotion_sweep(self) -> int:
        """
        掃描所有層級，將閒置過久的區塊降級。
        Returns: 降級的區塊數量
        """
        now = time.time()
        demoted = 0
        with self._lock:
            for tier in [MemoryTier.VRAM, MemoryTier.RAM]:
                target = MemoryTier(tier + 1)
                for bid in list(self._lru[tier].keys()):
                    blk = self._blocks.get(bid)
                    if blk and not blk.is_pinned:
                        idle = now - blk.last_access_ts
                        if idle > self._demote_idle_s:
                            if self._try_demote(bid, target):
                                demoted += 1
        return demoted

    # ── Query ──

    def get_block(self, block_id: str) -> Optional[Block]:
        return self._blocks.get(block_id)

    def get_tier_state(self, tier: MemoryTier) -> TierState:
        return self._tiers[tier]

    def get_all_tier_states(self) -> Dict[MemoryTier, TierState]:
        return dict(self._tiers)

    def get_blocks_in_tier(self, tier: MemoryTier) -> List[Block]:
        return [b for b in self._blocks.values() if b.tier == tier]

    @property
    def total_capacity_bytes(self) -> int:
        return sum(t.capacity_bytes for t in self._tiers.values())

    @property
    def total_used_bytes(self) -> int:
        return sum(t.used_bytes for t in self._tiers.values())

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_capacity_gb": self.total_capacity_bytes / (1024**3),
            "total_used_gb": self.total_used_bytes / (1024**3),
            "total_blocks": len(self._blocks),
            "total_allocs": self._total_allocs,
            "total_frees": self._total_frees,
            "total_promotions": self._total_promotions,
            "total_demotions": self._total_demotions,
            "tiers": {
                t.name: {
                    "capacity_gb": s.capacity_bytes / (1024**3),
                    "used_gb": s.used_bytes / (1024**3),
                    "utilization_pct": s.utilization_pct,
                    "blocks": s.block_count,
                    "bandwidth_mbs": s.bandwidth_mbs,
                    "latency_us": s.latency_us,
                }
                for t, s in self._tiers.items()
            },
        }

    # ── Internal ──

    def _do_allocate(self, block_id: str, size: int, tier: MemoryTier, tag: str) -> MemoryTier:
        blk = Block(
            block_id=block_id,
            size_bytes=size,
            tier=tier,
            data_tag=tag,
            last_access_ts=time.time(),
        )
        self._blocks[block_id] = blk
        self._tiers[tier].used_bytes += size
        self._tiers[tier].block_count += 1
        self._lru[tier][block_id] = True
        self._total_allocs += 1
        logger.debug("Allocated '%s' (%dMB) in %s", block_id, size // (1024**2), tier.name)
        return tier

    def _try_promote(self, block_id: str, target: MemoryTier) -> bool:
        blk = self._blocks.get(block_id)
        if blk is None or blk.tier <= target:
            return False

        target_state = self._tiers[target]
        if target_state.free_bytes < blk.size_bytes:
            # 嘗試 evict 目標層最冷的區塊
            if not self._try_evict(target, blk.size_bytes):
                return False

        if self._on_migrate and not self._on_migrate(block_id, blk.tier, target):
            return False

        # 執行遷移
        self._tiers[blk.tier].used_bytes -= blk.size_bytes
        self._tiers[blk.tier].block_count -= 1
        self._lru[blk.tier].pop(block_id, None)

        blk.tier = target
        self._tiers[target].used_bytes += blk.size_bytes
        self._tiers[target].block_count += 1
        self._lru[target][block_id] = True

        self._total_promotions += 1
        logger.debug("Promoted '%s' → %s", block_id, target.name)
        return True

    def _try_demote(self, block_id: str, target: MemoryTier) -> bool:
        blk = self._blocks.get(block_id)
        if blk is None or blk.tier >= target or blk.is_pinned:
            return False

        target_state = self._tiers[target]
        if target_state.free_bytes < blk.size_bytes:
            return False

        if self._on_migrate and not self._on_migrate(block_id, blk.tier, target):
            return False

        self._tiers[blk.tier].used_bytes -= blk.size_bytes
        self._tiers[blk.tier].block_count -= 1
        self._lru[blk.tier].pop(block_id, None)

        blk.tier = target
        self._tiers[target].used_bytes += blk.size_bytes
        self._tiers[target].block_count += 1
        self._lru[target][block_id] = True

        self._total_demotions += 1
        logger.debug("Demoted '%s' → %s", block_id, target.name)
        return True

    def _try_evict(self, tier: MemoryTier, needed_bytes: int) -> bool:
        """嘗試從指定層級 evict 區塊以騰出空間"""
        state = self._tiers[tier]
        freed = 0
        next_tier = MemoryTier(tier + 1) if tier < MemoryTier.EXTERNAL else None

        if next_tier is None:
            return False  # 最低層無法再往下 evict

        # 從 LRU 最舊的開始 evict
        for bid in list(self._lru[tier].keys()):
            if freed >= needed_bytes:
                break
            blk = self._blocks.get(bid)
            if blk and not blk.is_pinned:
                if self._try_demote(bid, next_tier):
                    freed += blk.size_bytes

        return state.free_bytes >= needed_bytes
