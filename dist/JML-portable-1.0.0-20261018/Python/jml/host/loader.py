"""插件装配器：扫描 / 拓扑排序 / 依序装配（对应技术方案 2.2 节启动流程）。

流程:
    discover(plugins_dir)   扫描子目录收集 manifest（坏 manifest 记入
                            load_errors，不中断整体扫描）
    topo_sort(manifests)    按 dependencies.plugins 拓扑排序；环检测
                            （CircularDependencyError，消息列出环成员）；
                            缺依赖（DependencyError，消息含双方）
    load_all(plugins_dir, disabled_ids=None)
                            discover → topo → 依序装配：
                            0. disabled_ids 里的插件**不 import、不 setup**，
                               记 failed（原因"插件已停用"）
                            1. 核对该插件 services.requires（缺 → 不加载，
                               记错误"插件 xxx 缺少服务 yyy/版本 n"）
                            2. 依赖了被停用/未装载插件的插件明确拒绝加载，
                               原因"缺少依赖插件 xxx（...）"（计划书 8.2/10.4）
                            3. 把插件目录加入 sys.path（宿主统一做的
                               **唯一**路径操作；插件自身禁止任何路径探测）
                            4. importlib 导入 entry 模块
                            5. 调用其 setup(ctx)
    容错纪律: 先加载的不受后面失败影响；单个插件 setup 抛异常 → 该插件
    标记加载失败并记录错误消息，不影响其它插件。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading

from jml.core.protocol import (
    CircularDependencyError,
    DependencyError,
    PluginError,
)
from jml import events
from jml.host import pages
from jml.host.manifest import load_manifest
from jml.host.plugin_context import PluginContext
from jml.host.plugin_state import DISABLED_REASON
from jml.host.registry import REGISTRY


def discover(plugins_dir: str) -> list[dict]:
    """扫描插件目录的子目录，收集合法 manifest。

    返回 manifest 列表；坏 manifest 的目录记入模块级 LOAD_ERRORS
    （"目录名: 错误消息"），不中断整体扫描。
    plugins_dir 本身不存在 → 抛 PluginError（调用方传错路径属硬错误）。
    """
    plugins_dir = os.fspath(plugins_dir)
    if not os.path.isdir(plugins_dir):
        raise PluginError(f"插件目录不存在: {plugins_dir}")

    manifests: list[dict] = []
    with _LOAD_ERRORS_LOCK:
        _LOAD_ERRORS.clear()
        for entry_name in sorted(os.listdir(plugins_dir)):
            sub_dir = os.path.join(plugins_dir, entry_name)
            if not os.path.isdir(sub_dir):
                continue  # 非目录（如 README.md）跳过
            if entry_name.startswith("."):
                continue  # 隐藏目录跳过
            try:
                manifests.append(load_manifest(sub_dir))
            except PluginError as exc:
                # 坏 manifest 记错误继续扫下一个（不中断整体）
                _LOAD_ERRORS.append(f"{entry_name}: {exc}")
    with _KNOWN_LOCK:
        _KNOWN_MANIFESTS.clear()
        for manifest in manifests:
            _KNOWN_MANIFESTS[manifest["id"]] = manifest
    return manifests


def topo_sort(manifests: list[dict], skipped_ids=None) -> list[dict]:
    """按 dependencies.plugins 拓扑排序，被依赖者在前。

    参数:
        skipped_ids: 已知"被停用/被排除"的插件 id 集合。依赖这些 id 时
            不抛 DependencyError（由调用方在装配阶段给出"插件已停用"的
            明确原因）；依赖**完全不存在**的 id 仍然抛错。

    异常:
        DependencyError         —— 依赖的插件既不在 manifests 里也不在
                                    skipped_ids 里（消息含双方）
        CircularDependencyError —— 依赖环（消息列出环成员）
    """
    skipped = {
        item for item in (skipped_ids or []) if isinstance(item, str) and item
    }
    by_id = {m["id"]: m for m in manifests}

    # 依赖图: dep_id -> [依赖它的插件 id]（正向邻接表）
    dependents: dict[str, list[str]] = {mid: [] for mid in by_id}
    # 入度: 插件 id -> 它依赖的、尚未就绪的插件数
    in_degree: dict[str, int] = {mid: 0 for mid in by_id}
    for mid, manifest in by_id.items():
        for dep in manifest["dependencies"]["plugins"]:
            if dep not in by_id:
                if dep in skipped:
                    # 依赖被停用：装配阶段由 load_all 明确拒绝依赖方
                    continue
                raise DependencyError(
                    f"插件 {mid} 缺少依赖插件 {dep}（已声明但未安装或被停用）"
                )
            dependents[dep].append(mid)
            in_degree[mid] += 1

    # Kahn 算法：入度为 0 者出队（同级按 id 排序保证结果稳定）
    ready = sorted(mid for mid, deg in in_degree.items() if deg == 0)
    ordered: list[dict] = []
    while ready:
        mid = ready.pop(0)
        ordered.append(by_id[mid])
        for dependent in sorted(dependents[mid]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)
        ready.sort()

    if len(ordered) != len(manifests):
        # 有环：剩下没入队的插件全部在环上（或依赖环）
        cycle_members = sorted(set(by_id) - {m["id"] for m in ordered})
        raise CircularDependencyError(
            f"插件依赖存在环，拒绝加载整环。环成员: {', '.join(cycle_members)}"
        )
    return ordered


def load_all(plugins_dir: str, disabled_ids=None) -> dict:
    """完整装配流程：discover → 排除停用 → topo → 依序导入并 setup。

    参数:
        disabled_ids: 被停用插件 id 集合。这些插件**不 import、不 setup**，
            结果里进入 failed 且 ``reason`` / ``blocked_reason`` 为
            "插件已停用"、``enabled`` 为 false；依赖它们的插件同样被拒绝
            加载，原因"缺少依赖插件 xxx（该插件已停用）"。

    返回:
        {
          "loaded":  [ {id, name, version, description, pages,
                        development_sample, enabled, blocked_reason} ],
          "failed":  [ {id, reason, enabled, blocked_reason} ],
          "services": {name: version},        # 装配后注册表快照
        }
        停用项不新增顶层列表（沿用 failed，C# 按 enabled 区分状态）。
    """
    global _LAST_REPORT

    with _REPORT_LOCK:
        # 装配开始即作废旧报告：中途抛错（如依赖环）时管理界面不会读到过期状态
        _LAST_REPORT = None

    disabled = {
        item.strip()
        for item in (disabled_ids or [])
        if isinstance(item, str) and item.strip()
    }
    manifests = discover(plugins_dir)
    # 被停用插件不参与拓扑（但作为"已知缺失依赖"传给 topo，避免整批失败）
    enabled_manifests = [
        manifest for manifest in manifests if manifest["id"] not in disabled
    ]
    ordered = topo_sort(enabled_manifests, skipped_ids=disabled)

    # 重新装配语义：上一次的已加载快照与配置实例都作废（配置随后按新 manifest 重建）
    with _LOADED_LOCK:
        _LOADED_MANIFESTS.clear()
    with _CONFIG_STORES_LOCK:
        _CONFIG_STORES.clear()
    # 页面钩子同样作废：重新装配后必须由插件重新注册（避免旧闭包残留）
    pages.reset()

    loaded: list[dict] = []
    failed: list[dict] = []
    failed_ids: set[str] = set()

    # discover 阶段的坏 manifest 先记入 failed（格式: "目录名: 消息"）
    with _LOAD_ERRORS_LOCK:
        for err in _LOAD_ERRORS:
            dir_name, _, reason = err.partition(": ")
            failed.append(
                {
                    "id": dir_name,
                    "reason": reason,
                    "enabled": True,
                    "blocked_reason": reason,
                }
            )
            failed_ids.add(dir_name)

    for manifest in ordered:
        plugin_id = manifest["id"]

        # 依赖被停用 / 依赖方已失败 → 明确拒绝，而不是让 setup 崩
        blocked_reason = _dependency_block_reason(manifest, disabled, failed_ids)
        if blocked_reason:
            failed.append(
                {
                    "id": plugin_id,
                    "reason": blocked_reason,
                    "enabled": True,
                    "blocked_reason": blocked_reason,
                }
            )
            failed_ids.add(plugin_id)
            continue

        try:
            _load_single(manifest)
        except PluginError as exc:
            # 插件体系错误：记录后继续装下一个（先加载的不受后面失败影响）
            # setup 中途注册过的页面钩子一并撤销，不留半装状态
            pages.PAGE_REGISTRY.clear_plugin(plugin_id)
            failed.append(
                {
                    "id": plugin_id,
                    "reason": str(exc),
                    "enabled": True,
                    "blocked_reason": str(exc),
                }
            )
            failed_ids.add(plugin_id)
            continue
        except Exception as exc:  # setup 内任意异常：同样只标记该插件失败
            pages.PAGE_REGISTRY.clear_plugin(plugin_id)
            reason = f"setup 执行失败: {type(exc).__name__}: {exc}"
            failed.append(
                {
                    "id": plugin_id,
                    "reason": reason,
                    "enabled": True,
                    "blocked_reason": reason,
                }
            )
            failed_ids.add(plugin_id)
            continue
        loaded.append(
            {
                "id": plugin_id,
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest["description"],
                "pages": list(manifest["pages"]),
                "development_sample": manifest["development_sample"],
                "enabled": True,
                "blocked_reason": "",
            }
        )
        # 设置界面按"已加载插件"枚举设置定义（失败插件不进快照）
        with _LOADED_LOCK:
            _LOADED_MANIFESTS[plugin_id] = manifest

    # 被停用插件：既不 import 也不 setup，明确记入 failed
    for manifest in sorted(
        (item for item in manifests if item["id"] in disabled),
        key=lambda item: item["id"],
    ):
        failed.append(
            {
                "id": manifest["id"],
                "reason": DISABLED_REASON,
                "enabled": False,
                "blocked_reason": DISABLED_REASON,
            }
        )

    # 记录本次装配结果：管理界面 / bootstrap 状态重算只需要状态，不重装插件
    with _REPORT_LOCK:
        _LAST_REPORT = {
            "dir": os.fspath(plugins_dir),
            "loaded": [dict(item) for item in loaded],
            "failed": [dict(item) for item in failed],
            "disabled": sorted(disabled),
        }

    return {
        "loaded": loaded,
        "failed": failed,
        "services": REGISTRY.names(),
    }


def _dependency_block_reason(
    manifest: dict, disabled: set[str], failed_ids: set[str]
) -> str:
    """依赖了被停用/未成功装载的插件 → 返回拒绝原因；否则空串。"""
    for dep in manifest["dependencies"]["plugins"]:
        if dep in disabled:
            return f"缺少依赖插件 {dep}（该插件已停用）"
        if dep in failed_ids:
            return f"缺少依赖插件 {dep}（该插件未能加载）"
    return ""


def _load_single(manifest: dict) -> None:
    """装配单个插件：核对 requires → sys.path → import → setup(ctx)。

    本函数抛出的异常由 load_all 统一捕获记录。
    """
    plugin_id = manifest["id"]

    # 1. 装配前核对 requires（缺 → 该插件不加载）
    for req in manifest["services"]["requires"]:
        if not REGISTRY.has(req["name"], req["version"]):
            raise DependencyError(
                f"插件 {plugin_id} 缺少服务 {req['name']}/版本 {req['version']}"
            )

    # 2. 把插件目录加入 sys.path（宿主统一做的唯一路径操作，加锁防并发装配）
    plugin_dir = manifest["dir"]
    with _SYS_PATH_LOCK:
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)

    # 3. 导入 entry 模块（模块名按插件 id 派生，隔离不同插件的同名模块）
    entry_path = os.path.join(plugin_dir, manifest["entry"])
    module_name = f"_jml_plugin_{plugin_id}"
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise PluginError(f"插件 {plugin_id} 的入口模块无法导入: {manifest['entry']}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # 注册后执行（支持模块内自引用）
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PluginError(
            f"插件 {plugin_id} 的入口模块执行失败: {type(exc).__name__}: {exc}"
        ) from exc

    # 4. 调用 setup(ctx)
    setup = getattr(module, "setup", None)
    if not callable(setup):
        raise PluginError(f"插件 {plugin_id} 的入口模块缺少 setup(ctx) 函数")

    ctx = _build_context(manifest)
    setup(ctx)  # 抛异常由 load_all 捕获（该插件标记失败，不影响其它）


def plugin_data_dir(plugin_id: str) -> str:
    """插件可写数据目录：%APPDATA%/JML/plugins/{plugin_id}/data。

    与配置文件同根（``config.py`` 的 config.json 位于上一级目录），由宿主
    统一推导并创建，插件只拿绝对路径（插件里不拼 APPDATA 路径）。
    APPDATA 缺失时回退到用户主目录（与 config.py 的保底规则一致）。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JML", "plugins", plugin_id, "data")


