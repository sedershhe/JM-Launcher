"""搜索 / 详情 / 封面业务单元测试（不联网：全部注入 conftest 共享假数据源）。

锁定：搜索字段归一化、total_pages、按 key 隔离的翻页快照（旧缺陷 #8）、
page 归一化、详情 200 字截断、封面 bytes/None 行为。
"""

from __future__ import annotations

import pytest

from jml.core import cover, detail, jm_source, searcher
from jml.core.errors import InvalidArgumentError
from jml.core.protocol import EVENT_SEARCH_PAGE_LOADED
from jml.host import events


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


def test_search_normalizes_results(fake_source_cls):
    source = fake_source_cls(
        search_results=[
            {
                "book_id": 123,
                "title": "T",
                "author": None,
                "page_count": "9",
                "tags": "单个标签",
                "cover_url": None,
                "extra": "ignored",
            }
        ]
    )
    session = searcher.SearchSession(source=source)

    result = session.search("关键词", 2)

    assert result["ok"] is True
    assert result["keyword"] == "关键词"
    assert result["page"] == 2
    assert result["total_pages"] == 3
    assert result["results"] == [
        {
            "book_id": "123",
            "title": "T",
            "author": "",
            "page_count": 9,
            "tags": ["单个标签"],
            "cover_url": "",
        }
    ]


def test_search_emits_event(fake_source_cls):
    received: list[dict] = []
    events.BUS.subscribe(EVENT_SEARCH_PAGE_LOADED, received.append)
    source = fake_source_cls()

    searcher.SearchSession(source=source).search("abc", 1)

    assert len(received) == 1
    payload = received[0]
    assert payload["type"] == EVENT_SEARCH_PAGE_LOADED
    assert payload["keyword"] == "abc"
    assert payload["page"] == 1
    assert payload["total_pages"] == 3
    assert payload["results"][0]["book_id"] == "1"


def test_search_empty_keyword_raises(fake_source_cls):
    with pytest.raises(InvalidArgumentError):
        searcher.SearchSession(source=fake_source_cls()).search("", 1)
    with pytest.raises(InvalidArgumentError):
        searcher.SearchSession(source=fake_source_cls()).search("   ", 2)


@pytest.mark.parametrize("page", [0, -1, "", "abc", None])
def test_search_page_normalized_to_first(fake_source_cls, page):
    source = fake_source_cls()
    result = searcher.SearchSession(source=source).search("k", page)
    assert result["page"] == 1
    assert source.search_calls == [("k", 1)]


def test_paging_uses_keyword_snapshot(fake_source_cls):
    """旧缺陷 #8：翻页用发起搜索时的关键词，而不是调用方新传的词。"""
    source = fake_source_cls()
    session = searcher.SearchSession(source=source)

    first = session.search("旧词", 1)
    second = session.search("新词", 2)

    assert first["keyword"] == "旧词"
    assert second["keyword"] == "旧词"
    assert source.search_calls == [("旧词", 1), ("旧词", 2)]


def test_new_search_resets_snapshot(fake_source_cls):
    source = fake_source_cls()
    session = searcher.SearchSession(source=source)

    session.search("词A", 1)
    session.search("词B", 1)

    assert session.keyword == "词B"
    assert source.search_calls == [("词A", 1), ("词B", 1)]


def test_page_two_without_previous_search_sets_snapshot(fake_source_cls):
    source = fake_source_cls()
    session = searcher.SearchSession(source=source)

    result = session.search("首次", 2)

    assert result["page"] == 2
    assert session.keyword == "首次"
    assert source.search_calls == [("首次", 2)]


def test_one_shot_search_returns_failure_form(fake_source_cls):
    source = fake_source_cls(search_error=RuntimeError("boom"))
    result = searcher.search("k", 1, source=source)
    assert result["ok"] is False
    assert result["error_code"] == "UNKNOWN_ERROR"


def test_sessions_are_isolated_by_key(fake_source_cls):
    """不同 session_key 各自 page=1 后各自 page=2 互不干扰。"""
    jm_source.set_source(fake_source_cls())
    searcher.reset_sessions()
    main = searcher.get_session("main")
    plugin = searcher.get_session("plugin:p1")

    main.search("主词", 1)
    plugin.search("插件词", 1)

    main_page2 = main.search("改词", 2)
    plugin_page2 = plugin.search("改词", 2)

    assert main_page2["keyword"] == "主词"
    assert plugin_page2["keyword"] == "插件词"
    assert main is not plugin


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


def test_get_detail_fields(fake_source_cls):
    album = {
        "book_id": "1",
        "title": "标题",
        "author": "作者A",
        "page_count": 30,
        "tags": ["全彩", "中文"],
        "description": "简介" * 150,
        "cover_url": "https://example.invalid/1.jpg",
        "chapters": [{"photo_id": "11", "index": 1, "title": "第1章"}],
    }
    result = detail.get_detail("1", source=fake_source_cls(album=album))

    assert result["ok"] is True
    assert result["book_id"] == "1"
    assert result["title"] == "标题"
    assert result["author"] == "作者A"
    assert result["page_count"] == 30
    assert result["tags"] == ["全彩", "中文"]
    assert result["cover_url"].endswith("1.jpg")
    assert result["description_truncated"] is True
    assert len(result["description"]) == detail.DESCRIPTION_LIMIT + 1
    assert result["description"].endswith(detail.ELLIPSIS)


def test_get_detail_empty_id_raises(fake_source_cls):
    with pytest.raises(InvalidArgumentError):
        detail.get_detail("", source=fake_source_cls())
    with pytest.raises(InvalidArgumentError):
        detail.get_detail(None, source=fake_source_cls())


@pytest.mark.parametrize(
    ("text", "limit", "expected", "truncated"),
    [
        ("短简介", 200, "短简介", False),
        ("字" * 200, 200, "字" * 200, False),
        ("字" * 201, 200, "字" * 200 + "…", True),
        ("  带空白  ", 200, "带空白", False),
        (None, 200, "", False),
    ],
    ids=["short", "exact_200", "over_200", "strip", "none"],
)
def test_truncate_description(text, limit, expected, truncated):
    assert detail.truncate_description(text, limit) == (expected, truncated)


# ---------------------------------------------------------------------------
# 封面
# ---------------------------------------------------------------------------


def test_fetch_cover_returns_bytes(fake_source_cls):
    source = fake_source_cls(cover_data=b"\x89PNG-raw")
    assert cover.fetch_cover("1", source=source) == b"\x89PNG-raw"
    assert source.cover_calls == ["1"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cover_error": RuntimeError("boom")},
        {"cover_data": None},
        {"cover_data": b""},
    ],
    ids=["exception", "none", "empty"],
)
def test_fetch_cover_failure_returns_none(fake_source_cls, kwargs):
    assert cover.fetch_cover("1", source=fake_source_cls(**kwargs)) is None


def test_fetch_cover_failure_records_diagnostic(fake_source_cls):
    from jml.core import diagnostics

    diagnostics.clear()
    source = fake_source_cls(cover_error=RuntimeError("boom"))

    assert cover.fetch_cover("42", source=source) is None

    records = diagnostics.recent()
    assert any("COVER_FETCH_FAILED" in item and "42" in item for item in records)


def test_fetch_cover_empty_id_returns_none(fake_source_cls):
    assert cover.fetch_cover("", source=fake_source_cls()) is None
    assert cover.fetch_cover(None, source=fake_source_cls()) is None
