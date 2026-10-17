"""loader.py（依赖环检测 / 拓扑排序 / load_all 流程）单元测试。

覆盖：两插件成环、三插件成环、依赖缺失、拓扑排序被依赖者在前、
load_all 完整流程（成功加载 + 失败记录 + 服务注册表快照）。

load_all 需要真实 import 插件入口模块，所以插件本体写在临时目录里，
由 conftest 的 reset_host_state 夹具在用例前后清理 sys.path 与 sys.modules。
"""

from __future__ import annotations

import pytest

from jml.core.protocol import CircularDependencyError, DependencyError, PluginError
from jml.host import loader
from jml.host.registry import REGISTRY


def _manifest(pid: str, deps: list[str], **overrides) -> dict:
    """拼一份最小合法 manifest（dir 字段对 topo_sort 无意义，占位为空串）。"""
    manifest = {
        "id": pid,
        "name": pid,
        "version": "0.1.0",
        "api_version": 1,
        "description": "",
        "entry": "main.py",
        "dependencies": {"plugins": list(deps)},
        "services": {"provides": [], "requires": []},
        "pages": [{"id": f"{pid}.main", "title": pid, "template": "list"}],
        "config": {},
        "development_sample": False,
        "dir": "",
    }
    manifest.update(overrides)
    return manifest


SIMPLE_ENTRY = "def setup(ctx):\n    pass\n"


def _build(make_plugin, root, specs, files_map=None):
    """在同一插件根目录下按 [(id, deps)] 造插件，返回 {id: 目录 Path}。"""
    root.mkdir(parents=True, exist_ok=True)
    files_map = files_map or {}
    dirs = {}
    for pid, deps in specs:
        manifest = _manifest(pid, deps)
        files = files_map.get(pid, {"main.py": SIMPLE_ENTRY})
        manifest.update(files.pop("__manifest__", {}))
        dirs[pid] = make_plugin(pid, manifest, files=files, root=root)
    return dirs


# ---------------------------------------------------------------------------
# topo_sort：依赖环与缺依赖
# ---------------------------------------------------------------------------


def test_topo_sort_two_plugin_cycle_raises():
    """两插件成环 A→B→A：抛 CircularDependencyError，消息列出两个环成员。"""
    manifests = [
        _manifest("plugin_a", ["plugin_b"]),
        _manifest("plugin_b", ["plugin_a"]),
    ]

    with pytest.raises(CircularDependencyError) as excinfo:
        loader.topo_sort(manifests)

    message = str(excinfo.value)
    assert "plugin_a" in message
    assert "plugin_b" in message


def test_topo_sort_three_plugin_cycle_raises():
    """三插件成环 A→B→C→A：抛 CircularDependencyError，三个成员都在消息里。"""
    manifests = [
        _manifest("plugin_a", ["plugin_b"]),
        _manifest("plugin_b", ["plugin_c"]),
        _manifest("plugin_c", ["plugin_a"]),
    ]

    with pytest.raises(CircularDependencyError) as excinfo:
        loader.topo_sort(manifests)

    message = str(excinfo.value)
    for pid in ("plugin_a", "plugin_b", "plugin_c"):
        assert pid in message


def test_topo_sort_self_cycle_raises():
    """自环 A→A：抛 CircularDependencyError。"""
    with pytest.raises(CircularDependencyError):
        loader.topo_sort([_manifest("plugin_a", ["plugin_a"])])


def test_topo_sort_missing_dependency_raises():
    """依赖缺失：A 依赖不存在的 missing_plugin，抛 DependencyError 且消息含双方。"""
    manifests = [_manifest("plugin_a", ["missing_plugin"])]

    with pytest.raises(DependencyError) as excinfo:
        loader.topo_sort(manifests)

    message = str(excinfo.value)
    assert "plugin_a" in message
    assert "missing_plugin" in message


def test_topo_sort_dependency_before_dependent():
    """拓扑排序正确：单个依赖链 A→B，B 排在 A 前面。"""
    manifests = [
        _manifest("plugin_a", ["plugin_b"]),
        _manifest("plugin_b", []),
    ]

    ordered = loader.topo_sort(manifests)

    assert [m["id"] for m in ordered] == ["plugin_b", "plugin_a"]


