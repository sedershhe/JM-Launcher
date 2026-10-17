"""搜索业务（功能计划书 4.2 + 旧缺陷 #8 的修正）。

翻页语义（关键，旧缺陷 #8）：

- 每次"发起搜索"（``page <= 1``）把关键词**快照**进会话；
- 翻页（``page > 1``）只用会话里的快照关键词，**忽略调用方新传入的关键词**。
  用户改了搜索框再点"下一页"时，翻的仍是上一次搜索结果，不会串页；
- 要换关键词必须重新发起搜索（page=1）。

事件：一次搜索成功后派发 ``search.page_loaded``，payload 字段
keyword / page / total_pages / results 与协议表一致。

本模块不联网：网络访问全部经 ``jm_source`` 数据源协议，测试可注入假数据源。
"""

from __future__ import annotations

import threading

from jml.core import diagnostics, jm_source
from jml.core.errors import InvalidArgumentError, error_payload
from jml.core.protocol import EVENT_SEARCH_PAGE_LOADED
from jml import events


class SearchSession:
    """一个搜索会话：保存"发起搜索时"的关键词快照，供翻页复用。"""

    def __init__(self, source=None) -> None:
        self._lock = threading.RLock()
        self._source = source
        self._keyword: str | None = None

    # -- 只读属性 -----------------------------------------------------------

    @property
    def keyword(self) -> str | None:
        """当前快照关键词（尚未搜索时为 None）。"""
        with self._lock:
            return self._keyword

    # -- 搜索 ---------------------------------------------------------------

    def search(self, keyword: object, page: object = 1) -> dict:
        """搜索/翻页，返回成功结构（失败时抛异常，由入口层转失败形态）。

        返回::

            {"ok": True, "keyword": str, "page": int,
             "total_pages": int, "results": [ {book_id,title,author,
             page_count,tags,cover_url}, ... ]}
        """
        page_number = _normalize_page(page)
        text = "" if keyword is None else str(keyword).strip()

        with self._lock:
            if page_number <= 1:
                if not text:
                    raise InvalidArgumentError("搜索关键词不能为空")
                self._keyword = text
                page_number = 1
            else:
                if self._keyword is None:
                    if not text:
                        raise InvalidArgumentError("搜索关键词不能为空")
                    self._keyword = text
                # 翻页绑定发起搜索时的关键词快照（旧缺陷 #8）
                text = self._keyword
            source = self._source or jm_source.get_source()

        raw = source.search(text, page_number)
        result = _normalize_result(raw, text, page_number)
        self._emit(result)
        return result

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _emit(result: dict) -> None:
        """派发 search.page_loaded；事件异常不得影响搜索结果返回。"""
        payload = {
            "type": EVENT_SEARCH_PAGE_LOADED,
            "keyword": result["keyword"],
            "page": result["page"],
            "total_pages": result["total_pages"],
            "results": result["results"],
        }
        try:
            events.BUS.dispatch(EVENT_SEARCH_PAGE_LOADED, payload)
        except Exception as exc:
            # 订阅者异常由事件总线单独捕获；这里兜底的是 payload 校验等极端情况
            diagnostics.record(
                "SEARCH_EVENT_FAILED",
                "search.page_loaded 派发失败",
                exc,
            )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _normalize_page(page: object) -> int:
    """把页码规范成正整数；无法解析或 < 1 一律按第 1 页处理。"""
    try:
        number = int(page)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    return number if number >= 1 else 1


def _normalize_result(raw: dict, keyword: str, page: int) -> dict:
    """把数据源返回值规范化成桥接契约结构（字段类型固定）。"""
    if not isinstance(raw, dict):
        raise ValueError(f"搜索数据源返回类型非法: {type(raw).__name__}")

    raw_results = raw.get("results") or []
    results: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        results.append(
            {
                "book_id": str(item.get("book_id") or ""),
                "title": str(item.get("title") or ""),
                "author": str(item.get("author") or ""),
                "page_count": _to_int(item.get("page_count")),
                "tags": [str(tag) for tag in tags],
                "cover_url": str(item.get("cover_url") or ""),
            }
        )

    return {
        "ok": True,
        "keyword": str(raw.get("keyword") or keyword),
        "page": _to_int(raw.get("page"), page),
        "total_pages": max(0, _to_int(raw.get("total_pages"))),
        "results": results,
    }


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 按调用方 key 隔离的搜索会话（单一所有者 + 互不串页）
# ---------------------------------------------------------------------------

#: key → 会话。主界面固定用 "main"；插件用 "plugin:<plugin_id>"。
_SESSIONS: dict[str, SearchSession] = {}
_SESSIONS_LOCK = threading.RLock()


def get_session(key: object = "main") -> SearchSession:
    """返回指定调用方 key 的搜索会话（不存在则创建）。

    隔离语义：主界面 ``main`` 与插件 ``plugin:<id>`` 各自保存自己的关键词
    快照，一方翻页不会改另一方，解决"两调用方交替 page=1/page=2 互相串页"。
    """
    identity = str(key or "main")
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(identity)
        if session is None:
            session = SearchSession()
            _SESSIONS[identity] = session
        return session


def get_shared_session() -> SearchSession:
    """兼容入口：返回 ``main`` 会话（新代码请用 ``get_session(key)``）。"""
    return get_session("main")


def reset_sessions() -> None:
    """清空所有搜索会话（测试隔离用；生产不调用）。"""
    with _SESSIONS_LOCK:
        _SESSIONS.clear()


def reset_shared_session() -> None:
    """兼容入口：等价于 ``reset_sessions()``。"""
    reset_sessions()


# ---------------------------------------------------------------------------
# 便捷入口（一次性搜索，不保留翻页快照）
# ---------------------------------------------------------------------------


def search(keyword: str, page: int = 1, source=None) -> dict:
    """一次性搜索：每次新建会话。

    需要"下一页继续用旧关键词"的调用方请持有 ``SearchSession`` 实例
    （后端入口与 jm.search 服务都是这么做的）。
    """
    try:
        return SearchSession(source=source).search(keyword, page)
    except Exception as exc:  # 一次性入口自行转失败形态，方便脚本/插件直接调用
        return error_payload(exc)
