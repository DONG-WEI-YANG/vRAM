"""
Error Recovery Chain for VRAM Operations
==========================================
借鑑自 OSS120BCLI recovery.py 的 Fallback Chain 模式。

當記憶體操作失敗時，依序嘗試一系列恢復策略：
  1. Retry      — 重試原始操作（可能是暫時性錯誤）
  2. Downgrade  — 降級到更慢但更可靠的層級
  3. Compress   — 壓縮資料後重試
  4. Evict      — 驅逐冷資料騰出空間
  5. Emergency  — 緊急模式（僅用 VRAM + RAM，放棄外部裝置）

每個策略有成功率追蹤，未來可用於自適應選擇。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, List, Dict, Any, Tuple

from .memory_pool import MemoryTier

logger = logging.getLogger(__name__)


class RecoveryStrategy(Enum):
    RETRY = "retry"
    DOWNGRADE_TIER = "downgrade_tier"
    COMPRESS_DATA = "compress_data"
    EVICT_COLD = "evict_cold"
    EMERGENCY_RAM_ONLY = "emergency_ram_only"


class ErrorCategory(Enum):
    """錯誤分類（決定恢復策略的選擇）"""
    DEVICE_DISCONNECTED = "device_disconnected"   # 裝置斷線
    DEVICE_FULL = "device_full"                   # 裝置空間不足
    DEVICE_OVERHEAT = "device_overheat"           # 裝置過熱
    IO_ERROR = "io_error"                         # I/O 錯誤
    TIMEOUT = "timeout"                           # 操作逾時
    PERMISSION = "permission"                     # 權限不足
    OUT_OF_MEMORY = "out_of_memory"               # 所有層級都滿
    UNKNOWN = "unknown"


@dataclass
class RecoveryAttempt:
    """單次恢復嘗試的記錄"""
    strategy: RecoveryStrategy
    success: bool
    duration_ms: float
    error_category: ErrorCategory
    detail: str = ""


@dataclass
class RecoveryStats:
    """恢復統計"""
    total_recoveries: int = 0
    successful_recoveries: int = 0
    failed_recoveries: int = 0
    strategy_success_rates: Dict[str, Tuple[int, int]] = field(
        default_factory=lambda: {s.value: (0, 0) for s in RecoveryStrategy}
    )  # strategy → (successes, attempts)


# ── 各種錯誤類別對應的恢復策略鏈 ──

RECOVERY_CHAINS: Dict[ErrorCategory, List[RecoveryStrategy]] = {
    ErrorCategory.DEVICE_DISCONNECTED: [
        RecoveryStrategy.EMERGENCY_RAM_ONLY,
    ],
    ErrorCategory.DEVICE_FULL: [
        RecoveryStrategy.EVICT_COLD,
        RecoveryStrategy.COMPRESS_DATA,
        RecoveryStrategy.DOWNGRADE_TIER,
        RecoveryStrategy.EMERGENCY_RAM_ONLY,
    ],
    ErrorCategory.DEVICE_OVERHEAT: [
        RecoveryStrategy.DOWNGRADE_TIER,
        RecoveryStrategy.EMERGENCY_RAM_ONLY,
    ],
    ErrorCategory.IO_ERROR: [
        RecoveryStrategy.RETRY,
        RecoveryStrategy.DOWNGRADE_TIER,
        RecoveryStrategy.EMERGENCY_RAM_ONLY,
    ],
    ErrorCategory.TIMEOUT: [
        RecoveryStrategy.RETRY,
        RecoveryStrategy.DOWNGRADE_TIER,
    ],
    ErrorCategory.OUT_OF_MEMORY: [
        RecoveryStrategy.EVICT_COLD,
        RecoveryStrategy.COMPRESS_DATA,
        RecoveryStrategy.EMERGENCY_RAM_ONLY,
    ],
    ErrorCategory.UNKNOWN: [
        RecoveryStrategy.RETRY,
        RecoveryStrategy.DOWNGRADE_TIER,
        RecoveryStrategy.EMERGENCY_RAM_ONLY,
    ],
}


class RecoveryChain:
    """
    記憶體操作的錯誤恢復鏈。

    使用方式：
        chain = RecoveryChain()
        chain.register_handler(RecoveryStrategy.RETRY, my_retry_fn)
        chain.register_handler(RecoveryStrategy.DOWNGRADE_TIER, my_downgrade_fn)

        try:
            device.write_block(...)
        except IOError as e:
            result = chain.execute(ErrorCategory.IO_ERROR, context={...})
            if result.success:
                logger.info("Recovered via %s", result.strategy)
            else:
                raise  # 所有恢復策略都失敗
    """

    def __init__(self, max_retries: int = 2):
        self._max_retries = max_retries
        self._handlers: Dict[RecoveryStrategy, Callable[[Dict], bool]] = {}
        self._stats = RecoveryStats()
        self._history: List[RecoveryAttempt] = []

    def register_handler(
        self,
        strategy: RecoveryStrategy,
        handler: Callable[[Dict[str, Any]], bool],
    ) -> None:
        """
        註冊恢復策略的處理函式。
        handler(context) → success: bool
        context 包含：error, block_id, tier, size 等操作資訊。
        """
        self._handlers[strategy] = handler

    def execute(
        self,
        error_category: ErrorCategory,
        context: Optional[Dict[str, Any]] = None,
    ) -> RecoveryAttempt:
        """
        執行恢復鏈。依序嘗試每個策略直到成功。

        Returns: 最後一次嘗試的結果
        """
        ctx = context or {}
        chain = RECOVERY_CHAINS.get(error_category, RECOVERY_CHAINS[ErrorCategory.UNKNOWN])

        logger.warning("Recovery chain triggered: %s (context: %s)",
                        error_category.value,
                        {k: v for k, v in ctx.items() if k != "data"})

        self._stats.total_recoveries += 1
        last_attempt = RecoveryAttempt(
            strategy=RecoveryStrategy.RETRY,
            success=False,
            duration_ms=0,
            error_category=error_category,
        )

        for strategy in chain:
            handler = self._handlers.get(strategy)
            if handler is None:
                logger.debug("No handler for %s, skipping", strategy.value)
                continue

            start = time.perf_counter()
            try:
                # 如果是 RETRY 策略，嘗試多次
                attempts = self._max_retries if strategy == RecoveryStrategy.RETRY else 1

                success = False
                for attempt in range(attempts):
                    logger.info("Trying %s (attempt %d/%d)...",
                                strategy.value, attempt + 1, attempts)
                    try:
                        success = handler(ctx)
                        if success:
                            break
                    except Exception as e:
                        logger.warning("%s attempt %d failed: %s",
                                        strategy.value, attempt + 1, e)

                elapsed = (time.perf_counter() - start) * 1000

                last_attempt = RecoveryAttempt(
                    strategy=strategy,
                    success=success,
                    duration_ms=elapsed,
                    error_category=error_category,
                    detail=f"{'OK' if success else 'FAILED'}",
                )
                self._history.append(last_attempt)
                self._update_stats(strategy, success)

                if success:
                    self._stats.successful_recoveries += 1
                    logger.info("Recovery SUCCESS via %s (%.1fms)",
                                strategy.value, elapsed)
                    return last_attempt

                logger.warning("Recovery %s FAILED, trying next strategy...",
                                strategy.value)

            except Exception as e:
                elapsed = (time.perf_counter() - start) * 1000
                logger.error("Recovery handler %s crashed: %s", strategy.value, e)
                last_attempt = RecoveryAttempt(
                    strategy=strategy, success=False,
                    duration_ms=elapsed, error_category=error_category,
                    detail=str(e),
                )
                self._history.append(last_attempt)
                self._update_stats(strategy, False)

        self._stats.failed_recoveries += 1
        logger.error("ALL recovery strategies exhausted for %s!", error_category.value)
        return last_attempt

    def classify_error(self, exception: Exception) -> ErrorCategory:
        """自動分類例外類型"""
        err_str = str(exception).lower()
        exc_type = type(exception).__name__.lower()

        if "disconnect" in err_str or "no such device" in err_str:
            return ErrorCategory.DEVICE_DISCONNECTED
        if "no space" in err_str or "full" in err_str or "enospc" in err_str:
            return ErrorCategory.DEVICE_FULL
        if "temperature" in err_str or "thermal" in err_str or "overheat" in err_str:
            return ErrorCategory.DEVICE_OVERHEAT
        if "timeout" in exc_type or "timeout" in err_str:
            return ErrorCategory.TIMEOUT
        if "permission" in err_str or "eacces" in err_str:
            return ErrorCategory.PERMISSION
        if "memory" in err_str or "oom" in err_str or isinstance(exception, MemoryError):
            return ErrorCategory.OUT_OF_MEMORY
        if "ioerror" in exc_type or "oserror" in exc_type or "errno" in err_str:
            return ErrorCategory.IO_ERROR

        return ErrorCategory.UNKNOWN

    @property
    def stats(self) -> RecoveryStats:
        return self._stats

    def get_strategy_ranking(self) -> List[Tuple[str, float]]:
        """
        取得各策略的成功率排名。
        用於自適應策略選擇。
        """
        ranking = []
        for strategy, (successes, attempts) in self._stats.strategy_success_rates.items():
            rate = successes / attempts if attempts > 0 else 0
            ranking.append((strategy, rate))
        ranking.sort(key=lambda x: x[1], reverse=True)
        return ranking

    def _update_stats(self, strategy: RecoveryStrategy, success: bool) -> None:
        key = strategy.value
        succ, attempts = self._stats.strategy_success_rates.get(key, (0, 0))
        self._stats.strategy_success_rates[key] = (
            succ + (1 if success else 0),
            attempts + 1,
        )
