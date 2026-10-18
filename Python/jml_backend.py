"""JML Python 侧后端入口（C# 经 pythonnet 调用）。

定位说明（重要）：
- 本模块是 JML 新项目自己的后端代码，严禁 import 旧项目
  （jm_app / jmhost_api / plugins 等），这是"零代码耦合"纪律的硬性要求。
- 阶段 2：在阶段 1 插件宿主之上挂载下载 / 搜索 / 详情 / 封面业务。

对外接口（除 ``health_check`` / ``version`` / ``fetch_cover`` 外，
**返回值一律是 ``json.dumps`` 出来的字符串**，``ensure_ascii=False``）::

    health_check() -> dict                         运行时与依赖自检（xUnit 读 dict，保持）
    version() -> str                               后端代码版本
    bootstrap(plugins_dir: str, disabled_json: str = "") -> str
                                                   宿主引导（幂等），返回 JSON
    list_plugins(plugins_dir: str, disabled_json: str = "") -> str
                                                   只扫描不加载，返回 JSON
    set_plugin_enabled(plugin_id: str, enabled: bool) -> str
                                                   插件启用/停用（写 disabled.json），返回 JSON
    get_plugin_management_state(plugins_dir: str) -> str
                                                   插件管理状态（含 dependents），返回 JSON
    plugin_page_load(plugin_id: str, page_id: str, params_json: str) -> str
                                                   结构化页面协议：载入页面，返回 JSON
    plugin_page_action(plugin_id: str, page_id: str, action_id: str,
                       payload_json: str) -> str   结构化页面协议：执行页面动作，返回 JSON
    plugin_page_image(plugin_id: str, page_id: str, index: int) -> bytes | None
                                                   结构化页面协议：图片原图字节（非 JSON 入口）
    list_plugin_settings() -> str                  已加载插件的设置定义，返回 JSON
    set_plugin_setting(plugin_id: str, key: str, value_json: str) -> str
                                                   写插件设置（按 schema 校验 + 落盘），返回 JSON
    set_event_sink(callback) -> str                注册事件出口；callback(事件 JSON 字符串)
    clear_event_sink() -> str                      移除事件出口
    set_data_root(config_root: str, plugin_data_root: str, log_dir: str = "") -> str
                                                   注入 C# 选定的数据根（实测可写），返回 JSON
    set_log_dir(path: str) -> str                  设置诊断落盘目录，返回 JSON
    write_process_diagnostics(diagnostic_path: str, log_path: str = "",
                              download_dir: str = "", temp_root: str = "") -> str
                                                   追加 Python 进程诊断（runtime-data-root.txt）
    parse_book_ids(text: str) -> str               编号解析，返回 JSON
    start_download(book_ids_json: str, target_dir: str, work_dir: str = "") -> str
                                                   登记批次后**立即返回** JSON（后台线程下载）
    cancel_download(batch_id: str) -> str          真取消（set 取消事件，不等待）
    get_progress(batch_id: str) -> str             查询批次状态，返回 JSON（契约 §3.16）
    search(keyword: str, page: int, session_key: str = "main") -> str
                                                   搜索/翻页，返回 JSON
    get_detail(book_id: str) -> str                详情，返回 JSON
    fetch_cover(book_id: str) -> bytes | None      封面原图字节（唯一非 JSON 入口）
    account_set_credentials(username: str, password: str) -> str
                                                   账号写入进程内存，返回 JSON
    account_login() -> str                         用内存凭据登录，返回 JSON
    account_login_with(username: str, password: str) -> str
                                                   一次性登录（不长期保留凭据）
    account_logout() -> str                        退出登录（默认保留内存凭据）
    account_status() -> str                        查询登录态（用户名打码）
    account_clear() -> str                         清空凭据与登录态（修改账号前）
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
import os
import threading

from jml.core import (
    account,
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
    ERROR_IO,
    ERROR_MESSAGES,
    ERROR_NOT_FOUND,
    ERROR_UNKNOWN,
    InvalidArgumentError,
    classify,
    error_payload,
    make_error,
    sanitize_text,
)
from jml.core.protocol import ConfigError

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
#:   disabled_ids   —— 上一次 bootstrap 使用的禁用列表（变化时重算状态）
#:   load_report    —— 上一次真实装配结果（禁用列表变化时基于它重算状态）
#:   event_callback —— C# 传进来的委托（强引用，防止被 GC）
#:   event_sink     —— 注册到事件总线的 sink（闭包，强引用）
#: 搜索会话不放在这里：统一由 ``jml.core.searcher.get_shared_session()``
#: 持有（后端入口与 jm.search 服务共用同一关键词快照，避免双所有者）。
_STATE: dict = {
    "bootstrapped": False,
    "result": None,
    "plugins_dir": None,
    "disabled_ids": [],
    "load_report": None,
    "event_callback": None,
    "event_sink": None,
}


def _dumps(value: object) -> str:
    """统一 JSON 序列化（中文不转义）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _failure(exc: BaseException) -> str:
    """异常 → 失败形态 JSON（入口函数兜底用）。"""
    return _dumps(error_payload(exc))


