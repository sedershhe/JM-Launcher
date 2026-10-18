"""下载历史插件（阶段 4 第一个插件）。

功能：把每次下载结束的结果记下来，并在插件页面里按时间倒序分页展示。

数据流::

    download.item_finished  → HistoryStore.on_item_finished
        追加一条业务记录（时间 / 本子号 / 标题 / 成功失败 / 输出路径 / 错误码）
        → 超过 keep_count 裁掉最旧 → **立即落盘**
    download.batch_finished → HistoryStore.on_batch_finished
        兜底再裁一次并打一条汇总日志
    download_history.main   → HistoryStore.page_payload
        读内存记录，按 time 倒序分页返回 list 模板 payload

落盘策略（二选一里选“每条即落”）：下载批次可能很长，中途崩溃或强退不应
丢掉已经完成的记录，因此每收到一条 ``download.item_finished`` 就原子写一次
``history.json``；``download.batch_finished`` 只做兜底裁剪与日志汇总。
``history.json`` 位于宿主提供的 ``ctx.data_dir``（插件不自己拼 APPDATA 路径），
内容只含 :data:`RECORD_FIELDS` 白名单里的业务字段，**不含任何凭据**
（password / token / cookie 等一律不落盘）。

容错：文件损坏（非法 JSON / 结构不对）→ 写诊断日志、清空内存并立即重建为空
文件；读写失败同样只写日志，绝不把异常抛给下载线程或宿主页面入口。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

#: 本插件在 manifest 中声明的页面 id
PAGE_ID = "download_history.main"

#: 历史文件（位于 ctx.data_dir 下）
HISTORY_FILENAME = "history.json"

#: keep_count 读不到时的兜底（schema 默认值同为 200）
DEFAULT_KEEP_COUNT = 200

#: 页面默认每页条数（可用 keep_count 上限截断）
DEFAULT_PAGE_SIZE = 50

#: 空历史文案
EMPTY_TEXT = "暂无下载记录"

#: 落盘白名单：只存这些业务字段，其余（含凭据类字段）一律不写
RECORD_FIELDS = ("time", "book_id", "title", "ok", "zip_path", "error_code")


def _to_text(value: object) -> str:
    """把事件字段转成字符串（None → 空串；数字编号也统一成字符串）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _positive_int(value: object, default: int) -> int:
    """把分页参数转成正整数；非法 / 非正数返回 default（bool 不算数字）。"""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _records_from_raw(raw: object) -> tuple[list[dict], int]:
    """从 JSON 原始值解析记录列表（按时间倒序），返回 (记录, 跳过条数)。

    兼容两种顶层形状：``{"version": 1, "records": [...]}`` 与裸数组。
    顶层形状非法 → 抛 ValueError（由调用方按“文件损坏”重建）。
    单条记录缺 time 或不是对象 → 跳过并计数（不让一条坏记录毁掉全部历史）。
    """
    if isinstance(raw, dict):
        items = raw.get("records")
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("历史文件顶层必须是对象或数组")

    if not isinstance(items, list):
        raise ValueError("历史文件的 records 必须是数组")

    records: list[dict] = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        time_text = item.get("time")
        if not isinstance(time_text, str) or not time_text:
            skipped += 1
            continue
        records.append(
            {
                "time": time_text,
                "book_id": _to_text(item.get("book_id")),
                "title": _to_text(item.get("title")),
                "ok": item.get("ok") is True,
                "zip_path": _to_text(item.get("zip_path")),
                "error_code": _to_text(item.get("error_code")),
            }
        )
    # 文件本身按“新→旧”写；同秒记录靠稳定排序保持写入顺序
    records.sort(key=lambda record: record["time"], reverse=True)
    return records, skipped


def _record_to_row(record: dict) -> dict:
    """把一条历史记录映射成 list 页面的一行（列 key 见 page_payload）。"""
    return {
        "time": record["time"],
        "book_id": record["book_id"],
        "title": record["title"],
        "result": "成功" if record["ok"] else "失败",
        "path": record["zip_path"],
    }


