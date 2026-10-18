"""插件启用/停用（阶段 4-5）离线测试。

覆盖：
- 被停用插件不 import、不 setup，failed 原因"插件已停用"；
- 依赖被停用插件 → 明确"缺少依赖插件 xxx（该插件已停用）"，不崩；
- disabled.json 原子写与容错读取；
- set_plugin_enabled 后 list_plugins / bootstrap 状态变化（重启生效语义）；
- dependents 多级依赖计算；
- 管理状态字段（enabled/status/reason/dependents）；
- 非法 id → NOT_FOUND。

全部离线：插件本体写在临时目录，APPDATA 由 conftest 指向临时目录。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

import jml_backend
from jml.host import loader, plugin_state


@pytest.fixture(autouse=True)
def _reset_backend_state():
    """清空 jml_backend 的 bootstrap 幂等缓存（否则用例之间互相复用结果）。"""
    jml_backend.reset_host_state()
    yield
    jml_backend.reset_host_state()


def _manifest(
    pid: str,
    deps: list[str] | None = None,
    *,
    description: str = "",
) -> dict:
    """最小合法 manifest（不声明服务，避免多插件测试互相抢注册名）。"""
    return {
        "id": pid,
        "name": f"插件{pid}",
        "version": "0.1.0",
        "api_version": 1,
        "description": description,
        "entry": "main.py",
        "dependencies": {"plugins": list(deps or [])},
        "services": {"provides": [], "requires": []},
        "pages": [{"id": f"{pid}.main", "title": f"插件{pid}", "template": "list"}],
        "config": {},
        "development_sample": False,
    }


SIMPLE_ENTRY = "def setup(ctx):\n    pass\n"


def _failed_by_id(report: dict) -> dict[str, dict]:
    return {item["id"]: item for item in report["failed"]}


# ---------------------------------------------------------------------------
# loader：停用真正阻止加载；依赖方明确拒绝
# ---------------------------------------------------------------------------


def test_disabled_plugin_is_not_imported_or_setup(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    # 入口一旦被 import 就会抛异常；用它证明"停用插件根本没被 import"。
    make_plugin(
        "a",
        _manifest("plugin_a"),
        files={"main.py": "raise RuntimeError('停用插件不应被 import')\n"},
        root=root,
    )
    plugin_state.save_disabled_ids(["plugin_a"])

    report = loader.load_all(
        str(root), plugin_state.load_disabled_ids()
    )

    assert report["loaded"] == []
    entry = _failed_by_id(report)["plugin_a"]
    assert entry["reason"] == "插件已停用"
    assert entry["enabled"] is False
    assert entry["blocked_reason"] == "插件已停用"
    assert "_jml_plugin_plugin_a" not in sys.modules


def test_dependent_of_disabled_plugin_is_rejected_with_clear_reason(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)
    make_plugin(
        "b",
        _manifest("plugin_b", ["plugin_a"]),
        files={"main.py": "raise RuntimeError('依赖方不应被 import')\n"},
        root=root,
    )
    plugin_state.save_disabled_ids(["plugin_a"])

    report = loader.load_all(
        str(root), plugin_state.load_disabled_ids()
    )

    assert report["loaded"] == []
    failed = _failed_by_id(report)
    assert failed["plugin_a"]["reason"] == "插件已停用"
    assert "缺少依赖插件 plugin_a" in failed["plugin_b"]["reason"]
    assert "已停用" in failed["plugin_b"]["reason"]
    assert failed["plugin_b"]["enabled"] is True
    assert "_jml_plugin_plugin_b" not in sys.modules


def test_transitive_dependent_is_also_rejected(make_plugin, tmp_path):
    """A ← B ← C：停用 A，B 明确拒绝；C 也因 B 未加载被拒绝。"""
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)
    make_plugin("b", _manifest("plugin_b", ["plugin_a"]), root=root)
    make_plugin("c", _manifest("plugin_c", ["plugin_b"]), root=root)
    plugin_state.save_disabled_ids(["plugin_a"])

    report = loader.load_all(str(root), plugin_state.load_disabled_ids())

    failed = _failed_by_id(report)
    assert "缺少依赖插件 plugin_a" in failed["plugin_b"]["reason"]
    assert "缺少依赖插件 plugin_b" in failed["plugin_c"]["reason"]
    assert report["loaded"] == []


# ---------------------------------------------------------------------------
# disabled.json：原子写、排序去重、损坏容错
# ---------------------------------------------------------------------------


def test_disabled_json_atomic_write_and_read():
    saved = plugin_state.save_disabled_ids(["plugin_b", "plugin_a", "plugin_a"])

    assert saved == ["plugin_a", "plugin_b"]
    path = plugin_state.disabled_plugins_path()
    with open(path, "r", encoding="utf-8") as handle:
        assert json.load(handle) == ["plugin_a", "plugin_b"]
    # 原子写完成后不留临时文件
    assert not os.path.exists(path + ".tmp")
    assert plugin_state.load_disabled_ids() == ["plugin_a", "plugin_b"]


def test_disabled_json_corrupt_or_non_list_is_treated_as_empty():
    path = plugin_state.disabled_plugins_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ 不是数组")
    assert plugin_state.load_disabled_ids() == []

    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"not": "a list"}, handle)
    assert plugin_state.load_disabled_ids() == []


def test_resolve_disabled_ids_blank_uses_persisted_store():
    plugin_state.save_disabled_ids(["plugin_a"])

    assert plugin_state.resolve_disabled_ids("") == ["plugin_a"]
    assert plugin_state.resolve_disabled_ids(None) == ["plugin_a"]
    assert plugin_state.resolve_disabled_ids('["plugin_b"]') == ["plugin_b"]


# ---------------------------------------------------------------------------
# 后端入口：状态变化 / NOT_FOUND / 管理状态
# ---------------------------------------------------------------------------


def test_set_plugin_enabled_changes_list_and_bootstrap_state(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)

    boot = json.loads(jml_backend.bootstrap(str(root)))
    assert [item["id"] for item in boot["loaded"]] == ["plugin_a"]
    assert boot["loaded"][0]["enabled"] is True
    assert boot["loaded"][0]["blocked_reason"] == ""

    disabled = json.loads(jml_backend.set_plugin_enabled("plugin_a", False))
    assert disabled["ok"] is True
    assert disabled["enabled"] is False
    assert disabled["status"] == "disabled"

    listed = json.loads(jml_backend.list_plugins(str(root)))
    assert listed["loaded"] == []
    assert listed["failed"][0]["id"] == "plugin_a"
    assert listed["failed"][0]["reason"] == "插件已停用"
    assert listed["failed"][0]["enabled"] is False

    # 未重启：bootstrap 结果的状态立即变化（但不热加载）
    boot_again = json.loads(jml_backend.bootstrap(str(root)))
    assert boot_again["loaded"] == []
    assert boot_again["failed"][0]["id"] == "plugin_a"
    assert boot_again["failed"][0]["enabled"] is False

    enabled = json.loads(jml_backend.set_plugin_enabled("plugin_a", True))
    assert enabled["enabled"] is True
    assert enabled["status"] == "enabled"

    listed_again = json.loads(jml_backend.list_plugins(str(root)))
    assert [item["id"] for item in listed_again["loaded"]] == ["plugin_a"]

    # 启动时确实装过 → 重新启用后 bootstrap 回到 loaded 状态
    boot_third = json.loads(jml_backend.bootstrap(str(root)))
    assert [item["id"] for item in boot_third["loaded"]] == ["plugin_a"]


def test_set_plugin_enabled_unknown_id_returns_not_found(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)
    jml_backend.bootstrap(str(root))

    data = json.loads(jml_backend.set_plugin_enabled("no_such_plugin", False))

    assert data["ok"] is False
    assert data["error_code"] == "NOT_FOUND"
    assert "no_such_plugin" in data["error_message"]


def test_set_plugin_enabled_empty_id_is_invalid_argument():
    data = json.loads(jml_backend.set_plugin_enabled("", True))
    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"


def test_bootstrap_accepts_explicit_disabled_json(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)
    make_plugin("b", _manifest("plugin_b"), root=root)

    data = json.loads(jml_backend.bootstrap(str(root), '["plugin_a"]'))

    assert [item["id"] for item in data["loaded"]] == ["plugin_b"]
    assert _failed_by_id(data)["plugin_a"]["reason"] == "插件已停用"
    # 显式列表不落盘（唯一真源仍由 set_plugin_enabled 写入）
    assert plugin_state.load_disabled_ids() == []


def test_management_state_fields_and_dependents(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a", description="被依赖"), root=root)
    make_plugin("b", _manifest("plugin_b", ["plugin_a"]), root=root)
    make_plugin("c", _manifest("plugin_c", ["plugin_b"]), root=root)
    make_plugin("d", _manifest("plugin_d"), root=root)
    jml_backend.bootstrap(str(root))
    plugin_state.save_disabled_ids(["plugin_d"])

    data = json.loads(jml_backend.get_plugin_management_state(str(root)))

    assert data["ok"] is True
    by_id = {item["id"]: item for item in data["plugins"]}
    assert set(by_id) == {"plugin_a", "plugin_b", "plugin_c", "plugin_d"}

    assert by_id["plugin_a"]["status"] == "loaded"
    assert by_id["plugin_a"]["enabled"] is True
    assert by_id["plugin_a"]["reason"] == ""
    assert by_id["plugin_a"]["dependents"] == ["plugin_b", "plugin_c"]
    assert by_id["plugin_b"]["dependents"] == ["plugin_c"]
    assert by_id["plugin_c"]["dependents"] == []
    assert by_id["plugin_a"]["description"] == "被依赖"

    assert by_id["plugin_d"]["status"] == "disabled"
    assert by_id["plugin_d"]["enabled"] is False
    assert by_id["plugin_d"]["reason"] == "插件已停用"


def test_management_state_dependent_of_disabled_is_failed(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)
    make_plugin("b", _manifest("plugin_b", ["plugin_a"]), root=root)
    jml_backend.bootstrap(str(root))
    plugin_state.save_disabled_ids(["plugin_a"])

    data = json.loads(jml_backend.get_plugin_management_state(str(root)))
    by_id = {item["id"]: item for item in data["plugins"]}

    assert by_id["plugin_a"]["status"] == "disabled"
    assert by_id["plugin_b"]["status"] == "failed"
    assert "缺少依赖插件 plugin_a" in by_id["plugin_b"]["reason"]
    assert by_id["plugin_b"]["enabled"] is True


def test_management_state_failed_plugin_reports_reason(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)
    make_plugin(
        "boom",
        _manifest("boom_plugin"),
        files={"main.py": "def setup(ctx):\n    raise RuntimeError('炸了')\n"},
        root=root,
    )
    jml_backend.bootstrap(str(root))

    data = json.loads(jml_backend.get_plugin_management_state(str(root)))
    by_id = {item["id"]: item for item in data["plugins"]}

    assert by_id["plugin_a"]["status"] == "loaded"
    assert by_id["boom_plugin"]["status"] == "failed"
    assert "炸了" in by_id["boom_plugin"]["reason"]


def test_list_plugins_carries_enabled_and_blocked_reason(make_plugin, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("a", _manifest("plugin_a"), root=root)
    plugin_state.save_disabled_ids(["plugin_a"])

    data = json.loads(jml_backend.list_plugins(str(root)))

    entry = data["failed"][0]
    assert entry["enabled"] is False
    assert entry["blocked_reason"] == "插件已停用"
