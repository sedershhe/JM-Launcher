"""错误分类与文案脱敏（对应旧缺陷 #2：#错误按异常类型判定，不按消息关键词猜）。

对外只提供四类能力：

1. ``AppError`` 家族：业务层自己抛出的可预期错误（带 error_code）；
2. ``classify(exc)``：按**异常类型**映射成 ``(error_code, error_message)``，
   映射流程里不读取异常消息内容做任何关键词判断；
3. ``error_payload(exc)``：桥接层返回的失败形态
   ``{"ok": False, "error_code": ..., "error_message": ...}``；
4. ``sanitize_text(text)``：日志/错误消息脱敏，覆盖
   password / passwd / pwd / token / cookie / authorization / username /
   account / secret / api_key，以及 ``Bearer xxx``、``Basic xxx``。

本模块不访问网络、不读写磁盘，可在任意线程调用。
"""

from __future__ import annotations

import json
import re
import zipfile
from typing import NamedTuple

# ---------------------------------------------------------------------------
# 错误码（C# 侧只认这些字符串，不解析中文文案）
# ---------------------------------------------------------------------------

ERROR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_NETWORK = "NETWORK_ERROR"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_RESPONSE_INVALID = "RESPONSE_INVALID"
ERROR_DOWNLOAD_PARTIAL = "DOWNLOAD_PARTIAL"
ERROR_JM = "JM_ERROR"
ERROR_PERMISSION_DENIED = "PERMISSION_DENIED"
ERROR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERROR_FILE_EXISTS = "FILE_EXISTS"
ERROR_ARCHIVE_INVALID = "ARCHIVE_INVALID"
ERROR_ARCHIVE_CLOSED = "ARCHIVE_CLOSED"
ERROR_PAGE_OUT_OF_RANGE = "PAGE_OUT_OF_RANGE"
ERROR_IMAGE_INVALID = "IMAGE_INVALID"
ERROR_INVALID_DATA = "INVALID_DATA"
ERROR_IO = "IO_ERROR"
ERROR_OUT_OF_MEMORY = "OUT_OF_MEMORY"
ERROR_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
ERROR_NOT_AVAILABLE = "NOT_AVAILABLE"
ERROR_CANCELLED = "CANCELLED"
ERROR_BATCH_NOT_FOUND = "BATCH_NOT_FOUND"
ERROR_INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
ERROR_NOT_LOGGED_IN = "NOT_LOGGED_IN"
ERROR_UNKNOWN = "UNKNOWN_ERROR"

#: 错误码 → 中文基础文案（界面展示用；详细原因另附在冒号之后）
ERROR_MESSAGES: dict[str, str] = {
    ERROR_INVALID_ARGUMENT: "参数不合法",
    ERROR_NOT_FOUND: "本子不存在或已下架",
    ERROR_NETWORK: "网络请求失败",
    ERROR_TIMEOUT: "网络请求超时",
    ERROR_RESPONSE_INVALID: "服务返回内容异常",
    ERROR_DOWNLOAD_PARTIAL: "部分图片下载失败",
    ERROR_JM: "禁漫接口异常",
    ERROR_PERMISSION_DENIED: "没有文件访问权限",
    ERROR_FILE_NOT_FOUND: "文件或目录不存在",
    ERROR_FILE_EXISTS: "同名文件已存在",
    ERROR_ARCHIVE_INVALID: "不是有效的 ZIP 压缩包",
    ERROR_ARCHIVE_CLOSED: "压缩包已关闭",
    ERROR_PAGE_OUT_OF_RANGE: "页码超出范围",
    ERROR_IMAGE_INVALID: "图片无法解码或像素无效",
    ERROR_INVALID_DATA: "数据格式不合法",
    ERROR_IO: "文件读写失败",
    ERROR_OUT_OF_MEMORY: "内存不足",
    ERROR_NOT_IMPLEMENTED: "功能尚未实现",
    ERROR_NOT_AVAILABLE: "功能当前不可用",
    ERROR_CANCELLED: "已取消",
    ERROR_BATCH_NOT_FOUND: "找不到该下载批次",
    ERROR_INVALID_CREDENTIALS: "账号或密码不正确",
    ERROR_NOT_LOGGED_IN: "尚未登录",
    ERROR_UNKNOWN: "未知错误",
}


