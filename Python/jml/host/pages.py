"""插件结构化页面协议（技术方案第三节方案 A）——注册表与路由。

纪律（安全与稳定红线）：

- 插件**只能**通过 ``PluginContext.register_page / register_page_action /
  register_page_image`` 注册自己 manifest ``pages[]`` 里声明过的页面；
  宿主在构造 ctx 时已把 plugin_id 绑定进闭包，插件没有参数能改它，
  因此不可能注册/覆盖别的插件的页面；
- 所有注册都在 :class:`PageRegistry` 的显式 RLock 内完成（旧缺陷 #10：
  不依赖 GIL 侥幸），读写路径都加锁；
- 页面数据一律是纯 JSON 可序列化 dict；插件**不得**返回任意界面描述
  （XAML 等），模板只能是 ``list`` / ``browser`` / ``action`` / ``form``，
  由 manifest 决定（本模块从 manifest 取，不读插件返回值）；
- 图片走独立入口 :func:`fetch_page_image` 返回原始 bytes，**不** base64
  塞进 JSON。

三个路由函数都按“插件已加载 → 页面已声明 → 钩子已注册”的顺序校验，
失败分别映射：``NOT_FOUND`` / ``NOT_IMPLEMENTED``（见 jml.core.errors）。
"""

from __future__ import annotations

import threading
from typing import Callable

from jml.core.errors import InvalidDataError, NotFoundError
from jml.core.protocol import PAGE_TEMPLATES, PluginError

#: 页面加载器：``loader(params: dict) -> dict``（模板 payload）
PageLoader = Callable[[dict], dict]

#: 页面动作处理器：``handler(payload: dict) -> dict``（可含 refresh/data/message）
PageActionHandler = Callable[[dict], dict]

#: 页面图片解析器：``resolver(index: int) -> bytes | None``
PageImageResolver = Callable[[int], object]


class PageError(PluginError):
    """页面注册非法（页面未在 manifest 声明、钩子不是可调用对象等）。

    继承 PluginError：插件 setup 期抛出 → 该插件加载失败并在插件管理界面
    显示明确原因（与 manifest 校验失败同一条处理路径）。
    """


