"""JML Python 侧后端入口（C# 经 pythonnet 调用）。

定位说明（重要）：
- 本模块是 JML 新项目自己的后端代码，严禁 import 旧项目
  （jm_app / jmhost_api / plugins 等），这是"零代码耦合"纪律的硬性要求。
- 阶段 2：在阶段 1 插件宿主之上挂载下载 / 搜索 / 详情 / 封面业务。

对外接口（除 ``health_check`` / ``version`` / ``fetch_cover`` 外，
**返回值一律是 ``json.dumps`` 出来的字符串**，``ensure_ascii=False``）::

    health_check() -> dict                         运行时与依赖自检（xUnit 读 dict，保持）
    version() -> str                               后端代码版本
    bootstrap(plugins_dir: str) -> str             宿主引导（幂等），返回 JSON
    list_plugins(plugins_dir: str) -> str          只扫描不加载，返回 JSON
    set_event_sink(callback) -> str                注册事件出口；callback(事件 JSON 字符串)
    clear_event_sink() -> str                      移除事件出口
    parse_book_ids(text: str) -> str               编号解析，返回 JSON
    start_download(book_ids_json: str, target_dir: str) -> str
                                                   登记批次后**立即返回** JSON（后台线程下载）
    cancel_download(batch_id: str) -> str          真取消（set 取消事件，不等待）
    search(keyword: str, page: int) -> str         搜索/翻页，返回 JSON
    get_detail(book_id: str) -> str                详情，返回 JSON
    fetch_cover(book_id: str) -> bytes | None      封面原图字节（唯一非 JSON 入口）
    emit_test_event(event_type: str, payload: dict) -> str
                                                   测试入口，返回 JSON

纪律：
- 入口函数**不向 C# 抛 Python 异常**：内部 catch 后返回
  ``{"ok": false, "error_code": ..., "error_message": ...}``；
- 共享状态放模块级 ``_STATE``，读写一律 ``threading.RLock``；
- 不硬编码任何绝对路径、不写凭据、日志/错误文案经 ``core.errors`` 脱敏。
"""

from __future__ import annotations

import json
import threading

from jml.core import (
    book_ids,
    cover,
    detail,
    diagnostics,
    downloader,
    jm_source,
    searcher,
)
from jml.core.errors import (
    ERROR_BATCH_NOT_FOUND,
    ERROR_INVALID_ARGUMENT,
    classify,
    error_payload,
    make_error,
)

__version__ = "0.3.0"

# ---------------------------------------------------------------------------
# 模块级单例状态（显式 dict + 锁，对应旧缺陷 #10 的显式加锁纪律）
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.RLock()

#: 只用于串行化 bootstrap 装配；不阻塞 set_event_sink 等 _STATE 读写
_BOOTSTRAP_LOCK = threading.Lock()

#: 宿主状态。键:
#:   bootstrapped   —— 是否已执行过 bootstrap（幂等标记）
#:   result         —— 上一次 bootstrap 的返回值（dict，重复调用原样返回）
#:   plugins_dir    —— 上一次 bootstrap 使用的插件目录
#:   event_callback —— C# 传进来的委托（强引用，防止被 GC）
#:   event_sink     —— 注册到事件总线的 sink（闭包，强引用）
#: 搜索会话不放在这里：统一由 ``jml.core.searcher.get_shared_session()``
#: 持有（后端入口与 jm.search 服务共用同一关键词快照，避免双所有者）。
_STATE: dict = {
    "bootstrapped": False,
    "result": None,
    "plugins_dir": None,
    "event_callback": None,
    "event_sink": None,
}


