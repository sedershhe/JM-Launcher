"""强化搜索插件：只做参数校验与 search 模板 payload 拼装。

搜索、通道降级、HTML 解析、域名与缓存都在宿主的 ``jm.search`` 服务里，
本插件不直接发 HTTP、不 import jmcomic，也不探测域名。
"""

from __future__ import annotations

# 必须与 plugin.json 里 pages[].id 完全一致。
PAGE_ID = "advanced_search.main"

# 宿主搜索服务（清单 services.requires 已声明）。
SEARCH_SERVICE = "jm.search"
SEARCH_VERSION = 1

# 宿主服务默认每页条数；响应没带 page_size 时兜底。
DEFAULT_PAGE_SIZE = 80

#: 五类筛选的定义。value 用宿主服务认识的语义字符串（main_tag 提交时转 int），
#: 不在插件里映射到 jmcomic 常量。
_FILTERS = (
    (
        "main_tag",
        "作品范围",
        (
            ("0", "站内"),
            ("1", "作品"),
            ("2", "作者"),
            ("3", "标签"),
            ("4", "登场角色"),
        ),
    ),
    (
        "order_by",
        "排序",
        (
            ("mr", "最新"),
            ("mv", "最多浏览"),
            ("mp", "最多图"),
            ("tf", "最多点赞"),
        ),
    ),
    (
        "time",
        "时间",
        (
            ("a", "全部"),
            ("t", "今日"),
            ("w", "本周"),
            ("m", "本月"),
        ),
    ),
    (
        "category",
        "分类",
        (
            ("0", "全部"),
            ("doujin", "同人"),
            ("single", "单本"),
            ("short", "短篇"),
            ("another", "其他"),
            ("hanman", "韩漫"),
            ("meiman", "美漫"),
            ("doujin_cosplay", "同人Cosplay"),
            ("3D", "3D"),
            ("english_site", "英站"),
        ),
    ),
    (
        "sub_category",
        "子分类",
        (
            ("", "--"),
            ("chinese", "汉化"),
            ("japanese", "日语"),
            ("CG", "CG"),
            ("other", "其他"),
            ("cosplay", "Cosplay"),
            ("3d", "3D"),
            ("youth", "青年"),
        ),
    ),
)

# 子分类只对特定分类有效（依据 jmcomic 常量与 2026-11-08 站点实测）。
_SUB_CATEGORY_RULES = {
    "doujin": ("chinese", "japanese", "CG"),
    "single": ("youth",),
    "another": ("other", "cosplay", "3d"),
}

#: 筛选 key → 服务 capabilities 里的能力键；main_tag / order_by 双通道都支持，不置灰。
_CAPABILITY_KEYS = {
    "time": "time",
    "category": "category",
    "sub_category": "sub_category",
}

_CAPABILITY_HINT = "接口通道不支持此筛选"

_DEFAULTS = {
    "keyword": "",
    "main_tag": "0",
    "order_by": "mr",
    "time": "a",
    "category": "0",
    "sub_category": "",
}

_EMPTY_TEXT = "没有匹配的作品，试试减少 + 条件或去掉 - 排除"
_NO_KEYWORD_TEXT = "输入关键词后点「搜索」"

_SEARCH_BOX = {
    "label": "关键词",
    "placeholder": "输入关键词，支持 + 包含 / - 排除，如：全彩 -人妻",
    "submit_text": "搜索",
}

_ADVANCED = {
    "label": "高级筛选",
    "default_expanded": False,
    "summary": "五类范围 · 排序 · 时间 · 分类",
}

_ROW_ACTIONS = [
    {"id": "open_detail", "title": "详情", "primary": True},
    {"id": "download", "title": "下载"},
]


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _positive_int(value, default: int = 1) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _non_negative_int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _pick(value, allowed: tuple[str, ...], default: str) -> str:
    text = _text(value)
    return text if text in allowed else default


def _allowed_values(key: str) -> tuple[str, ...]:
    for filter_key, _label, options in _FILTERS:
        if filter_key == key:
            return tuple(value for value, _text_ in options)
    return ()


def _normalise(params: dict) -> dict:
    """把宿主传来的平铺参数收敛成合法状态；非法值一律回落默认。"""
    keyword = _text(params.get("keyword"))
    state = {
        "keyword": keyword,
        "main_tag": _pick(
            params.get("main_tag"), _allowed_values("main_tag"), _DEFAULTS["main_tag"]
        ),
        "order_by": _pick(
            params.get("order_by"), _allowed_values("order_by"), _DEFAULTS["order_by"]
        ),
        "time": _pick(params.get("time"), _allowed_values("time"), _DEFAULTS["time"]),
        "category": _pick(
            params.get("category"), _allowed_values("category"), _DEFAULTS["category"]
        ),
        "sub_category": _pick(
            params.get("sub_category"),
            _allowed_values("sub_category"),
            _DEFAULTS["sub_category"],
        ),
        "page": _positive_int(params.get("page"), 1),
    }
    return state


def _capabilities(data: dict) -> dict:
    raw = data.get("capabilities")
    raw = raw if isinstance(raw, dict) else {}
    return {
        key: bool(raw.get(key, True))
        for key in ("time", "category", "sub_category")
    }


