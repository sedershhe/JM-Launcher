"""插件设置项入口（list_plugin_settings / set_plugin_setting）测试。

覆盖（对应阶段 3 任务 3-B）：
- 已加载插件按 manifest 的 config.schema.json 枚举字段（类型/标签/默认值/当前值/
  枚举/min/max）；
- 合法写入落到隔离 APPDATA 下的 ``JML/plugins/{id}/config.json`` 并可被重新读出；
- 非法类型 / 枚举外 / 越界 / 未知 key 一律 INVALID_ARGUMENT，内存与磁盘原值不变；
- 未知或未加载插件 → NOT_FOUND；入口永不向 C# 抛异常；
- password 类型不回值、不落盘（安全红线）。

夹具全部是临时目录里的假插件（``make_plugin`` 造的 plugin.json +
config.schema.json），**不往仓库 plugins/ 放真插件**；零联网。
"""

from __future__ import annotations

import json
import os

import pytest

import jml_backend
from jml.core.protocol import ConfigError
from jml.host import loader


def _settings_schema() -> dict:
    """覆盖全部六种界面字段类型的 schema（password 只允许空默认值）。"""
    return {
        "threads": {
            "type": "integer",
            "default": 4,
            "min": 1,
            "max": 32,
            "label": "并发数",
        },
        "keyword": {"type": "string", "default": "abc"},
        "enabled": {"type": "boolean", "default": True},
        "mode": {"type": "string", "default": "fast", "enum": ["fast", "safe", "auto"]},
        "work_dir": {"type": "string", "default": "", "ui": "path"},
        "token": {"type": "string", "default": "", "ui": "password"},
    }


@pytest.fixture
def settings_plugin(make_plugin, base_manifest):
    """夹具：造一个带 config.schema.json 的临时插件目录（返回插件目录 Path）。

    用法::

        plugin_dir = settings_plugin()                 # 默认六种字段
        plugin_dir = settings_plugin("other", "别名", {"x": {...}})
        loader.load_all(str(plugin_dir.parent))         # 真的装配（插件列表才有它）
    """

    def _make(
        plugin_id: str = "settings_demo",
        name: str = "设置演示插件",
        schema: dict | None = None,
    ):
        manifest = dict(base_manifest)
        manifest["id"] = plugin_id
        manifest["name"] = name
        manifest["config"] = {"schema": "config.schema.json"}
        return make_plugin(
            plugin_id,
            manifest,
            files={
                "main.py": "def setup(ctx):\n    pass\n",
                "config.schema.json": json.dumps(
                    schema if schema is not None else _settings_schema(),
                    ensure_ascii=False,
                ),
            },
        )

    return _make


def _load(plugin_dir) -> None:
    """装配插件目录的上一级（load_all 要求 plugins 根目录）。"""
    report = loader.load_all(str(plugin_dir.parent))
    assert report["failed"] == []


def _fields(data: dict) -> dict[str, dict]:
    """把 list_plugin_settings 的第一个插件的字段按 key 索引。"""
    assert data["ok"] is True
    assert len(data["plugins"]) == 1
    return {field["key"]: field for field in data["plugins"][0]["fields"]}


def _config_path(plugin_id: str = "settings_demo") -> str:
    """当前用例隔离 APPDATA 下该插件的配置文件路径。"""
    return os.path.join(
        os.environ["APPDATA"], "JML", "plugins", plugin_id, "config.json"
    )


# ---------------------------------------------------------------------------
# list_plugin_settings：枚举已加载插件的设置定义
# ---------------------------------------------------------------------------


def test_list_before_bootstrap_returns_empty_ok():
    """没有任何已加载插件时：ok=true + 空列表（不是错误）。"""
    raw = jml_backend.list_plugin_settings()
    assert isinstance(raw, str)
    assert json.loads(raw) == {"ok": True, "plugins": []}


def test_list_returns_plugin_fields_with_all_six_types(settings_plugin):
    _load(settings_plugin())

    data = json.loads(jml_backend.list_plugin_settings())
    group = data["plugins"][0]

    assert group["plugin_id"] == "settings_demo"
    assert group["plugin_name"] == "设置演示插件"
    fields = _fields(data)
    assert [field["type"] for field in group["fields"]] == [
        "number",
        "text",
        "bool",
        "select",
        "path",
        "password",
    ]

    # label 缺省用 key；显式 label 优先
    assert fields["threads"]["label"] == "并发数"
    assert fields["keyword"]["label"] == "keyword"

    # 默认值与当前值一致（默认值回填）
    assert fields["threads"]["default"] == 4
    assert fields["threads"]["value"] == 4
    assert fields["threads"]["min"] == 1
    assert fields["threads"]["max"] == 32
    assert fields["keyword"]["value"] == "abc"
    assert fields["enabled"]["default"] is True
    assert fields["enabled"]["value"] is True
    assert fields["mode"]["options"] == ["fast", "safe", "auto"]
    assert fields["mode"]["value"] == "fast"
    # 没有 min/max 的项不带这两个键（C# DTO 读为 null）
    assert "min" not in fields["keyword"]
    assert "max" not in fields["keyword"]


