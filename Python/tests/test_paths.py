"""数据目录（jml.host.paths + set_data_root）测试。

覆盖：
- 默认根保持旧行为（%APPDATA%/JML、<config_root>/plugins、log_dir=None）；
- configure 后 config.py / loader.py / plugin_state.py 的拼路径全部跟随新根；
- set_data_root 对不可写目录返回 INVALID_ARGUMENT，且不切换当前根；
- set_data_root 成功后插件配置与插件数据目录落到新根；
- 写探针是实测（目录 + 临时文件 + 删除），不是只看目录存在。

全部离线；APPDATA 由 conftest 指向临时目录，真实用户目录不被读写。
"""

from __future__ import annotations

import json
import os

import pytest

import jml_backend
from jml.core import diagnostics
from jml.host import config, loader, paths, plugin_state


@pytest.fixture(autouse=True)
def _reset_backend():
    jml_backend.reset_host_state()
    yield
    jml_backend.reset_host_state()


# ---------------------------------------------------------------------------
# 默认值与显式配置
# ---------------------------------------------------------------------------


def test_default_roots_follow_appdata(monkeypatch, tmp_path):
    """未注入时与旧实现一致：APPDATA/JML 与 APPDATA/JML/plugins。"""
    paths.reset()
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))

    assert paths.config_root() == os.path.join(str(tmp_path / "roaming"), "JML")
    assert paths.plugin_data_root() == os.path.join(
        str(tmp_path / "roaming"), "JML", "plugins"
    )
    assert paths.log_dir() is None


def test_configure_switches_all_path_builders(tmp_path):
    """configure 后三个模块拼路径都走新根，且不再读 APPDATA。"""
    # 先在默认根下构造配置对象，确认已存在实例也会跟随新根（不缓存旧路径）
    store = config.PluginConfig({"id": "demo"})
    default_path = store.storage_path

    config_root = tmp_path / "cfg"
    plugin_root = config_root / "plugins"
    log_root = tmp_path / "logs"
    paths.configure(str(config_root), str(plugin_root), str(log_root))

    assert paths.config_root() == str(config_root)
    assert paths.plugin_data_root() == str(plugin_root)
    assert paths.log_dir() == str(log_root)

    assert store.storage_path == os.path.join(
        str(plugin_root), "demo", "config.json"
    )
    assert store.storage_path != default_path
    assert loader.plugin_data_dir("demo") == os.path.join(
        str(plugin_root), "demo", "data"
    )
    assert plugin_state.disabled_plugins_path() == os.path.join(
        str(plugin_root), "disabled.json"
    )


def test_probe_writable_requires_real_write(tmp_path):
    assert paths.probe_writable(tmp_path / "ok") is True
    assert paths.probe_writable("") is False

    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert paths.probe_writable(blocker / "child") is False


# ---------------------------------------------------------------------------
# set_data_root
# ---------------------------------------------------------------------------


def test_set_data_root_rejects_unwritable_directory(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    before = paths.config_root()

    raw = jml_backend.set_data_root(
        str(blocker / "cfg"), str(blocker / "plugins"), ""
    )
    data = json.loads(raw)

    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"
    assert "不可写" in data["error_message"]
    # 失败不切换当前根
    assert paths.config_root() == before
    assert paths.log_dir() is None


def test_set_data_root_rejects_empty_paths():
    data = json.loads(jml_backend.set_data_root("", "", ""))
    assert data["ok"] is False
    assert data["error_code"] == "INVALID_ARGUMENT"


def test_set_data_root_switches_plugin_config_and_data_dir(
    tmp_path, make_plugin, base_manifest
):
    manifest = dict(base_manifest)
    manifest["id"] = "paths_demo"
    manifest["config"] = {
        "schema": {"threads": {"type": "integer", "default": 4, "min": 1, "max": 32}}
    }
    plugin_dir = make_plugin(
        "paths_demo",
        manifest,
        files={"main.py": "def setup(ctx):\n    pass\n"},
    )

    config_root = tmp_path / "cfg"
    plugin_root = config_root / "plugins"
    log_root = tmp_path / "logs"

    result = json.loads(
        jml_backend.set_data_root(str(config_root), str(plugin_root), str(log_root))
    )
    assert result["ok"] is True
    assert result["config_root"] == str(config_root)
    assert result["plugin_data_root"] == str(plugin_root)
    assert result["log_dir"] == str(log_root)
    assert diagnostics.current_log_dir() == str(log_root)

    report = json.loads(jml_backend.bootstrap(str(plugin_dir.parent)))
    assert report["ok"] is True
    assert [item["id"] for item in report["loaded"]] == ["paths_demo"]

    written = json.loads(
        jml_backend.set_plugin_setting("paths_demo", "threads", "8")
    )
    assert written["ok"] is True

    config_path = os.path.join(str(plugin_root), "paths_demo", "config.json")
    assert os.path.isfile(config_path)
    with open(config_path, encoding="utf-8") as handle:
        assert json.load(handle)["threads"] == 8

    # 旧默认根下不应出现这次写入
    old_root = os.path.join(os.environ["APPDATA"], "JML", "plugins")
    assert not os.path.isfile(
        os.path.join(old_root, "paths_demo", "config.json")
    )

    # 插件数据目录也跟随新根
    assert os.path.isdir(
        os.path.join(str(plugin_root), "paths_demo", "data")
    )
    # disabled.json 跟随新根
    jml_backend.set_plugin_enabled("paths_demo", False)
    assert os.path.isfile(os.path.join(str(plugin_root), "disabled.json"))
