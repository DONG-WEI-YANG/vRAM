"""
Event Hook System for VRAM Operations
=======================================
借鑑自 OSS120BCLI hooks.py 的事件驅動架構。

定義可配置的事件鉤子，在記憶體操作的關鍵時間點
觸發自訂行為，無需修改核心程式碼。

事件類型：
  PreAllocate     — 記憶體分配前（可阻擋）
  PostAllocate    — 記憶體分配後
  PreTransfer     — 資料傳輸前（可阻擋）
  PostTransfer    — 資料傳輸後
  PreMigrate      — 層級遷移前（可阻擋）
  PostMigrate     — 層級遷移後
  DeviceConnect   — 裝置連接
  DeviceDisconnect — 裝置斷線（緊急）
  HealthAlert     — 健康告警
  CircuitTrip     — 斷路器觸發
  CircuitRecover  — 斷路器恢復

Hook 可以：
  - 記錄操作到 audit log
  - 阻擋危險操作
  - 觸發自動最佳化
  - 發送通知
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, List, Dict, Any

logger = logging.getLogger(__name__)


class HookEvent(Enum):
    PRE_ALLOCATE = "pre_allocate"
    POST_ALLOCATE = "post_allocate"
    PRE_TRANSFER = "pre_transfer"
    POST_TRANSFER = "post_transfer"
    PRE_MIGRATE = "pre_migrate"
    POST_MIGRATE = "post_migrate"
    DEVICE_CONNECT = "device_connect"
    DEVICE_DISCONNECT = "device_disconnect"
    HEALTH_ALERT = "health_alert"
    CIRCUIT_TRIP = "circuit_trip"
    CIRCUIT_RECOVER = "circuit_recover"


@dataclass
class HookContext:
    """傳遞給 hook handler 的上下文"""
    event: HookEvent
    timestamp: float = field(default_factory=time.time)
    device_id: str = ""
    block_id: str = ""
    tier: str = ""
    size_bytes: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    # Pre-hooks 可設為 True 以阻擋操作
    blocked: bool = False
    block_reason: str = ""


@dataclass
class Hook:
    """單一事件鉤子定義"""
    name: str
    event: HookEvent
    handler: Callable[[HookContext], None]
    blocking: bool = False    # Pre-hooks: 可否阻擋操作
    priority: int = 100       # 數字越小優先級越高
    enabled: bool = True
    condition: Optional[Callable[[HookContext], bool]] = None  # 條件過濾


class EventHookManager:
    """
    事件鉤子管理器。

    使用方式：
        hooks = EventHookManager()

        # 註冊 hook
        hooks.register(Hook(
            name="audit_allocations",
            event=HookEvent.POST_ALLOCATE,
            handler=lambda ctx: audit_log.write(ctx),
        ))

        # 在操作點觸發
        ctx = HookContext(event=HookEvent.PRE_ALLOCATE, block_id="layer_42")
        hooks.fire(ctx)
        if ctx.blocked:
            raise PermissionError(ctx.block_reason)
    """

    def __init__(self):
        self._hooks: Dict[HookEvent, List[Hook]] = {event: [] for event in HookEvent}
        self._fire_count = 0
        self._block_count = 0

    def register(self, hook: Hook) -> None:
        """註冊一個 hook"""
        self._hooks[hook.event].append(hook)
        # 按優先級排序
        self._hooks[hook.event].sort(key=lambda h: h.priority)
        logger.debug("Registered hook '%s' for %s (pri=%d, blocking=%s)",
                      hook.name, hook.event.value, hook.priority, hook.blocking)

    def unregister(self, name: str) -> None:
        """移除指定名稱的 hook"""
        for event in self._hooks:
            self._hooks[event] = [h for h in self._hooks[event] if h.name != name]

    def fire(self, ctx: HookContext) -> HookContext:
        """
        觸發指定事件的所有 hook。

        Pre-hooks (blocking=True) 可以設定 ctx.blocked = True 來阻擋操作。
        """
        hooks = self._hooks.get(ctx.event, [])
        if not hooks:
            return ctx

        self._fire_count += 1

        for hook in hooks:
            if not hook.enabled:
                continue

            # 條件過濾
            if hook.condition and not hook.condition(ctx):
                continue

            try:
                hook.handler(ctx)
            except Exception as e:
                logger.error("Hook '%s' error: %s", hook.name, e)
                continue

            # 如果被阻擋，停止執行後續 hook
            if ctx.blocked and hook.blocking:
                self._block_count += 1
                logger.warning("Operation BLOCKED by hook '%s': %s",
                               hook.name, ctx.block_reason)
                break

        return ctx

    def get_hooks(self, event: Optional[HookEvent] = None) -> List[Hook]:
        """列出已註冊的 hook"""
        if event:
            return list(self._hooks.get(event, []))
        return [h for hooks in self._hooks.values() for h in hooks]

    def clear(self, event: Optional[HookEvent] = None) -> None:
        """清除 hook"""
        if event:
            self._hooks[event].clear()
        else:
            for e in self._hooks:
                self._hooks[e].clear()

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "total_hooks": sum(len(h) for h in self._hooks.values()),
            "total_fires": self._fire_count,
            "total_blocks": self._block_count,
            "hooks_per_event": {
                e.value: len(h) for e, h in self._hooks.items() if h
            },
        }


# ── 預建 Hook 工廠 ──

def create_audit_hook(audit_fn: Callable[[Dict], None]) -> Hook:
    """建立 audit log hook"""
    def handler(ctx: HookContext):
        audit_fn({
            "event": ctx.event.value,
            "timestamp": ctx.timestamp,
            "device_id": ctx.device_id,
            "block_id": ctx.block_id,
            "tier": ctx.tier,
            "size_bytes": ctx.size_bytes,
            "blocked": ctx.blocked,
        })

    return Hook(
        name="audit_logger",
        event=HookEvent.POST_ALLOCATE,  # 會被替換
        handler=handler,
        priority=999,  # 最低優先級（不影響其他 hook）
    )


def create_overheat_guard(temp_limit: float = 80.0) -> Hook:
    """建立過熱保護 hook（阻擋寫入）"""
    def handler(ctx: HookContext):
        temp = ctx.extra.get("temperature", 0)
        if temp >= temp_limit:
            ctx.blocked = True
            ctx.block_reason = f"Device temperature {temp}°C exceeds limit {temp_limit}°C"

    return Hook(
        name="overheat_guard",
        event=HookEvent.PRE_TRANSFER,
        handler=handler,
        blocking=True,
        priority=10,  # 高優先級
    )


def create_capacity_guard(min_free_pct: float = 5.0) -> Hook:
    """建立容量保護 hook（防止裝置寫滿）"""
    def handler(ctx: HookContext):
        free_pct = ctx.extra.get("free_pct", 100)
        if free_pct < min_free_pct:
            ctx.blocked = True
            ctx.block_reason = f"Device free space {free_pct:.1f}% below minimum {min_free_pct}%"

    return Hook(
        name="capacity_guard",
        event=HookEvent.PRE_ALLOCATE,
        handler=handler,
        blocking=True,
        priority=10,
    )