def test_list_omits_plugins_without_schema(tmp_path, make_plugin, base_manifest):
    """未声明 config.schema 的插件没有设置项，不出现在返回列表。"""
    manifest = dict(base_manifest)
    manifest["id"] = "no_settings"
    manifest["name"] = "无设置插件"
    manifest["config"] = {}
    plugin_dir = make_plugin("no_settings", manifest, root=tmp_path / "plugins")
    _load(plugin_dir)

    data = json.loads(jml_backend.list_plugin_settings())

    assert data == {"ok": True, "plugins": []}


def test_list_survives_host_failure(settings_plugin, monkeypatch):
    """枚举过程抛异常时也必须返回失败 JSON（入口不向 C# 抛异常）。"""
    _load(settings_plugin())

    def _boom():
        raise RuntimeError("枚举炸了")

    monkeypatch.setattr(loader, "loaded_plugins", _boom)
    data = json.loads(jml_backend.list_plugin_settings())

    assert data["ok"] is False
    assert data["error_code"] == "UNKNOWN_ERROR"
    assert data["error_message"]


# ---------------------------------------------------------------------------
# set_plugin_setting：合法写入与落盘
# ---------------------------------------------------------------------------


def test_set_valid_value_writes_config_under_isolated_appdata(settings_plugin):
    _load(settings_plugin())

    raw = jml_backend.set_plugin_setting("settings_demo", "threads", "16")
    data = json.loads(raw)

    assert data == {"ok": True, "key": "threads", "value": 16}
    path = _config_path()
    assert os.path.isfile(path)
    with open(path, "r", encoding="utf-8") as fh:
        stored = json.load(fh)
    assert stored["threads"] == 16
    assert stored["mode"] == "fast"  # 其它项按当前值一起落盘
    assert "token" not in stored  # password 项永不落盘

    # 再读一次：当前值已更新
    fields = _fields(json.loads(jml_backend.list_plugin_settings()))
    assert fields["threads"]["value"] == 16


def test_set_accepts_every_json_scalar_type(settings_plugin):
    _load(settings_plugin())

    assert json.loads(
        jml_backend.set_plugin_setting("settings_demo", "keyword", '"新文本"')
    )["value"] == "新文本"
    assert json.loads(
        jml_backend.set_plugin_setting("settings_demo", "enabled", "false")
    )["value"] is False
    assert json.loads(
        jml_backend.set_plugin_setting("settings_demo", "mode", '"safe"')
    )["value"] == "safe"
    assert json.loads(
        jml_backend.set_plugin_setting("settings_demo", "work_dir", '"D:/下载"')
    )["value"] == "D:/下载"


def test_set_numeric_enum_option(tmp_path, make_plugin, base_manifest):
    """enum 里是数字时也能正确校验与回写（select 的 options 不限于字符串）。"""
    manifest = dict(base_manifest)
    manifest["id"] = "num_select"
    manifest["config"] = {"schema": {"level": {"type": "integer", "default": 1, "enum": [1, 2, 3]}}}
    plugin_dir = make_plugin("num_select", manifest, root=tmp_path / "plugins")
    _load(plugin_dir)

    data = json.loads(jml_backend.set_plugin_setting("num_select", "level", "2"))

    assert data == {"ok": True, "key": "level", "value": 2}
    fields = _fields(json.loads(jml_backend.list_plugin_settings()))
    assert fields["level"]["type"] == "select"
    assert fields["level"]["options"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 非法值：拒绝写入并保持原值
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value_json", "needle"),
    [
        ("threads", '"8"', "类型非法"),
        ("mode", '"turbo"', "枚举"),
        ("threads", "0", "小于最小值"),
        ("threads", "99", "大于最大值"),
        ("not_in_schema", "1", "没有配置项"),
        ("enabled", '"yes"', "类型非法"),
    ],
    ids=["wrong_type", "outside_enum", "below_min", "above_max", "unknown_key", "bool_type"],
)
def test_invalid_value_rejected_and_original_kept(
    settings_plugin, key, value_json, needle
):
    _load(settings_plugin())
    before = _fields(json.loads(jml_backend.list_plugin_settings()))

    data = json.loads(jml_backend.set_plugin_setting("settings_demo", key, value_json))

    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"
    assert needle in data["error_message"]
    # 内存与磁盘都没有被写：非法写入不创建配置文件
    after = _fields(json.loads(jml_backend.list_plugin_settings()))
    assert after == before
    assert not os.path.exists(_config_path())


def test_invalid_value_does_not_clobber_existing_config(settings_plugin):
    """先合法写一次，再非法写：磁盘上的合法值保持不变。"""
    _load(settings_plugin())
    json.loads(jml_backend.set_plugin_setting("settings_demo", "threads", "16"))

    bad = json.loads(jml_backend.set_plugin_setting("settings_demo", "threads", "999"))

    assert bad["ok"] is False
    assert bad["error_code"] == "INVALID_ARGUMENT"
    with open(_config_path(), "r", encoding="utf-8") as fh:
        assert json.load(fh)["threads"] == 16