def test_topo_sort_chain_order():
    """拓扑排序正确：三层链 C→B→A，结果应为 A, B, C。"""
    manifests = [
        _manifest("plugin_c", ["plugin_b"]),
        _manifest("plugin_a", []),
        _manifest("plugin_b", ["plugin_a"]),
    ]

    ordered = loader.topo_sort(manifests)

    assert [m["id"] for m in ordered] == ["plugin_a", "plugin_b", "plugin_c"]


def test_topo_sort_diamond():
    """拓扑排序正确：菱形依赖 D→B,C；B→A；C→A，A 最前、D 最后。"""
    manifests = [
        _manifest("d", ["b", "c"]),
        _manifest("b", ["a"]),
        _manifest("c", ["a"]),
        _manifest("a", []),
    ]

    ordered = loader.topo_sort(manifests)
    order = [m["id"] for m in ordered]

    assert order[0] == "a"
    assert order[-1] == "d"
    assert set(order) == {"a", "b", "c", "d"}


def test_topo_sort_no_dependencies_is_stable():
    """无依赖：按 id 字典序排，同一输入重复排序结果一致。"""
    manifests = [_manifest("z_plugin", []), _manifest("a_plugin", []), _manifest("m_plugin", [])]

    first = [m["id"] for m in loader.topo_sort(manifests)]
    second = [m["id"] for m in loader.topo_sort(manifests)]

    assert first == ["a_plugin", "m_plugin", "z_plugin"]
    assert first == second


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def test_discover_collects_valid_manifests(make_plugin, tmp_path):
    """discover：扫到两个合法插件目录，返回 2 份 manifest。"""
    root = tmp_path / "plugins"
    make_plugin("p_alpha", _manifest("plugin_alpha", []), root=root)
    make_plugin("p_beta", _manifest("plugin_beta", []), root=root)

    manifests = loader.discover(str(root))

    assert sorted(m["id"] for m in manifests) == ["plugin_alpha", "plugin_beta"]


def test_discover_records_bad_manifest_and_continues(make_plugin, tmp_path):
    """discover 容错：坏 manifest 只记错误，不中断其余目录扫描。"""
    root = tmp_path / "plugins"
    root.mkdir()
    make_plugin("good_one", _manifest("plugin_good", []), root=root)
    bad = root / "bad_one"
    bad.mkdir()
    (bad / "plugin.json").write_text("{ broken", encoding="utf-8")

    manifests = loader.discover(str(root))

    assert [m["id"] for m in manifests] == ["plugin_good"]
    assert any("bad_one" in err for err in loader._LOAD_ERRORS)


def test_discover_missing_dir_raises(tmp_path):
    """discover：插件目录本身不存在 → 抛 PluginError（传错路径属硬错误）。"""
    with pytest.raises(PluginError):
        loader.discover(str(tmp_path / "does_not_exist"))


# ---------------------------------------------------------------------------
# load_all 完整流程
# ---------------------------------------------------------------------------


def test_load_all_success_registers_service(make_plugin, tmp_path):
    """load_all 成功路径：插件被加载，setup 注册的服务出现在 services 快照里。"""
    root = tmp_path / "plugins"
    entry = "def setup(ctx):\n    ctx.register_service('demo.svc', 1, lambda: 'ok')\n"
    manifest = _manifest(
        "demo_plugin",
        [],
        services={"provides": [{"name": "demo.svc", "version": 1}], "requires": []},
    )
    make_plugin("demo", manifest, files={"main.py": entry}, root=root)

    report = loader.load_all(str(root))

    assert [item["id"] for item in report["loaded"]] == ["demo_plugin"]
    assert report["failed"] == []
    assert report["services"] == {"demo.svc": 1}
    assert REGISTRY.resolve("demo.svc", 1)() == "ok"


def test_load_all_reports_page_declaration(make_plugin, tmp_path):
    """load_all 成功路径：loaded 条目带回插件声明的 pages 与 development_sample。"""
    root = tmp_path / "plugins"
    manifest = _manifest("page_plugin", [])
    manifest["pages"] = [{"id": "p1", "title": "首页", "template": "form"}]
    make_plugin("pages", manifest, root=root)

    report = loader.load_all(str(root))

    assert report["loaded"][0]["pages"] == [{"id": "p1", "title": "首页", "template": "form"}]
    assert report["loaded"][0]["development_sample"] is False


