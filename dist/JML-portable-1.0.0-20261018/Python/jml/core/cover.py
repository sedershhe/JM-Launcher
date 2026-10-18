"""封面字节（旧缺陷 #7 的落地：工作线程只产出 bytes，不解码成 UI 对象）。

- ``fetch_cover(book_id)`` 返回封面**原图字节**；
- 任何失败（编号为空、网络异常、图片为空）一律返回 ``None``，不抛异常，
  由 C# 侧显示暗红底 ``X``（计划书 11.6 的封面失败行为）；
- 本模块不做图像解码、不创建任何 WPF/PIL 对象，避免跨线程脆弱性。
"""

from __future__ import annotations

from jml.core import diagnostics, jm_source


def fetch_cover(book_id: object, source=None) -> bytes | None:
    """取封面原图字节；失败返回 ``None``（并写一条 COVER_FETCH_FAILED 诊断）。

    参数:
        book_id: 本子编号（空值直接返回 None）。
        source: 可选数据源（测试注入）；缺省用 ``jm_source.get_source()``。
    """
    identity = str(book_id).strip() if book_id is not None else ""
    if not identity:
        diagnostics.record("COVER_FETCH_FAILED", "封面请求缺少本子编号")
        return None

    try:
        active_source = source or jm_source.get_source()
        data = active_source.cover_bytes(identity)
    except Exception as exc:
        # 封面失败不打断主流程，也不把异常抛给 C#；但要留诊断
        diagnostics.record(
            "COVER_FETCH_FAILED",
            f"封面获取失败: book_id={identity}",
            exc,
        )
        return None

    if not data:
        diagnostics.record(
            "COVER_FETCH_FAILED",
            f"封面内容为空: book_id={identity}",
        )
        return None
    return bytes(data)
