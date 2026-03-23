"""
Circuit Breaker for External Storage Devices
==============================================
借鑑自 OSS120BCLI recovery.py 的 Circuit Breaker 模式。

當外部儲存裝置連續失敗 N 次時，自動「斷路」停止存取，
避免持續向故障裝置寫入導致資料損失或系統崩潰。

狀態機：
  CLOSED (正常) → 連續失敗 N 次 → OPEN (斷路)
  OPEN (斷路) → cooldown 過後 → HALF_OPEN (試探)
  HALF_OPEN → 成功 → CLOSED (恢復)
  HALF_OPEN → 失敗 → OPEN (繼續斷路)

每種操作類型有獨立的 breaker：
  - read_breaker:  讀取失敗（裝置斷線、I/O 錯誤）
  - write_breaker: 寫入失敗（裝置滿、過熱保護）
  - health_breaker: 健康檢查失敗（SMART 錯誤）
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Dict, Any

logger = logging.getLogger(__name__)


class BreakerState(Enum):
    CLOSED = "closed"         # 正常運作
    OPEN = "open"             # 斷路（拒絕所有操作）
    HALF_OPEN = "half_open"   # 試探（允許一次操作測試恢復）


@dataclass
class BreakerConfig:
    """斷路器配置"""
    failure_threshold: int = 3        # 連續失敗 N 次觸發斷路
    cooldown_seconds: float = 60.0    # 斷路後冷卻時間
    half_open_max_attempts: int = 1   # 半開狀態最多試探次數
    success_threshold: int = 2        # 半開狀態連續成功 N 次恢復


@dataclass
class BreakerStats:
    """斷路器統計"""
    total_calls: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_rejected: int = 0       # 被斷路器拒絕的呼叫次數
    total_trips: int = 0          # 觸發斷路的次數
    total_recoveries: int = 0     # 恢復次數
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    last_trip_time: float = 0.0
    current_state: str = "closed"


class CircuitBreaker:
    """
    單一操作類型的斷路器。

    用法：
        breaker = CircuitBreaker("sd_write", BreakerConfig(failure_threshold=3))

        if breaker.allow():
            try:
                result = device.write_block(...)
                breaker.record_success()
            except IOError:
                breaker.record_failure()
                # 觸發 fallback 策略
        else:
            # 斷路器已開啟，使用 fallback
            fallback_to_ram()
    """

    def __init__(self, name: str, config: Optional[BreakerConfig] = None):
        self._name = name
        self._config = config or BreakerConfig()
        self._lock = threading.Lock()

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._half_open_attempts = 0
        self._last_failure_time = 0.0
        self._last_trip_time = 0.0

        self._stats = BreakerStats()

        # 狀態變更回調
        self._on_trip: Optional[Callable[[str], None]] = None      # 斷路觸發
        self._on_recover: Optional[Callable[[str], None]] = None   # 恢復正常
        self._on_reject: Optional[Callable[[str], None]] = None    # 操作被拒絕

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._check_cooldown()
            return self._state

    @property
    def stats(self) -> BreakerStats:
        self._stats.current_state = self._state.value
        return self._stats

    def on_trip(self, callback: Callable[[str], None]) -> None:
        self._on_trip = callback

    def on_recover(self, callback: Callable[[str], None]) -> None:
        self._on_recover = callback

    def on_reject(self, callback: Callable[[str], None]) -> None:
        self._on_reject = callback

    def allow(self) -> bool:
        """
        檢查是否允許執行操作。

        Returns:
            True = 允許執行
            False = 斷路器已開啟，應使用 fallback
        """
        with self._lock:
            self._check_cooldown()

            if self._state == BreakerState.CLOSED:
                self._stats.total_calls += 1
                return True

            elif self._state == BreakerState.HALF_OPEN:
                if self._half_open_attempts < self._config.half_open_max_attempts:
                    self._half_open_attempts += 1
                    self._stats.total_calls += 1
                    logger.info("[%s] Half-open: allowing probe attempt %d/%d",
                                self._name, self._half_open_attempts,
                                self._config.half_open_max_attempts)
                    return True
                else:
                    self._stats.total_rejected += 1
                    return False

            else:  # OPEN
                self._stats.total_rejected += 1
                if self._on_reject:
                    self._on_reject(self._name)
                return False

    def record_success(self) -> None:
        """記錄操作成功"""
        with self._lock:
            self._stats.total_successes += 1
            self._stats.last_success_time = time.time()
            self._consecutive_failures = 0
            self._consecutive_successes += 1

            if self._state == BreakerState.HALF_OPEN:
                if self._consecutive_successes >= self._config.success_threshold:
                    self._transition(BreakerState.CLOSED)
                    logger.info("[%s] Circuit RECOVERED after %d consecutive successes",
                                self._name, self._consecutive_successes)
            elif self._state == BreakerState.CLOSED:
                pass  # 正常

    def record_failure(self, error: Optional[str] = None) -> None:
        """記錄操作失敗"""
        with self._lock:
            self._stats.total_failures += 1
            self._stats.last_failure_time = time.time()
            self._consecutive_failures += 1
            self._consecutive_successes = 0

            logger.warning("[%s] Failure #%d: %s",
                           self._name, self._consecutive_failures,
                           error or "unknown")

            if self._state == BreakerState.HALF_OPEN:
                # 半開狀態下失敗 → 重新斷路
                self._transition(BreakerState.OPEN)
                logger.warning("[%s] Half-open probe FAILED, re-opening circuit", self._name)

            elif self._state == BreakerState.CLOSED:
                if self._consecutive_failures >= self._config.failure_threshold:
                    self._transition(BreakerState.OPEN)
                    logger.critical(
                        "[%s] CIRCUIT TRIPPED after %d consecutive failures! "
                        "Cooldown: %.0fs",
                        self._name, self._consecutive_failures,
                        self._config.cooldown_seconds,
                    )

    def reset(self) -> None:
        """手動重置斷路器"""
        with self._lock:
            self._transition(BreakerState.CLOSED)
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            logger.info("[%s] Circuit manually reset", self._name)

    # ── Internal ──

    def _check_cooldown(self) -> None:
        """檢查是否已過冷卻期，可以轉為半開"""
        if self._state == BreakerState.OPEN:
            elapsed = time.time() - self._last_trip_time
            if elapsed >= self._config.cooldown_seconds:
                self._state = BreakerState.HALF_OPEN
                self._half_open_attempts = 0
                self._consecutive_successes = 0
                logger.info("[%s] Cooldown expired (%.0fs), entering HALF_OPEN",
                            self._name, elapsed)

    def _transition(self, new_state: BreakerState) -> None:
        old_state = self._state
        self._state = new_state

        if new_state == BreakerState.OPEN:
            self._last_trip_time = time.time()
            self._stats.total_trips += 1
            self._stats.last_trip_time = self._last_trip_time
            if self._on_trip:
                self._on_trip(self._name)

        elif new_state == BreakerState.CLOSED and old_state != BreakerState.CLOSED:
            self._stats.total_recoveries += 1
            self._consecutive_failures = 0
            if self._on_recover:
                self._on_recover(self._name)


class DeviceCircuitBreakers:
    """
    管理一個裝置的所有斷路器。

    每個裝置有三個獨立的 breaker：
      - read:   讀取操作
      - write:  寫入操作
      - health: 健康檢查
    """

    def __init__(
        self,
        device_id: str,
        read_config: Optional[BreakerConfig] = None,
        write_config: Optional[BreakerConfig] = None,
        health_config: Optional[BreakerConfig] = None,
    ):
        self.device_id = device_id
        self.read = CircuitBreaker(
            f"{device_id}_read",
            read_config or BreakerConfig(failure_threshold=5, cooldown_seconds=30),
        )
        self.write = CircuitBreaker(
            f"{device_id}_write",
            write_config or BreakerConfig(failure_threshold=3, cooldown_seconds=60),
        )
        self.health = CircuitBreaker(
            f"{device_id}_health",
            health_config or BreakerConfig(failure_threshold=3, cooldown_seconds=120),
        )

        # 統一的 fallback 回調
        self._fallback_handler: Optional[Callable[[str, str], None]] = None

    def on_any_trip(self, handler: Callable[[str, str], None]) -> None:
        """
        註冊任一 breaker 觸發時的回調。
        handler(device_id, breaker_name)
        """
        self._fallback_handler = handler
        for breaker in [self.read, self.write, self.health]:
            breaker.on_trip(lambda name: handler(self.device_id, name))

    def all_healthy(self) -> bool:
        """所有 breaker 都在 CLOSED 狀態"""
        return all(
            b.state == BreakerState.CLOSED
            for b in [self.read, self.write, self.health]
        )

    def any_open(self) -> bool:
        """任一 breaker 在 OPEN 狀態"""
        return any(
            b.state == BreakerState.OPEN
            for b in [self.read, self.write, self.health]
        )

    def reset_all(self) -> None:
        """重置所有 breaker"""
        self.read.reset()
        self.write.reset()
        self.health.reset()

    def get_summary(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "read": {"state": self.read.state.value, **vars(self.read.stats)},
            "write": {"state": self.write.state.value, **vars(self.write.stats)},
            "health": {"state": self.health.state.value, **vars(self.health.stats)},
            "all_healthy": self.all_healthy(),
        }
