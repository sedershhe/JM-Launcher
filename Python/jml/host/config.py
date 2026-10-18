"""插件配置契约（对应技术方案 2.5 节）。

- 按 manifest.config.schema 生成默认值（内联 dict 或同目录
  config.schema.json 文件）
- 写入时校验类型与范围（type/enum/min/max/required），非法 → ConfigError
  拒绝写入并回退默认值（对应旧缺陷 #5：schema 真正用于校验）
- ``describe_schema()`` 把 schema 枚举成设置界面字段列表（类型受限于
  text/password/number/bool/select/path），供主设置窗口动态表单使用
- 存储路径：``<plugin_data_root>/{id}/config.json``，默认
  ``%APPDATA%/JML/plugins/{id}/config.json``；实际根由 C# 启动时通过
  ``jml_backend.set_data_root`` 注入 ``jml.host.paths``（数据目录可降级），
  本模块**不直接读环境变量**（注释与 docstring 一律不写裸反斜杠路径，
  避免 SyntaxWarning——见计划书 11.5 坑 #4）

安全红线：配置文件不含任何凭据。schema 里 ``"ui": "password"`` 的项视为
凭据类字段：值只留在进程内存、绝不写入 config.json，``describe_schema()``
也不回传值（只给 has_value）；schema 本身同样不得声明非空 password 默认值。
真正的持久化凭据应走 C# 侧凭据管理器/DPAPI（阶段 3）。
"""

from __future__ import annotations

import json
import os
import threading

from jml.core.protocol import ConfigError
from jml.host import paths

#: 支持的 schema 字段类型（type 取值，用于校验）
_SUPPORTED_TYPES = ("string", "integer", "number", "boolean")

#: describe_schema() 输出的界面字段类型（C# 动态表单只认这六种）
UI_FIELD_TYPES = ("text", "password", "number", "bool", "select", "path")

