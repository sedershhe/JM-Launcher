"""错误分类与脱敏单元测试（旧缺陷 #2：按异常类型，不按消息关键词）。

覆盖：jmcomic 异常体系、内建异常、业务异常、兜底、error_payload 形态、
日志脱敏（password/passwd/token/cookie/authorization/username/Bearer）。
"""

from __future__ import annotations

import json
import zipfile

import jmcomic
import pytest
from PIL import UnidentifiedImageError

from jml.core import errors
from jml.core.errors import (
    AppError,
    ArchiveError,
    CancelledError,
    ImageError,
    InvalidArgumentError,
    NotFoundError,
)


def _jm(exc_type, message="测试异常", context=None):
    return exc_type(message, context or {})


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_jm(jmcomic.MissingAlbumPhotoException), errors.ERROR_NOT_FOUND),
        (_jm(jmcomic.RequestRetryAllFailException), errors.ERROR_NETWORK),
        (_jm(jmcomic.JsonResolveFailException), errors.ERROR_RESPONSE_INVALID),
        (_jm(jmcomic.ResponseUnexpectedException), errors.ERROR_RESPONSE_INVALID),
        (_jm(jmcomic.PartialDownloadFailedException), errors.ERROR_DOWNLOAD_PARTIAL),
        (_jm(jmcomic.JmcomicException), errors.ERROR_JM),
        (TimeoutError("timed out"), errors.ERROR_TIMEOUT),
        (ConnectionError("refused"), errors.ERROR_NETWORK),
        (PermissionError("denied"), errors.ERROR_PERMISSION_DENIED),
        (FileNotFoundError("missing"), errors.ERROR_FILE_NOT_FOUND),
        (FileExistsError("exists"), errors.ERROR_FILE_EXISTS),
        (OSError("io"), errors.ERROR_IO),
        (zipfile.BadZipFile("bad"), errors.ERROR_ARCHIVE_INVALID),
        (UnidentifiedImageError("bad image"), errors.ERROR_IMAGE_INVALID),
        (json.JSONDecodeError("bad json", "{", 0), errors.ERROR_INVALID_DATA),
        (ValueError("bad value"), errors.ERROR_INVALID_DATA),
        (TypeError("bad type"), errors.ERROR_INVALID_DATA),
        (KeyError("missing key"), errors.ERROR_INVALID_DATA),
        (IndexError("out of range"), errors.ERROR_INVALID_DATA),
        (MemoryError(), errors.ERROR_OUT_OF_MEMORY),
        (NotImplementedError("todo"), errors.ERROR_NOT_IMPLEMENTED),
        (RuntimeError("unexpected"), errors.ERROR_UNKNOWN),
    ],
    ids=[
        "missing_album", "retry_all_fail", "json_resolve", "response_unexpected",
        "partial_download", "jmcomic_base", "timeout", "connection", "permission",
        "file_not_found", "file_exists", "os_error", "bad_zip", "unidentified_image",
        "json_decode", "value", "type", "key", "index", "memory", "not_implemented",
        "unknown",
    ],
)
def test_classify_by_exception_type(exc, expected):
    """按类型映射到错误码（与异常消息内容无关）。"""
    assert errors.classify(exc).error_code == expected


def test_classify_business_errors():
    """业务异常家族直接采信自身 error_code。"""
    assert errors.classify(InvalidArgumentError("x")).error_code == errors.ERROR_INVALID_ARGUMENT
    assert errors.classify(NotFoundError("x")).error_code == errors.ERROR_NOT_FOUND
    assert errors.classify(ArchiveError("x")).error_code == errors.ERROR_ARCHIVE_INVALID
    assert errors.classify(ImageError("x")).error_code == errors.ERROR_IMAGE_INVALID
    assert errors.classify(CancelledError("x")).error_code == errors.ERROR_CANCELLED


def test_classify_does_not_match_message_keywords():
    """旧缺陷 #2 反例：消息里含 name/DNS 也不能把"本子不存在"判成网络错误。"""
    exc = jmcomic.MissingAlbumPhotoException("DNS 解析失败 name 超时", {})
    assert errors.classify(exc).error_code == errors.ERROR_NOT_FOUND

    plain = RuntimeError("name DNS 失败")
    assert errors.classify(plain).error_code == errors.ERROR_UNKNOWN


def test_classify_message_is_chinese_and_sanitized():
    exc = InvalidArgumentError("token=SUPER_SECRET 不能为空")
    info = errors.classify(exc)
    assert "SUPER_SECRET" not in info.error_message
    assert "***" in info.error_message
    assert info.error_message


def test_error_payload_shape():
    payload = errors.error_payload(FileNotFoundError("x"))
    assert payload["ok"] is False
    assert payload["error_code"] == errors.ERROR_FILE_NOT_FOUND
    assert isinstance(payload["error_message"], str) and payload["error_message"]


def test_make_error():
    payload = errors.make_error(errors.ERROR_BATCH_NOT_FOUND)
    assert payload["ok"] is False
    assert payload["error_code"] == errors.ERROR_BATCH_NOT_FOUND
    assert payload["error_message"]


def test_make_error_sanitizes_detail():
    payload = errors.make_error(errors.ERROR_INVALID_ARGUMENT, "password=abc123")
    assert "abc123" not in payload["error_message"]
    assert "***" in payload["error_message"]


def test_app_error_has_default_chinese_message():
    assert str(AppError()) == errors.ERROR_MESSAGES[errors.ERROR_UNKNOWN]


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("password=abc123", "abc123"),
        ("passwd: abc123", "abc123"),
        ("pwd=abc123", "abc123"),
        ("token=abc123", "abc123"),
        ("username=alice password=bob", "alice"),
        ("Cookie: sessionid=abc123; theme=dark", "abc123"),
        ("Authorization: Bearer SECRET-TOKEN-123", "SECRET-TOKEN-123"),
        ("Authorization: Basic YWxpY2U6Ym9i", "YWxpY2U6Ym9i"),
        ("account=alice", "alice"),
        ("api_key=abc123", "abc123"),
        ("secret=abc123", "abc123"),
    ],
)
def test_sanitize_text_masks_credentials(raw, secret):
    cleaned = errors.sanitize_text(raw)
    assert secret not in cleaned
    assert "***" in cleaned


def test_sanitize_text_keeps_normal_text():
    text = "开始下载 JM123456，共 3 页"
    assert errors.sanitize_text(text) == text


def test_sanitize_text_handles_none_and_truncation():
    assert errors.sanitize_text(None) == ""
    cleaned = errors.sanitize_text("x" * 50, max_length=10)
    assert cleaned == "x" * 10 + "…"
