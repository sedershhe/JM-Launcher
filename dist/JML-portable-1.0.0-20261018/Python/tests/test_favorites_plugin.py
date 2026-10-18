"""收藏夹插件测试（阶段 4-4，全部离线）。

覆盖（禁止真实登录）：
- 未登录 → ``plugin_page_load`` 返回 ``ok:false`` + ``NOT_LOGGED_IN`` +
  文案「未登录，无法浏览收藏夹」；
- 已登录（假数据源：账号登录 + jm.session 假客户端）→ 行字段映射
  （book_id/title/author/page_count/click_count/updated）、列定义、行动作；
- 目录切换：参数 / 配置 last_folder_id / 第一个目录；切换后按新目录重拉；
- 三种排序参数透传（mr / mv / mp）与非法值兜底；
- 分页（page 参数透传、total_pages 推算）；
- 空收藏夹文案「收藏夹是空的」；
- 行动作返回 intent（detail / download）；
- 缺 jm.session 服务时宿主拒绝加载，原因含「缺少服务 jm.session」；
- 插件不落任何凭据（配置里只有 last_folder_id / sort）。

夹具用真实 ``loader`` / ``jml_backend.bootstrap`` 装配仓库里的插件副本，
APPDATA 由 conftest 重定向到临时目录，不读写真实用户目录、不联网。
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

import jml_backend
from jml.core import account, jm_source
from jml.core.errors import ERROR_NOT_LOGGED_IN, NotLoggedInError
from jml.host import loader

PLUGIN_ID = "favorites"
PAGE_ID = "favorites.main"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PYTHON_ROOT = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_PYTHON_ROOT)
_PLUGIN_SRC = os.path.join(_REPO_ROOT, "plugins", PLUGIN_ID)


# ---------------------------------------------------------------------------
# 假数据源 / 假客户端 / 假收藏页
# ---------------------------------------------------------------------------


class FakeFavoritePage:
    """替身 ``JmFavoritePage``（只暴露插件实际读取的字段）。"""

    def __init__(self, content=(), folder_list=(), total=0, page_size=20):
        self.content = list(content)
        self.folder_list = list(folder_list)
        self.total = total
        self.page_size = page_size


class FakeFavoriteClient:
    """替身 jmcomic 客户端：记录收藏夹调用参数并返回预设页面。"""

    def __init__(self, page=None, responses=None, error=None):
        self.page = page if page is not None else FakeFavoritePage()
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict] = []

    def favorite_folder(self, page=1, order_by="mr", folder_id="0"):
        self.calls.append(
            {"page": page, "order_by": order_by, "folder_id": folder_id}
        )
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return self.page


class FakeFavoritesSource:
    """只实现收藏夹插件用到的数据源协议（登录 + 已登录客户端）。"""

    def __init__(self, client: FakeFavoriteClient) -> None:
        self.client = client
        self.logged_in = False
        self.login_calls: list[tuple[str, str]] = []
        self.logout_calls = 0

    def login(self, username: str, password: str) -> bool:
        self.login_calls.append((username, password))
        self.logged_in = True
        return True

    def logout(self) -> None:
        self.logout_calls += 1
        self.logged_in = False

    def is_logged_in(self) -> bool:
        return self.logged_in

    def logged_in_client(self):
        if not self.logged_in:
            raise NotLoggedInError("尚未登录，无法提供已登录的 jmcomic 客户端")
        return self.client


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend_state():
    jml_backend.reset_host_state()
    yield
    jml_backend.reset_host_state()


@pytest.fixture
def installed(tmp_path):
    """复制真实插件到临时 plugins 目录并 bootstrap（真实 loader 装配）。"""
    assert os.path.isdir(_PLUGIN_SRC), f"插件源码目录不存在: {_PLUGIN_SRC}"
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    shutil.copytree(_PLUGIN_SRC, plugins_root / PLUGIN_ID)

    report = json.loads(jml_backend.bootstrap(str(plugins_root)))
    assert report["ok"] is True, report
    return report


def _use_source(client: FakeFavoriteClient) -> FakeFavoritesSource:
    source = FakeFavoritesSource(client)
    jm_source.set_source(source)
    return source


def _login(source: FakeFavoritesSource) -> None:
    """用主程序账号入口登录（假数据源，离线；插件拿不到密码）。"""
    account.set_credentials("favorites_user", "favorites-pass")
    status = account.ensure_login()
    assert status["ok"] is True, status
    assert source.logged_in is True


def _load_page(**params) -> dict:
    raw = jml_backend.plugin_page_load(
        PLUGIN_ID, PAGE_ID, json.dumps(params, ensure_ascii=False)
    )
    return json.loads(raw)


def _load_page_ok(**params) -> dict:
    envelope = _load_page(**params)
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def _action(action_id: str, payload: dict) -> dict:
    return json.loads(
        jml_backend.plugin_page_action(
            PLUGIN_ID, PAGE_ID, action_id, json.dumps(payload, ensure_ascii=False)
        )
    )


# ---------------------------------------------------------------------------
# 装配 / 服务依赖
# ---------------------------------------------------------------------------


def test_plugin_loads_and_declares_list_page(installed):
    loaded = {item["id"]: item for item in installed["loaded"]}
    assert PLUGIN_ID in loaded
    assert loaded[PLUGIN_ID]["name"] == "收藏夹"
    assert loaded[PLUGIN_ID]["pages"] == [
        {"id": PAGE_ID, "title": "收藏夹", "template": "list"}
    ]
    # manifest 声明 requires jm.session/1，宿主 bootstrap 已注册
    assert installed["services"]["jm.session"] == 1
    # setup 已注册加载钩子（页面路由能通过"未实现钩子"检查的前提）
    from jml.host import pages as page_host

    assert (PLUGIN_ID, PAGE_ID) in page_host.PAGE_REGISTRY.registered_pages()


def test_missing_session_service_blocks_plugin_load(tmp_path):
    """缺 jm.session → 宿主拒绝加载，原因含「缺少服务 jm.session」。"""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    shutil.copytree(_PLUGIN_SRC, plugins_root / PLUGIN_ID)

    # 直接走 loader（不注册宿主服务）验证 requires 检查
    report = loader.load_all(str(plugins_root))

    failed = {item["id"]: item["reason"] for item in report["failed"]}
    assert PLUGIN_ID in failed
    assert "缺少服务 jm.session" in failed[PLUGIN_ID]
    assert report["loaded"] == []


# ---------------------------------------------------------------------------
# 未登录
# ---------------------------------------------------------------------------


def test_unauthenticated_page_is_not_logged_in(installed):
    client = FakeFavoriteClient()
    source = _use_source(client)  # 未设置内存凭据

    envelope = _load_page()

    assert envelope["ok"] is False
    assert envelope["error_code"] == ERROR_NOT_LOGGED_IN
    assert envelope["error_message"] == "未登录，无法浏览收藏夹"
    # 未登录不调用收藏夹接口，也不触发假登录
    assert client.calls == []
    assert source.login_calls == []


# ---------------------------------------------------------------------------
# 已登录：行数据 / 列 / 行动作
# ---------------------------------------------------------------------------


def _sample_page() -> FakeFavoritePage:
    return FakeFavoritePage(
        content=[
            (
                "123",
                {
                    "name": "标题A",
                    "author": ["作者甲", "作者乙"],
                    "total_photos": 42,
                    "total_views": 99,
                    "update_date": "2026-10-01",
                },
            ),
            ("456", {"name": "标题B", "author": "作者丙"}),
        ],
        folder_list=[
            {"FID": "0", "name": "全部"},
            {"FID": "7", "name": "子夹"},
        ],
        total=45,
        page_size=20,
    )


def test_logged_in_rows_columns_and_actions(installed):
    client = FakeFavoriteClient(page=_sample_page())
    source = _use_source(client)
    _login(source)

    data = _load_page_ok()

    assert [column["key"] for column in data["columns"]] == [
        "title",
        "author",
        "page_count",
        "click_count",
    ]
    assert [column["title"] for column in data["columns"]] == [
        "标题",
        "作者",
        "页数",
        "收藏数",
    ]
    assert data["rows"][0] == {
        "book_id": "123",
        "title": "标题A",
        "author": "作者甲, 作者乙",
        "page_count": 42,
        "click_count": 99,
        "updated": "2026-10-01",
    }
    # 缺页数 / 点击量 / 更新日期的条目给空值兜底，不发额外请求
    assert data["rows"][1] == {
        "book_id": "456",
        "title": "标题B",
        "author": "作者丙",
        "page_count": 0,
        "click_count": 0,
        "updated": "",
    }
    assert data["row_actions"] == [
        {"id": "detail", "title": "详情", "primary": True},
        {"id": "download", "title": "下载"},
    ]
    assert data["empty_text"] == "收藏夹是空的"
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["total"] == 45
    assert data["total_pages"] == 3
    assert data["folders"] == [
        {"id": "0", "name": "全部"},
        {"id": "7", "name": "子夹"},
    ]
    # 第一次调用（配置空）用「全部」= 0，目录清单里 0 有效，不重拉
    assert client.calls == [{"page": 1, "order_by": "mr", "folder_id": "0"}]


def test_row_with_malformed_item_is_skipped(installed):
    client = FakeFavoriteClient(
        page=FakeFavoritePage(
            content=["broken", ("", {"name": "无编号"}), ("9", {"name": "有效"})],
            total=3,
        )
    )
    source = _use_source(client)
    _login(source)

    data = _load_page_ok()

    assert [row["book_id"] for row in data["rows"]] == ["9"]


# ---------------------------------------------------------------------------
# 目录切换
# ---------------------------------------------------------------------------


def test_folder_defaults_to_first_folder_and_reloads(installed):
    """配置里没有 last_folder_id → 用第一个目录；与首次请求目录不同则重拉。"""
    first = FakeFavoritePage(
        folder_list=[
            {"FID": "7", "name": "子夹一"},
            {"FID": "8", "name": "子夹二"},
        ],
        total=1,
    )
    second = FakeFavoritePage(
        folder_list=[
            {"FID": "7", "name": "子夹一"},
            {"FID": "8", "name": "子夹二"},
        ],
        content=[("1", {"name": "收藏一"})],
        total=1,
    )
    client = FakeFavoriteClient(responses=[first, second])
    source = _use_source(client)
    _login(source)

    data = _load_page_ok()

    # 首次用 0（全部）探测目录清单，发现第一个目录是 7 → 按 7 重拉
    assert client.calls == [
        {"page": 1, "order_by": "mr", "folder_id": "0"},
        {"page": 1, "order_by": "mr", "folder_id": "7"},
    ]
    assert data["filters"][0]["key"] == "folder_id"
    assert data["filters"][0]["value"] == "7"
    assert [option["value"] for option in data["filters"][0]["options"]] == [
        "7",
        "8",
    ]
    # 目录已记住：下一次不带参数直接按 7 拉，不再探测
    client.calls.clear()
    data = _load_page_ok()
    assert client.calls == [{"page": 1, "order_by": "mr", "folder_id": "7"}]
    assert data["filters"][0]["value"] == "7"


def test_folder_switch_from_param(installed):
    client = FakeFavoriteClient(page=_sample_page())
    source = _use_source(client)
    _login(source)

    data = _load_page_ok(folder_id="7")

    assert client.calls[0]["folder_id"] == "7"
    assert data["filters"][0]["value"] == "7"
    store = loader.config_for(PLUGIN_ID)
    assert store is not None
    assert store.get("last_folder_id") == "7"


def test_invalid_folder_falls_back_to_first(installed):
    client = FakeFavoriteClient(page=_sample_page())
    source = _use_source(client)
    _login(source)

    data = _load_page_ok(folder_id="999")

    # 999 不在目录清单里 → 第一个目录 0
    assert client.calls[-1]["folder_id"] == "0"
    assert data["filters"][0]["value"] == "0"


def test_mobile_style_empty_folder_list_keeps_requested(installed):
    """移动端 API 不返回 folder_list：保留请求目录（缺省「全部」），补兜底项。"""
    client = FakeFavoriteClient(page=FakeFavoritePage(total=0))
    source = _use_source(client)
    _login(source)

    data = _load_page_ok(folder_id="7")

    assert client.calls == [{"page": 1, "order_by": "mr", "folder_id": "7"}]
    assert data["folders"] == [{"id": "7", "name": "收藏夹 7"}]
    assert data["filters"][0]["value"] == "7"


# ---------------------------------------------------------------------------
# 排序切换
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sort", ["mr", "mv", "mp"])
def test_sort_param_is_forwarded(installed, sort):
    client = FakeFavoriteClient(page=FakeFavoritePage(total=1))
    source = _use_source(client)
    _login(source)

    data = _load_page_ok(sort=sort)

    assert client.calls[0]["order_by"] == sort
    assert data["filters"][1]["key"] == "sort"
    assert data["filters"][1]["value"] == sort
    assert [option["value"] for option in data["filters"][1]["options"]] == [
        "mr",
        "mv",
        "mp",
    ]


def test_sort_falls_back_to_config_then_default(installed):
    from jml.host import loader as plugin_loader

    client = FakeFavoriteClient(page=FakeFavoritePage(total=1))
    source = _use_source(client)
    _login(source)

    store = plugin_loader.config_for(PLUGIN_ID)
    assert store is not None
    store.set("sort", "mp")

    data = _load_page_ok()
    assert client.calls[0]["order_by"] == "mp"
    assert data["filters"][1]["value"] == "mp"

    # 非法排序值 → 默认「最近收藏」mr
    client.calls.clear()
    data = _load_page_ok(sort="unknown")
    assert client.calls[0]["order_by"] == "mr"
    assert data["filters"][1]["value"] == "mr"


# ---------------------------------------------------------------------------
# 分页 / 空收藏夹
# ---------------------------------------------------------------------------


def test_pagination_forwards_page_and_computes_total_pages(installed):
    client = FakeFavoriteClient(page=FakeFavoritePage(total=45, page_size=20))
    source = _use_source(client)
    _login(source)

    data = _load_page_ok(page=2)

    assert client.calls[0]["page"] == 2
    assert data["page"] == 2
    assert data["total_pages"] == 3

    # 非法页码回退 1
    client.calls.clear()
    data = _load_page_ok(page=0)
    assert client.calls[0]["page"] == 1
    assert data["page"] == 1


def test_empty_favorites_shows_empty_text(installed):
    client = FakeFavoriteClient(page=FakeFavoritePage(content=[], total=0))
    source = _use_source(client)
    _login(source)

    data = _load_page_ok()

    assert data["rows"] == []
    assert data["total"] == 0
    assert data["total_pages"] == 1
    assert data["empty_text"] == "收藏夹是空的"


# ---------------------------------------------------------------------------
# 行动作 → intent
# ---------------------------------------------------------------------------


def test_detail_action_returns_detail_intent(installed):
    client = FakeFavoriteClient()
    source = _use_source(client)
    _login(source)

    result = _action("detail", {"row": {"book_id": "123", "title": "标题A"}, "row_index": 0, "page": 1})

    assert result["ok"] is True
    assert result["intent"] == {"kind": "detail", "book_id": "123"}
    assert result["refresh"] is False
    assert result["data"] is None


def test_download_action_returns_download_intent(installed):
    client = FakeFavoriteClient()
    source = _use_source(client)
    _login(source)

    result = _action("download", {"row": {"book_id": "456"}, "row_index": 1, "page": 1})

    assert result["ok"] is True
    assert result["intent"] == {"kind": "download", "book_ids": ["456"]}


def test_row_actions_without_book_id_are_invalid_argument(installed):
    client = FakeFavoriteClient()
    source = _use_source(client)
    _login(source)

    for action_id in ("detail", "download"):
        result = _action(action_id, {"row": {}})
        assert result["ok"] is False
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert "book_id" in result["error_message"]


# ---------------------------------------------------------------------------
# 凭据安全：配置里只有两个业务键
# ---------------------------------------------------------------------------


def test_plugin_config_contains_no_credentials(installed):
    client = FakeFavoriteClient(page=FakeFavoritePage(total=1))
    source = _use_source(client)
    _login(source)
    _load_page_ok()

    config_path = os.path.join(
        os.environ["APPDATA"], "JML", "plugins", PLUGIN_ID, "config.json"
    )
    with open(config_path, "r", encoding="utf-8") as handle:
        stored = json.load(handle)

    assert set(stored) == {"last_folder_id", "sort"}
    text = json.dumps(stored, ensure_ascii=False).lower()
    for word in ("password", "passwd", "token", "cookie", "authorization"):
        assert word not in text