def test_load_all_records_setup_failure(make_plugin, tmp_path):
    """load_all 容错：一个插件 setup 抛异常，只记 failed，不影响其它插件。"""
    root = tmp_path / "plugins"
    make_plugin(
        "boom",
        _manifest("boom_plugin", []),
        files={"main.py": "def setup(ctx):\n    raise RuntimeError('boom')\n"},
        root=root,
    )
    make_plugin("fine", _manifest("fine_plugin", []), root=root)

    report = loader.load_all(str(root))

    assert [item["id"] for item in report["loaded"]] == ["fine_plugin"]
    assert [item["id"] for item in report["failed"]] == ["boom_plugin"]
    assert "boom" in report["failed"][0]["reason"]


def test_load_all_records_missing_setup_function(make_plugin, tmp_path):
    """load_all 容错：入口模块没有 setup → 进 failed 并给出明确原因。"""
    root = tmp_path / "plugins"
    make_plugin(
        "nosetup",
        _manifest("nosetup_plugin", []),
        files={"main.py": "value = 1\n"},
        root=root,
    )

    report = loader.load_all(str(root))

    assert report["loaded"] == []
    assert report["failed"][0]["id"] == "nosetup_plugin"
    assert "setup" in report["failed"][0]["reason"]


def test_load_all_records_missing_required_service(make_plugin, tmp_path):
    """load_all 容错：requires 的服务无人提供 → 不加载，原因含服务名与版本。"""
    root = tmp_path / "plugins"
    manifest = _manifest(
        "needy_plugin",
        [],
        services={"provides": [], "requires": [{"name": "absent.svc", "version": 3}]},
    )
    make_plugin("needy", manifest, root=root)

    report = loader.load_all(str(root))

    assert report["loaded"] == []
    assert report["failed"][0]["id"] == "needy_plugin"
    assert "absent.svc" in report["failed"][0]["reason"]
    assert "3" in report["failed"][0]["reason"]


def test_load_all_respects_dependency_order(make_plugin, tmp_path):
    """load_all 顺序：被依赖者先加载并注册服务，依赖方 setup 里 get_service 成功。"""
    root = tmp_path / "plugins"
    provider_entry = "def setup(ctx):\n    ctx.register_service('base.svc', 1, 'base-ref')\n"
    consumer_entry = (
        "def setup(ctx):\n"
        "    ref = ctx.get_service('base.svc', 1)\n"
        "    ctx.register_service('consumer.svc', 1, ref)\n"
    )
    provider_manifest = _manifest(
        "provider_plugin",
        [],
        services={"provides": [{"name": "base.svc", "version": 1}], "requires": []},
    )
    consumer_manifest = _manifest(
        "consumer_plugin",
        ["provider_plugin"],
        services={
            "provides": [{"name": "consumer.svc", "version": 1}],
            "requires": [{"name": "base.svc", "version": 1}],
        },
    )
    make_plugin("provider", provider_manifest, files={"main.py": provider_entry}, root=root)
    make_plugin("consumer", consumer_manifest, files={"main.py": consumer_entry}, root=root)

    report = loader.load_all(str(root))

    assert report["failed"] == []
    assert [item["id"] for item in report["loaded"]] == ["provider_plugin", "consumer_plugin"]
    assert report["services"] == {"base.svc": 1, "consumer.svc": 1}
    assert REGISTRY.resolve("consumer.svc", 1) == "base-ref"


def test_load_all_reports_discover_errors_as_failed(tmp_path):
    """load_all：discover 阶段的坏目录也进 failed，id 用目录名。"""
    root = tmp_path / "plugins"
    root.mkdir()
    bad = root / "broken_dir"
    bad.mkdir()
    (bad / "plugin.json").write_text("[1,2]", encoding="utf-8")

    report = loader.load_all(str(root))

    assert report["loaded"] == []
    assert report["failed"][0]["id"] == "broken_dir"
    assert report["failed"][0]["reason"]


def test_load_all_cycle_surfaces_as_exception(make_plugin, tmp_path):
    """load_all 依赖环：属流程级失败，向上抛 CircularDependencyError 交调用方处理。"""
    root = tmp_path / "plugins"
    make_plugin("cyc_a", _manifest("cyc_a", ["cyc_b"]), root=root)
    make_plugin("cyc_b", _manifest("cyc_b", ["cyc_a"]), root=root)

    with pytest.raises(CircularDependencyError):
        loader.load_all(str(root))


def test_load_all_empty_directory(tmp_path):
    """load_all 空插件目录：loaded / failed 为空列表，services 为空 dict。"""
    root = tmp_path / "plugins"
    root.mkdir()

    report = loader.load_all(str(root))

    assert report == {"loaded": [], "failed": [], "services": {}}
