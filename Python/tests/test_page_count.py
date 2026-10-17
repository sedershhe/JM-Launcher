"""DATA-1 回归：页数必须取真实值（全部离线，假数据源注入）。

真机验收缺陷：搜索页 5/5 条与详情 2/2 本都显示"页数: 0"。
根因：jmcomic 2.7.1 的搜索列表接口不返回页数，专辑 API 适配层又把
``page_count`` 硬编码为 0；真实页数只在专辑原始 JSON 的 ``total_photos``。

本文件锁定四件事：

1. 列表接口带页数时原样透传，不发补全请求；
2. 列表不带页数时按"有界并发 3"做详情补全，且同一本只拉一次（缓存）；
3. 单条补全失败只让该条为 0，不影响其它条目；超时放弃时剩余条目为 0；
4. 详情入口拿到的就是真实页数。
"""

from __future__ import annotations

import threading
import time

import pytest

from jml.core import detail, searcher
from jml.core.protocol import EVENT_SEARCH_PAGE_LOADED
from jml.host import events


class FakePageSource:
    """只实现搜索 + 详情的最小假数据源，并为并发断言记录峰值。"""

    def __init__(
        self,
        results: list[dict],
        *,
        page_counts: dict[str, int] | None = None,
        default_page_count: int = 42,
        fail_books: set[str] | None = None,
        album_error: BaseException | None = None,
        sleep: float = 0.0,
    ) -> None:
        self.results = results
        self.page_counts = dict(page_counts or {})
        self.default_page_count = default_page_count
        self.fail_books = fail_books or set()
        self.album_error = album_error
        self.sleep = sleep

        self.album_calls: list[str] = []
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def search(self, keyword: str, page: int) -> dict:
        return {
            "keyword": keyword,
            "page": page,
            "total_pages": 1,
            "results": [dict(item) for item in self.results],
        }

    def get_album(self, book_id: str) -> dict:
        with self._lock:
            self.album_calls.append(book_id)
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            if self.sleep:
                time.sleep(self.sleep)
            if self.album_error is not None:
                raise self.album_error
            if book_id in self.fail_books:
                raise RuntimeError(f"假数据源详情失败: {book_id}")
            return {
                "book_id": book_id,
                "title": f"标题{book_id}",
                "author": "作者",
                "page_count": self.page_counts.get(book_id, self.default_page_count),
                "tags": ["标签"],
                "description": "简介",
                "cover_url": "",
                "chapters": [{"photo_id": book_id, "index": 1, "title": "第1章"}],
            }
        finally:
            with self._lock:
                self._active -= 1


def _result(book_id: str, page_count: int = 0) -> dict:
    return {
        "book_id": book_id,
        "title": f"标题{book_id}",
        "author": "作者",
        "page_count": page_count,
        "tags": ["标签"],
        "cover_url": "",
    }


# ---------------------------------------------------------------------------
# 搜索：透传 / 补全 / 并发 / 失败隔离 / 缓存 / 超时
# ---------------------------------------------------------------------------


def test_list_page_count_is_passed_through_without_extra_requests():
    source = FakePageSource([_result("1", 42), _result("2", 34)])

    result = searcher.SearchSession(source=source).search("关键词", 1)

    assert [item["page_count"] for item in result["results"]] == [42, 34]
    assert source.album_calls == [], "列表已带页数时不应再拉详情"


def test_missing_page_count_is_filled_with_bounded_concurrency():
    results = [_result(str(index)) for index in range(1, 6)]
    source = FakePageSource(results, page_counts={str(i): i * 10 for i in range(1, 6)})
    source.sleep = 0.05  # 让并发峰值稳定达到 3

    result = searcher.SearchSession(source=source).search("关键词", 1)

    assert [item["page_count"] for item in result["results"]] == [10, 20, 30, 40, 50]
    assert len(source.album_calls) == 5
    assert source.peak <= searcher.PAGE_COUNT_WORKERS
    assert source.peak >= 2, "补全应并发执行，而不是串行"


def test_page_count_completion_uses_workers_limit_constant():
    assert searcher.PAGE_COUNT_WORKERS == 3
    assert searcher.PAGE_COUNT_TIMEOUT > 0


def test_single_completion_failure_stays_zero_and_does_not_affect_others():
    source = FakePageSource([_result("1"), _result("2")], fail_books={"1"})

    result = searcher.SearchSession(source=source).search("关键词", 1)

    assert [item["page_count"] for item in result["results"]] == [0, 42]
    assert result["ok"] is True


def test_search_event_carries_filled_page_counts():
    received: list[dict] = []
    events.BUS.subscribe(EVENT_SEARCH_PAGE_LOADED, received.append)
    source = FakePageSource([_result("1")])

    searcher.SearchSession(source=source).search("关键词", 1)

    assert received, "应派发 search.page_loaded"
    assert received[-1]["results"][0]["page_count"] == 42


def test_page_count_is_cached_across_paging():
    source = FakePageSource([_result("1"), _result("2")])
    session = searcher.SearchSession(source=source)

    first = session.search("关键词", 1)
    first_calls = list(source.album_calls)
    second = session.search("改词", 2)  # 翻页：同一批结果

    assert [item["page_count"] for item in first["results"]] == [42, 42]
    assert [item["page_count"] for item in second["results"]] == [42, 42]
    assert source.album_calls == first_calls, "翻页不应重复拉同一本的页数"


def test_completion_timeout_keeps_remaining_zero(monkeypatch):
    monkeypatch.setattr(searcher, "PAGE_COUNT_TIMEOUT", 0.05)
    source = FakePageSource([_result("1"), _result("2")], sleep=0.2)

    started = time.monotonic()
    result = searcher.SearchSession(source=source).search("关键词", 1)
    elapsed = time.monotonic() - started

    assert [item["page_count"] for item in result["results"]] == [0, 0]
    assert elapsed < 1.0, "超时后必须立刻返回，不能等慢请求跑完"


# ---------------------------------------------------------------------------
# 详情入口
# ---------------------------------------------------------------------------


def test_detail_entry_returns_real_page_count():
    source = FakePageSource([], page_counts={"651712": 42})

    result = detail.get_detail("651712", source=source)

    assert result["ok"] is True
    assert result["page_count"] == 42


def test_detail_entry_keeps_zero_when_source_has_no_page_count():
    source = FakePageSource([], default_page_count=0)

    result = detail.get_detail("1", source=source)

    assert result["page_count"] == 0


@pytest.mark.parametrize("page", [1, 2])
def test_completion_runs_on_paging_too(page):
    source = FakePageSource([_result("9")], page_counts={"9": 17})
    session = searcher.SearchSession(source=source)

    result = session.search("关键词", page)

    assert result["page"] == page
    assert result["results"][0]["page_count"] == 17
