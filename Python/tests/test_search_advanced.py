"""强化搜索（双通道）单元测试。

覆盖：

- 网页通道成功解析：total 节点带 ``+`` 时记 ``total_capped``；
- 网页通道「无结果」是成功空结果，不降级；
- 网页通道全域名失败 → 降级 API，``capabilities`` 三项全 False，``hint`` 有说明；
- 非法参数回落默认值、``page_size`` 上限收敛；
- ``jm.search.search_advanced`` 服务方法的白名单与参数透传。

零联网：网页抓取函数与动态域名列表都被替换，API 客户端是假对象。
"""

from __future__ import annotations

import pytest

from jml.core import jm_source
from jml.services.host_services import call_service, register_host_services

# 网页搜索结果页 fixture：条目必须落在 <div class="well well-sm"> 与最后一个
# <div class="row"> 之间，才能被 jmcomic 的旧解析器提取。
HTML_ONE_PAGE_CAPPED = """
<html><body>
<div class="well well-sm">
  <div class="search-pagination-total">共 10000+ 部</div>
  <div class="item">
    <a href="/album/651712/abc" title="作品一"><img src="cover.jpg"></a>
    <div class="title-truncate tags ">
      <a>标签A</a>
      <a>标签B</a>
    </div>
  </div>
  <div class="row"></div>
</div>
</body></html>
"""

HTML_EMPTY = """
<html><body>
<div class="well well-sm">
  <div class="search-pagination-total">共 0 部</div>
  没有找到相关作品
  <div class="row"></div>
</div>
</body></html>
"""

HTML_BROKEN = "<html><body><h1>oops</h1></body></html>"

# 真实空结果页（P1 实测 nico.site）：没有 total 节点，只有「没发现A漫」文案，
# 也不满足 jmcomic 旧解析器的 <div class="well well-sm">…<div class="row"> 结构。
HTML_EMPTY_NO_TOTAL = """
<html><body>
<div class="row">
  <div class="well well-sm">
    <span class="title-truncate">搜寻: <span class="text-white">ZZZZ</span></span>
    <span class="text-danger">没发现A漫.</span>
    <h3>找不到和<span>ZZZZ</span>相符的內容</h3>
  </div>
</div>
</body></html>
"""

# 关键字过短等错误页：有 well 容器，但不是「搜索成功但 0 条」。
HTML_ERROR_PAGE = """
<html><body>
<div class="well well-sm">
  <fieldset>
  <legend>错误，關鍵字過短</legend>
  <div class="alert">請至少輸入兩個字以上。</div>
  </fieldset>
</div>
</body></html>
"""


class FakeApiPage:
    def __init__(self):
        self.content = [("123", {"name": "API作品", "tags": ["api标签"]})]
        self.page_count = 1
        self.total = 5


class FakeApiClient:
    """只实现搜索用的五个方法，记录调用参数。"""

    def __init__(self):
        self.calls = []

    def _search(self, name, kwargs):
        self.calls.append((name, kwargs))
        return FakeApiPage()

    def search_site(self, **kwargs):
        return self._search("search_site", kwargs)

    def search_work(self, **kwargs):
        return self._search("search_work", kwargs)

    def search_author(self, **kwargs):
        return self._search("search_author", kwargs)

    def search_tag(self, **kwargs):
        return self._search("search_tag", kwargs)

    def search_actor(self, **kwargs):
        return self._search("search_actor", kwargs)


@pytest.fixture(autouse=True)
def clean_html_cache():
    """conftest 不清模块级域名缓存，这里每用例清一次。"""
    jm_source._reset_html_cache()
    yield
    jm_source._reset_html_cache()


@pytest.fixture
def offline_domains(monkeypatch):
    """动态域名列表离线化，避免测试请求 jmcomic 发布页。"""
    monkeypatch.setattr(
        jm_source, "_dynamic_html_domains", lambda timeout=0: ["fake-search.local"]
    )
    return ["fake-search.local"]


def _patch_html(monkeypatch, html: str):
    calls = []

    def fake_fetch(url, domain, timeout=jm_source.HTML_CHANNEL_TIMEOUT):
        calls.append(url)
        return html

    monkeypatch.setattr(jm_source, "_fetch_html", fake_fetch)
    return calls


