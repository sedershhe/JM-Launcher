"""插件结构化页面协议测试（技术方案第三节方案 A）。

覆盖：
- ``plugin_page_load`` / ``plugin_page_action`` / ``plugin_page_image`` 三个
  入口的路由与返回信封；
- 未知插件 / 未声明页面 / 未实现钩子的 ``error_code``；
- 参数透传（``params_json`` / ``payload_json`` → 插件钩子的 dict）；
- 图片走独立入口返回原始 bytes（不 base64 进 JSON）；
- 注册不能跨插件越权（ctx 签名里没有 plugin_id，未声明页面注册被拒）；
- 并发注册显式加锁；
- 重新装配 / reset 后注册表清空。

全部离线：插件是临时目录里的假实现，不联网。
"""

from __future__ import annotations

import json
import threading

import pytest

import jml_backend
from jml.core import diagnostics
from jml.core.errors import (
    ERROR_INVALID_ARGUMENT,
    ERROR_INVALID_DATA,
    ERROR_NOT_FOUND,
    ERROR_NOT_IMPLEMENTED,
)
from jml.host import loader
from jml.host import pages as page_host


@pytest.fixture(autouse=True)
def _reset_backend_state():
    """bootstrap 幂等标记在 jml_backend._STATE 里，跨用例必须清掉。"""
    jml_backend.reset_host_state()
    yield
    jml_backend.reset_host_state()


#: 假插件：注册 list 页面 + 动作 + 图片，并把收到的参数原样回显
DEMO_ENTRY = """
def setup(ctx):
    def load_page(params):
        return {"echo": params}

    def handle_action(payload):
        return {
            "refresh": bool(payload.get("want_refresh")),
            "message": "动作已处理",
            "data": {"echo": payload},
        }

    def image(index):
        return b"IMAGE-" + str(index).encode("ascii")

    ctx.register_page("demo.main", load_page)
    ctx.register_page_action("demo.main", "open", handle_action)
    ctx.register_page_image("demo.main", image)
"""

#: 只注册加载钩子、不注册图片/动作的插件
LOAD_ONLY_ENTRY = """
def setup(ctx):
    ctx.register_page("demo.main", lambda params: {"echo": params})
"""


def _bootstrap(make_plugin, base_manifest, root, entry=DEMO_ENTRY):
    make_plugin("demo", base_manifest, files={"main.py": entry}, root=root)
    report = json.loads(jml_backend.bootstrap(str(root)))
    assert report["ok"] is True
    return report


# ---------------------------------------------------------------------------
# plugin_page_load：路由 + 信封
# ---------------------------------------------------------------------------


