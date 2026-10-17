"""事件总线汇聚出口（sink）单元测试（阶段 2 新增）。

覆盖：sink 收到 (event_type, payload)、投递顺序在订阅者之后、
无订阅者也会调用、sink 异常不影响 delivered、非可调用拒绝、clear/reset。
"""

from __future__ import annotations

import threading

import pytest

from jml.core import diagnostics
from jml.core.protocol import EVENT_DOWNLOAD_LOG, PluginError
from jml.host import events


def _log_payload(message: str = "hello") -> dict:
    return {"type": EVENT_DOWNLOAD_LOG, "level": "info", "message": message}


def test_sink_receives_type_and_payload_after_subscribers():
    order: list[str] = []

    def handler(payload: dict) -> None:
        order.append("subscriber")

    def sink(event_type: str, payload: dict) -> None:
        order.append(f"sink:{event_type}")
        assert payload["type"] == event_type

    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, handler)
    events.BUS.set_sink(sink)

    delivered = events.BUS.dispatch(EVENT_DOWNLOAD_LOG, _log_payload())

    assert delivered == 1
    assert order == ["subscriber", f"sink:{EVENT_DOWNLOAD_LOG}"]


def test_sink_called_without_subscribers():
    received: list[tuple[str, dict]] = []
    events.BUS.set_sink(lambda event_type, payload: received.append((event_type, payload)))

    delivered = events.BUS.dispatch(EVENT_DOWNLOAD_LOG, _log_payload("only-sink"))

    assert delivered == 0
    assert len(received) == 1
    assert received[0][0] == EVENT_DOWNLOAD_LOG
    assert received[0][1]["message"] == "only-sink"


def test_sink_exception_does_not_affect_subscribers():
    received: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, received.append)

    def bad_sink(event_type: str, payload: dict) -> None:
        raise RuntimeError("sink boom")

    events.BUS.set_sink(bad_sink)

    delivered = events.BUS.dispatch(EVENT_DOWNLOAD_LOG, _log_payload())

    assert delivered == 1
    assert len(received) == 1
    errors = events.BUS.last_dispatch_errors()
    assert any("汇聚出口" in item for item in errors)


def test_last_dispatch_errors_are_thread_local():
    """并发 dispatch 时各线程只看到自己那次的错误（旧实现是全局字段会串）。"""

    def handler(payload: dict) -> None:
        if payload["message"] == "boom":
            raise RuntimeError("handler boom")

    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, handler)
    a_dispatched = threading.Event()
    b_done = threading.Event()
    results: dict[str, list[str]] = {}

    def thread_a() -> None:
        events.BUS.dispatch(EVENT_DOWNLOAD_LOG, _log_payload("boom"))
        a_dispatched.set()
        # 故意等 B 的 dispatch 覆盖"全局"错误后再读，旧实现会读到空列表
        assert b_done.wait(5)
        results["a"] = events.BUS.last_dispatch_errors()

    def thread_b() -> None:
        assert a_dispatched.wait(5)
        events.BUS.dispatch(EVENT_DOWNLOAD_LOG, _log_payload("ok"))
        results["b"] = events.BUS.last_dispatch_errors()
        b_done.set()

    first = threading.Thread(target=thread_a, daemon=True)
    second = threading.Thread(target=thread_b, daemon=True)
    first.start()
    second.start()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert any("handler boom" in item for item in results["a"])
    assert results["b"] == []
    # 主线程从未 dispatch，也不应读到子线程留下的错误
    assert events.BUS.last_dispatch_errors() == []


def test_handler_and_sink_errors_are_recorded_in_diagnostics():
    """后台线程的 handler/sink 异常必须进诊断环表，主线程可读。"""
    diagnostics.clear()

    def bad_handler(payload: dict) -> None:
        raise RuntimeError("handler boom")

    def bad_sink(event_type: str, payload: dict) -> None:
        raise RuntimeError("sink boom")

    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, bad_handler)
    events.BUS.set_sink(bad_sink)

    events.BUS.dispatch(EVENT_DOWNLOAD_LOG, _log_payload())

    records = diagnostics.recent()
    assert any("EVENT_HANDLER_FAILED" in item for item in records)
    assert any("handler boom" in item for item in records)
    assert any("sink boom" in item for item in records)


def test_sink_not_called_when_validation_fails():
    received: list[tuple[str, dict]] = []
    events.BUS.set_sink(lambda event_type, payload: received.append((event_type, payload)))

    with pytest.raises(ValueError):
        events.BUS.dispatch(EVENT_DOWNLOAD_LOG, {"type": EVENT_DOWNLOAD_LOG})

    assert received == []


def test_set_sink_rejects_non_callable():
    with pytest.raises(PluginError):
        events.BUS.set_sink("not-callable")


def test_clear_sink():
    received: list[dict] = []
    events.BUS.set_sink(lambda event_type, payload: received.append(payload))
    events.BUS.clear_sink()

    events.BUS.dispatch(EVENT_DOWNLOAD_LOG, _log_payload())

    assert received == []
    assert events.BUS.sink() is None


def test_set_sink_none_equals_clear():
    events.BUS.set_sink(lambda event_type, payload: None)
    events.BUS.set_sink(None)
    assert events.BUS.sink() is None


def test_module_level_sink_helpers():
    received: list[str] = []
    events.set_sink(lambda event_type, payload: received.append(event_type))
    events.dispatch(EVENT_DOWNLOAD_LOG, _log_payload())
    assert received == [EVENT_DOWNLOAD_LOG]

    events.clear_sink()
    events.dispatch(EVENT_DOWNLOAD_LOG, _log_payload())
    assert received == [EVENT_DOWNLOAD_LOG]


def test_reset_drops_sink():
    events.BUS.set_sink(lambda event_type, payload: None)
    old_bus = events.BUS
    events.reset()
    assert events.BUS is not old_bus
    assert events.BUS.sink() is None
