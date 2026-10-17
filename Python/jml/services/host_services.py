"""主程序服务（阶段 2 真实化；插件通过 ``call_service`` 调用）。

四个服务（版本均为 1）：

- ``jm.search``   搜索 / 翻页（关键词快照语义见 ``core.searcher``）
- ``jm.detail``   详情
- ``jm.download`` 批量下载登记 / 取消 / 查询进度
- ``jm.account``  账号（阶段 3 才有真业务，这里返回结构化 NOT_AVAILABLE，不抛异常）

调用统一签名（技术方案 2.2）::

    call_service(name, version, method, payload) -> dict

返回值纪律：
- 成功 ``{"ok": True, ...业务字段}``；
- 失败 ``{"ok": False, "error_code": ..., "error_message": ...}``；
- 未知方法抛 ``ServiceError``（属调用方拼错方法名，不是业务失败）。

本模块不联网（网络在 ``core.jm_source``）、不感知 UI。
"""

from __future__ import annotations

from jml.core import detail, downloader, searcher
from jml.core.errors import (
    ERROR_BATCH_NOT_FOUND,
    ERROR_NOT_AVAILABLE,
    classify,
    error_payload,
    make_error,
)
from jml.core import diagnostics
from jml.core.protocol import ServiceError
from jml.host.registry import REGISTRY

#: 主程序服务版本（阶段 2 仍为 1）
_HOST_SERVICE_VERSION = 1


class _HostService:
    """主程序服务基类：方法名白名单 + 统一异常收口。"""

    #: 该服务允许插件调用的方法名
    METHODS: tuple[str, ...] = ()

    def __init__(self, service_name: str) -> None:
        self._service_name = service_name

    def call(self, method: str, payload: dict) -> dict:
        """统一调用入口: ``call_service(name, version, method, payload)``。"""
        if method not in self.METHODS:
            raise ServiceError(
                f"服务 {self._service_name} 不支持方法 {method!r}, "
                f"允许值: {', '.join(self.METHODS) or '(无)'}"
            )
        params = payload if isinstance(payload, dict) else {}
        handler = getattr(self, f"_method_{method}", None)
        if handler is None:
            raise ServiceError(
                f"服务 {self._service_name} 的方法 {method} 未绑定实现"
            )
        try:
            return handler(params)
        except ServiceError:
            raise
        except Exception as exc:
            # 服务层不把 Python 异常抛给插件调用方，但要留诊断（含错误类型/堆栈）
            info = classify(exc)
            diagnostics.record(
                info.error_code,
                f"服务 {self._service_name}.{method} 调用失败",
                exc,
            )
            return error_payload(exc)

    @staticmethod
    def _payload_page(payload: dict) -> object:
        return payload.get("page", 1)


# ---------------------------------------------------------------------------
# jm.search
# ---------------------------------------------------------------------------


class _SearchService(_HostService):
    METHODS = ("search", "get_page")

    # 会话不放在服务里：按调用方 key 从 searcher 取（单一所有者 + 隔离）。
    # 插件经 ctx.call_service 调用时 payload 带 _caller="plugin:<id>"；
    # 主界面/未带 caller 的调用一律用 "main"。

    def _search(self, payload: dict) -> dict:
        key = payload.get("_caller") or payload.get("_session_key") or "main"
        return searcher.get_session(key).search(
            payload.get("keyword", ""), self._payload_page(payload)
        )

    def _method_search(self, payload: dict) -> dict:
        return self._search(payload)

    def _method_get_page(self, payload: dict) -> dict:
        # 翻页：keyword 可缺省，page>1 时改用本调用方会话的快照关键词
        return self._search(payload)


# ---------------------------------------------------------------------------
# jm.detail
# ---------------------------------------------------------------------------


class _DetailService(_HostService):
    METHODS = ("get_detail",)

    def _method_get_detail(self, payload: dict) -> dict:
        return detail.get_detail(payload.get("book_id", ""))


# ---------------------------------------------------------------------------
# jm.download
# ---------------------------------------------------------------------------


class _DownloadService(_HostService):
    METHODS = ("start_batch", "cancel_batch", "get_progress")

    def _method_start_batch(self, payload: dict) -> dict:
        return downloader.start_batch(
            payload.get("book_ids") or [],
            payload.get("target_dir") or "",
            work_dir=payload.get("work_dir") or None,
        )

    def _method_cancel_batch(self, payload: dict) -> dict:
        batch_id = str(payload.get("batch_id") or "")
        if not downloader.cancel_batch(batch_id):
            return make_error(ERROR_BATCH_NOT_FOUND, f"找不到下载批次: {batch_id}")
        return {"ok": True, "batch_id": batch_id, "cancelled": True}

    def _method_get_progress(self, payload: dict) -> dict:
        return downloader.get_batch_status(str(payload.get("batch_id") or ""))


# ---------------------------------------------------------------------------
# jm.account（阶段 3 实现）
# ---------------------------------------------------------------------------


class _AccountService(_HostService):
    METHODS = ("login_status", "ensure_login")

    def call(self, method: str, payload: dict) -> dict:
        """账号功能未实现：返回结构化失败，**不抛异常**（否则插件一调就炸）。"""
        if method not in self.METHODS:
            raise ServiceError(
                f"服务 {self._service_name} 不支持方法 {method!r}, "
                f"允许值: {', '.join(self.METHODS) or '(无)'}"
            )
        return {
            "ok": False,
            "error_code": ERROR_NOT_AVAILABLE,
            "error_message": "账号功能阶段3实现",
        }


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


def call_service(name: str, version: int, method: str, payload: dict) -> dict:
    """宿主/插件统一的服务调用入口（技术方案 2.2 的签名）。

    异常:
        ServiceError —— 服务名/版本不存在，或服务对象没有 call 方法
    """
    provider = REGISTRY.resolve(name, version)
    call = getattr(provider, "call", None)
    if not callable(call):
        raise ServiceError(f"服务 {name}/版本 {version} 的提供者不支持 call 方法")
    return call(method, payload)