#: schema type → 默认界面字段类型（未声明 enum / ui 时使用）
_UI_TYPE_BY_SCHEMA_TYPE = {
    "string": "text",
    "integer": "number",
    "number": "number",
    "boolean": "bool",
}

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
        # 界面字段类型一次算好；password 项单独记住（不回值、不落盘）
        self._field_types = {
            key: self._ui_type(self._plugin_id, key, rule)
            for key, rule in self._schema.items()
        }
        self._password_keys = frozenset(
            key for key, ftype in self._field_types.items() if ftype == "password"
        )
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

    def validate(self, key: str, value: object) -> object:
        """只校验不写入（桥接层先用它区分"值非法"与"落盘失败"两种错误）。

        校验通过则原样返回值；不通过抛 ConfigError，配置与磁盘均不变。
        """
        with self._lock:
            return self._validate(key, value)

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

    def describe_schema(self) -> list[dict]:
        """把 schema 枚举成设置界面字段列表（顺序与 schema 声明一致）。

        每项::

            {"key": str, "label": str, "type": str,
             "default": <标量>, "value": <当前值>, "options": [<标量>...],
             "min": <标量>(可选), "max": <标量>(可选)}

        - ``type`` 只取 :data:`UI_FIELD_TYPES`（text/password/number/bool/
          select/path）；未声明 ``ui`` 时：有 ``enum`` → select，否则按
          schema type 映射；
        - ``label`` 优先取 schema 的 ``label``，其次 ``title``，都没有用 key；
        - password 项：``value`` 与 ``default`` 恒为空串，另给 ``has_value``
          布尔；**任何凭据值都不出现在返回值里**。
        """
        with self._lock:
            fields: list[dict] = []
            for key, rule in self._schema.items():
                ftype = self._field_types[key]
                value = self._values.get(key, self._defaults.get(key))
                field = {
                    "key": key,
                    "label": str(rule.get("label") or rule.get("title") or key),
                    "type": ftype,
                    "default": self._defaults.get(key),
                    "value": value,
                    "options": (
                        list(rule["enum"]) if isinstance(rule.get("enum"), list) else []
                    ),
                }
                if "min" in rule:
                    field["min"] = rule["min"]
                if "max" in rule:
                    field["max"] = rule["max"]
                if ftype == "password":
                    # 安全红线：密码值/默认值一律不回传，只说明"是否已设置"
                    field["default"] = ""
                    field["value"] = ""
                    field["has_value"] = bool(value)
                fields.append(field)
            return fields

    def is_password_field(self, key: str) -> bool:
        """该配置项是否为 password 类型（值不回传、不落盘）。"""
        return key in self._password_keys

    @property
    def storage_path(self) -> str:
        """配置文件落盘路径（每次按当前数据根计算，供诊断）。"""
        return self._storage_path(self._plugin_id)

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
            if PluginConfig._ui_type(plugin_id, key, rule) == "password":
                # 安全红线：密码项不得在 schema 里带非空默认值（凭据不入 schema/配置）
                if "default" in rule and rule["default"] not in ("", None):
                    raise ConfigError(
                        f"插件 {plugin_id} 的密码项 {key} 不得声明非空 default"
                    )
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
    def _ui_type(plugin_id: str, key: str, rule: dict) -> str:
        """把一个 schema 规则映射成界面字段类型（UI_FIELD_TYPES 之一）。

        规则：显式 ``ui`` 优先；否则有 ``enum`` → select；否则按 schema type
        映射（string→text / integer|number→number / boolean→bool）。
        未知 ``ui`` 视为 schema 写错，构造阶段就抛 ConfigError，尽早暴露。
        """
        hint = rule.get("ui")
        if hint is not None:
            if not isinstance(hint, str) or hint not in UI_FIELD_TYPES:
                raise ConfigError(
                    f"插件 {plugin_id} 的配置项 {key} 的 ui 非法: {hint!r}, "
                    f"允许值: {', '.join(UI_FIELD_TYPES)}"
                )
            return hint
        if isinstance(rule.get("enum"), list):
            return "select"
        return _UI_TYPE_BY_SCHEMA_TYPE[rule.get("type", "string")]

    @staticmethod
    def _storage_path(plugin_id: str) -> str:
        """配置落盘路径：``<plugin_data_root>/{id}/config.json``。

        根目录由 C# 启动时注入（见 :mod:`jml.host.paths`）；未注入时回退
        ``%APPDATA%/JML/plugins``，与旧实现兼容。
        """
        return os.path.join(paths.plugin_data_root(), plugin_id, "config.json")

    def _load_stored(self) -> None:
        """读已落盘的配置，合并到当前值（非法的存量值忽略，保持默认）。"""
        path = self.storage_path
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # 存量文件损坏 → 当作不存在，全部用默认值（不让坏文件炸宿主）
            return
        if not isinstance(stored, dict):
            return
        for key, value in stored.items():
            if key not in self._schema:
                continue  # schema 里已删除的项：忽略存量
            if key in self._password_keys:
                continue  # 密码项从不落盘；存量里的同名项（人工编辑/旧版）也不采用
            try:
                self._values[key] = self._validate(key, value)
            except ConfigError:
                continue  # 存量值不合法 → 忽略，保持默认值（对应旧缺陷 #5）

    def _save(self) -> None:
        """把当前值落盘（原子写：先写临时文件再替换）。

        安全红线：password 项永远不写入 config.json（值只留在进程内存）。
        """
        payload = {
            key: value
            for key, value in self._values.items()
            if key not in self._password_keys
        }
        path = self.storage_path
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except OSError as exc:
            # 面向用户的行动提示：绝大多数写盘失败都是数据目录不可写/被占用。
            # 保留 error_code=IO_ERROR（由桥接层映射），文案给出当前数据目录，
            # 便于用户检查权限或改用可写目录（不含任何凭据）。
            raise ConfigError(
                f"配置写入失败：数据目录不可写（当前数据目录：{directory}）；"
                f"原始错误：{exc}"
            ) from exc

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