def _dumps(value: object) -> str:
    """统一 JSON 序列化（中文不转义）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _failure(exc: BaseException) -> str:
    """异常 → 失败形态 JSON（入口函数兜底用）。"""
    return _dumps(error_payload(exc))


# ---------------------------------------------------------------------------
# 自检接口（保持原样，xUnit 依赖 dict 返回值）
# ---------------------------------------------------------------------------


def health_check() -> dict:
    """返回 Python 运行时与关键依赖的可用性。

    供 C# 侧在启动时做桥接冒烟验证，不发起任何网络请求。
    注意：本函数**保持返回 dict**（JML.Tests 的 xUnit 用例直接读字典字段）；
    其余 C# 可调用入口已统一改为 JSON 字符串。
    """
    import platform
    import sys

    result = {
        "ok": True,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "backend_version": __version__,
        "packages": {},
        "errors": [],
    }

    # 逐个探测依赖，单个库缺失不影响其它库的探测结果。
    # jmcomic 走 jm_source（唯一 import jmcomic 的模块），本入口不直接 import。
    for name in ("jmcomic", "PIL", "yaml", "curl_cffi", "Crypto"):
        try:
            if name == "jmcomic":
                version = jm_source.jmcomic_version()
                if not version:
                    raise ImportError("jmcomic 不可用")
            else:
                module = __import__(name)
                version = getattr(module, "__version__", None)
            result["packages"][name] = version if version else "available"
        except Exception as exc:  # 只记类型与消息，不打印堆栈（避免泄漏细节）
            result["packages"][name] = None
            result["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            result["ok"] = False

    return result


def version() -> str:
    """返回后端代码版本。"""
    return __version__


# ---------------------------------------------------------------------------
# 插件宿主入口（阶段 1；阶段 2 起返回 JSON 字符串）
# ---------------------------------------------------------------------------


def _host_failure(exc: BaseException) -> dict:
    """宿主级失败的统一 JSON 结构（bootstrap/list_plugins 共用）。"""
    info = classify(exc)
    return {
        "ok": False,
        "loaded": [],
        "failed": [{"id": "(host)", "reason": info.error_message}],
        "services": {},
        "error_code": info.error_code,
        "error_message": info.error_message,
    }


def bootstrap(plugins_dir: str) -> str:
    """宿主引导：注册主程序服务 + 装配插件目录，返回 JSON 字符串。

    返回结构::

        {
          "ok": bool,
          "loaded":  [ {"id", "name", "version", "pages": [...],
                        "development_sample": bool} ],
          "failed":  [ {"id", "reason"} ],
          "services": {"archive_reader": 1, "jm.search": 1, ...}
        }

    幂等：成功装配后第二次调用直接返回上次结果；**失败结果不缓存**，
    下一次调用会重试（便于用户修好插件目录后再点重试）。
    锁纪律：``_STATE_LOCK`` 只做缓存判断与提交，读盘/import/setup 在锁外，
    不阻塞 UI 线程的 ``set_event_sink``。
    """
    try:
        with _STATE_LOCK:
            if _STATE["bootstrapped"]:
                return _dumps(_STATE["result"])

        with _BOOTSTRAP_LOCK:
            # 双检：排队期间别的调用可能已经装配成功
            with _STATE_LOCK:
                if _STATE["bootstrapped"]:
                    return _dumps(_STATE["result"])

            from jml.host import loader
            from jml.services.host_services import register_host_services

            # 1. 主程序服务注册（jm.search / jm.detail / jm.download / jm.account）
            register_host_services()

            # 2. 装配插件（discover → topo → import + setup），锁外执行
            try:
                report = loader.load_all(plugins_dir)
            except Exception as exc:
                # 整体流程级失败（如 plugins_dir 不存在 / 依赖环）
                return _dumps(_host_failure(exc))

            result = {
                "ok": True,
                "loaded": report["loaded"],
                "failed": report["failed"],
                "services": report["services"],
            }
            with _STATE_LOCK:
                _STATE["bootstrapped"] = True
                _STATE["result"] = result
                _STATE["plugins_dir"] = plugins_dir
            return _dumps(result)
    except Exception as exc:  # 注册表等意外异常同样收口
        return _dumps(_host_failure(exc))


def list_plugins(plugins_dir: str) -> str:
    """只扫描 + 校验 manifest，不导入不装配，返回 JSON 字符串。

    供插件管理界面的"重新扫描"使用；失败时仍返回同构结构并附
    ``error_code`` / ``error_message``。

    注意：连宿主模块 import 都放在 try 内——宿主模块损坏时也要返回失败
    JSON，而不是把 ImportError 漏给 C#（契约 §1 入口不抛异常）。
    """
    try:
        from jml.host import loader
        from jml.host.registry import REGISTRY

        manifests = loader.discover(plugins_dir)
        loaded = [
            {
                "id": manifest["id"],
                "name": manifest["name"],
                "version": manifest["version"],
                "pages": list(manifest["pages"]),
                "development_sample": manifest["development_sample"],
            }
            for manifest in manifests
        ]
        # discover 阶段的坏 manifest → failed（loader 只读快照，不摸私有状态）
        failed = []
        for err in loader.recent_errors():
            dir_name, _, reason = err.partition(": ")
            failed.append({"id": dir_name, "reason": reason})
        return _dumps(
            {
                "ok": True,
                "loaded": loaded,
                "failed": failed,
                "services": REGISTRY.names(),
            }
        )
    except Exception as exc:
        return _dumps(_host_failure(exc))


# ---------------------------------------------------------------------------
# 事件下沉（Python → C#）
# ---------------------------------------------------------------------------


def set_event_sink(callback) -> str:
    """注册事件汇聚出口；``callback`` 是 C# 传进来的委托。

    回调签名：``callback(event_json: str)``，其中 ``event_json`` 形如::

        {"type": "download.log", "payload": { ... }}

    实现要点：
    - sink 在**所有 Python 订阅者投递完之后**才被调用（events.dispatch 内保证）；
    - 回调抛异常由事件总线记录，不影响订阅者投递；
    - 模块级状态持强引用，防止 pythonnet 委托被 GC。
    """
    try:
        if callback is None or not callable(callback):
            return _dumps(make_error(ERROR_INVALID_ARGUMENT, "事件回调必须是可调用对象"))

        from jml import events

        def _sink(event_type: str, payload: dict) -> None:
            try:
                text = _dumps({"type": event_type, "payload": payload})
            except Exception as exc:  # 序列化兜底：绝不让事件层把业务线程带崩
                diagnostics.record(
                    "EVENT_SERIALIZE_FAILED",
                    f"事件 {event_type} 序列化失败",
                    exc,
                )
                text = _dumps(
                    {
                        "type": event_type,
                        "payload": {
                            "type": event_type,
                            "serialization_error": True,
                        },
                    }
                )
            callback(text)

        with _STATE_LOCK:
            _STATE["event_callback"] = callback
            _STATE["event_sink"] = _sink
        events.BUS.set_sink(_sink)
        return _dumps({"ok": True})
    except Exception as exc:
        return _failure(exc)


def clear_event_sink() -> str:
    """移除事件汇聚出口（C# 窗口关闭时调用）。"""
    try:
        from jml import events

        with _STATE_LOCK:
            _STATE["event_callback"] = None
            _STATE["event_sink"] = None
        events.BUS.clear_sink()
        return _dumps({"ok": True})
    except Exception as exc:
        return _failure(exc)