class ErrorInfo(NamedTuple):
    """一次错误分类的结果。"""

    error_code: str
    error_message: str


# ---------------------------------------------------------------------------
# 业务异常家族（业务层主动抛出，classify 直接采信其 error_code）
# ---------------------------------------------------------------------------


class AppError(Exception):
    """业务层可预期错误的基类。"""

    error_code = ERROR_UNKNOWN

    def __init__(self, message: str = "") -> None:
        self.detail = sanitize_text(message)
        super().__init__(self.detail or ERROR_MESSAGES[self.error_code])


class InvalidArgumentError(AppError, ValueError):
    """参数不合法（空关键词、空编号列表等）。"""

    error_code = ERROR_INVALID_ARGUMENT


class NotFoundError(AppError):
    """目标不存在（本子、批次等）。"""

    error_code = ERROR_NOT_FOUND


class ArchiveError(AppError):
    """打包/校验失败。"""

    error_code = ERROR_ARCHIVE_INVALID


class ArchiveNotFoundError(AppError):
    """压缩包文件不存在（ZIP 浏览插件：选中的 ZIP 被删除/移动）。"""

    error_code = ERROR_FILE_NOT_FOUND


class ArchiveClosedError(AppError):
    """压缩包句柄已关闭（读图前必须先 ``list_images`` 打开）。

    ZIP 浏览插件用它区分"包不存在/无效"与"句柄被关闭"：关闭后必须由调用方
    重新打开才能取图，不静默重开（否则跨插件复用会掩盖调用方的状态错误）。
    """

    error_code = ERROR_ARCHIVE_CLOSED


class PageOutOfRangeError(AppError):
    """图片页码超出当前压缩包范围（0 起，含负数与非法值）。"""

    error_code = ERROR_PAGE_OUT_OF_RANGE


class InvalidDataError(AppError):
    """数据/协议形状不合法（如插件页面钩子返回的不是 dict）。"""

    error_code = ERROR_INVALID_DATA


class ImageError(AppError):
    """图片内容或像素无效。"""

    error_code = ERROR_IMAGE_INVALID


class CancelledError(AppError):
    """任务被取消（下载检查点轮询到取消标志）。"""

    error_code = ERROR_CANCELLED


class BatchNotFoundError(AppError):
    """找不到下载批次。"""

    error_code = ERROR_BATCH_NOT_FOUND


class InvalidCredentialsError(AppError):
    """账号或密码不正确（由数据源按**异常类型**给出，不靠文案判断）。"""

    error_code = ERROR_INVALID_CREDENTIALS


class NotLoggedInError(AppError):
    """尚未登录，且内存中没有可用于自动登录的凭据。"""

    error_code = ERROR_NOT_LOGGED_IN


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

#: 需要打码的敏感键（大小写不敏感）
_SENSITIVE_KEY_PATTERN = (
    r"password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|"
    r"cookie|authorization|username|user[_-]?name|account|secret|api[_-]?key"
)

#: key=value / key: value 形态；值可以是带引号串或到空白/分隔符为止
_KV_PATTERN = re.compile(
    rf"(?i)(\b(?:{_SENSITIVE_KEY_PATTERN})\b\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&}\]\)]+)"
)

#: 整行吞掉的请求头：Cookie / Authorization 的值本身可能含空格/分号/多段
_HEADER_VALUE_PATTERN = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization)\b"
    r"(\s*[:=]\s*)[^\r\n]+"
)

#: HTTP 认证头里的 Scheme + 令牌
_AUTH_SCHEME_PATTERN = re.compile(
    r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=\-]{4,})"
)


def sanitize_text(text: object, max_length: int = 800) -> str:
    """把一段日志/错误文本里的凭据打码，并限制最大长度。

    规则（与 C# 侧 ``Infrastructure/LogSanitizer.cs`` 覆盖范围一致）：
    - ``password/passwd/pwd/token/cookie/authorization/username/account/
      secret/api_key`` 等键的取值一律替换为 ``***``；
    - Cookie / Authorization 请求头整行取值打码；
    - ``Bearer xxx`` / ``Basic xxx`` 的令牌替换为 ``***``；
    - 超过 ``max_length`` 的部分截断并补省略号（防御超长响应体刷屏）。

    本函数是纯字符串处理，不做任何网络或文件操作。
    """
    if text is None:
        return ""

    value = str(text)
    value = _HEADER_VALUE_PATTERN.sub(
        lambda match: match.group(1) + match.group(2) + "***", value
    )
    value = _KV_PATTERN.sub(lambda match: match.group(1) + "***", value)
    value = _AUTH_SCHEME_PATTERN.sub(lambda match: match.group(1) + match.group(2) + "***", value)
    if max_length > 0 and len(value) > max_length:
        value = value[:max_length] + "…"
    return value


