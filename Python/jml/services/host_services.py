"""主程序注册的占位服务（阶段 2 才实现真业务）。

注册四个服务（版本均为 1），每个方法先抛 NotImplementedError("阶段2实现")——
但服务本身存在，让插件的 requires 可解析、装配流程可测：

- jm.search   搜索
- jm.detail   详情
- jm.download 下载
- jm.account  账号

调用统一签名（对应技术方案 2.2）:
    call_service(name, version, method, payload) -> dict
"""

from __future__ import annotations

from jml.core.protocol import ServiceError
from jml.host.registry import REGISTRY

#: 主程序服务版本（阶段 1 全部为 1）
_HOST_SERVICE_VERSION = 1


def _not_implemented(service: str, method: str):
    """生成阶段 1 的占位方法：服务存在但业务未实现。"""

    def _placeholder(*args, **kwargs):
        raise NotImplementedError(f"服务 {service}.{method} 阶段2实现")

    _placeholder.__name__ = method
    return _placeholder


class _HostService:
    """单个主程序服务的占位包装：常见方法名先占位，未知方法报 ServiceError。

    阶段 2 会把这里的方法逐个替换为真实业务实现（jmcomic 调用），
    对插件的调用签名不变。
    """

    #: 该服务允许插件调用的方法名（阶段 1 全部占位）
    METHODS: tuple[str, ...] = ()

    def __init__(self, service_name: str) -> None:
        self._service_name = service_name

    def call(self, method: str, payload: dict) -> dict:
        """统一调用入口: call_service(name, version, method, payload)。"""
        if method not in self.METHODS:
            raise ServiceError(
                f"服务 {self._service_name} 不支持方法 {method!r}, "
                f"允许值: {', '.join(self.METHODS) or '(无)'}"
            )
        raise NotImplementedError(f"服务 {self._service_name}.{method} 阶段2实现")


class _SearchService(_HostService):
    METHODS = ("search", "get_page")


class _DetailService(_HostService):
    METHODS = ("get_detail",)


class _DownloadService(_HostService):
    METHODS = ("start_batch", "cancel_batch", "get_progress")


class _AccountService(_HostService):
    METHODS = ("login_status", "ensure_login")


#: 主程序服务表: 服务名 → 服务实例
HOST_SERVICES: dict[str, _HostService] = {
    "jm.search": _SearchService("jm.search"),
    "jm.detail": _DetailService("jm.detail"),
    "jm.download": _DownloadService("jm.download"),
    "jm.account": _AccountService("jm.account"),
}


def register_host_services() -> None:
    """把四个主程序服务注册进服务注册表（bootstrap 时调用）。

    重复注册: bootstrap 幂等由 jml_backend 控制（二次调用不再进来），
    这里直接允许覆盖同版本（host 服务是宿主自己的，不是插件的）。
    """
    for name, instance in HOST_SERVICES.items():
        if REGISTRY.has(name, _HOST_SERVICE_VERSION):
            REGISTRY.unregister_service(name, _HOST_SERVICE_VERSION)
        REGISTRY.register_service(name, _HOST_SERVICE_VERSION, instance)
