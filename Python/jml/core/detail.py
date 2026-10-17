"""详情业务（功能计划书 4.3）。

规则：

- 展示封面 / 标题 / 作者 / 页数 / 标签 / 简介；
- 简介超过 **200 字**截断并追加省略号 ``…``（截断在 Python 侧完成，
  C# 只负责显示，不做任何长度判断）；
- ``description_truncated`` 明确告诉 UI 是否发生过截断。

本模块不联网（数据源注入），不感知 UI。
"""

from __future__ import annotations

from jml.core import jm_source
from jml.core.errors import InvalidArgumentError

#: 简介最大字符数（计划书 11.6 逐字锁定：超 200 字截断）
DESCRIPTION_LIMIT = 200

#: 截断省略号（单个字符，不是三个点）
ELLIPSIS = "…"


def truncate_description(text: object, limit: int = DESCRIPTION_LIMIT) -> tuple[str, bool]:
    """按字符数截断简介，返回 ``(展示文本, 是否截断)``。

    - 先去掉首尾空白；
    - ``len(text) > limit`` 时取前 ``limit`` 个字符并追加 ``…``；
    - ``limit <= 0`` 视为不限制（防御性用法，本业务固定 200）。
    """
    value = "" if text is None else str(text).strip()
    if limit <= 0 or len(value) <= limit:
        return value, False
    return value[:limit] + ELLIPSIS, True


def get_detail(book_id: object, source=None) -> dict:
    """取本子详情，返回桥接契约结构。

    返回::

        {"ok": True, "book_id": str, "title": str, "author": str,
         "page_count": int, "tags": [str], "description": str,
         "description_truncated": bool, "cover_url": str}

    异常:
        InvalidArgumentError —— book_id 为空
        其余异常由入口层 ``error_payload`` 转失败形态。
    """
    identity = str(book_id).strip() if book_id is not None else ""
    if not identity:
        raise InvalidArgumentError("本子编号不能为空")

    active_source = source or jm_source.get_source()
    album = active_source.get_album(identity)
    if not isinstance(album, dict):
        raise ValueError(f"详情数据源返回类型非法: {type(album).__name__}")

    tags = album.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    description, truncated = truncate_description(album.get("description"))

    return {
        "ok": True,
        "book_id": str(album.get("book_id") or identity),
        "title": str(album.get("title") or ""),
        "author": str(album.get("author") or ""),
        "page_count": _to_int(album.get("page_count")),
        "tags": [str(tag) for tag in tags],
        "description": description,
        "description_truncated": truncated,
        "cover_url": str(album.get("cover_url") or ""),
    }


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
