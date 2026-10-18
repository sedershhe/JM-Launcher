"""registry.py（服务注册表）边界单元测试。

已实测过的部分（插件加载链路里覆盖过）不再重复：注册成功路径、重复注册
报错。这里补边界：空名 /  bool 版本 / 精确版本不匹配 / names 快照 /
unregister 静默 / clear 后 resolve 抛异常。
"""

from __future__ import annotations

import pytest

from jml.core.protocol import ServiceError
from jml.host import registry as registry_module
from jml.host.registry import REGISTRY, ServiceRegistry, resolve


@pytest.fixture
def registry() -> ServiceRegistry:
    """每个用例一个全新注册表实例（不碰模块级单例的其它用例）。"""
    return ServiceRegistry()


def test_register_empty_name_raises_service_error(registry):
    """注册空字符串服务名：应抛 ServiceError，且不写入注册表。"""
    with pytest.raises(ServiceError) as excinfo:
        registry.register_service("", 1, "ref")

    assert "非空字符串" in str(excinfo.value)
    assert registry.names() == {}


def test_register_non_str_name_raises_service_error(registry):
    """注册非字符串服务名（如整数）：应抛 ServiceError。"""
    with pytest.raises(ServiceError):
        registry.register_service(123, 1, "ref")


def test_register_bool_version_raises_service_error(registry):
    """注册布尔版本号：True 是 int 子类但语义上不是版本号，应抛 ServiceError。"""
    with pytest.raises(ServiceError) as excinfo:
        registry.register_service("demo.svc", True, "ref")

    assert "整数" in str(excinfo.value)
    assert registry.names() == {}


def test_register_str_version_raises_service_error(registry):
    """注册字符串版本号："1" 不是整数，应抛 ServiceError。"""
    with pytest.raises(ServiceError):
        registry.register_service("demo.svc", "1", "ref")


def test_resolve_requires_exact_version(registry):
    """精确版本匹配：注册 (name, 2) 后用 resolve(name, 1) 找不到，应抛 ServiceError。"""
    registry.register_service("demo.svc", 2, "provider_v2")

    with pytest.raises(ServiceError) as excinfo:
        registry.resolve("demo.svc", 1)

    assert "缺少服务 demo.svc/版本 1" in str(excinfo.value)
    # 反向：用正确版本能取到
    assert registry.resolve("demo.svc", 2) == "provider_v2"


def test_same_name_different_versions_coexist(registry):
    """同名不同版本可共存：resolve 各自返回各自的 provider_ref，互不影响。"""
    registry.register_service("demo.svc", 1, "v1")
    registry.register_service("demo.svc", 2, "v2")

    assert registry.resolve("demo.svc", 1) == "v1"
    assert registry.resolve("demo.svc", 2) == "v2"
    assert registry.names() == {"demo.svc": 2}  # names 只保留最后写入的同名项


def test_has_reflects_exact_match(registry):
    """has() 精确匹配：已注册版本返回 True，未注册版本返回 False，不抛异常。"""
    registry.register_service("demo.svc", 3, "v3")

    assert registry.has("demo.svc", 3) is True
    assert registry.has("demo.svc", 4) is False
    assert registry.has("other.svc", 3) is False


def test_names_returns_snapshot(registry):
    """names() 返回快照：修改返回值不应影响注册表内部状态。"""
    registry.register_service("a.svc", 1, "a")
    registry.register_service("b.svc", 2, "b")

    snapshot = registry.names()
    assert snapshot == {"a.svc": 1, "b.svc": 2}

    snapshot["c.svc"] = 9  # 改快照不影响内部
    snapshot.pop("a.svc")
    assert registry.names() == {"a.svc": 1, "b.svc": 2}


def test_names_empty_registry(registry):
    """names() 空注册表：返回空 dict，不抛异常。"""
    assert registry.names() == {}


def test_unregister_twice_is_silent(registry):
    """unregister 未注册项与重复 unregister：都静默通过，不抛异常。"""
    registry.register_service("demo.svc", 1, "v1")

    registry.unregister_service("demo.svc", 1)  # 第一次：真删除
    registry.unregister_service("demo.svc", 1)  # 第二次：未注册，静默
    registry.unregister_service("never.existed", 7)  # 从未注册，静默

    assert registry.names() == {}
    with pytest.raises(ServiceError):
        registry.resolve("demo.svc", 1)


def test_clear_then_resolve_raises(registry):
    """clear 后 resolve：注册表清空，resolve 应抛 ServiceError。"""
    registry.register_service("demo.svc", 1, "v1")

    registry.clear()

    assert registry.names() == {}
    with pytest.raises(ServiceError):
        registry.resolve("demo.svc", 1)


def test_register_accepts_any_provider_ref(registry):
    """provider_ref 类型不受限：对象 / 函数 / None 都可注册，宿主不解释其内容。"""

    def factory():
        return "built"

    registry.register_service("obj.svc", 1, {"a": 1})
    registry.register_service("fn.svc", 1, factory)
    registry.register_service("none.svc", 1, None)

    assert registry.resolve("obj.svc", 1) == {"a": 1}
    assert registry.resolve("fn.svc", 1) is factory
    assert registry.resolve("none.svc", 1) is None


def test_module_level_singleton_reset():
    """模块级单例：register_service / resolve / reset() 走的是同一个 REGISTRY。"""
    registry_module.register_service("singleton.svc", 1, "ref")

    assert REGISTRY.has("singleton.svc", 1) is True
    assert resolve("singleton.svc", 1) == "ref"

    registry_module.reset()

    assert REGISTRY.names() == {}
    with pytest.raises(ServiceError):
        resolve("singleton.svc", 1)


def test_singleton_isolated_between_tests():
    """用例隔离：上一个用例注册的单例服务在本用例开始时已被清掉。"""
    assert REGISTRY.names() == {}
    with pytest.raises(ServiceError):
        resolve("singleton.svc", 1)
