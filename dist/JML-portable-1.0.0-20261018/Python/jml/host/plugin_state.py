"""插件启用/停用状态（阶段 4-5；唯一真源 = disabled.json）。

背景（计划书 8.2 / 10.4）：
- 停用某插件后，该插件**不得 import、不得 setup**；
- 依赖它的插件必须被明确拒绝，并给出"缺少依赖插件 xxx"的原因，
  而不是等到 setup 时才崩；
- 界面需要知道"停用后会影响谁"，因此这里按拓扑关系（可传递）算出
  dependents。

存储：``%APPDATA%/JML/plugins/disabled.json``，内容是被停用插件 id 的
JSON 数组（白名单语义：只记录"停用"，不记录"启用"）。写入用
"同目录临时文件 + os.replace" 原子替换。

边界：
- 本模块只做状态存取与状态推导，不 import 插件、不注册服务；
- 不硬编码绝对路径（APPDATA 缺失时回退用户主目录，与 config.py 一致）；
- 依赖 loader 的函数在函数体内惰性 import，避免模块循环依赖。
"""

from __future__ import annotations

import json
import os
import threading

from jml.core.errors import InvalidArgumentError

#: 禁用列表文件名（与各插件配置目录同级）
DISABLED_FILENAME = "disabled.json"

#: 被停用插件的统一原因文案（界面与 loader 共用，避免各处各写一份）
DISABLED_REASON = "插件已停用"

#: 管理状态三态
STATUS_LOADED = "loaded"
STATUS_DISABLED = "disabled"
STATUS_FAILED = "failed"

#: 读写 disabled.json 的进程内互斥（读-改-写必须整体串行）
_DISABLED_LOCK = threading.RLock()