# ---------------------------------------------------------------------------
# 业务入口（阶段 2）
# ---------------------------------------------------------------------------


def parse_book_ids(text: str) -> str:
    """解析批量编号输入，返回 JSON。

    成功::

        {"ok": true, "book_ids": ["123", "456"], "count": 2}

    空串 / 无有效数字 → ``book_ids`` 为空列表（仍算成功，不算错误）。
    """
    try:
        values = book_ids.parse_book_ids(text)
        return _dumps({"ok": True, "book_ids": values, "count": len(values)})
    except Exception as exc:
        return _failure(exc)


def start_download(
    book_ids_json: str, target_dir: str, work_dir: str = ""
) -> str:
    """登记下载批次并**立即返回**（后台线程跑下载，不阻塞 C#）。

    参数:
        book_ids_json: JSON 数组字符串（如 ``"[123, 456]"``）或纯文本编号串；
        target_dir: 下载目录（由调用方传入，不硬编码）；
        work_dir: 可选临时工作目录父目录（缺省用 ``target_dir``，不硬编码）。

    成功::

        {"ok": true, "batch_id": str, "total": int, "status": "running"}

    失败：``{"ok": false, "error_code", "error_message"}``
    """
    try:
        values = _coerce_book_ids(book_ids_json)
        if not values:
            return _dumps(make_error(ERROR_INVALID_ARGUMENT, "编号列表为空"))
        info = downloader.start_batch(
            values,
            target_dir,
            work_dir=work_dir or None,
        )
        return _dumps(
            {
                "ok": True,
                "batch_id": info["batch_id"],
                "total": info["total"],
                "status": info["status"],
            }
        )
    except Exception as exc:
        return _failure(exc)


def cancel_download(batch_id: str) -> str:
    """取消下载批次（只 set 取消事件，立即返回，不等待下载线程退出）。"""
    try:
        identity = str(batch_id or "").strip()
        if not identity:
            return _dumps(make_error(ERROR_INVALID_ARGUMENT, "批次号不能为空"))
        if not downloader.cancel_batch(identity):
            return _dumps(make_error(ERROR_BATCH_NOT_FOUND, f"找不到下载批次: {identity}"))
        return _dumps({"ok": True, "batch_id": identity, "cancelled": True})
    except Exception as exc:
        return _failure(exc)


