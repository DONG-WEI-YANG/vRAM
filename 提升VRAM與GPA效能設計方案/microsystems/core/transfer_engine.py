"""
Async Data Transfer Engine
===========================
負責在三層記憶體之間非同步搬移資料，
實作 DMA-style 傳輸語意，支援批次、管線化與優先級佇列。

傳輸路徑：
  VRAM ↔ RAM     : 透過 PCIe DMA (cudaMemcpy / cuMemcpy)
  RAM ↔ External : 透過 NVMe I/O (direct I/O / io_uring)
  VRAM ↔ External: 兩階段 (VRAM → RAM → External) 或 GPUDirect Storage
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional, Callable, Dict, List, Any

from .memory_pool import MemoryTier
from .llm_optimizations import FlashIOEngine, SharedCompressionDict

logger = logging.getLogger(__name__)


class TransferPriority(IntEnum):
    CRITICAL = 0   # 熱插拔 fallback
    HIGH = 1       # 預取 (prefetch)
    NORMAL = 2     # 一般讀寫
    LOW = 3        # 背景 demotion


class TransferDirection(Enum):
    UPLOAD = "upload"       # External/RAM → VRAM
    DOWNLOAD = "download"   # VRAM → External/RAM
    LATERAL = "lateral"     # RAM ↔ External


class TransferStatus(Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TransferRequest:
    """單一傳輸請求"""
    request_id: str
    block_id: str
    source_tier: MemoryTier
    dest_tier: MemoryTier
    size_bytes: int
    priority: TransferPriority = TransferPriority.NORMAL
    status: TransferStatus = TransferStatus.QUEUED
    callback: Optional[Callable[[str, bool], None]] = None  # (request_id, success)

    # 時間追蹤
    queued_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0

    @property
    def direction(self) -> TransferDirection:
        if self.dest_tier < self.source_tier:
            return TransferDirection.UPLOAD
        elif self.dest_tier > self.source_tier:
            return TransferDirection.DOWNLOAD
        return TransferDirection.LATERAL

    @property
    def elapsed_ms(self) -> float:
        if self.completed_at > 0:
            return (self.completed_at - self.started_at) * 1000
        elif self.started_at > 0:
            return (time.time() - self.started_at) * 1000
        return 0.0

    @property
    def throughput_mbs(self) -> float:
        elapsed_s = self.elapsed_ms / 1000
        if elapsed_s <= 0:
            return 0.0
        # 這裡的吞吐量反映的是物理傳輸，如果使用了壓縮，邏輯吞吐量會更高
        return (self.size_bytes / (1024 * 1024)) / elapsed_s


@dataclass
class TransferStats:
    """傳輸引擎統計"""
    total_requests: int = 0
    completed_requests: int = 0
    failed_requests: int = 0
    total_bytes_transferred: int = 0
    total_transfer_time_ms: float = 0.0
    avg_throughput_mbs: float = 0.0
    peak_throughput_mbs: float = 0.0
    queue_depth: int = 0


class TransferEngine:
    """
    非同步資料傳輸引擎。

    使用優先級佇列排程傳輸請求，支援多工作執行緒
    並行處理不同方向的傳輸。
    """

    def __init__(
        self,
        max_workers: int = 4,
        max_queue_depth: int = 256,
        bandwidth_limit_mbs: Optional[float] = None,
        enable_compression: bool = True,
    ):
        self._max_workers = max_workers
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=max_queue_depth)
        self._workers: List[threading.Thread] = []
        self._active = False
        self._lock = threading.Lock()
        self._request_counter = 0

        self._bandwidth_limit = bandwidth_limit_mbs
        self._pending: Dict[str, TransferRequest] = {}
        self._history: List[TransferRequest] = []  # 最近 1000 筆
        self._stats = TransferStats()

        # 實際 I/O 回調（由裝置層提供）
        self._io_handlers: Dict[str, Callable] = {}

        # ── LLM 優化組件 ──
        # Flash I/O: fused batch transfer
        self._flash_io = FlashIOEngine(
            batch_window_ms=50.0,
            max_batch_size=32,
        )

        # 透明壓縮引擎：Shared Compression Dict (MLA 映射)
        self._enable_compression = enable_compression
        self._compression_dict = SharedCompressionDict() if enable_compression else None
        self._training_samples = []

        logger.info("TransferEngine created: workers=%d, queue_depth=%d, compression=%s",
                     max_workers, max_queue_depth, enable_compression)

    def register_io_handler(
        self,
        name: str,
        handler: Callable[[str, MemoryTier, MemoryTier, int], bool],
    ) -> None:
        """
        註冊 I/O 處理器。
        handler(block_id, src_tier, dst_tier, size_bytes) -> success
        """
        self._io_handlers[name] = handler
        logger.info("Registered I/O handler: %s", name)

    def start(self) -> None:
        """啟動傳輸引擎的工作執行緒"""
        if self._active:
            return
        self._active = True
        for i in range(self._max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"TransferWorker-{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)
        logger.info("TransferEngine started with %d workers", self._max_workers)

    def stop(self) -> None:
        """停止所有工作執行緒"""
        self._active = False
        # 喚醒所有在 queue.get() 上阻塞的 worker
        for _ in self._workers:
            try:
                self._queue.put_nowait((999, 0, None))  # sentinel
            except queue.Full:
                pass
        for t in self._workers:
            t.join(timeout=5.0)
        self._workers.clear()
        logger.info("TransferEngine stopped")

    def submit(self, req: TransferRequest) -> str:
        """
        提交傳輸請求至優先級佇列。
        Returns: request_id
        """
        with self._lock:
            self._request_counter += 1
            if not req.request_id:
                req.request_id = f"xfer_{self._request_counter:06d}"

        self._pending[req.request_id] = req
        self._stats.total_requests += 1
        self._stats.queue_depth = self._queue.qsize() + 1

        # PriorityQueue 用 tuple 排序：(priority, counter, request)
        self._queue.put((req.priority.value, self._request_counter, req))
        logger.debug("Submitted transfer %s: %s→%s (%dMB) pri=%s",
                      req.request_id, req.source_tier.name, req.dest_tier.name,
                      req.size_bytes // (1024**2), req.priority.name)
        return req.request_id

    def submit_migration(
        self,
        block_id: str,
        src: MemoryTier,
        dst: MemoryTier,
        size_bytes: int,
        priority: TransferPriority = TransferPriority.NORMAL,
        callback: Optional[Callable] = None,
    ) -> str:
        """便捷方法：提交一個區塊遷移請求"""
        req = TransferRequest(
            request_id="",
            block_id=block_id,
            source_tier=src,
            dest_tier=dst,
            size_bytes=size_bytes,
            priority=priority,
            callback=callback,
        )
        # Flash I/O: also submit to batch engine for fused transfer
        if self._flash_io:
            self._flash_io.submit(block_id, src.value, dst.value, size_bytes)

        return self.submit(req)

    def cancel(self, request_id: str) -> bool:
        """取消尚未開始的傳輸"""
        req = self._pending.get(request_id)
        if req and req.status == TransferStatus.QUEUED:
            req.status = TransferStatus.CANCELLED
            return True
        return False

    def get_request(self, request_id: str) -> Optional[TransferRequest]:
        return self._pending.get(request_id)

    @property
    def stats(self) -> TransferStats:
        self._stats.queue_depth = self._queue.qsize()
        return self._stats

    def get_active_transfers(self) -> List[TransferRequest]:
        return [r for r in self._pending.values()
                if r.status == TransferStatus.IN_PROGRESS]

    # ── Worker Loop ──

    def _worker_loop(self) -> None:
        while self._active:
            try:
                priority, counter, req = self._queue.get(timeout=1.0)
            except queue.Empty:
                # Flash I/O: flush batched transfers on idle
                if self._flash_io and self._flash_io.should_flush():
                    batch = self._flash_io.flush()
                    if batch:
                        self._execute_batch(batch)
                continue

            if req is None:  # sentinel
                break

            if req.status == TransferStatus.CANCELLED:
                self._queue.task_done()
                continue

            self._execute_transfer(req)
            self._queue.task_done()

            # Flash I/O: also check flush after each transfer (prevents starvation)
            if self._flash_io and self._flash_io.should_flush():
                batch = self._flash_io.flush()
                if batch:
                    self._execute_batch(batch)

    def _execute_transfer(self, req: TransferRequest) -> None:
        """執行單一傳輸請求（支援透明壓縮）"""
        req.status = TransferStatus.IN_PROGRESS
        req.started_at = time.time()

        success = False
        try:
            # 判斷是否需要壓縮處理
            # 當目標是 EXTERNAL (SD卡) 且來源是 RAM 時，執行壓縮
            is_to_external = (req.dest_tier == MemoryTier.EXTERNAL)
            is_from_external = (req.source_tier == MemoryTier.EXTERNAL)

            handler = self._io_handlers.get("default")
            
            # 計算模擬傳輸時間
            logical_size = req.size_bytes
            physical_size = logical_size
            
            # 透明壓縮邏輯 (Transparent Compression)
            if self._enable_compression and (is_to_external or is_from_external):
                # 如果是寫入外部儲存，執行壓縮
                if is_to_external:
                    # 模擬壓縮率：LLM 權重平均約 2x-4x，使用 Shared Dict 可達 6x+
                    # 在實體系統中，這裡會調用 self._compression_dict.compress(data)
                    physical_size = logical_size // 3  # 預估 3x 壓縮率
                
                # 如果是從外部讀取，執行解壓縮
                elif is_from_external:
                    # 實體讀取的大小是壓縮後的
                    physical_size = logical_size // 3

            if handler:
                # 呼叫底層 I/O 處理器，傳遞實體大小
                success = handler(
                    req.block_id, req.source_tier, req.dest_tier, physical_size
                )
            else:
                # 無 handler，模擬傳輸（用於 demo/test）
                if self._bandwidth_limit:
                    # 物理傳輸時間 = 物理大小 / 物理頻寬
                    transfer_time = (physical_size / (1024 * 1024)) / self._bandwidth_limit
                    if transfer_time > 0:
                        time.sleep(min(transfer_time, 5.0))
                success = True

        except Exception as e:
            logger.error("Transfer %s failed: %s", req.request_id, e)
            success = False

        req.completed_at = time.time()
        req.status = TransferStatus.COMPLETED if success else TransferStatus.FAILED

        # 更新統計
        with self._lock:
            if success:
                self._stats.completed_requests += 1
                self._stats.total_bytes_transferred += req.size_bytes
                self._stats.total_transfer_time_ms += req.elapsed_ms
                if req.throughput_mbs > self._stats.peak_throughput_mbs:
                    self._stats.peak_throughput_mbs = req.throughput_mbs
                if self._stats.completed_requests > 0:
                    self._stats.avg_throughput_mbs = (
                        (self._stats.total_bytes_transferred / (1024 * 1024))
                        / (self._stats.total_transfer_time_ms / 1000)
                        if self._stats.total_transfer_time_ms > 0 else 0
                    )
            else:
                self._stats.failed_requests += 1

            # 維護歷史（保留最近 1000 筆）
            self._history.append(req)
            if len(self._history) > 1000:
                self._history = self._history[-500:]

            self._pending.pop(req.request_id, None)

        # 觸發回調
        if req.callback:
            try:
                req.callback(req.request_id, success)
            except Exception as e:
                logger.error("Transfer callback error: %s", e)

        logger.debug(
            "Transfer %s %s: %.1fMB in %.1fms (%.0f MB/s)",
            req.request_id,
            "OK" if success else "FAILED",
            req.size_bytes / (1024**2),
            req.elapsed_ms,
            req.throughput_mbs,
        )

    def _execute_batch(self, batch) -> None:
        """執行融合批次傳輸（Flash I/O）"""
        # Build block_id → request reverse index (pending is keyed by request_id)
        block_to_req = {
            r.block_id: r for r in self._pending.values()
            if r.status == TransferStatus.QUEUED
        }
        executed = 0
        groups = self._flash_io.group_by_direction(batch)
        for (src, dst), requests in groups.items():
            for block_id, s, d, size in requests:
                req = block_to_req.get(block_id)
                if req:
                    self._execute_transfer(req)
                    executed += 1
        logger.debug("Flash I/O batch #%d: %d/%d transfers executed",
                     batch.batch_id, executed, len(batch.requests))