def _create_data_dir(plugin_id: str) -> str:
    """创建插件数据目录并返回路径；失败属装配错误（该插件加载失败）。"""
    directory = plugin_data_dir(plugin_id)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise PluginError(
            f"插件 {plugin_id} 的数据目录创建失败: {exc}"
        ) from exc
    return directory


def _build_context(manifest: dict) -> PluginContext:
    """为插件构造 PluginContext（回调全部闭包转发，插件摸不到宿主对象）。"""
    plugin_id = manifest["id"]
    config_store = _get_or_create_config(manifest)
    data_dir = _create_data_dir(plugin_id)
    #: 本插件 manifest 声明过的页面 id（页面注册的唯一合法集合）
    declared_pages = {
        page["id"] for page in manifest["pages"] if isinstance(page.get("id"), str)
    }

    def on_register_service(name: str, version: int, ref: object) -> None:
        REGISTRY.register_service(name, version, ref)

    def on_subscribe(event_type: str, handler) -> None:
        events.BUS.subscribe(event_type, handler)

    def on_get_service(name: str, version: int) -> object:
        return REGISTRY.resolve(name, version)

    def on_config_get(key: str) -> object:
        return config_store.get(key)

    def on_config_set(key: str, value: object) -> None:
        # 与主设置窗口 set_plugin_setting 共用同一份 PluginConfig：
        # schema 校验只有一处，写入同样原子落盘（阶段 4 小扩展）
        config_store.set(key, value)

    def on_log(message: str) -> None:
        # 宿主日志：自动带插件前缀（当前走 stdout，阶段 1 简化实现；
        # 后续接主日志通道时替换这里，接口不变）
        print(f"[plugin:{plugin_id}] {message}")

    # -- 结构化页面协议：plugin_id 由闭包绑定，插件无法指定别人的 id ------

    def _require_declared(page_id: str) -> str:
        """页面 id 必须在该插件 manifest 的 pages[] 里（否则装配失败）。"""
        if not isinstance(page_id, str) or not page_id:
            raise pages.PageError(f"插件 {plugin_id} 的页面 id 必须是非空字符串")
        if page_id not in declared_pages:
            raise pages.PageError(
                f"插件 {plugin_id} 的页面 {page_id} 未在 manifest 的 pages[] 中声明，"
                "拒绝注册（插件不能注册未声明或别人的页面）"
            )
        return page_id

    def on_register_page(page_id: str, loader_fn) -> None:
        pages.PAGE_REGISTRY.register_loader(
            plugin_id, _require_declared(page_id), loader_fn
        )

    def on_register_page_action(page_id: str, action_id: str, handler) -> None:
        if not isinstance(action_id, str) or not action_id:
            raise pages.PageError(
                f"插件 {plugin_id} 的页面动作 id 必须是非空字符串"
            )
        pages.PAGE_REGISTRY.register_action(
            plugin_id, _require_declared(page_id), action_id, handler
        )

    def on_register_page_image(page_id: str, resolver) -> None:
        pages.PAGE_REGISTRY.register_image(
            plugin_id, _require_declared(page_id), resolver
        )

    return PluginContext(
        plugin_id,
        on_register_service=on_register_service,
        on_subscribe=on_subscribe,
        on_get_service=on_get_service,
        on_config_get=on_config_get,
        on_config_set=on_config_set,
        on_log=on_log,
        on_register_page=on_register_page,
        on_register_page_action=on_register_page_action,
        on_register_page_image=on_register_page_image,
        data_dir=data_dir,
    )


