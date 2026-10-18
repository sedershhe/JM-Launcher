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

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
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

    事件总线已下沉到 ``jml.events``，loader 通过 ``events.BUS`` 动态访问；
    ``events.reset()`` 换新单例后 loader 自动跟随，无需再手工回填 BUS。
    诊断环表与落盘目录也在这里重置。
    """
    from jml import events
    from jml.core import account, diagnostics, downloader, jm_source, searcher
    from jml.host import loader, registry

    def _reset_all() -> None:
        registry.reset()
        events.reset()
        loader.reset()
        diagnostics.set_log_dir(None)
        diagnostics.clear()
        # 阶段 2：共享搜索会话、数据源单例与下载批次登记也要隔离
        searcher.reset_shared_session()
        account.reset_state()
        jm_source.reset_source()
        downloader.reset()

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


# ---------------------------------------------------------------------------
# 共享假数据源（搜索/详情/封面/下载测试共用，避免每个文件各写一份）
# ---------------------------------------------------------------------------


def _fake_png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


FAKE_PNG = _fake_png_bytes()


class FakeSource:
    """可配置假数据源，覆盖 ``jm_source`` 协议的四个方法。

    下载相关参数：``books`` / ``chapters`` / ``images_per_chapter`` /
    ``fail_books`` / ``invalid_image_books`` / ``sleep`` / ``gate`` /
    ``tracker``（需有 ``enter()``/``exit()``）。
    搜索/详情/封面参数：``search_results`` / ``total_pages`` / ``album`` /
    ``album_error`` / ``cover_data`` / ``cover_error`` / ``search_error``。
    账号参数：``login_result`` / ``login_error`` / ``login_delay``。
    """

    def __init__(
        self,
        books: dict[str, str] | None = None,
        *,
        chapters: int = 1,
        images_per_chapter: int = 2,
        fail_books: set[str] | None = None,
        invalid_image_books: set[str] | None = None,
        sleep: float = 0.0,
        gate: threading.Event | None = None,
        tracker=None,
        search_results: list[dict] | None = None,
        total_pages: int = 3,
        album: dict | None = None,
        album_error: BaseException | None = None,
        cover_data: bytes | None = b"COVER-BYTES",
        cover_error: BaseException | None = None,
        search_error: BaseException | None = None,
        record_dest_dirs: bool = False,
        login_result: bool = True,
        login_error: BaseException | None = None,
        login_delay: float = 0.0,
        session_client: object | None = None,
    ) -> None:
        self.books = dict(books or {})
        self.chapters = chapters
        self.images_per_chapter = images_per_chapter
        self.fail_books = fail_books or set()
        self.invalid_image_books = invalid_image_books or set()
        self.sleep = sleep
        self.gate = gate
        self.tracker = tracker
        self.search_results = (
            list(search_results)
            if search_results is not None
            else [self.default_search_result()]
        )
        self.total_pages = total_pages
        self.album = dict(album) if album is not None else None
        self.album_error = album_error
        self.cover_data = cover_data
        self.cover_error = cover_error
        self.search_error = search_error
        self.record_dest_dirs = record_dest_dirs
        self.login_result = login_result
        self.login_error = login_error
        self.login_delay = login_delay
        #: jm.session 服务返回的客户端对象（阶段 4-4；默认 None）
        self.session_client = session_client

        self.search_calls: list[tuple[str, int]] = []
        self.album_calls: list[str] = []
        self.cover_calls: list[str] = []
        self.login_calls: list[tuple[str, str]] = []
        self.logout_calls = 0
        self.logged_in = False
        self.dest_dirs: list[str] = []
        self.downloaded = 0
        self.chapters_started = 0
        self.lock = threading.Lock()

    @staticmethod
    def default_search_result(book_id: str = "1") -> dict:
        return {
            "book_id": book_id,
            "title": f"标题{book_id}",
            "author": "作者",
            "page_count": 12,
            "tags": ["标签"],
            "cover_url": f"https://example.invalid/{book_id}.jpg",
        }

    @staticmethod
    def detail_album(book_id: str = "1") -> dict:
        return {
            "book_id": book_id,
            "title": "详情标题",
            "author": "作者甲",
            "page_count": 2,
            "tags": ["标签"],
            "description": "简" * 250,
            "cover_url": "https://example.invalid/1.jpg",
            "chapters": [{"photo_id": "11", "index": 1, "title": "第1章"}],
        }

    # -- jm_source 协议 -----------------------------------------------------

    def search(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        if self.search_error is not None:
            raise self.search_error
        return {
            "keyword": keyword,
            "page": page,
            "total_pages": self.total_pages,
            "results": list(self.search_results),
        }

    def get_album(self, book_id: str) -> dict:
        self.album_calls.append(book_id)
        if self.album_error is not None:
            raise self.album_error
        if book_id in self.fail_books:
            raise RuntimeError(f"假下载源拒绝 {book_id}")
        if self.album is not None:
            data = dict(self.album)
            data.setdefault("book_id", book_id)
            return data

        chapters = [
            {"photo_id": f"{book_id}-{index}", "index": index, "title": f"第{index}章"}
            for index in range(1, self.chapters + 1)
        ]
        return {
            "book_id": book_id,
            "title": self.books.get(book_id, "详情标题"),
            "author": "作者甲",
            "page_count": self.chapters * self.images_per_chapter,
            "tags": ["标签"],
            "description": "简" * 250,
            "cover_url": "https://example.invalid/1.jpg",
            "chapters": chapters,
        }

    def iter_chapter_images(self, book_id: str, chapter: dict, dest_dir: str):
        if self.tracker is not None:
            self.tracker.enter()
        try:
            with self.lock:
                self.chapters_started += 1
            if self.record_dest_dirs:
                self.dest_dirs.append(dest_dir)
            os.makedirs(dest_dir, exist_ok=True)
            for index in range(1, self.images_per_chapter + 1):
                if self.gate is not None:
                    self.gate.wait(timeout=5)
                if self.sleep:
                    time.sleep(self.sleep)
                path = os.path.join(
                    dest_dir, f"{book_id}_{chapter['index']}_{index}.png"
                )
                data = b"not-an-image" if book_id in self.invalid_image_books else FAKE_PNG
                with open(path, "wb") as handle:
                    handle.write(data)
                with self.lock:
                    self.downloaded += 1
                yield path
        finally:
            if self.tracker is not None:
                self.tracker.exit()

    def cover_bytes(self, book_id: str) -> bytes | None:
        self.cover_calls.append(book_id)
        if self.cover_error is not None:
            raise self.cover_error
        return self.cover_data

    # -- 账号（可选协议） ---------------------------------------------------

    def login(self, username: str, password: str) -> bool:
        if self.login_delay:
            time.sleep(self.login_delay)
        with self.lock:
            self.login_calls.append((username, password))
        if self.login_error is not None:
            raise self.login_error
        if self.login_result is False:
            return False
        self.logged_in = True
        return True

    def logout(self) -> None:
        with self.lock:
            self.logout_calls += 1
        self.logged_in = False

    def is_logged_in(self) -> bool:
        return self.logged_in

    def logged_in_client(self):
        """阶段 4-4：jm.session 服务取"已登录客户端"的假实现。"""
        from jml.core.errors import NotLoggedInError

        if not self.logged_in:
            raise NotLoggedInError("尚未登录，无法提供已登录的 jmcomic 客户端")
        return self.session_client


@pytest.fixture
def fake_source_cls():
    """返回共享 FakeSource 类（各测试按需实例化不同配置）。"""
    return FakeSource


# ---------------------------------------------------------------------------
# 可选的真实登录验证辅助（仅测试；产品代码绝不读取此文件）
# ---------------------------------------------------------------------------


def load_test_credentials() -> tuple[str, str] | None:
    """从 ``JML_TEST_CREDENTIALS_FILE`` 读取本地不入库 JSON。

    格式：``{"username": "...", "password": "..."}``。
    未设置环境变量时返回 ``None``；设置了但字段为空/JSON 不合法时抛
    ``ValueError``（调用方决定是否跳过真实登录）。

    **只在测试辅助里出现**：``Python/jml`` 产品代码没有任何读取该文件的路径，
    构建产物不包含凭据。
    """
    path = os.environ.get("JML_TEST_CREDENTIALS_FILE")
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        raise ValueError(
            "JML_TEST_CREDENTIALS_FILE 缺少 username 或 password"
        )
    return username, password