def test_invalid_json_value_rejected(settings_plugin):
    _load(settings_plugin())

    data = json.loads(jml_backend.set_plugin_setting("settings_demo", "threads", "{oops"))

    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"
    assert "JSON" in data["error_message"]


def test_unknown_plugin_is_not_found():
    data = json.loads(jml_backend.set_plugin_setting("ghost_plugin", "threads", "1"))

    assert data["ok"] is False
    assert data["error_code"] == "NOT_FOUND"
    assert "ghost_plugin" in data["error_message"]


@pytest.mark.parametrize(
    ("plugin_id", "key"),
    [("", "threads"), ("settings_demo", ""), ("   ", "threads"), ("settings_demo", "  ")],
    ids=["empty_plugin", "empty_key", "blank_plugin", "blank_key"],
)
def test_empty_plugin_or_key_is_invalid_argument(plugin_id, key):
    data = json.loads(jml_backend.set_plugin_setting(plugin_id, key, "1"))

    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"


def test_set_after_plugin_unloaded_is_not_found(settings_plugin):
    _load(settings_plugin())
    loader.reset()

    data = json.loads(jml_backend.set_plugin_setting("settings_demo", "threads", "1"))

    assert data["ok"] is False
    assert data["error_code"] == "NOT_FOUND"


def test_write_failure_maps_to_io_error(settings_plugin, monkeypatch):
    """校验通过但落盘失败：返回 IO_ERROR（不能误报成 INVALID_ARGUMENT）。"""
    _load(settings_plugin())
    store = loader.config_for("settings_demo")
    assert store is not None

    def _boom():
        raise ConfigError("插件 settings_demo 的配置写入失败: 模拟磁盘错误")

    monkeypatch.setattr(store, "_save", _boom)
    data = json.loads(jml_backend.set_plugin_setting("settings_demo", "threads", "16"))

    assert data["ok"] is False
    assert data["error_code"] == "IO_ERROR"
    assert "写入失败" in data["error_message"]


# ---------------------------------------------------------------------------
# password 类型：不回值、不落盘
# ---------------------------------------------------------------------------


def test_password_value_never_returned_nor_persisted(settings_plugin):
    secret = "PLUGIN-SECRET-42"
    _load(settings_plugin())

    raw = jml_backend.set_plugin_setting(
        "settings_demo", "token", json.dumps(secret)
    )
    data = json.loads(raw)

    assert data == {"ok": True, "key": "token", "value": "", "has_value": True}
    assert secret not in raw

    listed_raw = jml_backend.list_plugin_settings()
    assert secret not in listed_raw
    token = _fields(json.loads(listed_raw))["token"]
    assert token["type"] == "password"
    assert token["value"] == ""
    assert token["has_value"] is True

    with open(_config_path(), "r", encoding="utf-8") as fh:
        stored_text = fh.read()
    assert "token" not in stored_text
    assert secret not in stored_text


def test_password_has_value_false_before_set(settings_plugin):
    _load(settings_plugin())

    token = _fields(json.loads(jml_backend.list_plugin_settings()))["token"]

    assert token["value"] == ""
    assert token["has_value"] is False


def test_password_rejects_non_string_value(settings_plugin):
    _load(settings_plugin())

    data = json.loads(jml_backend.set_plugin_setting("settings_demo", "token", "123"))

    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"
    # 密码项的错误文案固定，不回显传入值/类型原文
    assert "password" in data["error_message"]
    assert "不回显" in data["error_message"]
    assert "has_value" not in data
    assert _fields(json.loads(jml_backend.list_plugin_settings()))["token"]["has_value"] is False


def test_password_validation_error_does_not_echo_value(
    tmp_path, make_plugin, base_manifest
):
    """密码项校验失败时，错误文案也不能带出用户输入的值。"""
    manifest = dict(base_manifest)
    manifest["id"] = "secret_enum"
    manifest["config"] = {
        "schema": {
            "token": {
                "type": "string",
                "ui": "password",
                "enum": ["alpha", "beta"],
            }
        }
    }
    plugin_dir = make_plugin("secret_enum", manifest, root=tmp_path / "plugins")
    _load(plugin_dir)
    secret = "LEAKED-SECRET-3"

    raw = jml_backend.set_plugin_setting("secret_enum", "token", json.dumps(secret))
    data = json.loads(raw)

    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"
    assert secret not in raw
    assert secret not in data["error_message"]
    assert "password" in data["error_message"]


# ---------------------------------------------------------------------------
# 入口永不向 C# 抛异常
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (None, None, None),
        (object(), object(), object()),
        ("ghost", "threads", "not-json"),
        ("", "", ""),
    ],
    ids=["none", "objects", "unknown_plugin", "blank"],
)
def test_set_plugin_setting_never_raises(args):
    raw = jml_backend.set_plugin_setting(*args)

    assert isinstance(raw, str)
    assert "ok" in json.loads(raw)
