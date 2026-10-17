"""pytest 公共夹具：保证每个用例之间的宿主状态隔离。

提供三类能力：
1. ``tmp_path``（覆盖内置实现）——自建临时目录并显式控制权限与回收：
   内置实现固定用 0o700 建目录，在受限环境（沙箱 / CI 受限账户）下会出现
   "目录建得出来但写不进去"的情况，这里改为默认权限并在用例结束后 rmtree。
   语义与内置 tmp_path 一致：函数级、每个用例一个独立目录。
2. ``isolated_appdata``（autouse）——把 APPDATA 指向临时目录，配置测试
   不会读写真实用户目录下的 %APPDATA%/JML/...。
3. ``reset_host_state``（autouse）——每个用例前后清空服务注册表、事件总线
   与 loader 模块级状态，并还原 sys.path、清理测试期导入的插件模块。

另外提供 ``base_manifest``（合法 manifest 模板）与 ``make_plugin``
（造临时插件目录的工厂）两个夹具，供各测试文件复用。

临时目录根的选择顺序：环境变量 JML_TEST_TMPROOT → 系统临时目录 →
仓库根下的 _pytest_tmp（哪个可写用哪个）。

本文件不 import 旧项目（jm_app / jmhost_api / plugins 等）的任何代码。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid

import pytest

# 让测试能 import 到 Python/jml 包：本文件位于 Python/tests/，上一级即 Python/
_PYTHON_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PYTHON_ROOT not in sys.path:
    sys.path.insert(0, _PYTHON_ROOT)

_REPO_ROOT = os.path.dirname(_PYTHON_ROOT)


def _writable_tmp_root() -> str:
    """挑一个"建出来的目录真的能写"的临时目录根。

    某些受限环境对 0o700 权限的目录会拒绝写入，这里用默认权限建目录并
    立刻做一次读写探测，确保返回的根目录真的可用。
    """
    candidates = [
        os.environ.get("JML_TEST_TMPROOT"),
        tempfile.gettempdir(),
        os.path.join(_REPO_ROOT, "_pytest_tmp"),
    ]
    errors = []
    for base in candidates:
        if not base:
            continue
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, "jml-probe-" + uuid.uuid4().hex[:8])
            os.mkdir(probe)
            with open(os.path.join(probe, "w.txt"), "w", encoding="utf-8") as fh:
                fh.write("probe")
            shutil.rmtree(probe, ignore_errors=True)
            return base
        except OSError as exc:
            errors.append(f"{base}: {exc}")
    raise RuntimeError("找不到可写的临时目录根: " + "; ".join(errors))


_TMP_ROOT: str | None = None


def _tmp_root() -> str:
    """返回本轮测试使用的临时目录根（惰性探测一次）。"""
    global _TMP_ROOT
    if _TMP_ROOT is None:
        _TMP_ROOT = _writable_tmp_root()
    return _TMP_ROOT


@pytest.fixture
def tmp_path():
    """自建 tmp_path：函数级独立目录，用例结束后递归删除。"""
    from pathlib import Path

    path = os.path.join(_tmp_root(), "jml-test-" + uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def tmp_root() -> str:
    """返回本轮测试使用的临时目录根（供需要手工造目录的用例使用）。"""
    return _tmp_root()


@pytest.fixture(autouse=True)
def isolated_appdata(monkeypatch, tmp_path):
    """把 APPDATA 重定向到临时目录，配置落盘不碰真实用户目录。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))


@pytest.fixture(autouse=True)
def reset_host_state():
    """每个用例前后重置单例状态，避免用例互相污染。

    loader 模块在 import 时把 ``events.BUS`` 绑定到自身命名空间，
    而 ``events.reset()`` 会换一个新总线对象，所以这里显式把 loader.BUS
    重新指向最新总线，保证 loader 内订阅的 handler 也一并被清掉。
    """
    from jml.host import events, loader, registry

    def _reset_all() -> None:
        registry.reset()
        events.reset()
        loader.reset()
        loader.BUS = events.BUS

    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    _reset_all()
    yield
    _reset_all()
    sys.path[:] = saved_path
    for name in set(sys.modules) - saved_modules:
        if name.startswith("_jml_plugin_"):
            sys.modules.pop(name, None)


@pytest.fixture
def base_manifest():
    """返回一份字段完整且合法的 manifest 副本（每用例独立，可随意改）。"""
    return {
        "id": "demo_plugin",
        "name": "演示插件",
        "version": "0.1.0",
        "api_version": 1,
        "description": "用于单元测试的演示插件",
        "entry": "main.py",
        "dependencies": {"plugins": []},
        "services": {
            "provides": [{"name": "demo.svc", "version": 1}],
            "requires": [],
        },
        "pages": [{"id": "demo.main", "title": "演示主页", "template": "list"}],
        "config": {},
        "development_sample": False,
    }


#: 默认插件入口文件内容：只做最小 setup，不做任何宿主内部操作
DEFAULT_ENTRY = (
    "def setup(ctx):\n"
    "    ctx.log('setup ok')\n"
    "    ctx.register_service('demo.svc', 1, lambda: 'demo')\n"
)


@pytest.fixture
def make_plugin(tmp_path):
    """工厂夹具：在临时插件根目录下造一个插件目录，返回其 Path。

    用法::

        make_plugin("dir_name", manifest_dict, files={"main.py": "..."})
    """

    def _make(dir_name: str, manifest: dict, files: dict | None = None, root=None):
        base = root if root is not None else (tmp_path / "plugins")
        base.mkdir(parents=True, exist_ok=True)
        plugin_dir = base / dir_name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for filename, content in (files or {"main.py": DEFAULT_ENTRY}).items():
            target = plugin_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return plugin_dir

    return _make
