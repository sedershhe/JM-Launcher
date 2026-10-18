"""诊断环形表单元测试（阶段 2 硬化）。

覆盖：内存有界、脱敏、可选落盘、堆栈格式化、异常不向调用方抛出。
"""

from __future__ import annotations

import os

import pytest

from jml.core import diagnostics


@pytest.fixture(autouse=True)
def _reset_diagnostics():
    diagnostics.set_log_dir(None)
    diagnostics.clear()
    yield
    diagnostics.set_log_dir(None)
    diagnostics.clear()


def test_record_and_recent_are_bounded(monkeypatch):
    monkeypatch.setattr(diagnostics, "CAPACITY", 3)
    # deque 的 maxlen 在创建时固定，这里直接重建内部 deque 以模拟容量
    from collections import deque

    monkeypatch.setattr(diagnostics, "_RECORDS", deque(maxlen=3))

    for index in range(5):
        diagnostics.record("TEST", f"message {index}")

    recent = diagnostics.recent()
    assert len(recent) == 3
    assert "message 2" in recent[0]
    assert "message 4" in recent[-1]


def test_record_sanitizes_credentials():
    diagnostics.record("TEST", "password=SUPER-SECRET token=abc123")

    text = diagnostics.recent()[-1]
    assert "SUPER-SECRET" not in text
    assert "abc123" not in text
    assert "***" in text


def test_record_with_exception_contains_sanitized_stack():
    def boom():
        raise RuntimeError("token=SECRET-IN-STACK")

    try:
        boom()
    except RuntimeError as exc:
        diagnostics.record("TEST", "失败", exc)

    text = diagnostics.recent()[-1]
    assert "Traceback" in text
    assert "SECRET-IN-STACK" not in text
    assert "***" in text


def test_recent_limit_and_clear():
    diagnostics.record("TEST", "a")
    diagnostics.record("TEST", "b")
    assert len(diagnostics.recent(1)) == 1
    assert diagnostics.recent(0) == []
    diagnostics.clear()
    assert diagnostics.recent() == []


def test_set_log_dir_writes_sanitized_file(tmp_path):
    diagnostics.set_log_dir(str(tmp_path))
    assert diagnostics.current_log_dir() == str(tmp_path)

    diagnostics.record("TEST", "password=FILE-SECRET")

    path = os.path.join(str(tmp_path), diagnostics.LOG_FILENAME)
    assert os.path.isfile(path)
    content = open(path, encoding="utf-8").read()
    assert "FILE-SECRET" not in content
    assert "***" in content


def test_set_log_dir_invalid_path_does_not_raise(tmp_path):
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")

    diagnostics.set_log_dir(str(file_path))

    assert diagnostics.current_log_dir() is None
    assert any("DIAGNOSTICS_LOG_DIR_FAILED" in item for item in diagnostics.recent())


def test_format_except_handles_none_traceback():
    text = diagnostics.format_except(RuntimeError("password=abc"))
    assert "password=***" in text or "abc" not in text
