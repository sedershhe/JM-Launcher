"""config.py（插件配置契约）单元测试。

覆盖：按 schema 生成默认值（内联 dict / 外部 json 文件）、set 合法写入、
set 非法类型 / 越界 / 越枚举 / 未知 key 均抛 ConfigError 且值不变、
required 缺 default 报错、存量文件损坏回退默认、存量含非法值忽略该项。

APPDATA 已由 conftest 的 autouse 夹具指向临时目录，落盘不碰真实用户目录。
"""

from __future__ import annotations

import json
import os

import pytest

from jml.core.protocol import ConfigError
from jml.host.config import PluginConfig


@pytest.fixture
def manifest_with_schema(base_manifest):
    """工厂：给 base_manifest 挂上指定 schema，返回可直接构造 PluginConfig 的 dict。"""

    def _make(schema: dict, plugin_id: str = "demo_plugin") -> dict:
        manifest = dict(base_manifest)
        manifest["id"] = plugin_id
        manifest["config"] = {"schema": schema}
        return manifest

    return _make


def test_defaults_from_inline_schema(manifest_with_schema):
    """内联 dict schema：按 default 生成默认值表；缺 default 的非必填项按类型补零值。"""
    schema = {
        "threads": {"type": "integer", "default": 4, "min": 1, "max": 32},
        "keyword": {"type": "string", "default": "abc"},
        "ratio": {"type": "number", "default": 1.5},
        "enabled": {"type": "boolean", "default": True},
        "note": {"type": "string"},  # 无 default、非 required → 空串
    }
    cfg = PluginConfig(manifest_with_schema(schema))

    assert cfg.defaults() == {
        "threads": 4,
        "keyword": "abc",
        "ratio": 1.5,
        "enabled": True,
        "note": "",
    }
    assert cfg.as_dict() == cfg.defaults()


def test_defaults_from_external_schema_file(manifest_with_schema, make_plugin):
    """外部 schema 文件：manifest.config.schema 写成文件名时，从插件目录读 json。"""
    schema = {"limit": {"type": "integer", "default": 20, "min": 1, "max": 100}}
    manifest = manifest_with_schema("config.schema.json")
    plugin_dir = make_plugin(
        "cfg_plugin",
        manifest,
        files={
            "main.py": "def setup(ctx):\n    pass\n",
            "config.schema.json": json.dumps(schema, ensure_ascii=False),
        },
    )
    manifest["dir"] = str(plugin_dir)

    cfg = PluginConfig(manifest)

    assert cfg.defaults() == {"limit": 20}
    assert cfg.schema() == schema


def test_external_schema_file_missing_raises(manifest_with_schema, make_plugin):
    """外部 schema 文件不存在：应抛 ConfigError，消息含插件 id。"""
    manifest = manifest_with_schema("no_such_schema.json")
    plugin_dir = make_plugin("cfg_missing", manifest)
    manifest["dir"] = str(plugin_dir)

    with pytest.raises(ConfigError) as excinfo:
        PluginConfig(manifest)

    assert "demo_plugin" in str(excinfo.value)


def test_empty_schema_yields_empty_defaults(manifest_with_schema):
    """未声明 config：schema 为空 dict，默认值表也为空，不报错。"""
    manifest = dict(manifest_with_schema({}))
    manifest["config"] = {}

    cfg = PluginConfig(manifest)

    assert cfg.defaults() == {}
    assert cfg.as_dict() == {}


def test_set_valid_value_persists(manifest_with_schema):
    """set 合法值：写入成功，get / as_dict 都能读到，并真的落盘。"""
    schema = {"threads": {"type": "integer", "default": 4, "min": 1, "max": 32}}
    cfg = PluginConfig(manifest_with_schema(schema))

    cfg.set("threads", 16)

    assert cfg.get("threads") == 16
    assert cfg.as_dict() == {"threads": 16}
    with open(cfg.storage_path, "r", encoding="utf-8") as fh:
        assert json.load(fh) == {"threads": 16}


def test_set_wrong_type_raises_and_keeps_value(manifest_with_schema):
    """set 非法类型：integer 项写字符串，抛 ConfigError 且原值不变。"""
    schema = {"threads": {"type": "integer", "default": 4}}
    cfg = PluginConfig(manifest_with_schema(schema))

    with pytest.raises(ConfigError) as excinfo:
        cfg.set("threads", "8")

    assert "类型非法" in str(excinfo.value)
    assert cfg.get("threads") == 4


