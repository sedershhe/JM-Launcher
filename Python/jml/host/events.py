"""兼容层：事件总线已下沉到 ``jml.events``（core 与 host 共用）。

历史代码/测试仍可 ``from jml.host import events`` 使用本模块；实现与单例
全部在 ``jml.events``，这里只做 re-export 与 ``BUS`` 的动态取当前值，
保证 ``reset()`` 替换单例后，通过本命名空间访问到的也是最新总线。
"""

from __future__ import annotations

import jml.events as _events

#: 类与函数直接 re-export
EventBus = _events.EventBus
reset = _events.reset
subscribe = _events.subscribe
unsubscribe = _events.unsubscribe
dispatch = _events.dispatch
set_sink = _events.set_sink
clear_sink = _events.clear_sink

__all__ = [
    "EventBus",
    "BUS",
    "reset",
    "subscribe",
    "unsubscribe",
    "dispatch",
    "set_sink",
    "clear_sink",
]


def __getattr__(name: str):
    """``events.BUS`` 动态返回 ``jml.events.BUS``（reset 后自动跟随）。"""
    if name == "BUS":
        return _events.BUS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