def _account_failure(exc: BaseException) -> str:
    """账号入口的兜底失败形态（**不拼接原始异常文案**，防凭据泄漏）。

    账号相关文本可能来自第三方登录异常，即使 ``sanitize_text`` 也可能
    漏掉裸密码；这里只回传错误码对应的固定中文文案。
    """
    info = classify(exc)
    return _dumps(
        {
            "ok": False,
            "logged_in": False,
            "username_masked": "",
            "error_code": info.error_code,
            "error_message": ERROR_MESSAGES.get(
                info.error_code, ERROR_MESSAGES[ERROR_UNKNOWN]
            ),
        }
    )


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
        "failed": [
            {
                "id": "(host)",
                "reason": info.error_message,
                "enabled": True,
                "blocked_reason": info.error_message,
            }
        ],
        "services": {},
        "error_code": info.error_code,
        "error_message": info.error_message,
    }


def bootstrap(plugins_dir: str, disabled_json: str = "") -> str:
    """宿主引导：注册主程序服务 + 装配插件目录，返回 JSON 字符串。

    参数:
        plugins_dir: 插件目录；
        disabled_json: 禁用列表（JSON 数组字符串，可为空）。**空串表示沿用
            磁盘上的 %APPDATA%/JML/plugins/disabled.json（唯一真源）**；
            非空表示本次调用显式指定的禁用集合（不落盘，供首启迁移使用）。

    返回结构::

        {
          "ok": bool,
          "loaded":  [ {"id", "name", "version", "description", "pages": [...],
                        "development_sample": bool,
                        "enabled": true, "blocked_reason": ""} ],
          "failed":  [ {"id", "reason", "enabled": bool, "blocked_reason": str} ],
          "services": {"archive_reader": 1, "jm.search": 1, ...}
        }

    幂等：成功装配后第二次调用直接返回上次结果；**失败结果不缓存**，
    下一次调用会重试（便于用户修好插件目录后再点重试）。
    禁用列表变化时不重新 import/setup（决策 #7：重启生效），只重算本次
    返回里的状态（停用项移入 failed，依赖方标记"缺少依赖插件"）。
    锁纪律：``_STATE_LOCK`` 只做缓存判断与提交，读盘/import/setup 在锁外，
    不阻塞 UI 线程的 ``set_event_sink``。
    """
    try:
        from jml.host import plugin_state

        disabled = plugin_state.resolve_disabled_ids(disabled_json)
        with _STATE_LOCK:
            if _STATE["bootstrapped"]:
                if sorted(disabled) == list(_STATE["disabled_ids"]):
                    return _dumps(_STATE["result"])

        with _BOOTSTRAP_LOCK:
            # 双检：排队期间别的调用可能已经装配成功
            with _STATE_LOCK:
                if (
                    _STATE["bootstrapped"]
                    and sorted(disabled) == list(_STATE["disabled_ids"])
                ):
                    return _dumps(_STATE["result"])
                cached = _STATE["result"] if _STATE["bootstrapped"] else None
                raw_report = (
                    _STATE["load_report"] if _STATE["bootstrapped"] else None
                )

            if cached is not None:
                # 只重算状态，不热加载（决策 #7）；基于"真实装配结果"重算，
                # 保证停用→再启用时能回到真实状态而不是丢失已加载信息。
                updated = plugin_state.refresh_bootstrap_result(
                    raw_report or cached, plugins_dir, disabled
                )
                with _STATE_LOCK:
                    _STATE["result"] = updated
                    _STATE["disabled_ids"] = sorted(disabled)
                return _dumps(updated)

            from jml.host import loader
            from jml.services.host_services import register_host_services

            # 1. 主程序服务注册（jm.search / jm.detail / jm.download / jm.account）
            register_host_services()

            # 2. 装配插件（discover → 排除停用 → topo → import + setup），锁外执行
            try:
                report = loader.load_all(plugins_dir, disabled)
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
                _STATE["disabled_ids"] = sorted(disabled)
                _STATE["load_report"] = report
            return _dumps(result)
    except Exception as exc:  # 注册表等意外异常同样收口
        return _dumps(_host_failure(exc))


