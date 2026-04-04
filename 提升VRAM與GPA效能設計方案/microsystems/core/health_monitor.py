"""
Device Health Monitor
======================
持續監控外部儲存裝置的健康狀態與效能指標，
提供告警機制以防止資料損失。

監控項目：
  - 溫度 (Temperature)
  - 剩餘壽命 (TBW / Wear Level)
  - 即時頻寬 (Throughput)
  - 延遲 (Latency)
  - 錯誤率 (Error Rate)
  - 連接狀態 (Connection Status)
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Callable, Any

logger = logging.getLogger(__name__)


class DeviceHealth(Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """健康告警"""
    timestamp: float
    level: AlertLevel
    device_id: str
    message: str
    metric_name: str = ""
    metric_value: float = 0.0
    threshold: float = 0.0


@dataclass
class DeviceMetrics:
    """裝置即時指標"""
    device_id: str
    device_type: str = ""         # "sd_express", "usb_nvme", "enclosure_nvme"
    timestamp: float = 0.0

    # 溫度
    temperature_celsius: float = 0.0

    # 壽命
    wear_level_pct: float = 100.0  # 剩餘壽命百分比
    total_bytes_written: int = 0
    tbw_limit: int = 0             # 廠商標示的 TBW 上限

    # 效能
    read_bandwidth_mbs: float = 0.0
    write_bandwidth_mbs: float = 0.0
    read_latency_us: float = 0.0
    write_latency_us: float = 0.0
    iops_read: int = 0
    iops_write: int = 0

    # 錯誤
    read_errors: int = 0
    write_errors: int = 0
    crc_errors: int = 0

    # 連線
    is_connected: bool = True
    connection_protocol: str = ""   # "pcie_gen4_x2", "usb4_v1", etc.
    uptime_seconds: float = 0.0

    @property
    def health(self) -> DeviceHealth:
        if not self.is_connected:
            return DeviceHealth.DISCONNECTED
        if self.temperature_celsius >= 85 or self.wear_level_pct <= 5:
            return DeviceHealth.CRITICAL
        if self.temperature_celsius >= 70 or self.wear_level_pct <= 10:
            return DeviceHealth.WARNING
        if self.read_errors + self.write_errors > 100:
            return DeviceHealth.WARNING
        return DeviceHealth.HEALTHY


class HealthMonitor:
    """
    裝置健康監控器。

    定期輪詢裝置狀態，發出告警並觸發回調。
    支援多裝置同時監控。
    """

    def __init__(
        self,
        check_interval_s: float = 5.0,
        temp_warning: float = 70.0,
        temp_critical: float = 85.0,
        wear_warning_pct: float = 10.0,
        wear_critical_pct: float = 5.0,
    ):
        self._interval = check_interval_s
        self._temp_warn = temp_warning
        self._temp_crit = temp_critical
        self._wear_warn = wear_warning_pct
        self._wear_crit = wear_critical_pct

        self._lock = threading.Lock()
        self._active = False
        self._thread: Optional[threading.Thread] = None

        self._devices: Dict[str, DeviceMetrics] = {}
        self._alerts: List[Alert] = []
        self._alert_callbacks: List[Callable[[Alert], None]] = []

        # 指標採集回調（由裝置層提供）
        self._collectors: Dict[str, Callable[[], DeviceMetrics]] = {}

        # 斷線回調（用於熱插拔防護）
        self._on_disconnect: Optional[Callable[[str], None]] = None
        self._on_reconnect: Optional[Callable[[str], None]] = None

        self._is_windows = platform.system().lower() == "windows"
        self._watcher = None

    def register_device(
        self,
        device_id: str,
        collector: Callable[[], DeviceMetrics],
    ) -> None:
        """
        註冊一個要監控的裝置。
        collector: 回調函式，呼叫時回傳該裝置的最新指標。
        """
        self._collectors[device_id] = collector
        self._devices[device_id] = DeviceMetrics(device_id=device_id)
        logger.info("Registered device for monitoring: %s", device_id)

    def unregister_device(self, device_id: str) -> None:
        self._collectors.pop(device_id, None)
        self._devices.pop(device_id, None)

    def on_alert(self, callback: Callable[[Alert], None]) -> None:
        """註冊告警回調"""
        self._alert_callbacks.append(callback)

    def on_disconnect(self, callback: Callable[[str], None]) -> None:
        """註冊斷線回調（熱插拔防護）"""
        self._on_disconnect = callback

    def on_reconnect(self, callback: Callable[[str], None]) -> None:
        self._on_reconnect = callback

    def attach_watcher(self, watcher) -> None:
        """
        Attach a DeviceWatcher for instant connection/disconnection detection.

        When attached, connection status is event-driven (< 1s).
        Temperature/wear/error polling continues at the configured interval.
        """
        from .device_watcher import DeviceEvent
        self._watcher = watcher

        def _on_watcher_event(change):
            if change.event == DeviceEvent.REMOVED:
                device_id = f"drive_{change.drive_letter}"
                logger.info("Watcher: instant disconnect for %s", device_id)
                if self._on_disconnect:
                    self._on_disconnect(device_id)
            elif change.event == DeviceEvent.ARRIVED:
                device_id = f"drive_{change.drive_letter}"
                logger.info("Watcher: instant reconnect for %s", device_id)
                if self._on_reconnect:
                    self._on_reconnect(device_id)

        watcher.on_change(_on_watcher_event)
        logger.info("HealthMonitor: attached DeviceWatcher (event-driven connection detection)")

    def start(self) -> None:
        """啟動背景監控執行緒"""
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="HealthMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("HealthMonitor started (interval=%.1fs, devices=%d)",
                     self._interval, len(self._collectors))

    def stop(self) -> None:
        self._active = False
        if self._thread:
            self._thread.join(timeout=10.0)
        logger.info("HealthMonitor stopped")

    def get_device_metrics(self, device_id: str) -> Optional[DeviceMetrics]:
        return self._devices.get(device_id)

    def get_all_metrics(self) -> Dict[str, DeviceMetrics]:
        return dict(self._devices)

    def get_alerts(self, limit: int = 50) -> List[Alert]:
        return self._alerts[-limit:]

    def get_overall_health(self) -> DeviceHealth:
        """取得所有裝置中最差的健康狀態"""
        if not self._devices:
            return DeviceHealth.UNKNOWN

        worst = DeviceHealth.HEALTHY
        priority = {
            DeviceHealth.HEALTHY: 0,
            DeviceHealth.UNKNOWN: 1,
            DeviceHealth.WARNING: 2,
            DeviceHealth.CRITICAL: 3,
            DeviceHealth.DISCONNECTED: 4,
        }
        for m in self._devices.values():
            if priority.get(m.health, 0) > priority.get(worst, 0):
                worst = m.health
        return worst

    def force_check(self) -> Dict[str, DeviceMetrics]:
        """立即執行一次全部裝置的健康檢查"""
        self._collect_all()
        return self.get_all_metrics()

    # ── NVMe SMART 資料讀取 ──

    @staticmethod
    def read_nvme_smart(device_path: str) -> Dict[str, Any]:
        """
        讀取 NVMe SMART 健康資訊。
        Linux: 使用 nvme-cli
        Windows: 使用 smartmontools 或 WMI
        """
        is_win = platform.system().lower() == "windows"
        result = {}

        try:
            if is_win:
                # Windows: smartctl
                out = subprocess.check_output(
                    ["smartctl", "-A", "-j", device_path],
                    timeout=10, text=True, stderr=subprocess.DEVNULL,
                )
                import json
                data = json.loads(out)
                attrs = data.get("nvme_smart_health_information_log", {})
                result["temperature"] = attrs.get("temperature", 0)
                result["percentage_used"] = attrs.get("percentage_used", 0)
                result["data_units_written"] = attrs.get("data_units_written", 0)
            else:
                # Linux: nvme smart-log
                out = subprocess.check_output(
                    ["nvme", "smart-log", device_path, "-o", "json"],
                    timeout=10, text=True, stderr=subprocess.DEVNULL,
                )
                import json
                data = json.loads(out)
                result["temperature"] = data.get("temperature", 0)
                result["percentage_used"] = data.get("percent_used", 0)
                result["data_units_written"] = data.get("data_units_written", 0)

        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            logger.debug("SMART data not available for %s", device_path)
        except Exception as e:
            logger.warning("Error reading SMART: %s", e)

        return result

    # ── Internal ──

    def _monitor_loop(self) -> None:
        while self._active:
            self._collect_all()
            time.sleep(self._interval)

    def _collect_all(self) -> None:
        for device_id, collector in list(self._collectors.items()):
            try:
                metrics = collector()
                prev = self._devices.get(device_id)

                with self._lock:
                    self._devices[device_id] = metrics

                # 檢查斷線
                if prev and prev.is_connected and not metrics.is_connected:
                    self._emit_alert(Alert(
                        timestamp=time.time(),
                        level=AlertLevel.CRITICAL,
                        device_id=device_id,
                        message=f"Device disconnected: {device_id}",
                        metric_name="connection",
                    ))
                    if self._on_disconnect:
                        self._on_disconnect(device_id)

                # 檢查重新連線
                elif prev and not prev.is_connected and metrics.is_connected:
                    self._emit_alert(Alert(
                        timestamp=time.time(),
                        level=AlertLevel.INFO,
                        device_id=device_id,
                        message=f"Device reconnected: {device_id}",
                        metric_name="connection",
                    ))
                    if self._on_reconnect:
                        self._on_reconnect(device_id)

                # 溫度檢查
                self._check_temperature(metrics)

                # 壽命檢查
                self._check_wear(metrics)

                # 錯誤率檢查
                self._check_errors(metrics)

            except Exception as e:
                logger.error("Health check failed for %s: %s", device_id, e)

    def _check_temperature(self, m: DeviceMetrics) -> None:
        if m.temperature_celsius >= self._temp_crit:
            self._emit_alert(Alert(
                timestamp=time.time(),
                level=AlertLevel.CRITICAL,
                device_id=m.device_id,
                message=f"CRITICAL: Temperature {m.temperature_celsius}°C exceeds {self._temp_crit}°C",
                metric_name="temperature",
                metric_value=m.temperature_celsius,
                threshold=self._temp_crit,
            ))
        elif m.temperature_celsius >= self._temp_warn:
            self._emit_alert(Alert(
                timestamp=time.time(),
                level=AlertLevel.WARNING,
                device_id=m.device_id,
                message=f"WARNING: Temperature {m.temperature_celsius}°C exceeds {self._temp_warn}°C",
                metric_name="temperature",
                metric_value=m.temperature_celsius,
                threshold=self._temp_warn,
            ))

    def _check_wear(self, m: DeviceMetrics) -> None:
        if m.wear_level_pct <= self._wear_crit:
            self._emit_alert(Alert(
                timestamp=time.time(),
                level=AlertLevel.CRITICAL,
                device_id=m.device_id,
                message=f"CRITICAL: Remaining life {m.wear_level_pct}% below {self._wear_crit}%",
                metric_name="wear_level",
                metric_value=m.wear_level_pct,
                threshold=self._wear_crit,
            ))
        elif m.wear_level_pct <= self._wear_warn:
            self._emit_alert(Alert(
                timestamp=time.time(),
                level=AlertLevel.WARNING,
                device_id=m.device_id,
                message=f"WARNING: Remaining life {m.wear_level_pct}% below {self._wear_warn}%",
                metric_name="wear_level",
                metric_value=m.wear_level_pct,
                threshold=self._wear_warn,
            ))

    def _check_errors(self, m: DeviceMetrics) -> None:
        total_errors = m.read_errors + m.write_errors + m.crc_errors
        if total_errors > 1000:
            self._emit_alert(Alert(
                timestamp=time.time(),
                level=AlertLevel.WARNING,
                device_id=m.device_id,
                message=f"High error count: {total_errors} total errors",
                metric_name="error_count",
                metric_value=total_errors,
            ))

    def _emit_alert(self, alert: Alert) -> None:
        # 去重：同一裝置、同一指標在 60 秒內不重複告警
        for recent in self._alerts[-20:]:
            if (recent.device_id == alert.device_id
                    and recent.metric_name == alert.metric_name
                    and (alert.timestamp - recent.timestamp) < 60):
                return

        self._alerts.append(alert)
        if len(self._alerts) > 500:
            self._alerts = self._alerts[-250:]

        logger.log(
            logging.CRITICAL if alert.level == AlertLevel.CRITICAL else logging.WARNING,
            "[%s] %s", alert.device_id, alert.message,
        )

        for cb in self._alert_callbacks:
            try:
                cb(alert)
            except Exception as e:
                logger.error("Alert callback error: %s", e)