def test_html_success_marks_total_capped(monkeypatch, offline_domains):
    _patch_html(monkeypatch, HTML_ONE_PAGE_CAPPED)
    source = jm_source.JmcomicSource()
    source._client = FakeApiClient()

    result = source.search_advanced("MANA", page=2, main_tag=1)

    assert result["channel"] == "html"
    assert result["capabilities"] == {
        "time": True,
        "category": True,
        "sub_category": True,
    }
    assert result["total"] == 10000
    assert result["total_capped"] is True
    assert result["total_pages"] == 125
    assert result["hint"] == ""
    assert result["page"] == 2
    assert result["page_size"] == 80
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert set(row) == {
        "book_id", "title", "author", "page_count", "tags", "cover_url",
    }
    assert row["book_id"] == "651712"
    assert row["title"] == "作品一"
    assert row["tags"] == ["标签A", "标签B"]
    # 网页通道成功时不应碰 API 客户端
    assert source._client.calls == []


def test_html_empty_result_is_success_not_degraded(monkeypatch, offline_domains):
    _patch_html(monkeypatch, HTML_EMPTY)
    client = FakeApiClient()
    source = jm_source.JmcomicSource()
    source._client = client

    result = source.search_advanced("空结果")

    assert result["channel"] == "html"
    assert result["total"] == 0
    assert result["total_pages"] == 0
    assert result["results"] == []
    assert result["hint"] == ""
    assert client.calls == []


def test_html_empty_page_without_total_is_success(monkeypatch, offline_domains):
    """真实空结果页可能没有 total 节点，只有「没发现A漫」文案。"""
    _patch_html(monkeypatch, HTML_EMPTY_NO_TOTAL)
    client = FakeApiClient()
    source = jm_source.JmcomicSource()
    source._client = client

    result = source.search_advanced("ZZZZ")

    assert result["channel"] == "html"
    assert result["total"] == 0
    assert result["total_pages"] == 0
    assert result["results"] == []
    assert result["hint"] == ""
    assert client.calls == []


def test_html_error_page_still_degrades(monkeypatch, offline_domains):
    """关键字过短错误页也有 well 容器，不能当成成功的空结果。"""
    _patch_html(monkeypatch, HTML_ERROR_PAGE)
    client = FakeApiClient()
    source = jm_source.JmcomicSource()
    source._client = client

    result = source.search_advanced("x")

    assert result["channel"] == "api"
    assert result["capabilities"] == {
        "time": False,
        "category": False,
        "sub_category": False,
    }
    assert result["hint"]
    assert client.calls[0][0] == "search_site"


def test_html_all_domains_fail_degrades_to_api(monkeypatch, offline_domains):
    def broken_fetch(url, domain, timeout=jm_source.HTML_CHANNEL_TIMEOUT):
        raise ConnectionError(f"DNS 失败: {domain}")

    monkeypatch.setattr(jm_source, "_fetch_html", broken_fetch)
    client = FakeApiClient()
    source = jm_source.JmcomicSource()
    source._client = client

    result = source.search_advanced("MANA", main_tag=2, channel="auto")

    assert result["channel"] == "api"
    assert result["capabilities"] == {
        "time": False,
        "category": False,
        "sub_category": False,
    }
    assert result["total_capped"] is False
    assert result["hint"]
    assert result["total"] == 5
    assert result["total_pages"] == 1
    assert client.calls[0][0] == "search_author"


def test_invalid_parameters_fall_back_and_page_size_is_capped(monkeypatch, offline_domains):
    urls = _patch_html(monkeypatch, HTML_ONE_PAGE_CAPPED)
    source = jm_source.JmcomicSource()

    result = source.search_advanced(
        "x",
        page=0,
        page_size=10**6,
        main_tag=99,
        order_by="zzz",
        time="q",
        category="nope",
        sub_category="nope",
        channel="bogus",
    )

    assert result["channel"] == "html"
    assert result["page"] == 1
    assert result["page_size"] == 80
    assert "main_tag=0" in urls[0]
    assert "o=mr" in urls[0]
    assert "t=a" in urls[0]
    # category / sub_category 非法 → 不带分类路径
    assert "/search/photos?" in urls[0]


def test_page_size_clamp_helper():
    assert jm_source._clamp_page_size(0) == 80
    assert jm_source._clamp_page_size(-5) == 80
    assert jm_source._clamp_page_size(10) == 10
    assert jm_source._clamp_page_size(10**6) == 80
    assert jm_source._clamp_page_size("abc") == 80


def test_html_uses_category_and_sub_category_path(monkeypatch, offline_domains):
    urls = _patch_html(monkeypatch, HTML_ONE_PAGE_CAPPED)
    source = jm_source.JmcomicSource()

    source.search_advanced("x", category="doujin", sub_category="CG")

    assert "/search/photos/doujin/sub/CG?" in urls[0]