def list_plugins(plugins_dir: str, disabled_json: str = "") -> str:
    """只扫描 + 校验 manifest，不导入不装配，返回 JSON 字符串。

    与 bootstrap 同构（loaded/failed/services），并给每个插件带上
    ``enabled`` 与 ``blocked_reason``；被停用插件进入 failed（enabled=false），
    依赖它们的插件以"缺少依赖插件 xxx"明确失败。
    ``disabled_json`` 语义同 :func:`bootstrap`（空串沿用磁盘真源）。

    供插件管理界面的"重新扫描"使用；失败时仍返回同构结构并附
    ``error_code`` / ``error_message``。

    注意：连宿主模块 import 都放在 try 内——宿主模块损坏时也要返回失败
    JSON，而不是把 ImportError 漏给 C#（契约 §1 入口不抛异常）。
    """
    try:
        from jml.host import loader, plugin_state
        from jml.host.registry import REGISTRY

        disabled = plugin_state.resolve_disabled_ids(disabled_json)
        manifests = loader.discover(plugins_dir)
        loaded, failed = plugin_state.scan_report(manifests, disabled)
        # discover 阶段的坏 manifest → failed（loader 只读快照，不摸私有状态）
        for err in loader.recent_errors():
            dir_name, _, reason = err.partition(": ")
            failed.append(
                {
                    "id": dir_name,
                    "reason": reason,
                    "enabled": True,
                    "blocked_reason": reason,
                }
            )
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
# 插件启用/停用入口（阶段 4-5；disabled.json 是唯一真源）
# ---------------------------------------------------------------------------


def set_plugin_enabled(plugin_id: str, enabled: bool) -> str:
    """启用/停用插件：写入 ``%APPDATA%/JML/plugins/disabled.json``。

    语义（决策 #7）：
    - 只更新持久化状态与后续 bootstrap / list_plugins 的结果；
    - **不热加载/不卸载**当前进程里已 import 的插件，重启后真正生效；
    - 写入是原子的（临时文件 + os.replace），失败返回 ``IO_ERROR``。

    返回::

        {"ok": true, "plugin_id": str, "enabled": bool,
         "status": "enabled" | "disabled"}

    失败：
    - 空 id → ``INVALID_ARGUMENT``；
    - 不在最近一次扫描结果里的 id → ``NOT_FOUND``；
    - 写盘失败 → ``IO_ERROR``。
    """
    try:
        from jml.host import loader, plugin_state

        pid = str(plugin_id or "").strip()
        if not pid:
            return _dumps(make_error(ERROR_INVALID_ARGUMENT, "插件 id 不能为空"))
        if pid not in loader.known_plugin_ids():
            return _dumps(make_error(ERROR_NOT_FOUND, f"插件不存在: {pid}"))

        disabled = set(plugin_state.load_disabled_ids())
        if enabled:
            disabled.discard(pid)
        else:
            disabled.add(pid)
        try:
            current = plugin_state.save_disabled_ids(disabled)
        except OSError as exc:
            return _dumps(make_error(ERROR_IO, f"插件停用状态写入失败: {exc}"))

        is_enabled = pid not in current
        return _dumps(
            {
                "ok": True,
                "plugin_id": pid,
                "enabled": is_enabled,
                "status": "enabled" if is_enabled else "disabled",
            }
        )
    except Exception as exc:
        return _failure(exc)