def disabled_plugins_path() -> str:
    """禁用列表落盘路径：``%APPDATA%/JML/plugins/disabled.json``。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JML", "plugins", DISABLED_FILENAME)


def load_disabled_ids() -> list[str]:
    """读取禁用列表（排序去重后的副本；文件缺失/损坏一律视为空列表）。"""
    with _DISABLED_LOCK:
        return _read_disabled_file()


def _read_disabled_file() -> list[str]:
    """不做加锁的读实现（由公开函数持锁调用）。"""
    path = disabled_plugins_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        # 文件不存在 / 坏 JSON / 编码问题：当作"没有停用任何插件"，
        # 不让一个可再生的状态文件把宿主启动弄崩。
        return []
    if not isinstance(raw, list):
        return []
    return sorted(
        {
            item.strip()
            for item in raw
            if isinstance(item, str) and item.strip()
        }
    )


def save_disabled_ids(plugin_ids) -> list[str]:
    """原子写入禁用列表，返回实际落盘的排序去重结果。

    写入顺序：临时文件 → flush + fsync → ``os.replace``。替换失败时清理
    临时文件，异常原样上抛（调用方转成 IO_ERROR，不静默丢状态）。
    """
    normalized = sorted(
        {
            item.strip()
            for item in plugin_ids
            if isinstance(item, str) and item.strip()
        }
    )
    with _DISABLED_LOCK:
        path = disabled_plugins_path()
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return normalized


def resolve_disabled_ids(disabled_json="") -> list[str]:
    """把入口参数解析成禁用 id 列表。

    参数:
        disabled_json: JSON 数组字符串（如 ``'["a","b"]'``）；空串/None
            表示"沿用磁盘上的 disabled.json"（唯一真源）。

    异常:
        InvalidArgumentError —— 非空但非法（不是 JSON 数组 / 元素不是
            非空字符串）。入口函数会把它收口成 INVALID_ARGUMENT。
    """
    if disabled_json is None:
        return load_disabled_ids()
    if isinstance(disabled_json, (list, tuple, set)):
        items = list(disabled_json)
    else:
        text = str(disabled_json).strip()
        if not text:
            return load_disabled_ids()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise InvalidArgumentError(f"禁用列表不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise InvalidArgumentError("禁用列表必须是 JSON 数组字符串")
        items = parsed

    normalized: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise InvalidArgumentError("禁用列表只能包含非空插件 id 字符串")
        normalized.append(item.strip())
    return sorted(set(normalized))


# ---------------------------------------------------------------------------
# 依赖关系与状态推导
# ---------------------------------------------------------------------------


def _manifests_by_id(manifests: list[dict]) -> dict[str, dict]:
    """manifest 列表 → {插件 id: manifest}（同一 id 重复时取先出现者）。"""
    result: dict[str, dict] = {}
    for manifest in manifests:
        plugin_id = manifest.get("id")
        if isinstance(plugin_id, str) and plugin_id and plugin_id not in result:
            result[plugin_id] = manifest
    return result


def _direct_dependencies(manifest: dict) -> list[str]:
    """取 manifest 声明的依赖插件 id（排序，保证状态文案稳定）。"""
    block = manifest.get("dependencies") or {}
    plugins = block.get("plugins") if isinstance(block, dict) else None
    if not isinstance(plugins, list):
        return []
    return sorted({item for item in plugins if isinstance(item, str) and item})


def dependencies_map(manifests: list[dict]) -> dict[str, list[str]]:
    """{插件 id: 它直接依赖的插件 id 列表}。"""
    return {
        plugin_id: _direct_dependencies(manifest)
        for plugin_id, manifest in _manifests_by_id(manifests).items()
    }


def dependents_map(manifests: list[dict]) -> dict[str, list[str]]:
    """{插件 id: 直接或间接依赖它的插件 id 列表（排序去重）}。

    多级依赖也计入：A ← B ← C 时，停用 A 会影响 B 与 C。
    依赖环里的成员不会把自己算进 dependents，且不会死循环。
    """
    by_id = _manifests_by_id(manifests)
    reverse: dict[str, list[str]] = {plugin_id: [] for plugin_id in by_id}
    for plugin_id, manifest in by_id.items():
        for dep in _direct_dependencies(manifest):
            if dep in reverse:
                reverse[dep].append(plugin_id)

    result: dict[str, list[str]] = {}
    for plugin_id in by_id:
        seen: set[str] = set()
        stack = list(reverse[plugin_id])
        while stack:
            current = stack.pop()
            if current == plugin_id or current in seen:
                continue
            seen.add(current)
            stack.extend(reverse[current])
        result[plugin_id] = sorted(seen)
    return result


def _dependency_block_reason(
    plugin_id: str,
    dependencies: dict[str, list[str]],
    disabled: set[str],
    failed_ids: set[str],
) -> str:
    """返回该插件因依赖问题被拒绝的原因；没有依赖问题返回空串。"""
    for dep in dependencies.get(plugin_id, []):
        if dep in disabled:
            return f"缺少依赖插件 {dep}（该插件已停用）"
        if dep in failed_ids:
            return f"缺少依赖插件 {dep}（该插件未能加载）"
    return ""


def scan_status(
    manifests: list[dict], disabled_ids
) -> dict[str, tuple[str, str]]:
    """只扫描（不 import）时每个插件的 (status, reason)。

    - 被停用 → disabled / "插件已停用"；
    - 依赖被停用或依赖本身不可用 → failed / "缺少依赖插件 ..."；
    - 其余 → loaded（"扫描通过"，不等于"已装配成功"）。
    """
    disabled = {
        item.strip()
        for item in (disabled_ids or [])
        if isinstance(item, str) and item.strip()
    }
    by_id = _manifests_by_id(manifests)
    dependencies = {
        plugin_id: _direct_dependencies(manifest)
        for plugin_id, manifest in by_id.items()
    }
    cache: dict[str, tuple[str, str]] = {}
    visiting: set[str] = set()

    def resolve(plugin_id: str) -> tuple[str, str]:
        cached = cache.get(plugin_id)
        if cached is not None:
            return cached
        if plugin_id in disabled:
            result = (STATUS_DISABLED, DISABLED_REASON)
        elif plugin_id not in by_id:
            result = (STATUS_FAILED, f"缺少依赖插件 {plugin_id}（该插件未安装）")
        elif plugin_id in visiting:
            result = (STATUS_FAILED, "插件依赖存在环")
        else:
            visiting.add(plugin_id)
            result = (STATUS_LOADED, "")
            for dep in dependencies.get(plugin_id, []):
                dep_status, _ = resolve(dep)
                if dep_status == STATUS_LOADED:
                    continue
                if dep_status == STATUS_DISABLED:
                    suffix = "（该插件已停用）"
                elif dep not in by_id:
                    suffix = "（该插件未安装）"
                else:
                    suffix = "（该插件未能加载）"
                result = (STATUS_FAILED, f"缺少依赖插件 {dep}{suffix}")
                break
            visiting.discard(plugin_id)
        cache[plugin_id] = result
        return result

    return {plugin_id: resolve(plugin_id) for plugin_id in by_id}


def scan_report(manifests: list[dict], disabled_ids) -> tuple[list[dict], list[dict]]:
    """把"只扫描"结果拆成 loaded / failed 两个列表（结构同 bootstrap）。

    两个列表的元素都带 ``enabled`` 与 ``blocked_reason``，C# 只渲染，
    不再自己判断"是否停用"。
    """
    statuses = scan_status(manifests, disabled_ids)
    loaded: list[dict] = []
    failed: list[dict] = []
    for manifest in sorted(manifests, key=lambda item: item["id"]):
        plugin_id = manifest["id"]
        status, reason = statuses[plugin_id]
        if status == STATUS_LOADED:
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
        else:
            failed.append(
                {
                    "id": plugin_id,
                    "reason": reason,
                    "enabled": status != STATUS_DISABLED,
                    "blocked_reason": reason,
                }
            )
    return loaded, failed


def _management_status(
    plugin_id: str,
    dependencies: dict[str, list[str]],
    disabled: set[str],
    loaded_ids: set[str],
    failed_reasons: dict[str, str],
    disabled_at_load: set[str],
    cache: dict[str, tuple[str, str]],
    visiting: set[str],
) -> tuple[str, str]:
    """推导管理界面的 (status, reason)：优先级 停用 > 依赖阻断 > 已加载 > 失败。"""
    cached = cache.get(plugin_id)
    if cached is not None:
        return cached
    if plugin_id in disabled:
        result = (STATUS_DISABLED, DISABLED_REASON)
    elif plugin_id in visiting:
        result = (STATUS_FAILED, "插件依赖存在环")
    else:
        visiting.add(plugin_id)
        blocked_reason = ""
        for dep in dependencies.get(plugin_id, []):
            dep_status, _ = _management_status(
                dep,
                dependencies,
                disabled,
                loaded_ids,
                failed_reasons,
                disabled_at_load,
                cache,
                visiting,
            )
            if dep_status == STATUS_LOADED:
                continue
            if dep_status == STATUS_DISABLED:
                blocked_reason = f"缺少依赖插件 {dep}（该插件已停用）"
            else:
                blocked_reason = f"缺少依赖插件 {dep}（该插件未能加载）"
            break
        visiting.discard(plugin_id)

        if blocked_reason:
            result = (STATUS_FAILED, blocked_reason)
        elif plugin_id in loaded_ids:
            result = (STATUS_LOADED, "")
        elif plugin_id in failed_reasons:
            reason = failed_reasons[plugin_id]
            if plugin_id in disabled_at_load and plugin_id not in disabled:
                # 上次装配时因停用被跳过，本次已重新启用：只能重启后加载
                reason = "插件已启用，重启后加载"
            result = (STATUS_FAILED, reason)
        else:
            result = (STATUS_FAILED, "尚未加载")
    cache[plugin_id] = result
    return result


def management_state(plugins_dir: str) -> list[dict]:
    """插件管理界面的完整状态：每个插件一行（含 dependents）。

    依赖 Python 侧上一次装配结果（``loader.loaded_plugins`` /
    ``loader.last_load_report``）；只扫描不触发 import。
    """
    from jml.host import loader

    manifests = loader.discover(plugins_dir)
    disabled = set(load_disabled_ids())
    dependencies = dependencies_map(manifests)
    dependents = dependents_map(manifests)
    loaded_ids = {manifest["id"] for manifest in loader.loaded_plugins()}
    report = loader.last_load_report() or {}
    failed_reasons = {
        item["id"]: item["reason"]
        for item in report.get("failed", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    disabled_at_load = {
        item for item in report.get("disabled", []) if isinstance(item, str)
    }

    rows: list[dict] = []
    cache: dict[str, tuple[str, str]] = {}
    for manifest in sorted(manifests, key=lambda item: item["id"]):
        plugin_id = manifest["id"]
        status, reason = _management_status(
            plugin_id,
            dependencies,
            disabled,
            loaded_ids,
            failed_reasons,
            disabled_at_load,
            cache,
            set(),
        )
        rows.append(
            {
                "id": plugin_id,
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest["description"],
                "enabled": status != STATUS_DISABLED,
                "status": status,
                "reason": reason,
                "dependents": list(dependents.get(plugin_id, [])),
            }
        )

    # discover 阶段的坏目录也展示为"加载失败"，否则界面上会凭空少一行。
    known_ids = {manifest["id"] for manifest in manifests}
    for error in loader.recent_errors():
        dir_name, _, reason = error.partition(": ")
        if not dir_name or dir_name in known_ids:
            continue
        rows.append(
            {
                "id": dir_name,
                "name": dir_name,
                "version": "",
                "description": "",
                "enabled": True,
                "status": STATUS_FAILED,
                "reason": reason,
                "dependents": [],
            }
        )
    return rows


def refresh_bootstrap_result(
    result: dict, plugins_dir: str, disabled_ids
) -> dict:
    """禁用列表变化后重算 bootstrap 结果（不热加载、不重新 import）。

    决策 #7：停用/启用只在下次启动真正生效；但 bootstrap/list 的**状态
    结果**必须立刻反映新的禁用列表。这里基于上一次装配结果做"状态重算"：
    新停用的插件移入 failed（enabled=false），依赖它的插件标记为
    "缺少依赖插件"；重新启用的插件提示"重启后加载"。
    """
    from jml.host import loader

    manifests = loader.discover(plugins_dir)
    disabled = {
        item.strip()
        for item in (disabled_ids or [])
        if isinstance(item, str) and item.strip()
    }
    dependencies = dependencies_map(manifests)

    previous_loaded = [dict(item) for item in result.get("loaded", [])]
    previous_failed = [dict(item) for item in result.get("failed", [])]
    loaded_ids = {
        item["id"] for item in previous_loaded if isinstance(item.get("id"), str)
    }

    loaded: list[dict] = []
    failed: list[dict] = []
    for entry in previous_loaded:
        plugin_id = entry.get("id", "")
        if plugin_id in disabled:
            failed.append(
                {
                    "id": plugin_id,
                    "reason": DISABLED_REASON,
                    "enabled": False,
                    "blocked_reason": DISABLED_REASON,
                }
            )
            continue

        blocked_reason = ""
        for dep in dependencies.get(plugin_id, []):
            if dep in disabled:
                blocked_reason = f"缺少依赖插件 {dep}（该插件已停用）"
                break
            if dep not in loaded_ids:
                blocked_reason = f"缺少依赖插件 {dep}（该插件未能加载）"
                break
        if blocked_reason:
            failed.append(
                {
                    "id": plugin_id,
                    "reason": blocked_reason,
                    "enabled": True,
                    "blocked_reason": blocked_reason,
                }
            )
            continue

        entry["enabled"] = True
        entry["blocked_reason"] = ""
        loaded.append(entry)

    for entry in previous_failed:
        plugin_id = entry.get("id", "")
        if plugin_id in disabled:
            failed.append(
                {
                    "id": plugin_id,
                    "reason": DISABLED_REASON,
                    "enabled": False,
                    "blocked_reason": DISABLED_REASON,
                }
            )
            continue

        reason = entry.get("reason") or ""
        enabled = entry.get("enabled", True)
        if enabled is False:
            # 上次因停用被跳过，现在重新启用：本次进程内无法补加载
            reason = "插件已启用，重启后加载"
            enabled = True
        failed.append(
            {
                "id": plugin_id,
                "reason": reason,
                "enabled": bool(enabled),
                "blocked_reason": reason,
            }
        )

    return {
        "ok": True,
        "loaded": loaded,
        "failed": failed,
        "services": dict(result.get("services") or {}),
    }
