"""插件配置契约（对应技术方案 2.5 节）。

- 按 manifest.config.schema 生成默认值（内联 dict 或同目录
  config.schema.json 文件）
- 写入时校验类型与范围（type/enum/min/max/required），非法 → ConfigError
  拒绝写入并回退默认值（对应旧缺陷 #5：schema 真正用于校验）
- 存储路径：%APPDATA%/JML/plugins/{id}/config.json
  （用 os.environ["APPDATA"]，注释与 docstring 一律不写裸反斜杠路径，
  避免 SyntaxWarning——见计划书 11.5 坑 #4）

安全红线：配置文件不含任何凭据（凭据走凭据管理器/DPAPI，阶段 3）。
"""

from __future__ import annotations

import json
import os
import threading

from jml.core.protocol import ConfigError

#: 支持的 schema 字段类型（type 取值）
_SUPPORTED_TYPES = ("string", "integer", "number", "boolean")

#: 类型校验时 Python 侧的映射（integer 排除 bool，bool 是 int 的子类）
_TYPE_CHECKERS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


class PluginConfig:
    """单个插件的配置存取（带 schema 校验）。

    用法:
        cfg = PluginConfig(manifest)        # 读 schema、生成默认值
        cfg.get("key")                       # 读当前值（无则默认值）
        cfg.set("key", value)                # 写入（校验失败抛 ConfigError，值不变）
        cfg.as_dict()                        # 导出全部当前值
    """

    def __init__(self, manifest: dict) -> None:
        self._lock = threading.RLock()
        self._plugin_id: str = manifest["id"]
        self._schema: dict = self._load_schema(manifest)
        self._defaults = self._build_defaults(self._plugin_id, self._schema)
        self._path = self._storage_path(self._plugin_id)
        self._values = dict(self._defaults)
        self._load_stored()

    # -- 对外接口 -----------------------------------------------------------

    def get(self, key: str, default: object = None) -> object:
        """读配置项；key 不在 schema 中时返回调用方给的 default。"""
        with self._lock:
            if key in self._values:
                return self._values[key]
            return default

    def set(self, key: str, value: object) -> None:
        """写配置项：先校验，非法抛 ConfigError（当前值保持不变），合法则落盘。

        非法写入不会改变任何已存值（回退默认语义由调用方在捕获后读取 get 实现）。
        """
        with self._lock:
            checked = self._validate(key, value)
            self._values[key] = checked
            self._save()

    def as_dict(self) -> dict:
        """返回全部配置项当前值的浅拷贝。"""
        with self._lock:
            return dict(self._values)

    def defaults(self) -> dict:
        """返回由 schema 生成的默认值表（浅拷贝）。"""
        with self._lock:
            return dict(self._defaults)

    def schema(self) -> dict:
        """返回 schema 本体（诊断与设置界面生成用）。"""
        return self._schema

    @property
    def storage_path(self) -> str:
        """配置文件落盘路径（只读属性，供诊断）。"""
        return self._path

    # -- 内部实现 -----------------------------------------------------------

    @staticmethod
    def _load_schema(manifest: dict) -> dict:
        """从 manifest 取 schema：内联 dict 或同目录 config.schema.json。

        schema 形如:
            {"key": {"type": "integer", "default": 200, "min": 10, "max": 5000,
                     "enum": [...](可选), "required": true(可选),
                     "title": "保留条数"(可选, 界面显示用)}}
        """
        config_block = manifest.get("config") or {}
        schema = config_block.get("schema")
        if isinstance(schema, dict):
            return schema
        if isinstance(schema, str):
            schema_path = os.path.join(manifest["dir"], schema)
            try:
                with open(schema_path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(
                    f"插件 {manifest['id']} 的配置 schema 文件无法读取: {exc}"
                ) from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"插件 {manifest['id']} 的配置 schema 顶层必须是 JSON 对象")
            return loaded
        # 未声明 config 或 schema 为空 → 空 schema（该插件没有可配置项）
        return {}

    @staticmethod
    def _build_defaults(plugin_id: str, schema: dict) -> dict:
        """按 schema 生成默认值表（schema 本身先做结构校验）。"""
        defaults: dict = {}
        for key, rule in schema.items():
            if not isinstance(rule, dict):
                raise ConfigError(f"插件 {plugin_id} 的配置项 {key} 的规则必须是 JSON 对象")
            ftype = rule.get("type", "string")
            if ftype not in _SUPPORTED_TYPES:
                raise ConfigError(
                    f"插件 {plugin_id} 的配置项 {key} 的 type 非法: {ftype!r}, "
                    f"允许值: {', '.join(_SUPPORTED_TYPES)}"
                )
            if "enum" in rule and not isinstance(rule["enum"], list):
                raise ConfigError(f"插件 {plugin_id} 的配置项 {key} 的 enum 必须是数组")
            if "default" in rule:
                # 默认值本身也要过一遍校验，schema 写错尽早暴露
                defaults[key] = PluginConfig._check_value(plugin_id, key, rule, rule["default"])
            else:
                # 没给 default：required 的项报错，非 required 的按类型空值
                if rule.get("required"):
                    raise ConfigError(
                        f"插件 {plugin_id} 的必填配置项 {key} 缺少 default"
                    )
                defaults[key] = {"string": "", "integer": 0, "number": 0.0, "boolean": False}[ftype]
        return defaults

    @staticmethod
    def _storage_path(plugin_id: str) -> str:
        """配置落盘路径：%APPDATA%/JML/plugins/{id}/config.json。

        APPDATA 环境变量缺失时回退到用户主目录（罕见，仅保底）。
        """
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "JML", "plugins", plugin_id, "config.json")

    def _load_stored(self) -> None:
        """读已落盘的配置，合并到当前值（非法的存量值忽略，保持默认）。"""
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # 存量文件损坏 → 当作不存在，全部用默认值（不让坏文件炸宿主）
            return
        if not isinstance(stored, dict):
            return
        for key, value in stored.items():
            if key not in self._schema:
                continue  # schema 里已删除的项：忽略存量
            try:
                self._values[key] = self._validate(key, value)
            except ConfigError:
                continue  # 存量值不合法 → 忽略，保持默认值（对应旧缺陷 #5）

    def _save(self) -> None:
        """把当前值落盘（原子写：先写临时文件再替换）。"""
        directory = os.path.dirname(self._path)
        os.makedirs(directory, exist_ok=True)
        tmp_path = self._path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._values, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            raise ConfigError(f"插件 {self._plugin_id} 的配置写入失败: {exc}") from exc

    def _validate(self, key: str, value: object) -> object:
        """校验单值，通过则原样返回，不通过抛 ConfigError（中文消息）。"""
        if key not in self._schema:
            raise ConfigError(f"插件 {self._plugin_id} 没有配置项 {key}，拒绝写入")
        return self._check_value(self._plugin_id, key, self._schema[key], value)

    @staticmethod
    def _check_value(plugin_id: str, key: str, rule: dict, value: object) -> object:
        """静态校验函数：type / enum / min / max / required。"""
        ftype = rule.get("type", "string")
        if ftype not in _TYPE_CHECKERS or not _TYPE_CHECKERS[ftype](value):
            raise ConfigError(
                f"插件 {plugin_id} 的配置项 {key} 类型非法: 期望 {ftype}, "
                f"实际 {type(value).__name__} ({value!r})，已回退默认值"
            )
        if "enum" in rule and value not in rule["enum"]:
            raise ConfigError(
                f"插件 {plugin_id} 的配置项 {key} 的值 {value!r} 不在允许的枚举内: "
                f"{rule['enum']!r}，已回退默认值"
            )
        if "min" in rule:
            try:
                if value < rule["min"]:
                    raise ConfigError(
                        f"插件 {plugin_id} 的配置项 {key} 的值 {value!r} 小于最小值 "
                        f"{rule['min']!r}，已回退默认值"
                    )
            except TypeError:
                pass  # 不可比较类型（如 boolean）跳过范围检查
        if "max" in rule:
            try:
                if value > rule["max"]:
                    raise ConfigError(
                        f"插件 {plugin_id} 的配置项 {key} 的值 {value!r} 大于最大值 "
                        f"{rule['max']!r}，已回退默认值"
                    )
            except TypeError:
                pass
        return value
