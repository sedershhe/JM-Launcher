"""manifest.py（plugin.json 校验）单元测试。

覆盖：协议版本不符、必填字段逐项缺失、entry 目录穿越、entry 文件不存在、
services/pages/dependencies 各形状错误，以及全字段合法的成功路径。
"""

from __future__ import annotations

import json

import pytest

from jml.core.protocol import PluginError, ProtocolVersionError
from jml.host.manifest import load_manifest

# # 用例清单（共 33 个）
# # ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("id", "name", "version", "api_version", "entry", "services", "pages")


def test_api_version_not_supported_raises_protocol_version_error(base_manifest, make_plugin):
    """协议版本不符：api_version 写成 2，应抛 ProtocolVersionError 且消息含插件 id。"""
    base_manifest["api_version"] = 2
    plugin_dir = make_plugin("bad_version", base_manifest)

    with pytest.raises(ProtocolVersionError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "demo_plugin" in str(excinfo.value)


def test_api_version_bool_raises_protocol_version_error(base_manifest, make_plugin):
    """协议版本类型错：api_version 写成布尔 True（bool 是 int 子类），应拒绝。"""
    base_manifest["api_version"] = True
    plugin_dir = make_plugin("bool_version", base_manifest)

    with pytest.raises(ProtocolVersionError):
        load_manifest(str(plugin_dir))


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_raises_plugin_error(base_manifest, make_plugin, field):
    """缺必填字段：逐个删掉 id/name/version/api_version/entry/services/pages，均应抛 PluginError。"""
    base_manifest.pop(field)
    plugin_dir = make_plugin(f"missing_{field}", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert field in str(excinfo.value)


def test_entry_escape_outside_plugin_dir_raises(base_manifest, make_plugin):
    """entry 目录穿越：entry 写成 ../outside.py，应抛 PluginError（不得指向插件目录外）。"""
    base_manifest["entry"] = "../outside.py"
    plugin_dir = make_plugin("escape", base_manifest)
    # 穿越目标文件真实存在，确保触发的是"越界"校验而非"文件不存在"
    (plugin_dir.parent / "outside.py").write_text("def setup(ctx): pass", encoding="utf-8")

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "目录之外" in str(excinfo.value)


def test_entry_not_py_file_raises(base_manifest, make_plugin):
    """entry 不是 .py：写成 main.txt，应抛 PluginError。"""
    base_manifest["entry"] = "main.txt"
    plugin_dir = make_plugin("not_py", base_manifest, files={"main.txt": "x"})

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert ".py" in str(excinfo.value)


def test_entry_file_missing_raises(base_manifest, make_plugin):
    """entry 文件不存在：manifest 里声明 main.py 但目录里没有，应抛 PluginError。"""
    plugin_dir = make_plugin("no_entry", base_manifest, files={})
    # 工厂夹具在 files 为空时仍会写入默认入口，这里显式删掉它造出"文件缺失"
    (plugin_dir / "main.py").unlink()

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "入口文件不存在" in str(excinfo.value)


def test_services_provides_not_list_raises(base_manifest, make_plugin):
    """services.provides 非数组：写成对象，应抛 PluginError。"""
    base_manifest["services"]["provides"] = {"name": "demo.svc", "version": 1}
    plugin_dir = make_plugin("provides_obj", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "provides" in str(excinfo.value)


def test_services_requires_not_list_raises(base_manifest, make_plugin):
    """services.requires 非数组：写成字符串，应抛 PluginError。"""
    base_manifest["services"]["requires"] = "demo.other"
    plugin_dir = make_plugin("requires_str", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "requires" in str(excinfo.value)


def test_services_block_not_dict_raises(base_manifest, make_plugin):
    """services 块本身非对象：写成数组，应抛 PluginError。"""
    base_manifest["services"] = []
    plugin_dir = make_plugin("services_list", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "services" in str(excinfo.value)


@pytest.mark.parametrize("block", ["provides", "requires"])
@pytest.mark.parametrize("field", ["name", "version"])
def test_service_item_missing_field_raises(base_manifest, make_plugin, block, field):
    """服务项缺 name/version：provides 与 requires 各测一遍，均应抛 PluginError。"""
    item = {"name": "demo.svc", "version": 1}
    item.pop(field)
    base_manifest["services"][block] = [item]
    plugin_dir = make_plugin(f"{block}_no_{field}", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert field in str(excinfo.value)


def test_service_item_version_not_int_raises(base_manifest, make_plugin):
    """服务项 version 非整数：写成 "1" 字符串，应抛 PluginError。"""
    base_manifest["services"]["provides"] = [{"name": "demo.svc", "version": "1"}]
    plugin_dir = make_plugin("svc_ver_str", base_manifest)

    with pytest.raises(PluginError):
        load_manifest(str(plugin_dir))


def test_service_item_name_empty_raises(base_manifest, make_plugin):
    """服务项 name 为空串：应抛 PluginError。"""
    base_manifest["services"]["provides"] = [{"name": "", "version": 1}]
    plugin_dir = make_plugin("svc_name_empty", base_manifest)

    with pytest.raises(PluginError):
        load_manifest(str(plugin_dir))


def test_page_template_not_allowed_raises(base_manifest, make_plugin):
    """pages template 不在允许列表：写成 "free_canvas"，应抛 PluginError 且消息含允许值。"""
    base_manifest["pages"] = [{"id": "p1", "title": "页", "template": "free_canvas"}]
    plugin_dir = make_plugin("bad_template", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "list" in str(excinfo.value)


@pytest.mark.parametrize("field", ["id", "title", "template"])
def test_page_item_missing_field_raises(base_manifest, make_plugin, field):
    """pages 项缺 id/title/template：逐个删，均应抛 PluginError。"""
    page = {"id": "p1", "title": "页", "template": "list"}
    page.pop(field)
    base_manifest["pages"] = [page]
    plugin_dir = make_plugin(f"page_no_{field}", base_manifest)

    with pytest.raises(PluginError):
        load_manifest(str(plugin_dir))


def test_pages_not_list_raises(base_manifest, make_plugin):
    """pages 非数组：写成对象，应抛 PluginError。"""
    base_manifest["pages"] = {"id": "p1", "title": "页", "template": "list"}
    plugin_dir = make_plugin("pages_obj", base_manifest)

    with pytest.raises(PluginError):
        load_manifest(str(plugin_dir))


def test_pages_duplicate_id_raises(base_manifest, make_plugin):
    """pages 有重复页面 id：应抛 PluginError。"""
    page = {"id": "p1", "title": "页", "template": "list"}
    base_manifest["pages"] = [page, dict(page)]
    plugin_dir = make_plugin("page_dup", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "重复" in str(excinfo.value)


def test_dependencies_plugins_not_str_list_raises(base_manifest, make_plugin):
    """dependencies.plugins 含非字符串项：应抛 PluginError。"""
    base_manifest["dependencies"] = {"plugins": ["other_plugin", 123]}
    plugin_dir = make_plugin("deps_bad", base_manifest)

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "dependencies.plugins" in str(excinfo.value)


def test_dependencies_plugins_not_list_raises(base_manifest, make_plugin):
    """dependencies.plugins 非数组：写成字符串，应抛 PluginError。"""
    base_manifest["dependencies"] = {"plugins": "other_plugin"}
    plugin_dir = make_plugin("deps_str", base_manifest)

    with pytest.raises(PluginError):
        load_manifest(str(plugin_dir))


def test_manifest_file_missing_raises(tmp_path):
    """manifest 文件缺失：插件目录里没有 plugin.json，应抛 PluginError 且消息含目录名。"""
    plugin_dir = tmp_path / "no_manifest"
    plugin_dir.mkdir()

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "no_manifest" in str(excinfo.value)


def test_manifest_json_broken_raises(tmp_path):
    """manifest JSON 损坏：内容不是合法 JSON，应抛 PluginError。"""
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "损坏" in str(excinfo.value)


def test_manifest_top_level_not_object_raises(tmp_path):
    """manifest 顶层不是 JSON 对象：写成数组，应抛 PluginError。"""
    plugin_dir = tmp_path / "toplist"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(PluginError) as excinfo:
        load_manifest(str(plugin_dir))

    assert "顶层" in str(excinfo.value)


def test_valid_manifest_returns_normalized_dict(base_manifest, make_plugin):
    """成功路径：全字段合法时返回规范化 dict（含 dir，services/pages 结构归一）。"""
    plugin_dir = make_plugin("good", base_manifest)

    result = load_manifest(str(plugin_dir))

    assert result["id"] == "demo_plugin"
    assert result["name"] == "演示插件"
    assert result["version"] == "0.1.0"
    assert result["api_version"] == 1
    assert result["entry"] == "main.py"
    assert result["description"] == "用于单元测试的演示插件"
    assert result["development_sample"] is False
    assert result["dependencies"] == {"plugins": []}
    assert result["services"]["provides"] == [{"name": "demo.svc", "version": 1}]
    assert result["services"]["requires"] == []
    assert result["pages"] == [{"id": "demo.main", "title": "演示主页", "template": "list"}]
    assert result["config"] == {}
    assert result["dir"] == str(plugin_dir.resolve())


def test_valid_manifest_fills_optional_defaults(base_manifest, make_plugin):
    """成功路径补默认值：缺省 description / dependencies / config 时自动补齐为空值。"""
    for field in ("description", "dependencies", "config", "development_sample"):
        base_manifest.pop(field, None)
    plugin_dir = make_plugin("minimal", base_manifest)

    result = load_manifest(str(plugin_dir))

    assert result["description"] == ""
    assert result["dependencies"] == {"plugins": []}
    assert result["config"] == {}
    assert result["development_sample"] is False


def test_valid_manifest_keeps_page_order(base_manifest, make_plugin):
    """成功路径保序：多个合法页面按声明顺序返回，不重排。"""
    base_manifest["pages"] = [
        {"id": "p_z", "title": "尾页", "template": "form"},
        {"id": "p_a", "title": "首页", "template": "browser"},
    ]
    plugin_dir = make_plugin("ordered", base_manifest)

    result = load_manifest(str(plugin_dir))

    assert [p["id"] for p in result["pages"]] == ["p_z", "p_a"]
    assert [p["template"] for p in result["pages"]] == ["form", "browser"]


def test_valid_manifest_reads_inline_config_schema(base_manifest, make_plugin):
    """成功路径携带 config：内联 schema 原样保留在返回 dict 里。"""
    schema = {"limit": {"type": "integer", "default": 20, "min": 1, "max": 100}}
    base_manifest["config"] = {"schema": schema}
    plugin_dir = make_plugin("with_schema", base_manifest)

    result = load_manifest(str(plugin_dir))

    assert result["config"]["schema"] == schema
    # 返回的是拷贝，改返回值不应影响再次读取
    result["config"]["schema"]["limit"]["default"] = 999
    again = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    assert again["config"]["schema"]["limit"]["default"] == 20
