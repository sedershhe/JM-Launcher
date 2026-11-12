"""强化搜索插件的离线单元测试。

插件只依赖宿主注入的 ctx，所以这里用假 ctx 直接驱动 load / action，
不启动主程序、不联网、不写真实用户目录。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PLUGIN_PATH = (
    Path(__file__).resolve().parents[2] / "plugins" / "advanced_search" / "main.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "advanced_search_plugin_main", PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_plugin()


class FakeCtx:
    """够用的插件上下文：记录注册结果与服务调用。"""

    def __init__(self, response=None):
        self.plugin_id = "advanced_search"
        self.data_dir = ""
        self.response = response if response is not None else {}
        self.calls = []
        self.pages = {}
        self.actions = {}
        self.logs = []

    def register_page(self, page_id, load):
        self.pages[page_id] = load

    def register_page_action(self, page_id, action_id, handler):
        self.actions[(page_id, action_id)] = handler

    def call_service(self, name, version, method, payload):
        self.calls.append(
            {"name": name, "version": version, "method": method, "payload": payload}
        )
        return self.response

    def log(self, message):
        self.logs.append(str(message))


def _response() -> dict:
    return {
        "ok": True,
        "channel": "html",
        "capabilities": {"time": False, "category": True, "sub_category": False},
        "total_capped": False,
        "keyword": "全彩 -人妻",
        "page": 1,
        "page_size": 80,
        "total": 257,
        "total_pages": 4,
        "results": [
            {
                "book_id": "1482019",
                "title": "[MANA] NO.43",
                "author": "MANA",
                "page_count": 120,
                "tags": ["全彩", "CG"],
                "cover_url": "https://example.invalid/cover.jpg",
            },
            {
                "book_id": 1482020,
                "title": "第二本",
                "author": "",
                "page_count": "0",
                "tags": [],
                "cover_url": "",
            },
        ],
        "hint": "网页通道",
    }


def _make_page(response=None):
    ctx = FakeCtx(_response() if response is None else response)
    main.setup(ctx)
    return ctx, ctx.pages[main.PAGE_ID]


def test_setup_registers_page_and_actions():
    ctx, _load = _make_page()

    assert set(ctx.pages) == {main.PAGE_ID}
    assert set(ctx.actions) == {
        (main.PAGE_ID, "open_detail"),
        (main.PAGE_ID, "download"),
    }
    assert ctx.logs


def test_load_builds_search_payload_and_maps_rows():
    ctx, load = _make_page()

    data = load(
        {
            "keyword": " 全彩 -人妻 ",
            "page": 2,
            "main_tag": "2",
            "order_by": "mv",
            "time": "w",
            "category": "doujin",
            "sub_category": "CG",
        }
    )

    assert len(ctx.calls) == 1
    call = ctx.calls[0]
    assert call["name"] == "jm.search"
    assert call["version"] == 1
    assert call["method"] == "search_advanced"
    assert call["payload"] == {
        "keyword": "全彩 -人妻",
        "page": 2,
        "main_tag": 2,
        "order_by": "mv",
        "time": "w",
        "category": "doujin",
        "sub_category": "CG",
    }
    # 通道由宿主默认 auto，插件不传。
    assert "channel" not in call["payload"]

    assert data["search_box"] == {
        "label": "关键词",
        "placeholder": "输入关键词，支持 + 包含 / - 排除，如：全彩 -人妻",
        "value": "全彩 -人妻",
        "submit_text": "搜索",
    }
    assert data["advanced"] == {
        "label": "高级筛选",
        "default_expanded": False,
        "summary": "五类范围 · 排序 · 时间 · 分类",
    }

    # 协议 §6.9 限制筛选不超过 5 个；方案里也是 main_tag/order_by/time/category/sub_category。
    filters = data["filters"]
    assert [item["key"] for item in filters] == [
        "main_tag",
        "order_by",
        "time",
        "category",
        "sub_category",
    ]
    assert [item["label"] for item in filters] == [
        "作品范围",
        "排序",
        "时间",
        "分类",
        "子分类",
    ]
    for item in filters:
        assert set(item) >= {"key", "label", "options", "value", "enabled"}
        assert item["options"]
        assert all(set(option) == {"value", "label"} for option in item["options"])

    # capabilities 映射：time/sub_category 关闭并给提示，category 保持可用且无提示。
    by_key = {item["key"]: item for item in filters}
    assert by_key["time"]["enabled"] is False
    assert by_key["time"]["hint"] == "接口通道不支持此筛选"
    assert by_key["sub_category"]["enabled"] is False
    assert by_key["sub_category"]["hint"] == "接口通道不支持此筛选"
    assert by_key["category"]["enabled"] is True
    assert "hint" not in by_key["category"]
    assert by_key["main_tag"]["enabled"] is True
    assert by_key["order_by"]["enabled"] is True

    assert [item["value"] for item in filters] == [
        "2",
        "mv",
        "w",
        "doujin",
        "CG",
    ]

    assert data["rows"] == [
        {
            "book_id": "1482019",
            "thumb_book_id": "1482019",
            "title": "[MANA] NO.43",
            "author": "MANA",
            "page_count": 120,
            "tags": ["全彩", "CG"],
        },
        {
            "book_id": "1482020",
            "thumb_book_id": "1482020",
            "title": "第二本",
            "author": "",
            "page_count": 0,
            "tags": [],
        },
    ]

    assert data["page"] == 1
    assert data["page_size"] == 80
    assert data["total"] == 257
    assert data["total_pages"] == 4
    assert data["channel"] == "html"
    assert data["capabilities"] == {
        "time": False,
        "category": True,
        "sub_category": False,
    }
    assert data["hint"] == "网页通道"
    assert data["row_actions"] == [
        {"id": "open_detail", "title": "详情", "primary": True},
        {"id": "download", "title": "下载"},
    ]
    assert data["row_double_click_action"] == "open_detail"
    assert data["empty_text"] == "没有匹配的作品，试试减少 + 条件或去掉 - 排除"


def test_load_without_keyword_does_not_call_service():
    ctx, load = _make_page()

    data = load({"page": 3, "main_tag": "99", "order_by": "tr"})

    assert ctx.calls == []
    assert data["rows"] == []
    assert data["page"] == 3
    assert data["total"] == 0
    assert data["total_pages"] == 0
    assert data["channel"] == ""
    assert data["capabilities"] == {
        "time": True,
        "category": True,
        "sub_category": True,
    }
    assert all(item["enabled"] is True for item in data["filters"])
    assert all("hint" not in item for item in data["filters"])
    assert data["empty_text"] == "输入关键词后点「搜索」"
    assert data["search_box"]["value"] == ""


def test_invalid_params_fall_back_to_defaults_and_are_passed_through():
    ctx, load = _make_page()

    data = load(
        {
            "keyword": "测试",
            "page": 0,
            "main_tag": 99,
            "order_by": "tr",
            "time": "bad",
            "category": "not-a-category",
            "sub_category": "not-a-sub",
        }
    )

    assert ctx.calls[0]["payload"] == {
        "keyword": "测试",
        "page": 1,
        "main_tag": 0,
        "order_by": "mr",
        "time": "a",
        "category": "0",
        "sub_category": None,
    }
    assert [item["value"] for item in data["filters"]] == [
        "0",
        "mr",
        "a",
        "0",
        "",
    ]


def test_actions_return_protocol_intents_and_unknown_message():
    ctx, _load = _make_page()
    page = main.AdvancedSearchPage(ctx)

    assert page.action_open_detail({"row": {"book_id": "1482019"}}) == {
        "ok": True,
        "intent": {"kind": "detail", "book_id": "1482019"},
    }
    assert page.action_download({"row": {"book_id": 1482019}}) == {
        "ok": True,
        "intent": {"kind": "download", "book_ids": ["1482019"]},
    }

    # 宿主 loader 的动作 payload 也可能把 book_id 放在顶层，兼容一下。
    assert page.action_open_detail({"book_id": "123"})["intent"] == {
        "kind": "detail",
        "book_id": "123",
    }

    assert page.dispatch("not-exist", {}) == {
        "ok": False,
        "refresh": False,
        "message": "未知操作",
    }

    missing = page.action_open_detail({"row": {}})
    assert missing["ok"] is False
    assert missing["message"]


def test_empty_response_keeps_required_shape():
    ctx, load = _make_page({})

    data = load({"keyword": "空结果"})

    assert data["rows"] == []
    assert data["capabilities"] == {
        "time": True,
        "category": True,
        "sub_category": True,
    }
    assert data["total"] == 0
    assert data["total_pages"] == 0
    assert data["channel"] == ""
    assert len(data["filters"]) == 5
    assert data["empty_text"] == "没有匹配的作品，试试减少 + 条件或去掉 - 排除"


@pytest.mark.parametrize(
    "filter_key,expected_count",
    # sub_category 未指定分类时默认 category="0"，只剩空值占位一项。
    [("main_tag", 5), ("order_by", 4), ("time", 4), ("category", 10), ("sub_category", 1)],
)
def test_filter_option_counts(filter_key, expected_count):
    _ctx, load = _make_page()
    data = load({"keyword": "x"})
    item = {f["key"]: f for f in data["filters"]}[filter_key]
    assert len(item["options"]) == expected_count


@pytest.mark.parametrize(
    "category,expected_values",
    [
        ("doujin", ["", "chinese", "japanese", "CG"]),
        ("single", ["", "youth"]),
        ("another", ["", "other", "cosplay", "3d"]),
    ],
)
def test_sub_category_options_follow_category(category, expected_values):
    _ctx, load = _make_page()

    data = load({"keyword": "x", "category": category})
    item = {f["key"]: f for f in data["filters"]}["sub_category"]

    assert [option["value"] for option in item["options"]] == expected_values


def test_invalid_sub_category_falls_back_to_empty_for_all_category():
    _ctx, load = _make_page()

    data = load({"keyword": "x", "category": "0", "sub_category": "chinese"})
    item = {f["key"]: f for f in data["filters"]}["sub_category"]

    assert [option["value"] for option in item["options"]] == [""]
    assert item["value"] == ""