class PageRegistry:
    """线程安全的页面钩子注册表（宿主全局唯一，:data:`PAGE_REGISTRY`）。

    键一律带上 plugin_id：注册时由宿主绑定的 plugin_id 决定归属，
    插件无法通过任何入口指定别人的 plugin_id。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # {(plugin_id, page_id): loader}
        self._loaders: dict[tuple[str, str], PageLoader] = {}
        # {(plugin_id, page_id, action_id): handler}
        self._actions: dict[tuple[str, str, str], PageActionHandler] = {}
        # {(plugin_id, page_id): resolver}
        self._images: dict[tuple[str, str], PageImageResolver] = {}

    # -- 写路径（全部加锁） -----------------------------------------------

    def register_loader(self, plugin_id: str, page_id: str, loader: PageLoader) -> None:
        """登记页面加载器（同键重复注册覆盖旧值，便于插件重新装配）。"""
        _require_identity(plugin_id, page_id)
        self._require_callable(loader, "页面加载器")
        with self._lock:
            self._loaders[(plugin_id, page_id)] = loader

    def register_action(
        self,
        plugin_id: str,
        page_id: str,
        action_id: str,
        handler: PageActionHandler,
    ) -> None:
        """登记页面动作处理器（按 action_id 区分）。"""
        _require_identity(plugin_id, page_id)
        if not isinstance(action_id, str) or not action_id:
            raise PageError("页面动作 id 必须是非空字符串")
        self._require_callable(handler, "页面动作处理器")
        with self._lock:
            self._actions[(plugin_id, page_id, action_id)] = handler

    def register_image(self, plugin_id: str, page_id: str, resolver: PageImageResolver) -> None:
        """登记页面图片解析器（按 index 返回原图 bytes）。"""
        _require_identity(plugin_id, page_id)
        self._require_callable(resolver, "页面图片解析器")
        with self._lock:
            self._images[(plugin_id, page_id)] = resolver

    # -- 读路径（也加锁） -------------------------------------------------

    def loader_for(self, plugin_id: str, page_id: str) -> PageLoader | None:
        with self._lock:
            return self._loaders.get((plugin_id, page_id))

    def action_for(
        self, plugin_id: str, page_id: str, action_id: str
    ) -> PageActionHandler | None:
        with self._lock:
            return self._actions.get((plugin_id, page_id, action_id))

    def image_for(self, plugin_id: str, page_id: str) -> PageImageResolver | None:
        with self._lock:
            return self._images.get((plugin_id, page_id))

    # -- 生命周期 ---------------------------------------------------------

    def clear(self) -> None:
        """清空全部注册（测试隔离与重新装配用）。"""
        with self._lock:
            self._loaders.clear()
            self._actions.clear()
            self._images.clear()

    def clear_plugin(self, plugin_id: str) -> None:
        """清空单个插件的全部页面注册（卸载插件用）。"""
        with self._lock:
            for key in [k for k in self._loaders if k[0] == plugin_id]:
                self._loaders.pop(key, None)
            for key in [k for k in self._actions if k[0] == plugin_id]:
                self._actions.pop(key, None)
            for key in [k for k in self._images if k[0] == plugin_id]:
                self._images.pop(key, None)

    def registered_pages(self) -> list[tuple[str, str]]:
        """已登记加载器的 ``(plugin_id, page_id)`` 快照（诊断与测试用）。"""
        with self._lock:
            return sorted(self._loaders)

    @staticmethod
    def _require_callable(value: object, label: str) -> None:
        if not callable(value):
            raise PageError(f"{label}必须是可调用对象，实际为 {type(value).__name__}")


def _require_identity(plugin_id: str, page_id: str) -> None:
    if not isinstance(plugin_id, str) or not plugin_id:
        raise PageError("页面注册缺少插件 id")
    if not isinstance(page_id, str) or not page_id:
        raise PageError(f"插件 {plugin_id} 的页面 id 必须是非空字符串")


#: 模块级单例注册表（宿主全局唯一）
PAGE_REGISTRY = PageRegistry()


def reset() -> None:
    """重置模块级单例（测试隔离 / 重新装配用）。"""
    PAGE_REGISTRY.clear()


# ---------------------------------------------------------------------------
# 路由：插件已加载 → 页面已声明 → 钩子已注册
# ---------------------------------------------------------------------------


def require_declared_page(plugin_id: str, page_id: str) -> tuple[dict, dict]:
    """校验插件已加载且页面在该插件 manifest 的 ``pages[]`` 里。

    返回 ``(manifest, page)``；任一环节不满足抛 :class:`NotFoundError`
    （错误码 ``NOT_FOUND``），消息中文且含插件 id / 页面 id。
    """
    pid = plugin_id if isinstance(plugin_id, str) else ""
    page_key = page_id if isinstance(page_id, str) else ""
    if not pid.strip():
        raise NotFoundError("插件 id 不能为空")
    if not page_key.strip():
        raise NotFoundError("页面 id 不能为空")

    # 惰性 import：loader 顶层会 import 本模块，这里必须避免循环导入
    from jml.host import loader

    manifest = None
    for item in loader.loaded_plugins():
        if item.get("id") == pid:
            manifest = item
            break
    if manifest is None:
        raise NotFoundError(f"插件未加载或不存在: {pid}")

    for page in manifest.get("pages", []):
        if page.get("id") == page_key:
            return manifest, page

    raise NotFoundError(f"插件 {pid} 未声明页面: {page_key}")


def load_page(plugin_id: str, page_id: str, params: dict) -> dict:
    """执行页面加载钩子，返回完整信封（模板与标题来自 manifest）。

    返回::

        {"ok": true, "template": "list|browser|action|form",
         "title": "<manifest 声明标题>", "data": {<插件返回的模板 payload>}}

    异常:
        NotFoundError           —— 插件未加载 / 页面未声明
        NotImplementedError     —— 插件未注册该页面的加载钩子
        InvalidDataError        —— 插件返回值不是 dict（协议违约，INVALID_DATA）
    """
    manifest, page = require_declared_page(plugin_id, page_id)
    template = page.get("template", "")
    if template not in PAGE_TEMPLATES:
        # manifest 校验已拦截，这里是防御；不把插件数据当界面描述直接执行
        raise InvalidDataError(
            f"插件 {manifest['id']} 的页面 {page_id} 模板非法: {template!r}"
        )

    loader_fn = PAGE_REGISTRY.loader_for(manifest["id"], page_id)
    if loader_fn is None:
        raise NotImplementedError(
            f"插件 {manifest['id']} 的页面 {page_id} 未实现加载钩子"
            "（请在 setup 中调用 ctx.register_page）"
        )

    payload = loader_fn(dict(params or {}))
    if not isinstance(payload, dict):
        raise InvalidDataError(
            f"插件 {manifest['id']} 的页面 {page_id} 加载钩子必须返回 dict，"
            f"实际为 {type(payload).__name__}"
        )

    return {
        "ok": True,
        "template": template,
        "title": page.get("title", ""),
        "data": payload,
    }


def run_page_action(plugin_id: str, page_id: str, action_id: str, payload: dict) -> dict:
    """执行页面动作钩子，返回统一动作信封。

    插件 handler 返回的 dict 可含四个键（其余键忽略）::

        {"refresh": bool, "data": dict | null, "message": str, "intent": dict}

    - ``refresh=true`` 表示界面应整页重新加载（``plugin_page_load``）；
    - ``data`` 为该页面模板的新 payload（可为 null）；
    - ``message`` 为状态文案；
    - ``intent``（阶段 4-4 扩展）为**给 C# 的转发意图**，例如收藏夹的
      ``{"kind": "detail", "book_id": "123"}` / ``{"kind": "download",
      "book_ids": ["123"]}``；宿主原样透传，不解释含义（C# 只转发给主窗口，
      不做业务判断）。没有 intent 时信封里不出现该键。

    返回::

        {"ok": true, "refresh": bool, "data": dict | null, "message": str
         [, "intent": dict]}
    """
    manifest, _page = require_declared_page(plugin_id, page_id)
    identity = action_id if isinstance(action_id, str) else ""
    handler = PAGE_REGISTRY.action_for(manifest["id"], page_id, identity) if identity else None
    if handler is None:
        raise NotImplementedError(
            f"插件 {manifest['id']} 的页面 {page_id} 未实现动作钩子: {identity!r}"
            "（请在 setup 中调用 ctx.register_page_action）"
        )

    result = handler(dict(payload or {}))
    if not isinstance(result, dict):
        raise InvalidDataError(
            f"插件 {manifest['id']} 的页面 {page_id} 动作 {identity} 必须返回 dict，"
            f"实际为 {type(result).__name__}"
        )

    data = result.get("data")
    envelope = {
        "ok": True,
        "refresh": bool(result.get("refresh", False)),
        "data": data if isinstance(data, dict) else None,
        "message": str(result.get("message", "") or ""),
    }
    intent = result.get("intent")
    if isinstance(intent, dict) and intent:
        # 载荷原样透传：kind / book_id / book_ids 的含义由插件与 C# 约定，
        # 宿主管线不解释（避免把业务判断搬回宿主）。
        envelope["intent"] = intent
    return envelope


def fetch_page_image(plugin_id: str, page_id: str, index: int) -> bytes | None:
    """取页面图片原图字节；任何失败返回 ``None``（对应 ``bytes | None`` 契约）。

    与 ``fetch_cover`` 同风格：**不抛异常、不返回 JSON**，调用方（C#）拿到
    ``None`` 时显示图片占位。失败原因写入诊断环表。
    """
    try:
        manifest, _page = require_declared_page(plugin_id, page_id)
        resolver = PAGE_REGISTRY.image_for(manifest["id"], page_id)
        if resolver is None:
            return None
        try:
            position = int(index)
        except (TypeError, ValueError):
            return None
        value = resolver(position)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        return None
    except Exception as exc:
        _record_image_failure(plugin_id, page_id, exc)
        return None


def _record_image_failure(plugin_id: str, page_id: str, exc: BaseException) -> None:
    """图片入口失败写诊断（绝不让异常穿到 C#，与 fetch_cover 一致）。"""
    try:
        from jml.core import diagnostics

        diagnostics.record(
            "PAGE_IMAGE_FAILED",
            f"页面图片入口异常: plugin_id={plugin_id} page_id={page_id}",
            exc,
        )
    except Exception:  # pragma: no cover - 诊断不可用时也不能让入口崩
        pass
