"""诊断记录（有界环形表，默认只在内存）。

用途：所有"不能抛给用户、但必须可排查"的异常都记到这里，而不是 ``except: pass``。

- 默认只在内存保留最近 ``CAPACITY`` 条，不创建任何文件、不硬编码路径；
- ``set_log_dir(path)`` 由 C# 后续传入时才追加落盘到
  ``<path>/diagnostics.log``（不传就完全不碰磁盘）；
- 所有消息与堆栈都经 ``sanitize_text`` 脱敏；
- 线程安全：显式 ``threading.RLock``。
"""

from __future__ import annotations

import os
import threading
import traceback
from collections import deque

from jml.core.errors import sanitize_text

#: 内存中保留的最近诊断条数
CAPACITY = 200

#: 落盘文件名（仅在 set_log_dir 后使用；相对 log_dir，不是绝对路径）
LOG_FILENAME = "diagnostics.log"

_LOCK = threading.RLock()
_RECORDS: deque[str] = deque(maxlen=CAPACITY)
_LOG_DIR: str | None = None


def format_except(exc: BaseException) -> str:
    """把异常（含堆栈）格式化成脱敏文本。"""
    try:
        text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    except Exception:  # 极端情况下 format_exception 自身失败
        text = f"{type(exc).__name__}: {exc}"
    return sanitize_text(text, max_length=4000)


def record(error_code: str, message: object, exc: BaseException | None = None) -> None:
    """记录一条诊断（内存 + 可选落盘），绝不向调用方抛异常。"""
    text = sanitize_text(message)
    if exc is not None:
        stack = format_except(exc)
        text = f"{text}\n{stack}" if text else stack
    line = f"[{error_code}] {text}" if error_code else text

    with _LOCK:
        _RECORDS.append(line)
        log_dir = _LOG_DIR
        if log_dir:
            _append_file(log_dir, line)


def recent(limit: int | None = None) -> list[str]:
    """返回最近若干条诊断副本（最新在末尾）。"""
    with _LOCK:
        items = list(_RECORDS)
    if limit is not None and limit >= 0:
        return items[-limit:] if limit else []
    return items


def clear() -> None:
    """清空内存诊断（测试隔离用）。"""
    with _LOCK:
        _RECORDS.clear()


def set_log_dir(path: str | None) -> None:
    """设置诊断落盘目录；传 ``None``/空串关闭落盘。

    目录由调用方给定（C# 后续传入），本模块不硬编码任何路径。
    设置后立即尝试创建目录；失败只记内存诊断，不抛异常。
    """
    global _LOG_DIR
    if not path:
        with _LOCK:
            _LOG_DIR = None
        return

    directory = os.fspath(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        _append_memory("DIAGNOSTICS_LOG_DIR_FAILED", f"诊断目录不可用: {directory}: {exc}")
        with _LOCK:
            _LOG_DIR = None
        return

    with _LOCK:
        _LOG_DIR = directory


def current_log_dir() -> str | None:
    """返回当前诊断落盘目录（未设置返回 None）。"""
    with _LOCK:
        return _LOG_DIR


def _append_file(log_dir: str, line: str) -> None:
    """在已持锁时追加一行到诊断文件；失败只记内存，不再递归落盘。"""
    path = os.path.join(log_dir, LOG_FILENAME)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
    except OSError as exc:
        _RECORDS.append(
            f"[DIAGNOSTICS_WRITE_FAILED] {sanitize_text(path)}: {sanitize_text(exc)}"
        )


def _append_memory(error_code: str, message: str) -> None:
    with _LOCK:
        _RECORDS.append(f"[{error_code}] {sanitize_text(message)}")
