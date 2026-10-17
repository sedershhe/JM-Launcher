"""manifest（plugin.json）读取与校验。

规则（对应技术方案 2.1 节加载规则表）：
- 必填字段缺失 / JSON 损坏 / api_version 不符 / entry 不存在 → 抛对应
  PluginError 子类，错误消息中文且必须含插件 id 或目录名；
- services.provides / requires 规范化为 [{"name": str, "version": int}]；
- pages 规范化为 [{"id", "title", "template"}]，template 必须属于
  protocol.PAGE_TEMPLATES；
- 本模块只做"单目录校验"，不涉及扫描与排序（那是 loader 的职责）。

路径纪律：本模块不硬编码任何绝对路径，插件目录由调用方传入。
"""

from __future__ import annotations

import json
import os

from jml.core.protocol import (
    API_VERSION,
    PAGE_TEMPLATES,
    PluginError,
    ProtocolVersionError,
)

#: manifest 文件名（固定，位于插件目录根部）
MANIFEST_FILENAME = "plugin.json"

#: manifest 必填字段
_REQUIRED_FIELDS = ("id", "name", "version", "api_version", "entry", "services", "pages")

#: services 块必填子字段
_REQUIRED_SERVICE_FIELDS = ("name", "version")

#: pages 块必填子字段
_REQUIRED_PAGE_FIELDS = ("id", "title", "template")


def load_manifest(plugin_dir: str) -> dict:
    """读取并校验一个插件目录的 plugin.json，返回规范化后的 manifest dict。

    参数:
        plugin_dir: 插件目录（绝对或相对路径均可，由调用方传入）。

    返回:
        dict，至少包含：
        {
          "id": str, "name": str, "version": str, "api_version": int,
          "description": str,             # 可缺省，缺省为空串
          "entry": str,                   # 相对插件目录的入口文件名
          "dependencies": {"plugins": [...]},
          "services": {
              "provides": [{"name": str, "version": int}],
              "requires": [{"name": str, "version": int}],
          },
          "pages": [{"id": str, "title": str, "template": str}],
          "config": {"schema": <内联 dict 或文件名>},   # 可缺省
          "development_sample": bool,     # 测试插件标记，可缺省，缺省 False
          "dir": str,                     # 插件目录路径（宿主内部使用）
        }

    异常:
        PluginError           —— JSON 损坏 / 必填字段缺失 / 字段类型错
        ProtocolVersionError —— api_version != API_VERSION
        错误消息均为中文且含插件 id（能取到时）或目录名。
    """
    plugin_dir = os.fspath(plugin_dir)
    dir_label = os.path.basename(os.path.normpath(plugin_dir))
    manifest_path = os.path.join(plugin_dir, MANIFEST_FILENAME)

    if not os.path.isfile(manifest_path):
        raise PluginError(f"插件目录 {dir_label} 缺少 {MANIFEST_FILENAME}，无法加载")

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginError(f"插件 {dir_label} 的 {MANIFEST_FILENAME} 损坏或无法读取: {exc}") from exc

    if not isinstance(raw, dict):
        raise PluginError(f"插件 {dir_label} 的 {MANIFEST_FILENAME} 顶层必须是 JSON 对象")

    # 必填字段检查（错误消息带插件 id：能取到就用 id，否则用目录名）
    pid = raw.get("id")
    label = pid if isinstance(pid, str) and pid else dir_label
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise PluginError(f"插件 {label} 的 {MANIFEST_FILENAME} 缺少必填字段: {field}")

    # 协议版本检查（不兼容即拒绝，不做降级）
    api_version = raw["api_version"]
    if not isinstance(api_version, int) or isinstance(api_version, bool):
        raise ProtocolVersionError(
            f"插件 {label} 的 api_version 必须是整数，实际为 {api_version!r}"
        )
    if api_version != API_VERSION:
        raise ProtocolVersionError(
            f"插件 {label} 的协议版本不兼容: 插件 api_version={api_version}, "
            f"宿主支持 api_version={API_VERSION}，拒绝加载"
        )

    # 基础字段类型检查
    if not isinstance(pid, str) or not pid:
        raise PluginError(f"插件 {dir_label} 的 id 必须是非空字符串")
    for field in ("name", "version", "entry"):
        value = raw[field]
        if not isinstance(value, str) or not value:
            raise PluginError(f"插件 {label} 的字段 {field} 必须是非空字符串")

    # entry 必须指向插件目录内存在的 .py 文件（禁止目录穿越到插件外）
    entry = raw["entry"]
    if not entry.endswith(".py"):
        raise PluginError(f"插件 {label} 的 entry 必须指向 .py 文件，实际为 {entry!r}")
    entry_path = os.path.normpath(os.path.join(plugin_dir, entry))
    if not entry_path.startswith(os.path.normpath(plugin_dir) + os.sep):
        raise PluginError(f"插件 {label} 的 entry 不得指向插件目录之外: {entry!r}")
    if not os.path.isfile(entry_path):
        raise PluginError(f"插件 {label} 的入口文件不存在: {entry}")

    # services 块规范化
    services_raw = raw["services"]
    if not isinstance(services_raw, dict):
        raise PluginError(f"插件 {label} 的 services 必须是 JSON 对象")
    provides = _normalize_services(label, services_raw.get("provides", []), "provides")
    requires = _normalize_services(label, services_raw.get("requires", []), "requires")

    # pages 块规范化
    pages_raw = raw["pages"]
    if not isinstance(pages_raw, list):
        raise PluginError(f"插件 {label} 的 pages 必须是数组")
    pages = []
    seen_page_ids: set[str] = set()
    for index, page in enumerate(pages_raw):
        if not isinstance(page, dict):
            raise PluginError(f"插件 {label} 的 pages[{index}] 必须是 JSON 对象")
        for field in _REQUIRED_PAGE_FIELDS:
            if field not in page:
                raise PluginError(f"插件 {label} 的 pages[{index}] 缺少必填字段: {field}")
        page_id = page["id"]
        title = page["title"]
        template = page["template"]
        if not isinstance(page_id, str) or not page_id:
            raise PluginError(f"插件 {label} 的 pages[{index}].id 必须是非空字符串")
        if page_id in seen_page_ids:
            raise PluginError(f"插件 {label} 的页面 id 重复: {page_id}")
        seen_page_ids.add(page_id)
        if not isinstance(title, str) or not title:
            raise PluginError(f"插件 {label} 的 pages[{index}].title 必须是非空字符串")
        if template not in PAGE_TEMPLATES:
            raise PluginError(
                f"插件 {label} 的 pages[{index}].template 非法: {template!r}, "
                f"允许值: {', '.join(PAGE_TEMPLATES)}"
            )
        pages.append({"id": page_id, "title": title, "template": template})

    # dependencies 块规范化（可缺省，缺省为空）
    deps_raw = raw.get("dependencies", {})
    if not isinstance(deps_raw, dict):
        raise PluginError(f"插件 {label} 的 dependencies 必须是 JSON 对象")
    deps_plugins = deps_raw.get("plugins", [])
    if not isinstance(deps_plugins, list) or not all(
        isinstance(d, str) and d for d in deps_plugins
    ):
        raise PluginError(f"插件 {label} 的 dependencies.plugins 必须是插件 id 字符串数组")

    # config 块（可缺省；schema 为内联 dict 或同目录文件名）
    config_raw = raw.get("config", {})
    if not isinstance(config_raw, dict):
        raise PluginError(f"插件 {label} 的 config 必须是 JSON 对象")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise PluginError(f"插件 {label} 的 description 必须是字符串")

    development_sample = raw.get("development_sample", False)
    if not isinstance(development_sample, bool):
        raise PluginError(f"插件 {label} 的 development_sample 必须是布尔值")

    return {
        "id": pid,
        "name": raw["name"],
        "version": raw["version"],
        "api_version": api_version,
        "description": description,
        "entry": entry,
        "dependencies": {"plugins": list(deps_plugins)},
        "services": {"provides": provides, "requires": requires},
        "pages": pages,
        "config": dict(config_raw),
        "development_sample": development_sample,
        "dir": os.path.abspath(plugin_dir),
    }