def test_page_load_returns_manifest_template_and_plugin_payload(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    raw = jml_backend.plugin_page_load("demo_plugin", "demo.main", '{"page": 2}')

    assert isinstance(raw, str)  # C# 只收 JSON 字符串
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["template"] == "list"  # 模板来自 manifest，不是插件返回值
    assert data["title"] == "演示主页"
    assert data["data"]["echo"] == {"page": 2}


def test_page_load_passes_empty_object_when_params_blank(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    for blank in ("", "   ", None):
        data = json.loads(jml_backend.plugin_page_load("demo_plugin", "demo.main", blank))
        assert data["ok"] is True
        assert data["data"]["echo"] == {}


def test_page_load_invalid_params_json_is_invalid_argument(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    data = json.loads(jml_backend.plugin_page_load("demo_plugin", "demo.main", "{坏 JSON"))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_ARGUMENT

    not_object = json.loads(
        jml_backend.plugin_page_load("demo_plugin", "demo.main", "[1, 2]")
    )
    assert not_object["ok"] is False
    assert not_object["error_code"] == ERROR_INVALID_ARGUMENT


def test_page_load_unknown_plugin_is_not_found(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()

    data = json.loads(jml_backend.plugin_page_load("no_such_plugin", "demo.main", "{}"))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_NOT_FOUND
    assert "no_such_plugin" in data["error_message"]


def test_page_load_undeclared_page_is_not_found(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    data = json.loads(jml_backend.plugin_page_load("demo_plugin", "demo.other", "{}"))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_NOT_FOUND
    assert "demo.other" in data["error_message"]


def test_page_load_without_hook_is_not_implemented(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    # 只注册 setup 但什么都不声明钩子的插件
    _bootstrap(make_plugin, base_manifest, root, entry="def setup(ctx):\n    pass\n")

    data = json.loads(jml_backend.plugin_page_load("demo_plugin", "demo.main", "{}"))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_NOT_IMPLEMENTED
    assert "register_page" in data["error_message"]


def test_page_load_loader_exception_becomes_error_payload(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    entry = (
        "def setup(ctx):\n"
        "    def boom(params):\n"
        "        raise ValueError('页面数据坏了')\n"
        "    ctx.register_page('demo.main', boom)\n"
    )
    _bootstrap(make_plugin, base_manifest, root, entry=entry)

    data = json.loads(jml_backend.plugin_page_load("demo_plugin", "demo.main", "{}"))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_DATA
    assert "页面数据坏了" in data["error_message"]


def test_page_load_non_dict_payload_is_rejected(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    entry = (
        "def setup(ctx):\n"
        "    ctx.register_page('demo.main', lambda params: ['not', 'a', 'dict'])\n"
    )
    _bootstrap(make_plugin, base_manifest, root, entry=entry)

    data = json.loads(jml_backend.plugin_page_load("demo_plugin", "demo.main", "{}"))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_DATA
    assert "dict" in data["error_message"]


# ---------------------------------------------------------------------------
# plugin_page_action
# ---------------------------------------------------------------------------


def test_page_action_routes_and_normalizes_envelope(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    data = json.loads(
        jml_backend.plugin_page_action(
            "demo_plugin", "demo.main", "open", '{"want_refresh": true, "row": 3}'
        )
    )

    assert data["ok"] is True
    assert data["refresh"] is True
    assert data["message"] == "动作已处理"
    assert data["data"]["echo"] == {"want_refresh": True, "row": 3}


def test_page_action_defaults_when_handler_returns_bare_dict(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    entry = (
        "def setup(ctx):\n"
        "    ctx.register_page('demo.main', lambda params: {})\n"
        "    ctx.register_page_action('demo.main', 'noop', lambda payload: {})\n"
    )
    _bootstrap(make_plugin, base_manifest, root, entry=entry)

    data = json.loads(jml_backend.plugin_page_action("demo_plugin", "demo.main", "noop", ""))

    assert data == {
        "ok": True,
        "refresh": False,
        "data": None,
        "message": "",
    }


def test_page_action_passes_through_optional_intent(
    make_plugin, base_manifest, tmp_path
):
    """阶段 4-4：插件返回 intent 时宿主原样透传；没有 intent 时信封不含该键。"""
    root = tmp_path / "plugins"
    entry = (
        "def setup(ctx):\n"
        "    ctx.register_page('demo.main', lambda params: {})\n"
        "    ctx.register_page_action(\n"
        "        'demo.main', 'detail',\n"
        "        lambda payload: {'intent': {'kind': 'detail',"
        " 'book_id': payload['row']['book_id']}},\n"
        "    )\n"
        "    ctx.register_page_action('demo.main', 'noop', lambda payload: {})\n"
    )
    _bootstrap(make_plugin, base_manifest, root, entry=entry)

    with_intent = json.loads(
        jml_backend.plugin_page_action(
            "demo_plugin", "demo.main", "detail", '{"row": {"book_id": "123"}}'
        )
    )
    assert with_intent["ok"] is True
    assert with_intent["intent"] == {"kind": "detail", "book_id": "123"}
    assert with_intent["refresh"] is False
    assert with_intent["data"] is None

    without_intent = json.loads(
        jml_backend.plugin_page_action("demo_plugin", "demo.main", "noop", "")
    )
    assert "intent" not in without_intent


def test_page_action_unknown_action_is_not_implemented(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    data = json.loads(
        jml_backend.plugin_page_action("demo_plugin", "demo.main", "missing", "{}")
    )

    assert data["ok"] is False
    assert data["error_code"] == ERROR_NOT_IMPLEMENTED
    assert "missing" in data["error_message"]


def test_page_action_invalid_payload_json_is_invalid_argument(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    data = json.loads(
        jml_backend.plugin_page_action("demo_plugin", "demo.main", "open", "not json")
    )

    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_ARGUMENT


def test_page_action_unknown_plugin_is_not_found(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()

    data = json.loads(
        jml_backend.plugin_page_action("ghost", "demo.main", "open", "{}")
    )

    assert data["ok"] is False
    assert data["error_code"] == ERROR_NOT_FOUND


# ---------------------------------------------------------------------------
# plugin_page_image
# ---------------------------------------------------------------------------


def test_page_image_returns_raw_bytes(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)

    assert jml_backend.plugin_page_image("demo_plugin", "demo.main", 3) == b"IMAGE-3"
    assert jml_backend.plugin_page_image("demo_plugin", "demo.main", 0) == b"IMAGE-0"


def test_page_image_returns_none_when_hook_missing(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root, entry=LOAD_ONLY_ENTRY)

    assert jml_backend.plugin_page_image("demo_plugin", "demo.main", 0) is None


def test_page_image_returns_none_for_unknown_plugin_and_page(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()

    assert jml_backend.plugin_page_image("ghost", "demo.main", 0) is None
    assert jml_backend.plugin_page_image("", "", 0) is None


def test_page_image_failure_is_recorded_in_diagnostics(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    entry = (
        "def setup(ctx):\n"
        "    def broken(index):\n"
        "        raise OSError('图片读不出来')\n"
        "    ctx.register_page('demo.main', lambda params: {})\n"
        "    ctx.register_page_image('demo.main', broken)\n"
    )
    _bootstrap(make_plugin, base_manifest, root, entry=entry)

    assert jml_backend.plugin_page_image("demo_plugin", "demo.main", 0) is None
    assert any("PAGE_IMAGE_FAILED" in line for line in diagnostics.recent())


def test_page_image_non_bytes_result_returns_none(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    entry = (
        "def setup(ctx):\n"
        "    ctx.register_page('demo.main', lambda params: {})\n"
        "    ctx.register_page_image('demo.main', lambda index: 'not bytes')\n"
    )
    _bootstrap(make_plugin, base_manifest, root, entry=entry)

    assert jml_backend.plugin_page_image("demo_plugin", "demo.main", 0) is None


# ---------------------------------------------------------------------------
# 注册越权防护
# ---------------------------------------------------------------------------


def test_registration_rejects_page_not_declared_in_manifest(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    entry = (
        "def setup(ctx):\n"
        "    ctx.register_page('not.declared', lambda params: {})\n"
    )
    make_plugin("demo", base_manifest, files={"main.py": entry}, root=root)

    report = json.loads(jml_backend.bootstrap(str(root)))

    assert report["loaded"] == []
    failed = {item["id"]: item["reason"] for item in report["failed"]}
    assert "not.declared" in failed["demo_plugin"]
    assert "未在 manifest" in failed["demo_plugin"]
    # 装配失败后不留任何页面注册
    assert page_host.PAGE_REGISTRY.registered_pages() == []


def test_plugin_cannot_register_another_plugins_page(
    make_plugin, base_manifest, tmp_path
):
    """A 注册 demo.main；B 试图注册同一个 id（未在自己的 manifest 声明）→ 拒绝且不覆盖 A。"""
    root = tmp_path / "plugins"

    manifest_a = {
        **base_manifest,
        "id": "plugin_a",
        "name": "插件A",
        "pages": [{"id": "demo.main", "title": "A 页面", "template": "list"}],
    }
    entry_a = (
        "def setup(ctx):\n"
        "    ctx.register_page('demo.main', lambda params: {'owner': 'A'})\n"
    )
    make_plugin("plugin_a", manifest_a, files={"main.py": entry_a}, root=root)

    manifest_b = {
        **base_manifest,
        "id": "plugin_b",
        "name": "插件B",
        "pages": [{"id": "b.main", "title": "B 页面", "template": "list"}],
    }
    entry_b = (
        "def setup(ctx):\n"
        "    # 没有 plugin_id 参数，只能填页面 id；这里填的是 A 的页面\n"
        "    ctx.register_page('demo.main', lambda params: {'owner': 'B'})\n"
    )
    make_plugin("plugin_b", manifest_b, files={"main.py": entry_b}, root=root)

    report = json.loads(jml_backend.bootstrap(str(root)))

    assert [item["id"] for item in report["loaded"]] == ["plugin_a"]
    failed = {item["id"]: item["reason"] for item in report["failed"]}
    assert "plugin_b" in failed
    assert "demo.main" in failed["plugin_b"]

    data = json.loads(jml_backend.plugin_page_load("plugin_a", "demo.main", "{}"))
    assert data["data"]["owner"] == "A"  # A 的加载器没被覆盖


def test_ctx_exposes_no_plugin_id_parameter_for_page_registration():
    """页面注册方法签名里没有 plugin_id：结构上无法指定别人的插件。"""
    import inspect

    from jml.host.plugin_context import PluginContext

    for name in ("register_page", "register_page_action", "register_page_image"):
        parameters = list(inspect.signature(getattr(PluginContext, name)).parameters)
        assert "plugin_id" not in parameters


# ---------------------------------------------------------------------------
# 并发注册与生命周期
# ---------------------------------------------------------------------------


def test_concurrent_page_registration_is_thread_safe(
    make_plugin, base_manifest, tmp_path
):
    count = 60
    root = tmp_path / "plugins"
    manifest = {
        **base_manifest,
        "pages": [
            {"id": f"demo.p{i}", "title": f"页{i}", "template": "list"}
            for i in range(count)
        ],
    }
    entry = f"""
import threading

def setup(ctx):
    def make(index):
        return lambda params: {{"index": index, "echo": params}}

    threads = [
        threading.Thread(target=ctx.register_page, args=("demo.p" + str(i), make(i)))
        for i in range({count})
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
"""
    make_plugin("demo", manifest, files={"main.py": entry}, root=root)
    report = json.loads(jml_backend.bootstrap(str(root)))
    assert report["ok"] is True
    assert [item["id"] for item in report["loaded"]] == ["demo_plugin"]

    registered = page_host.PAGE_REGISTRY.registered_pages()
    assert len(registered) == count
    for i in range(count):
        data = json.loads(jml_backend.plugin_page_load("demo_plugin", f"demo.p{i}", "{}"))
        assert data["ok"] is True
        assert data["data"]["index"] == i


def test_page_registry_uses_explicit_lock():
    """注册表必须有显式锁（旧缺陷 #10：不依赖 GIL 侥幸）。"""
    assert isinstance(page_host.PAGE_REGISTRY._lock, type(threading.RLock()))


def test_reassembly_clears_stale_page_registrations(
    make_plugin, base_manifest, tmp_path
):
    root = tmp_path / "plugins"
    _bootstrap(make_plugin, base_manifest, root)
    assert page_host.PAGE_REGISTRY.registered_pages() == [("demo_plugin", "demo.main")]

    loader.reset()

    assert page_host.PAGE_REGISTRY.registered_pages() == []
    data = json.loads(jml_backend.plugin_page_load("demo_plugin", "demo.main", "{}"))
    assert data["ok"] is False
    assert data["error_code"] == ERROR_NOT_FOUND
