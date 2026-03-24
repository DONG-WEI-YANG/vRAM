"""
Striped Swap Scheduler — RAID-0 Style Parallel I/O
====================================================
將多個外接裝置的 mmap swap 組成 stripe group，
按速度加權分配 block，讀寫時平行調度以最大化頻寬。

效果：
  3 台裝置各 5/14/2 MB/s → 平行後等效 21 MB/s
  載入 256MB 模型層：51 秒 → 12 秒
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any

logger = logging.getLogger(__name__)


@dataclass
class DeviceSlot:
    """一個裝置在 stripe group 中的角色"""
    engine: Any                # MmapSwapEngine instance
    speed_mbs: float           # 等效速度
    weight: float = 0.0        # 分配權重 (0-1)
    allocated_blocks: int = 0  # 已分配的 block 數
    total_blocks: int = 0      # 裝置的總 block 數
    # Runtime protection
    throttled: bool = False    # 延遲過高時標記
    avg_latency_ms: float = 0.0  # 滑動平均延遲
    io_count: int = 0         # I/O 次數
    io_errors: int = 0        # I/O 錯誤次數


@dataclass
class StripedBlock:
    """跨裝置的邏輯 block 映射"""
    logical_id: int            # 全域邏輯 block ID
    device_idx: int            # 所在裝置的 index
    local_block_id: int        # 該裝置內的 block ID
    label: str = ""


class StripedSwapScheduler:
    """
    RAID-0 style swap scheduler across multiple devices.

    - allocate(): 按速度加權分配 block 到最佳裝置
    - write() / read(): 單一 block I/O（導向正確裝置）
    - write_parallel() / read_parallel(): 多 block 平行 I/O
    """

    def __init__(self):
        self._devices: List[DeviceSlot] = []
        self._block_map: Dict[int, StripedBlock] = {}  # logical_id → StripedBlock
        self._next_logical_id = 0
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None

    def add_device(self, engine: Any, speed_mbs: float) -> int:
        """加入一個裝置到 stripe group，回傳 device index。"""
        idx = len(self._devices)
        status = engine.status()
        total_blocks = status.get("total_blocks", 0)

        self._devices.append(DeviceSlot(
            engine=engine,
            speed_mbs=speed_mbs,
            total_blocks=total_blocks,
        ))
        self._recalc_weights()

        logger.info("Striped device %d added: %.1f MB/s, %d blocks",
                     idx, speed_mbs, total_blocks)
        return idx

    def _recalc_weights(self):
        """根據速度重算每個裝置的分配權重。"""
        total_speed = sum(d.speed_mbs for d in self._devices)
        if total_speed > 0:
            for d in self._devices:
                d.weight = d.speed_mbs / total_speed

    @property
    def device_count(self) -> int:
        return len(self._devices)

    @property
    def total_speed_mbs(self) -> float:
        return sum(d.speed_mbs for d in self._devices)

    @property
    def total_blocks(self) -> int:
        return sum(d.total_blocks for d in self._devices)

    @property
    def allocated_blocks(self) -> int:
        return len(self._block_map)

    def allocate(self, label: str = "") -> Optional[StripedBlock]:
        """
        分配一個 logical block，按速度加權選裝置。
        快裝置分到更多 block（負載平衡）。
        """
        with self._lock:
            # 選最「空閒」的裝置（已分配 / 權重 最小的）
            # 節流中的裝置降低優先級但不完全跳過（避免其他裝置也壓力過大）
            best = None
            best_ratio = float('inf')
            for i, dev in enumerate(self._devices):
                if dev.allocated_blocks >= dev.total_blocks:
                    continue  # 滿了
                if dev.weight <= 0:
                    continue
                ratio = dev.allocated_blocks / dev.weight
                if dev.throttled:
                    ratio *= 10  # 節流裝置排到最後
                if ratio < best_ratio:
                    best_ratio = ratio
                    best = i

            if best is None:
                return None  # 全部裝置都滿

            dev = self._devices[best]
            local_id = dev.allocated_blocks  # 從 0 開始遞增
            logical_id = self._next_logical_id
            self._next_logical_id += 1

            block = StripedBlock(
                logical_id=logical_id,
                device_idx=best,
                local_block_id=local_id,
                label=label,
            )
            self._block_map[logical_id] = block
            dev.allocated_blocks += 1

            return block

    # ── Runtime Protection Constants ──
    THROTTLE_LATENCY_MS = 5000    # 單次 I/O > 5 秒 → 標記節流
    UNTHROTTLE_LATENCY_MS = 1000  # 平均延遲降回 1 秒 → 解除節流
    LATENCY_ALPHA = 0.3           # 滑動平均權重（越大越敏感）

    def write(self, logical_id: int, data: bytes, offset: int = 0) -> bool:
        """寫入單一 block，追蹤延遲，自動節流保護。"""
        block = self._block_map.get(logical_id)
        if not block:
            return False
        dev = self._devices[block.device_idx]
        swap = getattr(dev.engine, '_swap', None)
        if not swap:
            return False

        t0 = time.perf_counter()
        ok = swap.write_block(block.local_block_id, data, offset)
        self._update_latency(dev, time.perf_counter() - t0, ok)
        return ok

    def read(self, logical_id: int, offset: int = 0,
             size: int = -1) -> Optional[bytes]:
        """讀取單一 block，追蹤延遲，自動節流保護。"""
        block = self._block_map.get(logical_id)
        if not block:
            return None
        dev = self._devices[block.device_idx]
        swap = getattr(dev.engine, '_swap', None)
        if not swap:
            return None

        t0 = time.perf_counter()
        result = swap.read_block(block.local_block_id, offset, size)
        self._update_latency(dev, time.perf_counter() - t0, result is not None)
        return result

    def _update_latency(self, dev: DeviceSlot, elapsed_s: float, success: bool):
        """更新裝置延遲統計，觸發/解除節流。"""
        latency_ms = elapsed_s * 1000
        dev.io_count += 1

        if not success:
            dev.io_errors += 1

        # 滑動平均（EMA）
        if dev.avg_latency_ms == 0:
            dev.avg_latency_ms = latency_ms
        else:
            dev.avg_latency_ms = (
                self.LATENCY_ALPHA * latency_ms +
                (1 - self.LATENCY_ALPHA) * dev.avg_latency_ms
            )

        # 節流觸發：單次延遲過高
        if latency_ms > self.THROTTLE_LATENCY_MS and not dev.throttled:
            dev.throttled = True
            logger.warning("Device #%d throttled: latency %.0fms > %dms",
                           self._devices.index(dev), latency_ms,
                           self.THROTTLE_LATENCY_MS)

        # 節流解除：平均延遲恢復
        if dev.throttled and dev.avg_latency_ms < self.UNTHROTTLE_LATENCY_MS:
            dev.throttled = False
            logger.info("Device #%d unthrottled: avg latency %.0fms",
                         self._devices.index(dev), dev.avg_latency_ms)

    def write_parallel(self, items: List[Tuple[int, bytes]]) -> List[bool]:
        """
        平行寫入多個 block。每個裝置的 I/O 同時進行。

        Args:
            items: [(logical_id, data), ...]
        Returns:
            [success, ...] 對應每個 item
        """
        if not items:
            return []
        pool = self._get_pool()
        futures = {}
        for i, (lid, data) in enumerate(items):
            futures[pool.submit(self.write, lid, data)] = i
        results = [False] * len(items)
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception:
                results[idx] = False
        return results

    def read_parallel(self, logical_ids: List[int],
                      size: int = -1) -> List[Optional[bytes]]:
        """
        平行讀取多個 block。每個裝置的 I/O 同時進行。

        Args:
            logical_ids: [logical_id, ...]
        Returns:
            [bytes_or_None, ...] 對應每個 id
        """
        if not logical_ids:
            return []
        pool = self._get_pool()
        futures = {}
        for i, lid in enumerate(logical_ids):
            futures[pool.submit(self.read, lid, 0, size)] = i
        results: List[Optional[bytes]] = [None] * len(logical_ids)
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception:
                results[idx] = None
        return results

    def _get_pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            # worker 數 = 裝置數，每裝置一個 I/O 執行緒
            self._pool = ThreadPoolExecutor(
                max_workers=max(len(self._devices), 2),
                thread_name_prefix="stripe-io",
            )
        return self._pool

    def stats(self) -> Dict[str, Any]:
        """回傳 stripe group 統計。"""
        devices = []
        for i, dev in enumerate(self._devices):
            devices.append({
                "index": i,
                "speed_mbs": dev.speed_mbs,
                "weight": round(dev.weight, 2),
                "allocated": dev.allocated_blocks,
                "total": dev.total_blocks,
                "throttled": dev.throttled,
                "avg_latency_ms": round(dev.avg_latency_ms, 1),
                "io_count": dev.io_count,
                "io_errors": dev.io_errors,
            })
        return {
            "device_count": len(self._devices),
            "total_speed_mbs": round(self.total_speed_mbs, 1),
            "total_blocks": self.total_blocks,
            "allocated_blocks": self.allocated_blocks,
            "devices": devices,
        }

    def close(self):
        """關閉 thread pool。"""
        if self._pool:
            self._pool.shutdown(wait=False)
            self._pool = None