def _normalize_services(label: str, block, block_name: str) -> list[dict]:
    """规范化 provides/requires 数组为 [{"name", "version"}]。

    参数 block 为原始 JSON 值；任何形状/类型错误都抛 PluginError（中文消息，
    含插件 label 与块名）。
    """
    if not isinstance(block, list):
        raise PluginError(f"插件 {label} 的 services.{block_name} 必须是数组")
    normalized: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(block):
        if not isinstance(item, dict):
            raise PluginError(f"插件 {label} 的 services.{block_name}[{index}] 必须是 JSON 对象")
        for field in _REQUIRED_SERVICE_FIELDS:
            if field not in item:
                raise PluginError(
                    f"插件 {label} 的 services.{block_name}[{index}] 缺少必填字段: {field}"
                )
        name = item["name"]
        version = item["version"]
        if not isinstance(name, str) or not name:
            raise PluginError(
                f"插件 {label} 的 services.{block_name}[{index}].name 必须是非空字符串"
            )
        if not isinstance(version, int) or isinstance(version, bool):
            raise PluginError(
                f"插件 {label} 的 services.{block_name}[{index}].version 必须是整数"
            )
        if name in seen:
            raise PluginError(
                f"插件 {label} 的 services.{block_name} 中服务名重复: {name}"
            )
        seen.add(name)
        normalized.append({"name": name, "version": version})
    return normalized
