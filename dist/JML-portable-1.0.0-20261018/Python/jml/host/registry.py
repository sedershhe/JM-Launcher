"""服务注册表（核心机制，对应技术方案 2.2 节）。

- 主程序与插件均可注册服务：register_service(name, version, provider_ref)
- provider_ref 是插件注册的可调用工厂或对象（宿主不解释其内容）
- 版本匹配为**精确匹配**：resolve(name, version) 只返回版本完全一致的
  注册项，不做任何降级兼容
- 线程安全：显式 threading.RLock，读路径也加锁（对应旧缺陷 #10，
  不依赖 GIL 侥幸）

本模块是模块级单例（REGISTRY），全局共享一份注册表；
测试需要干净环境时用 reset()。
"""

from __future__ import annotations

import threading

from jml.core.protocol import ServiceError


class ServiceRegistry:
    """线程安全的服务注册表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 结构: {(name, version): provider_ref}
        self._services: dict[tuple[str, int], object] = {}

    # -- 写路径 -----------------------------------------------------------

    def register_service(self, name: str, version: int, provider_ref: object) -> None:
        """注册一个服务。

        参数:
            name: 服务名（如 "archive_reader"、"jm.search"）
            version: 服务协议版本（整数，精确匹配用）
            provider_ref: 服务提供者（可调用工厂或对象，宿主不解释）

        异常:
            ServiceError —— 同名同版本重复注册（绝不静默覆盖）
        """
        if not isinstance(name, str) or not name:
            raise ServiceError("服务名必须是非空字符串")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ServiceError(f"服务 {name} 的版本必须是整数")
        with self._lock:
            key = (name, version)
            if key in self._services:
                raise ServiceError(f"服务 {name}/版本 {version} 已被注册，不允许重复注册")
            self._services[key] = provider_ref

    def unregister_service(self, name: str, version: int) -> None:
        """注销一个服务（测试与停用插件时用，未注册时静默忽略）。"""
        with self._lock:
            self._services.pop((name, version), None)

    def clear(self) -> None:
        """清空注册表（测试用）。"""
        with self._lock:
            self._services.clear()

    # -- 读路径（也加锁，对应旧缺陷 #10） ---------------------------------

    def resolve(self, name: str, version: int) -> object:
        """解析服务：精确匹配版本。

        返回注册时的 provider_ref。

        异常:
            ServiceError("缺少服务 xxx/版本 n") —— 找不到该服务与版本。
        """
        with self._lock:
            key = (name, version)
            if key not in self._services:
                raise ServiceError(f"缺少服务 {name}/版本 {version}")
            return self._services[key]

    def has(self, name: str, version: int) -> bool:
        """判断服务是否存在（精确匹配版本），不抛异常。"""
        with self._lock:
            return (name, version) in self._services

    def names(self) -> dict[str, int]:
        """返回当前已注册服务的 {name: version} 快照（供 bootstrap 汇报）。"""
        with self._lock:
            return {name: version for (name, version) in self._services}


#: 模块级单例注册表（宿主全局唯一）
REGISTRY = ServiceRegistry()


def reset() -> None:
    """重置模块级单例（测试隔离用）。"""
    REGISTRY.clear()


def register_service(name: str, version: int, provider_ref: object) -> None:
    """模块级便捷入口：注册服务到单例注册表。"""
    REGISTRY.register_service(name, version, provider_ref)


def resolve(name: str, version: int) -> object:
    """模块级便捷入口：从单例注册表解析服务。"""
    return REGISTRY.resolve(name, version)


def has(name: str, version: int) -> bool:
    """模块级便捷入口：判断单例注册表中服务是否存在。"""
    return REGISTRY.has(name, version)