def get_plugin_management_state(plugins_dir: str) -> str:
    """返回插件管理界面所需的完整状态（每个插件一行）。

    成功::

        {"ok": true, "plugins": [
            {"id", "name", "version", "description",
             "enabled": bool,
             "status": "loaded" | "disabled" | "failed",
             "reason": str,               # 空串表示正常
             "dependents": [plugin_id, ...]}   # 直接或间接依赖它的插件
        ]}

    只扫描 manifest，不 import 插件；``status`` 依据最近一次 bootstrap
    的装配结果 + 当前 disabled.json 推导。坏 manifest 目录也会作为
    failed 行出现，避免界面凭空少一行。
    """
    try:
        from jml.host import plugin_state

        plugins = plugin_state.management_state(plugins_dir)
        return _dumps({"ok": True, "plugins": plugins})
    except Exception as exc:
        info = classify(exc)
        return _dumps(
            {
                "ok": False,
                "plugins": [],
                "error_code": info.error_code,
                "error_message": info.error_message,
            }
        )


# ---------------------------------------------------------------------------
# 插件结构化页面协议入口（技术方案第三节方案 A）
# ---------------------------------------------------------------------------


def _parse_json_object(text, label: str) -> dict:
    """把 ``*_json`` 参数解析成 dict；空串视为空 dict。

    非 JSON / 非对象一律抛 ``InvalidArgumentError``（→ INVALID_ARGUMENT），
    绝不把字符串当 dict 继续用。
    """
    if text is None:
        return {}
    value = text
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise InvalidArgumentError(f"{label}不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidArgumentError(
            f"{label}必须是 JSON 对象，实际为 {type(value).__name__}"
        )
    return value


def plugin_page_load(plugin_id: str, page_id: str, params_json: str) -> str:
    """载入插件页面，返回 JSON 字符串。

    成功::

        {"ok": true, "template": "list|browser|action|form",
         "title": "<manifest 页面标题>", "data": {<模板 payload>}}

    - ``plugin_id`` 必须是**已加载**插件，``page_id`` 必须在该插件
      manifest 的 ``pages[]`` 里，否则 ``NOT_FOUND``；
    - 插件未实现加载钩子（没有 ``ctx.register_page``）→ ``NOT_IMPLEMENTED``；
    - ``params_json`` 为空串 → 空 dict；非法 JSON / 非对象 → ``INVALID_ARGUMENT``；
    - 模板类型与标题**只来自 manifest**，插件返回值不能改变它们，
      也不能返回任意界面描述。
    """
    try:
        from jml.host import pages

        params = _parse_json_object(params_json, "页面参数")
        return _dumps(pages.load_page(plugin_id, page_id, params))
    except Exception as exc:
        return _failure(exc)


def plugin_page_action(
    plugin_id: str, page_id: str, action_id: str, payload_json: str
) -> str:
    """执行插件页面动作，返回 JSON 字符串。

    成功::

        {"ok": true, "refresh": bool, "data": {..}|null, "message": "..."}

    ``refresh=true`` 表示界面应整页重新调用 ``plugin_page_load``；
    ``data`` 为该模板的新 payload（可为 null）；``message`` 为状态文案。
    插件未实现该页面动作钩子 → ``NOT_IMPLEMENTED``。
    """
    try:
        from jml.host import pages

        payload = _parse_json_object(payload_json, "动作参数")
        return _dumps(pages.run_page_action(plugin_id, page_id, action_id, payload))
    except Exception as exc:
        return _failure(exc)


def plugin_page_image(plugin_id: str, page_id: str, index: int) -> bytes | None:
    """取插件页面图片原图 bytes（唯一非 JSON 入口之一）。

    失败（插件未加载 / 页面未声明 / 未注册图片钩子 / 解析器抛异常 /
    返回值不是 bytes）一律返回 ``None``，同时写诊断；C# 侧显示图片占位。
    """
    try:
        from jml.host import pages

        return pages.fetch_page_image(plugin_id, page_id, index)
    except Exception:  # pragma: no cover - fetch_page_image 内部已收口
        return None


# ---------------------------------------------------------------------------
# 插件设置项入口（阶段 3；schema → 主设置窗口动态表单）
# ---------------------------------------------------------------------------


