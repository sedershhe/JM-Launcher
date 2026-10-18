"""下载历史插件测试（阶段 4 第一个插件，全部离线）。

覆盖：
- 用**临时插件目录 + 真实 loader** 装配仓库里的真实插件（复制后再 bootstrap，
  不读写真实用户目录；APPDATA 由 conftest 重定向到临时目录）；
- ``download.item_finished`` → 页面 ``plugin_page_load`` 的行包含该记录、
  字段齐全、按时间倒序；
- keep_count 调小后继续写 → 超限裁掉最旧记录（内存与落盘一致）；
- 分页：11 条 + page_size=10 → 第 1 页 10 行、第 2 页 1 行、total 正确；
- 历史文件损坏 → 重建为空历史且不抛异常（并写诊断日志）；
- 落盘只用业务字段白名单，不含 password/token/cookie 等凭据；
- 宿主新增的 ``ctx.data_dir``：loader 构造 context 时创建并传入。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

import pytest

import jml_backend
from jml import events
from jml.host import loader
from jml.host import pages as page_host

PLUGIN_ID = "download_history"
PAGE_ID = "download_history.main"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PYTHON_ROOT = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_PYTHON_ROOT)
_PLUGIN_SRC = os.path.join(_REPO_ROOT, "plugins", PLUGIN_ID)

#: 落盘白名单（与插件实现一致；测试独立写一份，防止实现偷偷加字段）
_BUSINESS_FIELDS = {"time", "book_id", "title", "ok", "zip_path", "error_code"}

#: 绝不允许出现在历史文件里的凭据类字段名
_CREDENTIAL_WORDS = ("password", "token", "cookie", "authorization")


@pytest.fixture(autouse=True)
def _reset_backend_state():
    """bootstrap 幂等标记在 jml_backend._STATE 里，跨用例必须清掉。"""
    jml_backend.reset_host_state()
    yield
    jml_backend.reset_host_state()


@pytest.fixture
def installed(tmp_path):
    """把真实插件复制到临时 plugins 目录，用真实 loader 装配。"""
    assert os.path.isdir(_PLUGIN_SRC), f"插件源码目录不存在: {_PLUGIN_SRC}"
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    shutil.copytree(_PLUGIN_SRC, plugins_root / PLUGIN_ID)

    report = json.loads(jml_backend.bootstrap(str(plugins_root)))
    assert report["ok"] is True, report
    return report


def _data_dir() -> str:
    """宿主应给本插件创建的数据目录（与契约一致，位于隔离后的 APPDATA 下）。"""
    return os.path.join(os.environ["APPDATA"], "JML", "plugins", PLUGIN_ID, "data")


def _history_path() -> str:
    return os.path.join(_data_dir(), "history.json")


def _read_history() -> dict:
    with open(_history_path(), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _dispatch_item(
    book_id: str,
    *,
    ok: bool = True,
    title: str | None = None,
    zip_path: str | None = None,
    error_code: str = "",
    **extra,
) -> None:
    """派发一条 ``download.item_finished``（额外字段用来验证“凭据不落盘”）。"""
    payload = {
        "type": "download.item_finished",
        "book_id": str(book_id),
        "title": f"标题{book_id}" if title is None else title,
        "ok": ok,
        "zip_path": (
            f"C:/downloads/标题{book_id}-JM{book_id}.zip"
            if zip_path is None and ok
            else ("" if zip_path is None else zip_path)
        ),
        "error_code": "" if ok else (error_code or "NETWORK"),
        "error_message": "" if ok else "网络请求失败",
        "batch_id": "batch-1",
    }
    payload.update(extra)
    events.dispatch("download.item_finished", payload)


def _load_page(**params) -> dict:
    raw = jml_backend.plugin_page_load(
        PLUGIN_ID, PAGE_ID, json.dumps(params, ensure_ascii=False)
    )
    envelope = json.loads(raw)
    assert envelope["ok"] is True, envelope
    return envelope["data"]


# ---------------------------------------------------------------------------
# 装配：manifest / 页面声明 / ctx.data_dir
# ---------------------------------------------------------------------------


def test_plugin_loads_and_declares_list_page(installed):
    loaded = {item["id"]: item for item in installed["loaded"]}
    assert PLUGIN_ID in loaded
    assert loaded[PLUGIN_ID]["name"] == "下载历史"
    assert loaded[PLUGIN_ID]["pages"] == [
        {"id": PAGE_ID, "title": "下载历史", "template": "list"}
    ]
    # 注册表里只应有本插件声明过的页面（真实 setup 已调用 register_page）
    assert page_host.PAGE_REGISTRY.registered_pages() == [(PLUGIN_ID, PAGE_ID)]

    envelope = json.loads(jml_backend.plugin_page_load(PLUGIN_ID, PAGE_ID, ""))
    assert envelope["ok"] is True
    assert envelope["template"] == "list"
    assert envelope["title"] == "下载历史"
    assert envelope["data"]["rows"] == []
    assert envelope["data"]["total"] == 0
    assert envelope["data"]["empty_text"] == "暂无下载记录"


def test_loader_creates_plugin_data_dir(installed):
    """宿主扩展：loader 在构造 ctx 时创建并传入可写数据目录。"""
    assert os.path.isdir(_data_dir())
    assert os.path.normpath(_data_dir()).endswith(
        os.path.join("JML", "plugins", PLUGIN_ID, "data")
    )
    # 配置默认值照常生效（与数据目录同根，互不覆盖）
    store = loader.config_for(PLUGIN_ID)
    assert store is not None
    assert store.get("keep_count") == 200


# ---------------------------------------------------------------------------
# 事件记录 → 页面行
# ---------------------------------------------------------------------------


def test_item_finished_is_recorded_with_all_fields(installed):
    _dispatch_item("101", title="标题甲", zip_path="C:/downloads/标题甲-JM101.zip")
    _dispatch_item("102", ok=False, error_code="NETWORK")

    data = _load_page()

    assert [column["key"] for column in data["columns"]] == [
        "time",
        "book_id",
        "result",
        "path",
    ]
    assert [column["title"] for column in data["columns"]] == [
        "时间",
        "本子号",
        "结果",
        "输出路径",
    ]
    assert data["row_actions"] == []
    assert data["empty_text"] == "暂无下载记录"
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["page_size"] == 50

    newest, oldest = data["rows"]
    assert newest["book_id"] == "102"
    assert newest["result"] == "失败"
    assert newest["path"] == ""

    assert oldest["book_id"] == "101"
    assert oldest["title"] == "标题甲"
    assert oldest["result"] == "成功"
    assert oldest["path"] == "C:/downloads/标题甲-JM101.zip"
    # 本地 ISO 时间，可直接被 C#/Python 解析
    assert datetime.fromisoformat(oldest["time"]).tzinfo is not None

    # 落盘字段与页面行字段都齐全，且不含额外键
    stored = _read_history()
    assert stored["version"] == 1
    assert len(stored["records"]) == 2
    for record in stored["records"]:
        assert set(record) == _BUSINESS_FIELDS


def test_rows_are_newest_first(installed):
    for book_id in ("1", "2", "3"):
        _dispatch_item(book_id)

    rows = _load_page()["rows"]

    assert [row["book_id"] for row in rows] == ["3", "2", "1"]


# ---------------------------------------------------------------------------
# keep_count 裁剪
# ---------------------------------------------------------------------------


def test_keep_count_trims_oldest_records(installed):
    for index in range(3):
        _dispatch_item(str(index))

    store = loader.config_for(PLUGIN_ID)
    assert store is not None
    store.set("keep_count", 10)  # schema 允许的最小值

    for index in range(3, 15):  # 累计 15 条，超过 10
        _dispatch_item(str(index))

    data = _load_page()
    assert data["total"] == 10
    # 默认 page_size=50 被 keep_count=10 上限截断
    assert data["page_size"] == 10
    assert [row["book_id"] for row in data["rows"]] == [
        str(index) for index in range(14, 4, -1)
    ]

    stored = _read_history()
    assert len(stored["records"]) == 10
    assert stored["records"][0]["book_id"] == "14"
    assert stored["records"][-1]["book_id"] == "5"


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------


def test_pagination_eleven_records_page_size_ten(installed):
    for index in range(11):
        _dispatch_item(str(100 + index))

    first = _load_page(page=1, page_size=10)
    second = _load_page(page=2, page_size=10)

    assert first["page"] == 1
    assert first["page_size"] == 10
    assert first["total"] == 11
    assert first["total_pages"] == 2
    assert len(first["rows"]) == 10
    assert [row["book_id"] for row in first["rows"]] == [
        str(index) for index in range(110, 100, -1)
    ]

    assert second["page"] == 2
    assert len(second["rows"]) == 1
    assert second["rows"][0]["book_id"] == "100"
    assert second["total"] == 11


def test_page_params_are_defensive(installed):
    _dispatch_item("401")

    # 非法页码/每页条数回退默认值（不抛异常）
    data = _load_page(page=0, page_size=0)
    assert data["page"] == 1
    assert data["page_size"] == 50

    # 页码超出范围 → 空行，total 仍然是真实条数
    beyond = _load_page(page=99)
    assert beyond["rows"] == []
    assert beyond["total"] == 1


# ---------------------------------------------------------------------------
# 损坏恢复 / 凭据安全
# ---------------------------------------------------------------------------


def test_corrupt_history_file_is_rebuilt_empty(installed, capsys):
    os.makedirs(_data_dir(), exist_ok=True)
    with open(_history_path(), "w", encoding="utf-8") as handle:
        handle.write("{ 这不是合法 JSON")

    data = _load_page()  # 不得抛异常

    assert data["rows"] == []
    assert data["total"] == 0
    assert "损坏" in capsys.readouterr().out  # 写了诊断日志

    # 损坏文件被重建为合法空历史
    assert _read_history()["records"] == []


def test_history_file_contains_no_credential_fields(installed):
    _dispatch_item(
        "201",
        password="should-not-persist",
        token="tok-123",
        cookie="session=abc",
        authorization="Bearer xyz",
    )

    with open(_history_path(), "r", encoding="utf-8") as handle:
        text = handle.read()
    lowered = text.lower()

    for word in _CREDENTIAL_WORDS:
        assert word not in lowered
    for secret in ("should-not-persist", "tok-123", "session=abc", "Bearer xyz"):
        assert secret not in text

    stored = json.loads(text)
    assert set(stored["records"][0]) == _BUSINESS_FIELDS


def test_batch_finished_keeps_history(installed, capsys):
    _dispatch_item("301")
    events.dispatch(
        "download.batch_finished",
        {
            "type": "download.batch_finished",
            "total": 1,
            "success": 1,
            "failed": 0,
            "cancelled": 0,
            "batch_id": "batch-1",
        },
    )

    assert "批次下载结束" in capsys.readouterr().out
    data = _load_page()
    assert data["total"] == 1
    assert data["rows"][0]["book_id"] == "301"