# ---------------------------------------------------------------------------
# 模块级状态
# ---------------------------------------------------------------------------

#: discover 阶段的坏 manifest 错误（"目录名: 消息" 列表）
_LOAD_ERRORS: list[str] = []
_LOAD_ERRORS_LOCK = threading.RLock()


def recent_errors() -> list[str]:
    """返回最近一次扫描/装配过程中记录的坏 manifest 错误快照。

    只读快照（副本），调用方（如 jml_backend.list_plugins）不需要也不允许
    直接访问模块私有状态。元素格式固定为 ``"目录名: 错误消息"``。
    """
    with _LOAD_ERRORS_LOCK:
        return list(_LOAD_ERRORS)

#: sys.path 操作锁（load_all 并发调用时保护路径装配）
_SYS_PATH_LOCK = threading.RLock()

#: 插件配置实例缓存: {plugin_id: PluginConfig}（避免重复读盘）
_CONFIG_STORES: dict[str, "object"] = {}
_CONFIG_STORES_LOCK = threading.RLock()

#: 已成功装配的插件 manifest: {plugin_id: manifest}（顺序无关，读取时按 id 排序）
_LOADED_MANIFESTS: dict[str, dict] = {}
_LOADED_LOCK = threading.RLock()

#: 上一次 discover 扫到的 manifest: {plugin_id: manifest}
#: 供"不带 plugins_dir 的 set_plugin_enabled"校验 id 是否存在，
#: 以及管理界面在"未重新扫描"时复用。
_KNOWN_MANIFESTS: dict[str, dict] = {}
_KNOWN_LOCK = threading.RLock()