def list_plugin_settings() -> str:
    """返回已加载插件的设置定义，供主设置窗口生成动态表单。

    成功::

        {"ok": true, "plugins": [
            {"plugin_id": str, "plugin_name": str,
             "fields": [{"key", "label", "type", "default", "value",
                         "options": [...], "min", "max"}（password 另有 has_value）]}
        ]}

    约束：
    - 只列**已加载且声明了非空 config.schema** 的插件（加载失败/无设置项的
      插件不出现）；顺序按 plugin_id 排序，保证界面稳定；
    - ``type`` ∈ text / password / number / bool / select / path；
    - password 项 ``value`` 恒为空串、附 ``has_value`` 布尔，**凭据值不回传**；
    - 值取插件 setup 期间实际使用的同一个配置实例（默认值已回填）。
    """
    try:
        from jml.host import loader

        plugins = []
        for manifest in loader.loaded_plugins():
            store = loader.config_for(manifest["id"])
            if store is None:
                continue
            fields = store.describe_schema()
            if not fields:
                continue
            plugins.append(
                {
                    "plugin_id": manifest["id"],
                    "plugin_name": manifest["name"],
                    "fields": fields,
                }
            )
        return _dumps({"ok": True, "plugins": plugins})
    except Exception as exc:
        return _failure(exc)


def set_plugin_setting(plugin_id: str, key: str, value_json: str) -> str:
    """写入单个插件设置项：按 manifest schema 校验，非法拒绝且保持原值。

    参数:
        plugin_id: 插件 id（必须已加载）；
        key: schema 里声明的设置项 key；
        value_json: 设置值的 JSON 文本（如 ``"\\"文本\\""`` / ``16`` / ``true``）。

    成功::

        {"ok": true, "key": str, "value": <标量>}     # password 项额外 has_value，
                                                       # value 恒为空串且不落盘

    失败：
    - 空 plugin_id / key、value_json 不是合法 JSON、类型/枚举/范围不符
      → ``INVALID_ARGUMENT``（拒绝写入，内存与磁盘上的原值都不变）；
    - 插件不存在或未加载 → ``NOT_FOUND``；
    - 校验通过但写 config.json 失败（权限/磁盘）→ ``IO_ERROR``。
    """
    try:
        pid = str(plugin_id or "").strip()
        name = str(key or "").strip()
        if not pid or not name:
            return _dumps(
                make_error(ERROR_INVALID_ARGUMENT, "插件 id 与设置项 key 不能为空")
            )

        from jml.host import loader

        loaded_ids = {manifest["id"] for manifest in loader.loaded_plugins()}
        store = loader.config_for(pid)
        if store is None or pid not in loaded_ids:
            return _dumps(make_error(ERROR_NOT_FOUND, f"插件未加载或不存在: {pid}"))

        try:
            value = json.loads(value_json) if isinstance(value_json, str) else value_json
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return _dumps(
                make_error(ERROR_INVALID_ARGUMENT, f"设置值不是合法 JSON: {exc}")
            )

        try:
            store.validate(name, value)
        except ConfigError as exc:
            # ConfigError 不是 AppError，不能交给 classify（会落到 UNKNOWN_ERROR）；
            # 校验失败统一映射成契约 §1.1 的 INVALID_ARGUMENT（拒绝写入，原值不变）。
            return _dumps(
                make_error(
                    ERROR_INVALID_ARGUMENT, _setting_error_message(store, name, exc)
                )
            )

        try:
            store.set(name, value)
        except ConfigError as exc:
            # 校验已通过，此处的 ConfigError 只可能来自落盘失败（见 _save）。
            return _dumps(make_error(ERROR_IO, _setting_error_message(store, name, exc)))

        current = store.get(name)
        if store.is_password_field(name):
            # 安全红线：password 值永不回传
            return _dumps(
                {"ok": True, "key": name, "value": "", "has_value": bool(current)}
            )
        return _dumps({"ok": True, "key": name, "value": current})
    except Exception as exc:
        return _failure(exc)


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


