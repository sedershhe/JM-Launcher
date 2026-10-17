"""类型化事件总线（core 与 host 共用，对应技术方案 2.3 节，禁止裸 dict）。

分层说明：本模块位于 ``jml`` 包根，**不依赖 host**，因此 ``core`` 可以安全
使用事件总线而不会反向拉起插件宿主（loader/config/manifest）。历史路径
``jml.host.events`` 保留为兼容 re-export。

- subscribe(event_type, handler) / unsubscribe / dispatch(event_type, payload)
- 订阅表与分发均显式加锁（对应旧缺陷 #10）
- dispatch 时校验 payload 为 dict 且含 "type" 字段（值须等于事件类型）；
  再按 EVENT_PAYLOAD_SCHEMAS 校验必填字段，缺字段 → ValueError
- handler 抛异常时捕获记入错误列表，不得中断其它 handler
- set_sink(fn) / clear_sink()：事件汇聚出口。dispatch 在投递完所有订阅者后
  调用 ``fn(event_type, payload)``，供桥接层序列化成 JSON 推给 C#；出口
  读写显式加锁，出口异常只记错误、不影响订阅者投递
"""

from __future__ import annotations

import threading

from jml.core import diagnostics
from jml.core.protocol import EVENT_PAYLOAD_SCHEMAS, PluginError


class EventBus:
    """线程安全的类型化事件总线。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 结构: {event_type: [handler, ...]}（保持订阅顺序）
        self._subscribers: dict[str, list] = {}
        # 最近一次 dispatch 中各 handler 抛出的异常（诊断用，不中断分发）。
        # 线程局部：并发 dispatch 时各线程只看到自己那次调用的错误。
        self._local = threading.local()
        # 事件汇聚出口：dispatch 投递完订阅者后调用 sink(event_type, payload)
        self._sink = None

    # -- 汇聚出口 -----------------------------------------------------------

    def set_sink(self, sink) -> None:
        """注册事件汇聚出口；``sink`` 为 None 等价于 clear_sink()。"""
        if sink is not None and not callable(sink):
            raise PluginError("事件汇聚出口必须是可调用对象")
        with self._lock:
            self._sink = sink

    def clear_sink(self) -> None:
        """移除事件汇聚出口（未注册时静默忽略）。"""
        with self._lock:
            self._sink = None

    def sink(self):
        """返回当前汇聚出口（测试与诊断用；无出口时返回 None）。"""
        with self._lock:
            return self._sink

    # -- 订阅管理 -----------------------------------------------------------

    def subscribe(self, event_type: str, handler) -> None:
        """订阅事件（handler(payload: dict) -> None）。"""
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
        """向所有订阅者派发一个事件，返回投递到的 handler 数。"""
        self._validate_payload(event_type, payload)

        with self._lock:
            handlers = list(self._subscribers.get(event_type, ()))

        errors: list[str] = []
        delivered = 0
        for handler in handlers:
            try:
                handler(payload)
                delivered += 1
            except Exception as exc:  # 单个 handler 失败不影响其它
                errors.append(
                    f"事件 {event_type} 的处理器 "
                    f"{getattr(handler, '__name__', repr(handler))} "
                    f"抛出异常: {type(exc).__name__}: {exc}"
                )
                # 线程局部错误主线程读不到，必须同时进有界诊断环表
                diagnostics.record(
                    "EVENT_HANDLER_FAILED",
                    f"事件 {event_type} 的处理器抛出异常",
                    exc,
                )

        with self._lock:
            sink = self._sink
        if sink is not None:
            try:
                sink(event_type, payload)
            except Exception as exc:
                errors.append(
                    f"事件 {event_type} 的汇聚出口抛出异常: "
                    f"{type(exc).__name__}: {exc}（不影响订阅者投递）"
                )
                # C# 回调异常同样写诊断，后台线程的错误主线程也能看到
                diagnostics.record(
                    "EVENT_HANDLER_FAILED",
                    f"事件 {event_type} 的汇聚出口（C# 回调）抛出异常",
                    exc,
                )

        self._local.last_errors = errors
        return delivered

    def last_dispatch_errors(self) -> list[str]:
        """返回**当前线程**最近一次 dispatch 收集的异常描述。"""
        return list(getattr(self._local, "last_errors", ()))

    # -- 校验 ---------------------------------------------------------------

    @staticmethod
    def _validate_payload(event_type: str, payload: object) -> None:
        """dispatch 前的统一校验（禁止裸 dict）。"""
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


def set_sink(sink) -> None:
    """模块级便捷入口：给单例总线注册事件汇聚出口。"""
    BUS.set_sink(sink)


def clear_sink() -> None:
    """模块级便捷入口：移除单例总线的事件汇聚出口。"""
    BUS.clear_sink()