# ---------------------------------------------------------------------------
# 按异常类型分类
# ---------------------------------------------------------------------------

#: 类型 → 错误码规则（惰性构建一次，保持优先级：子类在前）
_TYPE_RULES: list[tuple[type, str]] | None = None


def _build_type_rules() -> list[tuple[type, str]]:
    """构建"异常类型 → 错误码"规则表（只依赖 isinstance，不读消息）。

    jmcomic / PIL 都是可选依赖：缺失时跳过对应规则，其余规则照常工作。
    """
    rules: list[tuple[type, str]] = []

    # jmcomic 的异常体系（规则表由 jm_source 提供：它是唯一 import jmcomic
    # 的模块；这里只看类型，不做任何消息关键词匹配）
    try:
        from jml.core import jm_source

        rules.extend(jm_source.exception_rules())
    except Exception:  # pragma: no cover - 依环境而定
        pass

    # Pillow：无法识别的图片
    try:
        from PIL import UnidentifiedImageError

        rules.append((UnidentifiedImageError, ERROR_IMAGE_INVALID))
    except Exception:  # pragma: no cover - 依环境而定
        pass

    rules.extend(
        [
            (zipfile.BadZipFile, ERROR_ARCHIVE_INVALID),
            (json.JSONDecodeError, ERROR_INVALID_DATA),
            # 内建异常：子类在前，父类兜底
            (TimeoutError, ERROR_TIMEOUT),
            (ConnectionError, ERROR_NETWORK),
            (PermissionError, ERROR_PERMISSION_DENIED),
            (FileNotFoundError, ERROR_FILE_NOT_FOUND),
            (FileExistsError, ERROR_FILE_EXISTS),
            (OSError, ERROR_IO),
            (MemoryError, ERROR_OUT_OF_MEMORY),
            (NotImplementedError, ERROR_NOT_IMPLEMENTED),
            (ValueError, ERROR_INVALID_DATA),
            (TypeError, ERROR_INVALID_DATA),
            (KeyError, ERROR_INVALID_DATA),
            (IndexError, ERROR_INVALID_DATA),
        ]
    )
    return rules


def _type_rules() -> list[tuple[type, str]]:
    global _TYPE_RULES
    if _TYPE_RULES is None:
        _TYPE_RULES = _build_type_rules()
    return _TYPE_RULES


def classify(exc: BaseException) -> ErrorInfo:
    """按异常**类型**把异常映射成 ``(error_code, error_message)``。

    - ``AppError`` 家族直接采信自身的 error_code 与消息；
    - 其余异常只做 ``isinstance`` 判断，绝不读取消息里的关键词；
    - 未命中任何类型 → ``UNKNOWN_ERROR``。
    """
    if isinstance(exc, AppError):
        message = sanitize_text(exc.detail or "") or ERROR_MESSAGES[exc.error_code]
        return ErrorInfo(exc.error_code, message)

    error_code = ERROR_UNKNOWN
    for exc_type, code in _type_rules():
        if isinstance(exc, exc_type):
            error_code = code
            break

    base = ERROR_MESSAGES[error_code]
    detail = sanitize_text(str(exc)).strip()
    message = f"{base}: {detail}" if detail else base
    return ErrorInfo(error_code, message)


def error_payload(exc: BaseException) -> dict:
    """把异常转成桥接层统一的失败形态。"""
    info = classify(exc)
    return {
        "ok": False,
        "error_code": info.error_code,
        "error_message": info.error_message,
    }


def make_error(error_code: str, error_message: str = "") -> dict:
    """构造一个失败形态（不抛异常时使用）。"""
    base = ERROR_MESSAGES.get(error_code, ERROR_MESSAGES[ERROR_UNKNOWN])
    message = sanitize_text(error_message).strip() or base
    return {"ok": False, "error_code": error_code, "error_message": message}
