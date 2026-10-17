"""类型化事件总线（对应技术方案 2.3 节，禁止裸 dict）。

- subscribe(event_type, handler) / unsubscribe / dispatch(event_type, payload)
- 订阅表与分发均显式加锁（对应旧缺陷 #10）
- dispatch 时校验 payload 为 dict 且含 "type" 字段（值须等于事件类型）；
  再按 EVENT_PAYLOAD_SCHEMAS 校验必填字段，缺字段 → ValueError
  （含事件类型与缺失字段名）
- handler 抛异常时捕获记入错误列表，不得中断其它 handler
"""

from __future__ import annotations

import threading

from jml.core.protocol import EVENT_PAYLOAD_SCHEMAS, PluginError


class EventBus:
    """线程安全的类型化事件总线。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 结构: {event_type: [handler, ...]}（保持订阅顺序）
        self._subscribers: dict[str, list] = {}
        # 最近一次 dispatch 中各 handler 抛出的异常（诊断用，不中断分发）
        self._last_errors: list[str] = []

    # -- 订阅管理 -----------------------------------------------------------

    def subscribe(self, event_type: str, handler) -> None:
        """订阅事件。

        参数:
            event_type: 事件类型常量（jml.core.protocol 中定义）
            handler: 回调，签名 handler(payload: dict) -> None

        异常:
            PluginError —— event_type 不是已知类型 / handler 不可调用
        """
        if event_type not in EVENT_PAYLOAD_SCHEMAS:
            raise PluginError(f"未知事件类型: {event_type!r}，拒绝订阅")
        if not callable(handler):
            raise PluginError(f"事件 {event_type} 的订阅者必须是可调用对象")
        with self._lock:
            handlers = self._subscribers.setdefault(event_type, [])
            if handler not in handlers:
                handlers.append(handler)

    def unsubscribe(self, event_type: str, handler) -> None:
        """退订事件（未订阅时静默忽略）。"""
        with self._lock:
            handlers = self._subscribers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

    def subscriber_count(self, event_type: str) -> int:
        """返回某事件类型的订阅者数量（测试与诊断用）。"""
        with self._lock:
            return len(self._subscribers.get(event_type, ()))

    # -- 分发 ---------------------------------------------------------------

    def dispatch(self, event_type: str, payload: dict) -> int:
        """向所有订阅者派发一个事件，返回投递到的 handler 数。

        校验规则（任一不过即抛异常，**不调用任何 handler**）：
        1. event_type 必须是已知类型；
        2. payload 必须是 dict 且含 "type" 字段，且 type 值等于 event_type；
        3. payload 必须含 EVENT_PAYLOAD_SCHEMAS[event_type] 列出的全部字段。

        handler 异常处理：捕获并记入错误列表，不中断其它 handler。
        """
        self._validate_payload(event_type, payload)

        # 加锁内取 handler 快照，避免分发过程中订阅表被并发修改
        with self._lock:
            handlers = list(self._subscribers.get(event_type, ()))

        errors: list[str] = []
        delivered = 0
        for handler in handlers:
            try:
                handler(payload)
                delivered += 1
            except Exception as exc:  # 单个 handler 失败不影响其它（记错不中断）
                errors.append(
                    f"事件 {event_type} 的处理器 {getattr(handler, '__name__', repr(handler))} "
                    f"抛出异常: {type(exc).__name__}: {exc}"
                )

        with self._lock:
            self._last_errors = errors
        return delivered

    def last_dispatch_errors(self) -> list[str]:
        """返回最近一次 dispatch 收集的 handler 异常描述（诊断用）。"""
        with self._lock:
            return list(self._last_errors)

    # -- 校验 ---------------------------------------------------------------

    @staticmethod
    def _validate_payload(event_type: str, payload: object) -> None:
        """dispatch 前的统一校验（禁止裸 dict：必须携带 type 字段）。"""
        if event_type not in EVENT_PAYLOAD_SCHEMAS:
            raise PluginError(f"未知事件类型: {event_type!r}，拒绝派发")
        if not isinstance(payload, dict):
            raise ValueError(f"事件 {event_type} 的 payload 必须是 dict")
        if "type" not in payload:
            raise ValueError(f"事件 {event_type} 的 payload 缺少 type 字段（禁止裸 dict）")
        if payload["type"] != event_type:
            raise ValueError(
                f"事件 {event_type} 的 payload.type 不匹配: {payload['type']!r}"
            )
        required = EVENT_PAYLOAD_SCHEMAS[event_type]
        missing = [field for field in required if field not in payload]
        if missing:
            raise ValueError(
                f"事件 {event_type} 的 payload 缺少必填字段: {', '.join(missing)}"
            )


#: 模块级单例事件总线（宿主全局唯一）
BUS = EventBus()


def reset() -> None:
    """重置模块级单例（测试隔离用）。"""
    global BUS
    BUS = EventBus()


def subscribe(event_type: str, handler) -> None:
    """模块级便捷入口：订阅单例总线事件。"""
    BUS.subscribe(event_type, handler)


def unsubscribe(event_type: str, handler) -> None:
    """模块级便捷入口：退订单例总线事件。"""
    BUS.unsubscribe(event_type, handler)


def dispatch(event_type: str, payload: dict) -> int:
    """模块级便捷入口：向单例总线派发事件，返回投递到的 handler 数。"""
    return BUS.dispatch(event_type, payload)