def search(keyword: str, page: int, session_key: str = "main") -> str:
    """搜索/翻页，返回 JSON。

    翻页语义：``page <= 1`` 视为发起新搜索并快照关键词；``page > 1``
    使用**本调用方 key 的**会话快照关键词（旧缺陷 #8 的修正）。
    ``session_key`` 默认 ``"main"``（主界面）；插件由 host 传
    ``plugin:<plugin_id>``，与主界面互不串页。

    成功::

        {"ok": true, "keyword": str, "page": int, "total_pages": int,
         "results": [{"book_id","title","author","page_count",
                      "tags","cover_url"}]}
    """
    try:
        return _dumps(searcher.get_session(session_key).search(keyword, page))
    except Exception as exc:
        return _failure(exc)


def set_log_dir(path: str) -> str:
    """设置诊断落盘目录（可选；缺省只在内存保留）。

    C# 后续可传入 ``%LOCALAPPDATA%\\JML\\logs`` 之类的自有目录；本入口
    不做任何路径推导/硬编码，传空串表示关闭落盘。
    """
    try:
        diagnostics.set_log_dir(path or None)
        return _dumps({"ok": True, "log_dir": diagnostics.current_log_dir() or ""})
    except Exception as exc:
        return _failure(exc)


def get_diagnostics(limit: int = 100) -> str:
    """读取诊断环表（已脱敏）。

    返回::

        {"ok": true, "records": ["[...] ...", ...], "count": int}

    ``limit <= 0`` 表示全部（环表上限 200 条）；默认最近 100 条。
    """
    try:
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = 100
        records = diagnostics.recent(None if count <= 0 else count)
        return _dumps({"ok": True, "records": records, "count": len(records)})
    except Exception as exc:
        return _failure(exc)


def get_detail(book_id: str) -> str:
    """本子详情，返回 JSON（简介 200 字截断在 Python 侧完成）。"""
    try:
        return _dumps(detail.get_detail(book_id))
    except Exception as exc:
        return _failure(exc)


def fetch_cover(book_id: str) -> bytes | None:
    """封面原图 bytes；失败返回 None（唯一非 JSON 入口），同时写诊断。"""
    try:
        return cover.fetch_cover(book_id)
    except Exception as exc:
        diagnostics.record(
            "COVER_FETCH_FAILED",
            f"封面入口异常: book_id={book_id}",
            exc,
        )
        return None


def emit_test_event(event_type: str, payload: dict) -> str:
    """测试入口：向事件总线派发一个事件，返回 JSON。

    成功::

        {"ok": true, "delivered": int, "errors": [str, ...]}

    校验失败（未知类型/缺字段/裸 dict）时 ok=false 且 errors 含原因，
    不抛异常（便于 C# 侧直接展示）。
    """
    try:
        from jml import events

        delivered = events.BUS.dispatch(event_type, payload)
    except Exception as exc:
        info = classify(exc)
        return _dumps(
            {
                "ok": False,
                "delivered": 0,
                "errors": [f"{info.error_code}: {info.error_message}"],
            }
        )
    return _dumps(
        {
            "ok": True,
            "delivered": delivered,
            "errors": list(events.BUS.last_dispatch_errors()),
        }
    )


def reset_host_state() -> None:
    """重置宿主单例状态（仅测试与开发自检用，C# 不应调用）。

    清空 bootstrap 幂等标记、事件出口、搜索会话、服务注册表、事件总线、
    loader 内部状态、真实数据源单例与下载批次登记。
    """
    with _STATE_LOCK:
        _STATE["bootstrapped"] = False
        _STATE["result"] = None
        _STATE["plugins_dir"] = None
        _STATE["event_callback"] = None
        _STATE["event_sink"] = None

    from jml import events
    from jml.host import loader, registry

    registry.reset()
    events.reset()
    loader.reset()
    searcher.reset_shared_session()
    jm_source.reset_source()
    downloader.reset()
    diagnostics.set_log_dir(None)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _coerce_book_ids(value) -> list[str]:
    """把 C#/插件传来的编号参数交给 ``book_ids.normalize_ids`` 严格校验。

    接受三种形态：
    - JSON 数组字符串（``"[1, 2, 3]"``）；
    - 纯文本编号串（``"1-2"``）；
    - Python list/tuple（jm.download 服务/测试直接传）。

    非纯数字 token 一律由 ``normalize_ids`` 抛 ``InvalidArgumentError``，
    绝不静默丢弃后继续下载。
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return book_ids.normalize_ids(parsed)
        if parsed is not None:
            return book_ids.normalize_ids(str(parsed))
        return book_ids.normalize_ids(text)

    return book_ids.normalize_ids(value)


if __name__ == "__main__":
    # 允许单独在命令行执行自检：python jml_backend.py [plugins_dir]
    import sys

    print(_dumps(health_check()))
    if len(sys.argv) > 1:
        print(bootstrap(sys.argv[1]))