def test_set_below_min_raises_and_keeps_value(manifest_with_schema):
    """set 低于 min：threads=0（min=1）抛 ConfigError，原值不变。"""
    schema = {"threads": {"type": "integer", "default": 4, "min": 1, "max": 32}}
    cfg = PluginConfig(manifest_with_schema(schema))

    with pytest.raises(ConfigError) as excinfo:
        cfg.set("threads", 0)

    assert "小于最小值" in str(excinfo.value)
    assert cfg.get("threads") == 4


def test_set_above_max_raises_and_keeps_value(manifest_with_schema):
    """set 高于 max：threads=999（max=32）抛 ConfigError，原值不变。"""
    schema = {"threads": {"type": "integer", "default": 4, "min": 1, "max": 32}}
    cfg = PluginConfig(manifest_with_schema(schema))

    with pytest.raises(ConfigError) as excinfo:
        cfg.set("threads", 999)

    assert "大于最大值" in str(excinfo.value)
    assert cfg.get("threads") == 4


def test_set_outside_enum_raises_and_keeps_value(manifest_with_schema):
    """set 不在 enum 内：mode 写了第四个值，抛 ConfigError，原值不变。"""
    schema = {
        "mode": {"type": "string", "default": "fast", "enum": ["fast", "safe", "auto"]}
    }
    cfg = PluginConfig(manifest_with_schema(schema))

    with pytest.raises(ConfigError) as excinfo:
        cfg.set("mode", "turbo")

    assert "枚举" in str(excinfo.value)
    assert cfg.get("mode") == "fast"


def test_set_inside_enum_succeeds(manifest_with_schema):
    """set 命中 enum：合法值写入成功。"""
    schema = {
        "mode": {"type": "string", "default": "fast", "enum": ["fast", "safe", "auto"]}
    }
    cfg = PluginConfig(manifest_with_schema(schema))

    cfg.set("mode", "safe")

    assert cfg.get("mode") == "safe"


def test_set_unknown_key_raises(manifest_with_schema):
    """set schema 里不存在的 key：抛 ConfigError，且不新增任何项。"""
    schema = {"threads": {"type": "integer", "default": 4}}
    cfg = PluginConfig(manifest_with_schema(schema))

    with pytest.raises(ConfigError) as excinfo:
        cfg.set("not_in_schema", 1)

    assert "not_in_schema" in str(excinfo.value)
    assert cfg.as_dict() == {"threads": 4}


def test_validate_only_checks_without_writing(manifest_with_schema):
    """validate 只校验：合法返回值，非法抛 ConfigError，都不改当前值与磁盘。"""
    schema = {"threads": {"type": "integer", "default": 4, "min": 1, "max": 32}}
    cfg = PluginConfig(manifest_with_schema(schema))

    assert cfg.validate("threads", 8) == 8
    assert cfg.get("threads") == 4
    assert not os.path.exists(cfg.storage_path)

    with pytest.raises(ConfigError):
        cfg.validate("threads", 99)
    with pytest.raises(ConfigError):
        cfg.validate("not_in_schema", 1)
    assert cfg.as_dict() == {"threads": 4}


def test_required_without_default_raises(manifest_with_schema):
    """required=true 但缺 default：构造阶段就抛 ConfigError（不静默给零值）。"""
    schema = {"token": {"type": "string", "required": True}}

    with pytest.raises(ConfigError) as excinfo:
        PluginConfig(manifest_with_schema(schema))

    assert "缺少 default" in str(excinfo.value)


def test_unsupported_type_raises(manifest_with_schema):
    """schema 里 type 不在支持列表：构造阶段抛 ConfigError。"""
    schema = {"weird": {"type": "object", "default": {}}}

    with pytest.raises(ConfigError) as excinfo:
        PluginConfig(manifest_with_schema(schema))

    assert "type 非法" in str(excinfo.value)