def get_progress(batch_id: str) -> str:
    """查询下载批次状态，返回 JSON（契约 §3.16；字段见 §四的 BatchStatus）。

    成功::

        {"ok": true, "batch_id": str, "status": "running"|"finished",
         "total": int, "cancel_requested": bool, "result": dict|null}

    失败（不向 C# 抛异常）：
    - 空批次号 → ``INVALID_ARGUMENT``；
    - 未知批次 → ``BATCH_NOT_FOUND``，``error_message`` 含传入 id。

    锁纪律：本入口无新增共享状态，不需要占用 ``_STATE_LOCK``；批次登记表由
    ``downloader`` 自己的锁保护，本入口只读取状态副本。不新增单例，也不在
    C# 侧另走宿主服务或后端私有函数。
    """
    try:
        identity = str(batch_id or "").strip()
        if not identity:
            return _dumps(make_error(ERROR_INVALID_ARGUMENT, "批次号不能为空"))
        return _dumps(downloader.get_batch_status(identity))
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


def set_data_root(
    config_root: str, plugin_data_root: str, log_dir: str = ""
) -> str:
    """注入 C# 侧选定的数据根（三级降级链的最终结果），返回 JSON。

    C# 必须在 ``PythonRuntime.Initialize()`` 之后、任何写盘入口（插件 bootstrap、
    设置写入、诊断落盘）之前调用本入口一次。参数语义：

    - ``config_root``：配置/状态根（settings 的 Python 侧对应物、插件配置、
      ``disabled.json``、插件数据目录都挂在它下面）；
    - ``plugin_data_root``：插件配置与插件数据根（通常
      ``<config_root>/plugins``，C# 侧为 ``AppPaths.PluginDataRoot``）；
    - ``log_dir``：诊断落盘目录；空串表示不落盘。

    校验规则（实测，不只看目录是否存在）：逐个目录做“创建目录 + 写临时文件 +
    删除”探针；任一必填目录不可写返回 ``INVALID_ARGUMENT``，此时**不改变**
    当前生效的数据根，所有后续写盘仍走旧根。成功后才切换，并同步设置诊断
    落盘目录。

    成功::

        {"ok": true, "config_root": str, "plugin_data_root": str, "log_dir": str}
    """
    try:
        from jml.host import paths

        config = str(config_root or "").strip()
        plugin = str(plugin_data_root or "").strip()
        logs = str(log_dir or "").strip()
        if not config or not plugin:
            return _dumps(
                make_error(
                    ERROR_INVALID_ARGUMENT,
                    "配置目录与插件数据目录不能为空",
                )
            )

        failures: list[str] = []
        for label, directory in (("配置目录", config), ("插件数据目录", plugin)):
            if not paths.probe_writable(directory):
                failures.append(f"{label}不可写：{directory}")
        if logs and not paths.probe_writable(logs):
            failures.append(f"日志目录不可写：{logs}")

        if failures:
            message = "数据目录不可写（" + "；".join(failures) + "）"
            # 诊断环表自动脱敏；这里只记录路径与级别，不含凭据。
            diagnostics.record("DATA_ROOT_INVALID", message)
            return _dumps(make_error(ERROR_INVALID_ARGUMENT, message))

        # 只有全部实测通过才切换：避免半切换导致配置写到新根、日志还在旧根。
        paths.configure(config, plugin, logs or None)
        diagnostics.set_log_dir(logs or None)
        return _dumps(
            {
                "ok": True,
                "config_root": paths.config_root(),
                "plugin_data_root": paths.plugin_data_root(),
                "log_dir": paths.log_dir() or "",
            }
        )
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


# ---------------------------------------------------------------------------
# 进程级诊断（C# 在 set_data_root 之后调用一次）
# ---------------------------------------------------------------------------

#: 追加到 runtime-data-root.txt 的节标题
_PROCESS_DIAGNOSTICS_TITLE = "—— Python 进程诊断（set_data_root 之后追加） ——"


def _one_line(value: object) -> str:
    """压成单行并脱敏（诊断正文不换行，便于 grep）。"""
    return " ".join(sanitize_text(value).split())