def test_html_url_carries_channel_filters(monkeypatch, offline_domains):
    urls = _patch_html(monkeypatch, HTML_ONE_PAGE_CAPPED)
    jm_source.JmcomicSource().search_advanced(
        "查询 词", page=3, main_tag=4, order_by="tf", time="w"
    )

    url = urls[0]
    assert "main_tag=4" in url
    assert "o=tf" in url
    assert "t=w" in url
    assert "page=3" in url
    assert "search_query=%E6%9F%A5%E8%AF%A2+%E8%AF%8D" in url


def test_channel_api_skips_html(monkeypatch, offline_domains):
    urls = _patch_html(monkeypatch, HTML_ONE_PAGE_CAPPED)
    client = FakeApiClient()
    source = jm_source.JmcomicSource()
    source._client = client

    result = source.search_advanced("MANA", channel="api")

    assert result["channel"] == "api"
    assert result["hint"] == ""
    assert urls == []
    assert client.calls[0][0] == "search_site"
    # time / category / sub_category 只支持网页通道：API 固定用「全部」
    kwargs = client.calls[0][1]
    assert kwargs["time"] == "a"
    assert kwargs["category"] == "0"
    assert kwargs["sub_category"] is None


def test_channel_api_ignores_html_only_filters(monkeypatch, offline_domains):
    _patch_html(monkeypatch, HTML_ONE_PAGE_CAPPED)
    client = FakeApiClient()
    source = jm_source.JmcomicSource()
    source._client = client

    result = source.search_advanced(
        "MANA", channel="api", time="w", category="doujin", sub_category="CG"
    )

    assert result["capabilities"] == {
        "time": False,
        "category": False,
        "sub_category": False,
    }
    kwargs = client.calls[0][1]
    assert kwargs["time"] == "a"
    assert kwargs["category"] == "0"
    assert kwargs["sub_category"] is None


def test_channel_html_fails_and_degrades_with_hint(monkeypatch, offline_domains):
    def broken_fetch(url, domain, timeout=jm_source.HTML_CHANNEL_TIMEOUT):
        return HTML_BROKEN

    monkeypatch.setattr(jm_source, "_fetch_html", broken_fetch)
    source = jm_source.JmcomicSource()
    source._client = FakeApiClient()

    result = source.search_advanced("MANA", channel="html")

    assert result["channel"] == "api"
    assert result["hint"] == jm_source._HTML_DEGRADED_HINT


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------


class RecordingSource:
    def __init__(self):
        self.calls = []

    def search_advanced(self, keyword, **kwargs):
        self.calls.append((keyword, kwargs))
        return {"channel": "html", "total": 1, "results": []}


@pytest.fixture
def recording_source():
    source = RecordingSource()
    jm_source.set_source(source)
    register_host_services()
    return source


def test_service_search_advanced_envelope_and_passthrough(recording_source):
    result = call_service(
        "jm.search",
        1,
        "search_advanced",
        {
            "keyword": "  MANA  ",
            "page": 2,
            "page_size": 20,
            "main_tag": 2,
            "order_by": "tf",
            "time": "w",
            "category": "doujin",
            "sub_category": "CG",
            "channel": "html",
        },
    )

    assert result["ok"] is True
    assert result["channel"] == "html"
    assert result["total"] == 1
    assert recording_source.calls == [
        (
            "MANA",
            {
                "page": 2,
                "page_size": 20,
                "main_tag": 2,
                "order_by": "tf",
                "time": "w",
                "category": "doujin",
                "sub_category": "CG",
                "channel": "html",
            },
        )
    ]


def test_service_search_advanced_invalid_values_use_defaults(recording_source):
    result = call_service(
        "jm.search",
        1,
        "search_advanced",
        {
            "keyword": None,
            "page": -3,
            "page_size": 0,
            "main_tag": 9,
            "order_by": "zzz",
            "time": "",
            "category": "bad",
            "sub_category": "bad",
            "channel": "bad",
        },
    )

    assert result["ok"] is True
    assert recording_source.calls == [
        (
            "",
            {
                "page": 1,
                "page_size": 80,
                "main_tag": 0,
                "order_by": "mr",
                "time": "a",
                "category": "0",
                "sub_category": None,
                "channel": "auto",
            },
        )
    ]


def test_service_search_advanced_exception_becomes_error_envelope(recording_source):
    def boom(keyword, **kwargs):
        raise RuntimeError("通道爆炸")

    recording_source.search_advanced = boom

    result = call_service("jm.search", 1, "search_advanced", {"keyword": "x"})

    assert result["ok"] is False
    assert result["error_code"]
    assert "通道爆炸" in result["error_message"]