def test_bad_default_value_raises(manifest_with_schema):
    """schema 写错 default（类型都不对）：构造阶段就抛 ConfigError，尽早暴露。"""
    schema = {"threads": {"type": "integer", "default": "four"}}

    with pytest.raises(ConfigError):
        PluginConfig(manifest_with_schema(schema))


def test_broken_stored_config_falls_back_to_defaults(manifest_with_schema):
    """存量配置文件损坏（非合法 JSON）：构造不崩溃，全部回退默认值。"""
    schema = {"threads": {"type": "integer", "default": 4, "min": 1, "max": 32}}
    manifest = manifest_with_schema(schema)
    cfg = PluginConfig(manifest)  # 先建一次拿到落盘路径
    os.makedirs(os.path.dirname(cfg.storage_path), exist_ok=True)
    with open(cfg.storage_path, "w", encoding="utf-8") as fh:
        fh.write("{ this is broken json")

    cfg2 = PluginConfig(manifest)

    assert cfg2.as_dict() == {"threads": 4}


def test_stored_illegal_value_ignored(manifest_with_schema):
    """存量含非法值：该项忽略保持默认，同一文件里的合法项正常生效。"""
    schema = {
        "threads": {"type": "integer", "default": 4, "min": 1, "max": 32},
        "keyword": {"type": "string", "default": "abc"},
    }
    manifest = manifest_with_schema(schema)
    cfg = PluginConfig(manifest)
    os.makedirs(os.path.dirname(cfg.storage_path), exist_ok=True)
    with open(cfg.storage_path, "w", encoding="utf-8") as fh:
        json.dump({"threads": 9999, "keyword": "hello"}, fh)  # threads 越界

    cfg2 = PluginConfig(manifest)

    assert cfg2.get("threads") == 4  # 非法 → 回退默认
    assert cfg2.get("keyword") == "hello"  # 合法 → 采用存量


def test_stored_unknown_key_ignored(manifest_with_schema):
    """存量含 schema 里已删除的 key：忽略，不写进当前值。"""
    schema = {"threads": {"type": "integer", "default": 4}}
    manifest = manifest_with_schema(schema)
    cfg = PluginConfig(manifest)
    os.makedirs(os.path.dirname(cfg.storage_path), exist_ok=True)
    with open(cfg.storage_path, "w", encoding="utf-8") as fh:
        json.dump({"threads": 8, "removed_key": "x"}, fh)

    cfg2 = PluginConfig(manifest)

    assert cfg2.as_dict() == {"threads": 8}
    assert cfg2.get("removed_key") is None


def test_get_unknown_key_returns_default_arg(manifest_with_schema):
    """get 未知 key：返回调用方给的 default 参数，而不是抛异常。"""
    schema = {"threads": {"type": "integer", "default": 4}}
    cfg = PluginConfig(manifest_with_schema(schema))

    assert cfg.get("nope") is None
    assert cfg.get("nope", 42) == 42


def test_get_known_key_ignores_default_arg(manifest_with_schema):
    """get 已知 key：即使传了 default 参数，也返回真实值。"""
    schema = {"threads": {"type": "integer", "default": 4}}
    cfg = PluginConfig(manifest_with_schema(schema))

    assert cfg.get("threads", 999) == 4


def test_defaults_returns_copy(manifest_with_schema):
    """defaults() 返回拷贝：改动返回值不应污染配置内部默认值。"""
    schema = {"threads": {"type": "integer", "default": 4}}
    cfg = PluginConfig(manifest_with_schema(schema))

    defaults = cfg.defaults()
    defaults["threads"] = 999

    assert cfg.get("threads") == 4


def test_storage_path_under_appdata(manifest_with_schema):
    """落盘路径：位于 APPDATA/JML/plugins/{id}/config.json，且带插件 id。"""
    cfg = PluginConfig(manifest_with_schema({}, plugin_id="my_plugin"))

    path = cfg.storage_path
    assert path.endswith(os.path.join("JML", "plugins", "my_plugin", "config.json"))
    assert "my_plugin" in path


# ---------------------------------------------------------------------------
# describe_schema（设置界面字段枚举）与 password 安全语义
# ---------------------------------------------------------------------------