def _format_probe(detail: dict) -> str:
    """把 ``paths.probe_writable_detailed`` 的回执压成一行。"""
    writable = detail.get("writable")
    if writable is None:
        # 未实际探测（例如下载目录不存在）：如实说明，不冒充“可写/不可写”。
        return (
            f"结果：未探测；目录：{detail.get('directory') or '（空）'}；"
            f"原因：{_one_line(detail.get('message') or detail.get('stage') or '未指定')}"
        )

    parts = [
        f"结果：{'可写' if writable else '不可写'}",
        f"目录：{detail.get('directory') or '（空）'}",
        f"探针文件：{detail.get('probe_file') or '（空）'}",
        f"完整路径：{detail.get('probe_path') or '（空）'}",
    ]
    if not writable:
        parts.extend(
            [
                f"失败阶段：{detail.get('stage') or '未知'}",
                f"异常：{detail.get('error_type') or '无（探针返回不可写）'}",
                f"errno：{detail.get('errno')}",
                f"winerror：{detail.get('winerror')}",
                f"消息：{_one_line(detail.get('message') or '')}",
            ]
        )
    return "；".join(parts)


def _build_process_diagnostics(download_dir: str, temp_root: str) -> str:
    """收集 Python 进程与环境诊断正文（不写盘，纯文本）。"""
    import datetime
    import sys
    import tempfile

    from jml.host import paths

    probes: list[tuple[str, dict]] = []
    probes.append(
        (
            "配置根（jml.host.paths.config_root）",
            paths.probe_writable_detailed(paths.config_root()),
        )
    )
    system_temp = tempfile.gettempdir()
    probes.append(
        ("系统临时根（tempfile.gettempdir）", paths.probe_writable_detailed(system_temp))
    )
    if temp_root and os.path.abspath(temp_root) != os.path.abspath(system_temp):
        probes.append(
            (
                "下载临时根（C# AppPaths.TempDirectory）",
                paths.probe_writable_detailed(temp_root),
            )
        )

    download_note = ""
    if download_dir:
        if os.path.isdir(download_dir):
            probes.append(
                ("下载目录（C# 已保存的下载目录）", paths.probe_writable_detailed(download_dir))
            )
        else:
            # 不替用户创建下载目录：诊断只对“已存在但可能不可写”的目标做实测。
            download_note = (
                f"  [下载目录] 不存在或不是目录，跳过写探针（避免诊断创建用户目录）："
                f"{download_dir}"
            )

    lines = [
        _PROCESS_DIAGNOSTICS_TITLE,
        f"时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"sys.executable：{sys.executable}",
        f"sys.version：{_one_line(sys.version)}",
        f"os.getpid()：{os.getpid()}",
        f"os.getcwd()：{os.getcwd()}",
        "环境变量（仅目录类）：",
    ]
    for name in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "USERPROFILE"):
        lines.append(f"  {name}={_one_line(os.environ.get(name, '（未设置）'))}")

    lines.append("写探针（失败时记录异常类型 + errno + winerror + 完整路径）：")
    for label, detail in probes:
        lines.append(f"  [{label}] {_format_probe(detail)}")
    if download_note:
        lines.append(download_note)
    elif not download_dir:
        lines.append("  [下载目录] 未指定（首次运行或 C# 侧没有已保存的下载目录）")
    return "\n".join(lines)


def _append_diagnostics_file(path: str, text: str) -> str:
    """把诊断正文追加到指定文件；返回空串表示成功，否则是脱敏后的失败说明。"""
    target = str(path or "").strip()
    if not target:
        return ""
    try:
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n" + text + "\n")
        return ""
    except OSError as exc:
        return (
            f"{type(exc).__name__}: errno={getattr(exc, 'errno', None)}, "
            f"winerror={getattr(exc, 'winerror', None)}"
        )