class HistoryStore:
    """下载历史的读写与分页（线程安全；所有公开方法可在任意线程调用）。"""

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._path = os.path.join(ctx.data_dir, HISTORY_FILENAME)
        #: 下载线程写、页面线程读，必须显式加锁（旧缺陷 #10：不靠 GIL）
        self._lock = threading.RLock()
        #: 记录列表，始终“新→旧”排列（下标 0 最新）
        self._records: list[dict] = []
        self._loaded = False
        self._trim_notice_logged = False

    # -- 事件处理 -----------------------------------------------------------

    def on_item_finished(self, payload: dict) -> None:
        """``download.item_finished``：记录一条并立即落盘。"""
        record = self._make_record(payload)
        with self._lock:
            self._ensure_loaded_locked()
            self._records.insert(0, record)
            self._trim_locked()
            self._persist_locked()

    def on_batch_finished(self, payload: dict) -> None:
        """``download.batch_finished``：兜底裁剪 + 汇总日志（记录已逐条落盘）。"""
        with self._lock:
            self._ensure_loaded_locked()
            if self._trim_locked():
                self._persist_locked()
            kept = len(self._records)

        total, success, failed = self._batch_counts(payload)
        self._ctx.log(
            f"批次下载结束：成功 {success} / 共 {total}，失败 {failed}；"
            f"历史当前保留 {kept} 条"
        )

    @staticmethod
    def _batch_counts(payload: dict) -> tuple[int, int, int]:
        """从批次事件里安全取 total/success/failed（异常值一律当 0）。"""

        def _count(key: str) -> int:
            try:
                value = int(payload.get(key, 0))
            except (TypeError, ValueError):
                return 0
            return value if value > 0 else 0

        return _count("total"), _count("success"), _count("failed")

    @staticmethod
    def _make_record(payload: dict) -> dict:
        """按白名单从事件 payload 生成一条业务记录（凭据类字段不会进来）。"""
        return {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "book_id": _to_text(payload.get("book_id")),
            "title": _to_text(payload.get("title")),
            "ok": payload.get("ok") is True,
            "zip_path": _to_text(payload.get("zip_path")),
            "error_code": _to_text(payload.get("error_code")),
        }

    # -- 页面数据 -----------------------------------------------------------

    def keep_count(self) -> int:
        """当前保留条数（schema 校验保证 10~10000；异常值退回默认）。"""
        try:
            value = int(self._ctx.config_get("keep_count"))
        except (TypeError, ValueError):
            return DEFAULT_KEEP_COUNT
        return value if value > 0 else DEFAULT_KEEP_COUNT

    def page_payload(self, params: dict) -> dict:
        """构造 ``list`` 模板 payload（分页参数见契约 §12.2 / §12.3）。"""
        page = _positive_int(params.get("page"), 1)
        keep = self.keep_count()
        # 每页条数受 keep_count 上限截断：界面不可能要求比保留上限更多的行
        page_size = min(_positive_int(params.get("page_size"), DEFAULT_PAGE_SIZE), keep)

        with self._lock:
            self._ensure_loaded_locked()
            if self._trim_locked():
                self._persist_locked()
            records = list(self._records)  # 锁内快照，锁外再切片

        total = len(records)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        rows = [_record_to_row(record) for record in records[start : start + page_size]]

        return {
            "columns": [
                {"key": "time", "title": "时间", "width": 200},
                {"key": "book_id", "title": "本子号", "width": 120},
                {"key": "result", "title": "结果", "width": 80},
                {"key": "path", "title": "输出路径"},
            ],
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "row_actions": [],
            "empty_text": EMPTY_TEXT,
        }

    # -- 内部实现（调用方须持锁） -------------------------------------------

    def _trim_locked(self) -> bool:
        """超过 keep_count 时裁掉最旧的记录；返回是否有裁剪。"""
        keep = self.keep_count()
        excess = len(self._records) - keep
        if excess <= 0:
            return False
        del self._records[keep:]
        if not self._trim_notice_logged:
            # 长批次里每条都会触发裁剪，只提示一次避免日志刷屏
            self._trim_notice_logged = True
            self._ctx.log(f"下载历史超过保留条数 {keep}，已裁掉 {excess} 条最旧记录")
        return True

    def _ensure_loaded_locked(self) -> None:
        """首次使用时读盘；损坏则重建空历史（只写诊断，不抛异常）。"""
        if self._loaded:
            return
        self._loaded = True
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            records, skipped = _records_from_raw(raw)
        except (OSError, ValueError, TypeError) as exc:
            self._rebuild_locked(exc)
            return

        self._records = records
        if skipped:
            self._ctx.log(f"下载历史有 {skipped} 条记录格式不合法，已忽略")
            self._persist_locked()

    def _rebuild_locked(self, exc: BaseException) -> None:
        """损坏恢复：清空内存并立即写回合法空文件。"""
        self._records = []
        self._ctx.log(
            f"下载历史文件损坏，已重建为空历史: {type(exc).__name__}: {exc}"
        )
        self._persist_locked()

    def _persist_locked(self) -> bool:
        """原子写 history.json（临时文件 + os.replace）；失败只写日志。"""
        payload = {"version": 1, "records": list(self._records)}
        directory = os.path.dirname(self._path)
        tmp_path = self._path + ".tmp"
        try:
            os.makedirs(directory, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
            return True
        except OSError as exc:
            # 磁盘满 / 无权限等：下载本身已成功，不能因为写历史失败而中断
            self._ctx.log(f"下载历史写入失败（不影响下载结果）: {exc}")
            return False


def setup(ctx) -> None:
    """插件入口：订阅下载事件 + 注册下载历史页面。"""
    data_dir = getattr(ctx, "data_dir", "")
    if not isinstance(data_dir, str) or not data_dir:
        raise RuntimeError("插件上下文缺少 data_dir（宿主未提供可写数据目录）")

    store = HistoryStore(ctx)
    ctx.subscribe("download.item_finished", store.on_item_finished)
    ctx.subscribe("download.batch_finished", store.on_batch_finished)
    ctx.register_page(PAGE_ID, store.page_payload)