#: 上一次 load_all 的装配结果快照（供 bootstrap 状态重算 / 管理界面读原因）
_LAST_REPORT: dict | None = None
_REPORT_LOCK = threading.RLock()


def loaded_plugins() -> list[dict]:
    """已加载插件的 manifest 快照（按 id 排序，浅拷贝）。

    只读快照：调用方（设置窗口、诊断）不得直接改宿主内部状态；
    加载失败的插件不在返回列表里。
    """
    with _LOADED_LOCK:
        return [dict(_LOADED_MANIFESTS[pid]) for pid in sorted(_LOADED_MANIFESTS)]


def known_plugin_ids() -> list[str]:
    """最近一次 discover 扫描到的插件 id（排序副本；坏目录不算）。

    ``set_plugin_enabled`` 用它区分"非法 id（NOT_FOUND）"与"合法 id"，
    不依赖是否已经装配成功（加载失败的插件也允许停用）。
    """
    with _KNOWN_LOCK:
        return sorted(_KNOWN_MANIFESTS)


def last_load_report() -> dict | None:
    """最近一次 load_all 的结果快照（只读副本）；尚未装配过返回 None。"""
    with _REPORT_LOCK:
        if _LAST_REPORT is None:
            return None
        return {
            "dir": _LAST_REPORT.get("dir", ""),
            "loaded": [dict(item) for item in _LAST_REPORT.get("loaded", [])],
            "failed": [dict(item) for item in _LAST_REPORT.get("failed", [])],
            "disabled": list(_LAST_REPORT.get("disabled", [])),
        }