def _filters_payload(state: dict, capabilities: dict) -> list[dict]:
    filters = []
    for key, label, options in _FILTERS:
        capability_key = _CAPABILITY_KEYS.get(key)
        enabled = True if capability_key is None else capabilities[capability_key]
        item_options = [
            {"value": value, "label": option_label}
            for value, option_label in options
        ]
        value = state[key]
        if key == "sub_category":
            # category 可能是 int 或 str，统一转 str 再查表；缺失按 "0" 处理。
            allowed = _SUB_CATEGORY_RULES.get(str(state.get("category", "0")), ())
            item_options = [
                option
                for option in item_options
                if option["value"] == "" or option["value"] in allowed
            ]
            current = str(state.get("sub_category", "") or "")
            value = "" if current and current not in allowed else current
        item = {
            "key": key,
            "label": label,
            "options": item_options,
            "value": value,
            "enabled": enabled,
        }
        if not enabled:
            item["hint"] = _CAPABILITY_HINT
        filters.append(item)
    return filters


def _rows_of(results) -> list[dict]:
    if not isinstance(results, list):
        return []
    rows = []
    for item in results:
        if not isinstance(item, dict):
            continue
        book_id = _text(item.get("book_id"))
        tags = item.get("tags")
        if isinstance(tags, str):
            tags = [tags] if tags else []
        elif not isinstance(tags, list):
            tags = []
        rows.append(
            {
                "book_id": book_id,
                "thumb_book_id": book_id,
                "title": _text(item.get("title")),
                "author": _text(item.get("author")),
                "page_count": _non_negative_int(item.get("page_count"), 0),
                "tags": [str(tag) for tag in tags if _text(tag)],
            }
        )
    return rows


def _page_payload(state: dict, data: dict) -> dict:
    capabilities = _capabilities(data)
    return {
        "search_box": {**_SEARCH_BOX, "value": state["keyword"]},
        "filters": _filters_payload(state, capabilities),
        "advanced": dict(_ADVANCED),
        "rows": _rows_of(data.get("results")),
        "page": _positive_int(data.get("page"), state["page"]),
        "page_size": _positive_int(data.get("page_size"), DEFAULT_PAGE_SIZE),
        "total": _non_negative_int(data.get("total"), 0),
        "total_pages": _non_negative_int(data.get("total_pages"), 0),
        "channel": _text(data.get("channel")),
        "capabilities": capabilities,
        "hint": _text(data.get("hint")),
        "row_actions": [dict(item) for item in _ROW_ACTIONS],
        "row_double_click_action": "open_detail",
        "empty_text": _EMPTY_TEXT,
    }


def _empty_payload(state: dict, empty_text: str) -> dict:
    """首次进入 / 没关键词：不调服务，筛选项全部可用。"""
    capabilities = {"time": True, "category": True, "sub_category": True}
    return {
        "search_box": {**_SEARCH_BOX, "value": state["keyword"]},
        "filters": _filters_payload(state, capabilities),
        "advanced": dict(_ADVANCED),
        "rows": [],
        "page": state["page"],
        "page_size": DEFAULT_PAGE_SIZE,
        "total": 0,
        "total_pages": 0,
        "channel": "",
        "capabilities": capabilities,
        "hint": "",
        "row_actions": [dict(item) for item in _ROW_ACTIONS],
        "row_double_click_action": "open_detail",
        "empty_text": empty_text,
    }


def _row_book_id(payload: dict) -> str:
    row = payload.get("row")
    if isinstance(row, dict):
        book_id = _text(row.get("book_id"))
        if book_id:
            return book_id
    return _text(payload.get("book_id"))


class AdvancedSearchPage:
    """强化搜索页面对象。"""

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def load(self, params: dict) -> dict:
        params = params if isinstance(params, dict) else {}
        state = _normalise(params)

        if not state["keyword"]:
            return _empty_payload(state, _NO_KEYWORD_TEXT)

        data = self._ctx.call_service(
            SEARCH_SERVICE,
            SEARCH_VERSION,
            "search_advanced",
            {
                "keyword": state["keyword"],
                "page": state["page"],
                "main_tag": int(state["main_tag"]),
                "order_by": state["order_by"],
                "time": state["time"],
                "category": state["category"],
                "sub_category": state["sub_category"] or None,
            },
        )
        if not isinstance(data, dict):
            data = {}
        return _page_payload(state, data)

    # -- 行动作 -------------------------------------------------------------

    def action_open_detail(self, payload: dict) -> dict:
        book_id = _row_book_id(payload if isinstance(payload, dict) else {})
        if not book_id:
            return {"ok": False, "refresh": False, "message": "缺少作品编号，无法打开详情"}
        return {"ok": True, "intent": {"kind": "detail", "book_id": book_id}}

    def action_download(self, payload: dict) -> dict:
        book_id = _row_book_id(payload if isinstance(payload, dict) else {})
        if not book_id:
            return {"ok": False, "refresh": False, "message": "缺少作品编号，无法下载"}
        return {"ok": True, "intent": {"kind": "download", "book_ids": [book_id]}}

    def dispatch(self, action_id: str, payload: dict) -> dict:
        """按动作 id 分发；未知 id 走协议约定的失败返回。

        宿主只把注册过的 id 路由进来，这个方法便于自测和未来扩展。
        """
        if action_id == "open_detail":
            return self.action_open_detail(payload)
        if action_id == "download":
            return self.action_download(payload)
        return {"ok": False, "refresh": False, "message": "未知操作"}


def setup(ctx) -> None:
    page = AdvancedSearchPage(ctx)
    ctx.register_page(PAGE_ID, page.load)
    ctx.register_page_action(PAGE_ID, "open_detail", page.action_open_detail)
    ctx.register_page_action(PAGE_ID, "download", page.action_download)
    ctx.log(f"{ctx.plugin_id} 强化搜索页已装载")
