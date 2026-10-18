"""PluginContext 单元测试（阶段 2：插件唯一跨服务调用路径）。

覆盖：
- ``ctx.call_service(name, version, method, payload)`` 正常转发；
- 服务缺失 → ServiceError；
- 方法缺失 / 服务对象没有 call → ServiceError；
- ctx 不暴露 registry / bus 等宿主对象（旧缺陷 #9）。
"""

from __future__ import annotations

import pytest

from jml.core.protocol import ServiceError
from jml.host.plugin_context import PluginContext


class FakeService:
    """带 call 的服务替身。"""

    def call(self, method: str, payload: dict) -> dict:
        if method != "echo":
            raise ServiceError(f"服务 fake 不支持方法 {method!r}")
        return {"ok": True, "echo": payload}


class ProviderWithoutCall:
    """没有 call 方法的错误服务对象。"""

    pass


def _make_context(
    services: dict[tuple[str, int], object], data_dir: str = ""
) -> PluginContext:
    def on_get_service(name: str, version: int) -> object:
        key = (name, version)
        if key not in services:
            raise ServiceError(f"缺少服务 {name}/版本 {version}")
        return services[key]

    return PluginContext(
        "test_plugin",
        on_register_service=lambda name, version, ref: None,
        on_subscribe=lambda event_type, handler: None,
        on_get_service=on_get_service,
        on_config_get=lambda key: None,
        on_log=lambda message: None,
        on_register_page=lambda page_id, loader: None,
        on_register_page_action=lambda page_id, action_id, handler: None,
        on_register_page_image=lambda page_id, resolver: None,
        data_dir=data_dir,
    )


def test_call_service_forwards_to_provider():
    ctx = _make_context({("fake.svc", 1): FakeService()})
    payload = {"value": 42, "_caller": "plugin:forged"}

    result = ctx.call_service("fake.svc", 1, "echo", payload)

    # host 注入调用方标识供会话隔离；插件伪造的 _caller 被覆盖
    assert result == {
        "ok": True,
        "echo": {"value": 42, "_caller": "plugin:test_plugin"},
    }
    assert payload == {"value": 42, "_caller": "plugin:forged"}


def test_call_service_missing_service_raises():
    ctx = _make_context({})

    with pytest.raises(ServiceError) as excinfo:
        ctx.call_service("missing.svc", 1, "echo", {})
    assert "missing.svc" in str(excinfo.value)


def test_call_service_missing_method_raises():
    ctx = _make_context({("fake.svc", 1): FakeService()})

    with pytest.raises(ServiceError) as excinfo:
        ctx.call_service("fake.svc", 1, "no_such_method", {})
    assert "no_such_method" in str(excinfo.value)


def test_call_service_provider_without_call_raises():
    ctx = _make_context({("bad.svc", 1): ProviderWithoutCall()})

    with pytest.raises(ServiceError):
        ctx.call_service("bad.svc", 1, "anything", {})


def test_call_service_wraps_host_lookup_error_as_service_error():
    """宿主查服务时抛任何异常，插件侧都只看到 ServiceError。"""

    def boom(name: str, version: int):
        raise KeyError("host internals")

    ctx = PluginContext(
        "test_plugin",
        on_register_service=lambda name, version, ref: None,
        on_subscribe=lambda event_type, handler: None,
        on_get_service=boom,
        on_config_get=lambda key: None,
        on_log=lambda message: None,
        on_register_page=lambda page_id, loader: None,
        on_register_page_action=lambda page_id, action_id, handler: None,
        on_register_page_image=lambda page_id, resolver: None,
        data_dir="",
    )

    with pytest.raises(ServiceError):
        ctx.call_service("any.svc", 1, "echo", {})


def test_context_does_not_expose_host_objects():
    """插件摸不到 registry / bus / 宿主私有对象（旧缺陷 #9）。"""
    ctx = _make_context({})
    for name in ("registry", "bus", "BUS", "REGISTRY"):
        assert not hasattr(ctx, name)

    public_methods = {
        name
        for name in dir(ctx)
        if not name.startswith("_") and callable(getattr(ctx, name))
    }
    assert public_methods == {
        "register_service",
        "subscribe",
        "get_service",
        "call_service",
        "config_get",
        "register_page",
        "register_page_action",
        "register_page_image",
        "log",
    }


def test_context_exposes_read_only_data_dir():
    """宿主传入的数据目录只读暴露：插件用它落盘，但不自己拼 APPDATA 路径。"""
    ctx = _make_context({}, data_dir="C:/tmp/jml-plugin-data")

    assert ctx.data_dir == "C:/tmp/jml-plugin-data"
    with pytest.raises(AttributeError):
        ctx.data_dir = "C:/tmp/other"