def config_for(plugin_id: str):
    """取已加载插件的配置存储（不存在返回 None，不新建）。

    与插件 setup 期间 ``ctx.config_get`` 用的是同一个实例，保证设置窗口
    读到的当前值与插件读到的一致。
    """
    with _CONFIG_STORES_LOCK:
        return _CONFIG_STORES.get(plugin_id)


def _get_or_create_config(manifest: dict):
    """取或建插件的配置存储（缓存按插件 id）。"""
    from jml.host.config import PluginConfig

    pid = manifest["id"]
    with _CONFIG_STORES_LOCK:
        store = _CONFIG_STORES.get(pid)
        if store is None:
            store = PluginConfig(manifest)
            _CONFIG_STORES[pid] = store
        return store


def reset() -> None:
    """清空模块级状态（测试隔离用；不清 REGISTRY/BUS，由各自模块清）。"""
    global _LOAD_ERRORS
    with _LOAD_ERRORS_LOCK:
        _LOAD_ERRORS = []
    with _CONFIG_STORES_LOCK:
        _CONFIG_STORES.clear()
    with _LOADED_LOCK:
        _LOADED_MANIFESTS.clear()
    with _KNOWN_LOCK:
        _KNOWN_MANIFESTS.clear()
    with _REPORT_LOCK:
        global _LAST_REPORT
        _LAST_REPORT = None
    # 页面钩子注册表随装配状态一起清空（插件重新装配后由插件重新注册）
    pages.reset()