def test_describe_schema_maps_all_ui_types(manifest_with_schema):
    """六种界面类型：number/text/bool/select/path/password，顺序与 schema 一致。"""
    schema = {
        "threads": {"type": "integer", "default": 4, "min": 1, "max": 32, "label": "并发数"},
        "keyword": {"type": "string", "default": "abc"},
        "enabled": {"type": "boolean", "default": True},
        "mode": {"type": "string", "default": "fast", "enum": ["fast", "safe"]},
        "work_dir": {"type": "string", "default": "", "ui": "path"},
        "token": {"type": "string", "default": "", "ui": "password"},
    }
    cfg = PluginConfig(manifest_with_schema(schema))

    fields = cfg.describe_schema()

    assert [field["key"] for field in fields] == list(schema)
    by_key = {field["key"]: field for field in fields}
    assert [field["type"] for field in fields] == [
        "number",
        "text",
        "bool",
        "select",
        "path",
        "password",
    ]
    assert by_key["threads"]["label"] == "并发数"
    assert by_key["threads"]["min"] == 1
    assert by_key["threads"]["max"] == 32
    assert by_key["keyword"]["label"] == "keyword"  # 缺省用 key
    assert by_key["mode"]["options"] == ["fast", "safe"]
    assert by_key["mode"]["value"] == "fast"
    assert by_key["work_dir"]["value"] == ""


def test_describe_schema_unknown_ui_hint_raises(manifest_with_schema):
    """未知 ui 提示：构造阶段抛 ConfigError（schema 写错尽早暴露）。"""
    schema = {"thing": {"type": "string", "default": "x", "ui": "slider"}}

    with pytest.raises(ConfigError) as excinfo:
        PluginConfig(manifest_with_schema(schema))

    assert "ui 非法" in str(excinfo.value)


def test_password_value_never_returned_nor_persisted(manifest_with_schema):
    """password 项：describe_schema 不回值，只给 has_value；config.json 不含它。"""
    schema = {
        "threads": {"type": "integer", "default": 4},
        "token": {"type": "string", "ui": "password"},
    }
    cfg = PluginConfig(manifest_with_schema(schema))
    secret = "CONFIG-SECRET-9"

    before = {f["key"]: f for f in cfg.describe_schema()}["token"]
    assert before["value"] == ""
    assert before["has_value"] is False

    cfg.set("token", secret)

    after = {f["key"]: f for f in cfg.describe_schema()}["token"]
    assert after["value"] == ""
    assert after["has_value"] is True
    assert secret not in json.dumps(cfg.describe_schema(), ensure_ascii=False)
    # 插件本进程仍能读到（只对 C# 隐藏）
    assert cfg.get("token") == secret

    with open(cfg.storage_path, "r", encoding="utf-8") as fh:
        stored_text = fh.read()
    assert "token" not in stored_text
    assert secret not in stored_text


def test_password_non_empty_default_rejected(manifest_with_schema):
    """password 项不得在 schema 里声明非空默认值（凭据不入 schema）。"""
    schema = {"token": {"type": "string", "ui": "password", "default": "hunter2"}}

    with pytest.raises(ConfigError) as excinfo:
        PluginConfig(manifest_with_schema(schema))

    assert "密码项" in str(excinfo.value)
    assert "不得声明非空 default" in str(excinfo.value)


def test_password_empty_default_is_allowed(manifest_with_schema):
    """空默认值（常见写法）允许，且不会被当成已设置。"""
    cfg = PluginConfig(
        manifest_with_schema(
            {"token": {"type": "string", "ui": "password", "default": ""}}
        )
    )

    token = {f["key"]: f for f in cfg.describe_schema()}["token"]
    assert token["value"] == ""
    assert token["has_value"] is False


def test_stored_password_value_ignored(manifest_with_schema):
    """存量 config.json 里若有人手写了 password 值：不采用（保持未设置）。"""
    schema = {"token": {"type": "string", "ui": "password"}}
    cfg = PluginConfig(manifest_with_schema(schema))
    os.makedirs(os.path.dirname(cfg.storage_path), exist_ok=True)
    with open(cfg.storage_path, "w", encoding="utf-8") as fh:
        json.dump({"token": "LEAKED"}, fh)

    cfg2 = PluginConfig(manifest_with_schema(schema))

    assert cfg2.get("token") == ""
    assert cfg2.is_password_field("token") is True
    assert cfg2.is_password_field("other") is False