def write_process_diagnostics(
    diagnostic_path: str,
    log_path: str = "",
    download_dir: str = "",
    temp_root: str = "",
) -> str:
    """收集并追加 Python 进程诊断，返回诊断正文（纯文本）。

    由 C# 在 ``set_data_root`` 之后调用一次：``diagnostic_path`` 是 C# 计算好的
    ``runtime-data-root.txt`` **绝对路径**，``log_path`` 是
    ``logs/startup-diagnostics.log`` 绝对路径——Python 不自行拼装任何一个。

    诊断内容：``sys.executable`` / ``sys.version`` / pid / cwd、APPDATA 等目录类
    环境变量，以及对配置根、系统临时根、C# 指定的下载临时根 / 下载目录各做一次
    实测写探针（失败时记录异常类型 + errno + winerror + 完整路径）。

    纪律：本入口不向 C# 抛异常；写盘失败只把原因附在返回文本里。
    """
    try:
        text = _build_process_diagnostics(
            str(download_dir or "").strip(),
            str(temp_root or "").strip(),
        )
    except Exception as exc:
        return (
            f"{_PROCESS_DIAGNOSTICS_TITLE}\n"
            f"进程诊断生成失败：{type(exc).__name__}: {_one_line(exc)}"
        )

    errors: list[str] = []
    for label, path in (
        ("runtime-data-root.txt", diagnostic_path),
        ("startup-diagnostics.log", log_path),
    ):
        error = _append_diagnostics_file(path, text)
        if error:
            errors.append(f"{label} 追加失败（{error}）")

    if errors:
        text = text + "\n" + "\n".join(errors)
        diagnostics.record("PROCESS_DIAGNOSTICS_WRITE_FAILED", "；".join(errors))
    else:
        diagnostics.record("PROCESS_DIAGNOSTICS", "Python 进程诊断已追加")
    return text


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


# ---------------------------------------------------------------------------
# 账号入口（阶段 3；凭据只在内存，绝不落盘）
# ---------------------------------------------------------------------------


def account_set_credentials(username: str, password: str) -> str:
    """把账号密码放进进程内存（覆盖旧值），返回 JSON。

    成功::

        {"ok": true, "logged_in": bool, "username_masked": str,
         "error_code": "", "error_message": ""}

    空账号 / 空密码 → ``INVALID_ARGUMENT``。**凭据不写文件、不进日志**；
    返回值只含打码用户名，不含明文密码。
    """
    try:
        return _dumps(account.set_credentials(username, password))
    except Exception as exc:  # pragma: no cover - account 内部已收口
        return _account_failure(exc)


def account_login() -> str:
    """用内存中的凭据登录，返回 JSON（成功 ``logged_in=true``）。"""
    try:
        return _dumps(account.login())
    except Exception as exc:  # pragma: no cover
        return _account_failure(exc)


def account_login_with(username: str, password: str) -> str:
    """一次性登录：凭据不写入 Python 长期内存，返回 JSON。

    供 C# 把凭据管理器 / DPAPI 读出的账号密码直接递进来。登录成功后
    会话仍在数据源客户端（收藏夹可用），但 Python 侧不再保留密码。
    """
    try:
        return _dumps(account.login_with(username, password))
    except Exception as exc:  # pragma: no cover
        return _account_failure(exc)


def account_logout() -> str:
    """退出登录：默认保留内存凭据，只清登录态，返回 JSON。"""
    try:
        return _dumps(account.logout())
    except Exception as exc:  # pragma: no cover
        return _account_failure(exc)


def account_status() -> str:
    """查询登录态；用户名必须打码，返回 JSON。"""
    try:
        return _dumps(account.login_status())
    except Exception as exc:  # pragma: no cover
        return _account_failure(exc)


def account_clear() -> str:
    """清空内存凭据与登录态（修改账号前调用），返回 JSON。"""
    try:
        return _dumps(account.clear_credentials())
    except Exception as exc:  # pragma: no cover
        return _account_failure(exc)


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
        _STATE["disabled_ids"] = []
        _STATE["load_report"] = None
        _STATE["event_callback"] = None
        _STATE["event_sink"] = None

    from jml import events
    from jml.host import loader, paths, registry

    registry.reset()
    events.reset()
    loader.reset()
    searcher.reset_shared_session()
    account.reset_state()
    jm_source.reset_source()
    downloader.reset()
    paths.reset()
    diagnostics.set_log_dir(None)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _setting_error_message(store, key: str, exc: BaseException) -> str:
    """插件设置错误文案：password 项不回显任何值。

    ``PluginConfig`` 的校验错误原文会带非法值 repr；密码值属于凭据，
    即使校验失败也不能出现在 C# 可展示的错误消息里，统一换成固定文案。
    """
    try:
        is_secret = store.is_password_field(key)
    except Exception:  # pragma: no cover - 防御：store 形状异常按普通项处理
        is_secret = False
    if is_secret:
        return "password 设置值不合法（出于安全不回显具体值）"
    return str(exc)


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
