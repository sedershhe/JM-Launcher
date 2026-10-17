"""插件装配器：扫描 / 拓扑排序 / 依序装配（对应技术方案 2.2 节启动流程）。

流程:
    discover(plugins_dir)   扫描子目录收集 manifest（坏 manifest 记入
                            load_errors，不中断整体扫描）
    topo_sort(manifests)    按 dependencies.plugins 拓扑排序；环检测
                            （CircularDependencyError，消息列出环成员）；
                            缺依赖（DependencyError，消息含双方）
    load_all(plugins_dir)   discover → topo → 依序装配：
                            1. 核对该插件 services.requires（缺 → 不加载，
                               记错误"插件 xxx 缺少服务 yyy/版本 n"）
                            2. 把插件目录加入 sys.path（宿主统一做的
                               **唯一**路径操作；插件自身禁止任何路径探测）
                            3. importlib 导入 entry 模块
                            4. 调用其 setup(ctx)
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
from jml.host.manifest import load_manifest
from jml.host.plugin_context import PluginContext
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
    return manifests


def topo_sort(manifests: list[dict]) -> list[dict]:
    """按 dependencies.plugins 拓扑排序，被依赖者在前。

    异常:
        DependencyError         —— 依赖的插件不在 manifests 里（消息含
                                    缺失插件名与需要它的插件名）
        CircularDependencyError —— 依赖环（消息列出环成员）
    """
    by_id = {m["id"]: m for m in manifests}

    # 依赖图: dep_id -> [依赖它的插件 id]（正向邻接表）
    dependents: dict[str, list[str]] = {mid: [] for mid in by_id}
    # 入度: 插件 id -> 它依赖的、尚未就绪的插件数
    in_degree: dict[str, int] = {mid: 0 for mid in by_id}
    for mid, manifest in by_id.items():
        for dep in manifest["dependencies"]["plugins"]:
            if dep not in by_id:
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


def load_all(plugins_dir: str) -> dict:
    """完整装配流程：discover → topo → 依序导入并 setup。

    返回:
        {
          "loaded":  [ {id, name, version, pages, development_sample} ],
          "failed":  [ {id, reason} ],        # 含 discover 阶段的坏目录
          "services": {name: version},        # 装配后注册表快照
        }
    """
    manifests = discover(plugins_dir)
    ordered = topo_sort(manifests)

    loaded: list[dict] = []
    failed: list[dict] = []

    # discover 阶段的坏 manifest 先记入 failed（格式: "目录名: 消息"）
    with _LOAD_ERRORS_LOCK:
        for err in _LOAD_ERRORS:
            dir_name, _, reason = err.partition(": ")
            failed.append({"id": dir_name, "reason": reason})

    for manifest in ordered:
        plugin_id = manifest["id"]
        try:
            _load_single(manifest)
        except PluginError as exc:
            # 插件体系错误：记录后继续装下一个（先加载的不受后面失败影响）
            failed.append({"id": plugin_id, "reason": str(exc)})
            continue
        except Exception as exc:  # setup 内任意异常：同样只标记该插件失败
            failed.append(
                {"id": plugin_id, "reason": f"setup 执行失败: {type(exc).__name__}: {exc}"}
            )
            continue
        loaded.append(
            {
                "id": plugin_id,
                "name": manifest["name"],
                "version": manifest["version"],
                "pages": list(manifest["pages"]),
                "development_sample": manifest["development_sample"],
            }
        )

    return {
        "loaded": loaded,
        "failed": failed,
        "services": REGISTRY.names(),
    }


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


def _build_context(manifest: dict) -> PluginContext:
    """为插件构造 PluginContext（回调全部闭包转发，插件摸不到宿主对象）。"""
    plugin_id = manifest["id"]
    config_store = _get_or_create_config(manifest)

    def on_register_service(name: str, version: int, ref: object) -> None:
        REGISTRY.register_service(name, version, ref)

    def on_subscribe(event_type: str, handler) -> None:
        events.BUS.subscribe(event_type, handler)

    def on_get_service(name: str, version: int) -> object:
        return REGISTRY.resolve(name, version)

    def on_config_get(key: str) -> object:
        return config_store.get(key)

    def on_log(message: str) -> None:
        # 宿主日志：自动带插件前缀（当前走 stdout，阶段 1 简化实现；
        # 后续接主日志通道时替换这里，接口不变）
        print(f"[plugin:{plugin_id}] {message}")

    return PluginContext(
        plugin_id,
        on_register_service=on_register_service,
        on_subscribe=on_subscribe,
        on_get_service=on_get_service,
        on_config_get=on_config_get,
        on_log=on_log,
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
