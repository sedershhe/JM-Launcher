"""ZIP 打包与产物校验（功能计划书 4.1 的"质量保证"、技术方案第七节锚点）。

打包规则（逐字锁定）：

- ZIP 内条目名 ``001.ext``、``002.ext`` …（三位零填充，``f"{i:03d}"``）；
- 后缀统一取**首张图片**的扩展名；扩展名缺失时回退 ``.jpg``；
- 图片按传入顺序写入，不压缩（图片本身已是压缩格式，ZIP_STORED 省 CPU）。

校验规则（任一不过关 → 该本失败并给出原因，由下载层删除半成品 ZIP）：

1. ZIP 能正常打开；
2. 条目数 == 图片数（expected_count）；
3. 无零字节条目；
4. 每个条目都能被 Pillow 解码；
5. 每张图像素宽高都 > 0。

本模块不联网、不感知 UI，只做本地文件与字节处理。
"""

from __future__ import annotations

import errno
import io
import os
import uuid
import zipfile
from typing import NamedTuple

from jml.core.errors import (
    ERROR_ARCHIVE_INVALID,
    ERROR_IMAGE_INVALID,
    ERROR_PERMISSION_DENIED,
    AppError,
    ArchiveError,
)

# 注意：Pillow **不在模块顶层 import**。`import jml_backend` 只应加载核心
# 模块；Pillow 缺失时 health_check 仍要能结构化上报，而不是启动即 ImportError。

#: ZIP 内条目编号位数（``001``）
ENTRY_DIGITS = 3

#: 图片扩展名缺失时的回退后缀
DEFAULT_SUFFIX = ".jpg"

#: 校验结果的细分原因（error_code 给 C# 用，reason 给日志/测试用）
REASON_UNOPENABLE = "ARCHIVE_UNOPENABLE"
REASON_COUNT_MISMATCH = "ARCHIVE_ENTRY_COUNT"
REASON_EMPTY_ENTRY = "IMAGE_EMPTY"
REASON_UNDECODABLE = "IMAGE_UNDECODABLE"
REASON_INVALID_SIZE = "IMAGE_INVALID_SIZE"

#: 目录可写探针文件（与宿主 ``jml.host.paths`` / C# ``AppPaths`` 同一口径）
WRITE_PROBE_PREFIX = ".jml-write-probe-"
WRITE_PROBE_SUFFIX = ".tmp"

#: 判定"没权限/被占用"的 Windows 错误码：5=拒绝访问、32/33=共享冲突
_PERMISSION_WINERRORS = (5, 32, 33)


# ---------------------------------------------------------------------------
# 目录可写探针与写失败诊断（下载目录预检、ZIP 写入失败共用）
# ---------------------------------------------------------------------------


def _exception_summary(exc: BaseException) -> str:
    """把异常压成 ``类型: errno=n[, winerror=n]``（不拼原消息，避免泄露）。"""
    errno_value = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    text = f"{type(exc).__name__}: errno={errno_value}"
    if winerror is not None:
        text += f", winerror={winerror}"
    return text


def is_write_permission_failure(exc: BaseException) -> bool:
    """判断是否"没权限/被占用"型写入失败（其它 ``OSError`` 不改错误码语义）。"""
    if isinstance(exc, PermissionError):
        return True
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return True
    return getattr(exc, "winerror", None) in _PERMISSION_WINERRORS


def write_probe(directory: str) -> None:
    """实测目录可写：创建唯一探针文件 → 写入 → 删除。

    探针文件名固定为 ``.jml-write-probe-<rand>.tmp``；成功/失败都尽力删除，
    不允许在下载目录留下任何残留。

    异常:
        OSError —— 建文件/写入/删除任一步失败（原始异常直接上抛，
                   由调用方决定错误码与文案）。
    """
    probe = os.path.join(
        os.fspath(directory),
        f"{WRITE_PROBE_PREFIX}{uuid.uuid4().hex}{WRITE_PROBE_SUFFIX}",
    )
    try:
        with open(probe, "x", encoding="utf-8") as handle:
            handle.write("J")
            handle.flush()
        os.unlink(probe)
    except OSError:
        try:
            os.unlink(probe)
        except OSError:
            pass
        raise


def describe_target_unwritable(directory: str, exc: BaseException) -> str:
    """生成"下载目录不可写"的可行动文案（绝对路径 + 异常类型 + errno/winerror）。"""
    absolute = os.path.abspath(os.fspath(directory))
    return (
        f"下载目录不可写：{absolute}（{_exception_summary(exc)}）；"
        "请换一个目录或检查安全软件"
    )


def _same_name_file_status(path: str) -> str:
    """判断同名文件是否存在、是否能以读写方式打开（被占用则打不开）。"""
    if not os.path.exists(path):
        return "同名文件不存在（未被占用）"
    try:
        handle = os.open(path, os.O_RDWR)
    except OSError:
        return "同名文件可能被占用（请关闭占用它的程序后重试）"
    try:
        os.close(handle)
    except OSError:
        pass
    return "同名文件可打开（未被占用）"


def describe_write_failure(path: str, exc: BaseException) -> str:
    """生成写产物失败的可诊断文案。

    内容固定包含：绝对路径、原始异常类型与 errno/winerror、**再探一次**目标
    目录是否可写、同名文件是否已存在且被占用（``os.open(O_RDWR)`` 打不开
    即提示"同名文件可能被占用"）。
    """
    absolute = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(absolute) or os.getcwd()

    parts = [f"写产物失败：{absolute}（{_exception_summary(exc)}）"]
    try:
        write_probe(directory)
    except OSError as probe_exc:
        parts.append(f"目标目录不可写（{_exception_summary(probe_exc)}）")
    else:
        parts.append("目标目录可写")
    parts.append(_same_name_file_status(absolute))
    return "；".join(parts)


class ArtifactWriteError(AppError, PermissionError):
    """写 ZIP/占位文件失败（错误码仍为 ``PERMISSION_DENIED``）。

    消息由 :func:`describe_write_failure` 生成，带绝对路径与占用判断；下载层
    另按 ``path`` / ``errno`` / ``winerror`` 补一条 error 级 ``download.log``。
    同时继承 ``PermissionError``：调用方按 ``OSError`` 兜底也能捕获。
    """

    error_code = ERROR_PERMISSION_DENIED

    def __init__(self, path: str, exc: BaseException) -> None:
        absolute = os.path.abspath(os.fspath(path))
        try:
            message = describe_write_failure(absolute, exc)
        except Exception:  # 诊断生成绝不能反过来掩盖原始写入失败
            message = f"写产物失败：{absolute}（{_exception_summary(exc)}）"
        # 先走父类构造（OSError 会重置 errno/winerror），再补回结构化字段
        super().__init__(message)
        self.path = absolute
        self.errno = getattr(exc, "errno", None)
        self.winerror = getattr(exc, "winerror", None)
        self.cause_type = type(exc).__name__


class ValidationResult(NamedTuple):
    """ZIP 校验结果。"""

    ok: bool
    error_code: str
    error_message: str
    reason: str


def entry_name(index: int, suffix: str, total: int | None = None) -> str:
    """生成 ZIP 条目名：三位起零填充 + 后缀。

    ``total`` 给出总页数时按 ``max(3, len(str(total)))`` 取宽度：
    1000 页时是 ``0001.png``…``1000.png``；不传 ``total`` 时按序号自身宽度。
    """
    if index < 1:
        raise ValueError(f"ZIP 条目序号必须从 1 开始，实际为 {index}")
    width = ENTRY_DIGITS
    if total is not None and total > 0:
        width = max(ENTRY_DIGITS, len(str(total)))
    return f"{index:0{width}d}{suffix or DEFAULT_SUFFIX}"


def suffix_from_images(image_paths: list[str]) -> str:
    """取首张图片的扩展名；列表为空或无扩展名时回退 ``.jpg``。"""
    if not image_paths:
        return DEFAULT_SUFFIX
    suffix = os.path.splitext(os.fspath(image_paths[0]))[1]
    return suffix or DEFAULT_SUFFIX


def pack_zip(image_paths: list[str], zip_path: str) -> int:
    """把图片按顺序写入 ZIP，返回条目数。

    异常:
        ArchiveError      —— 图片列表为空（没有可打包的内容）
        FileNotFoundError —— 某张图片文件不存在
        OSError           —— ZIP 写入失败（打包失败时删除半成品文件）
    """
    paths = [os.fspath(path) for path in image_paths]
    if not paths:
        raise ArchiveError("没有可打包的图片")

    suffix = suffix_from_images(paths)
    total = len(paths)
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for index, source in enumerate(paths, start=1):
                archive.write(source, arcname=entry_name(index, suffix, total))
    except BaseException as exc:
        # 先按"失败现场"生成可诊断文案（半成品可能正被 AV 锁定），再清半成品。
        write_error = (
            ArtifactWriteError(zip_path, exc)
            if is_write_permission_failure(exc)
            else None
        )
        # 半成品 ZIP 不留在下载目录（调用方已提前占位，这里负责清掉）
        try:
            os.remove(zip_path)
        except OSError as cleanup_exc:
            from jml.core import diagnostics

            diagnostics.record(
                "ARCHIVE_CLEANUP_FAILED", f"清理半成品 ZIP 失败: {zip_path}", cleanup_exc
            )
        if write_error is not None:
            raise write_error from exc
        raise
    return len(paths)


def validate_zip(zip_path: str, expected_count: int) -> ValidationResult:
    """校验 ZIP 产物，返回 ``ValidationResult``（不抛异常）。"""
    try:
        expected = int(expected_count)
    except (TypeError, ValueError):
        expected = -1

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        return ValidationResult(
            False,
            ERROR_ARCHIVE_INVALID,
            f"不是有效的 ZIP 压缩包: {exc}",
            REASON_UNOPENABLE,
        )

    with archive:
        infos = archive.infolist()
        if len(infos) != expected:
            return ValidationResult(
                False,
                ERROR_ARCHIVE_INVALID,
                f"压缩包条目数 {len(infos)} 与图片数 {expected} 不一致",
                REASON_COUNT_MISMATCH,
            )

        try:
            from PIL import Image, UnidentifiedImageError
        except ImportError as exc:
            return ValidationResult(
                False,
                ERROR_IMAGE_INVALID,
                f"图片校验依赖 Pillow 不可用: {exc}",
                REASON_UNDECODABLE,
            )

        for info in infos:
            if info.file_size <= 0:
                return ValidationResult(
                    False,
                    ERROR_IMAGE_INVALID,
                    f"压缩包条目 {info.filename} 为空（0 字节）",
                    REASON_EMPTY_ENTRY,
                )
            try:
                data = archive.read(info)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                return ValidationResult(
                    False,
                    ERROR_ARCHIVE_INVALID,
                    f"压缩包条目 {info.filename} 无法读取: {exc}",
                    REASON_UNOPENABLE,
                )
            if not data:
                return ValidationResult(
                    False,
                    ERROR_IMAGE_INVALID,
                    f"压缩包条目 {info.filename} 为空（0 字节）",
                    REASON_EMPTY_ENTRY,
                )

            try:
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    width, height = image.size
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                return ValidationResult(
                    False,
                    ERROR_IMAGE_INVALID,
                    f"压缩包条目 {info.filename} 无法解码: {type(exc).__name__}",
                    REASON_UNDECODABLE,
                )
            if width <= 0 or height <= 0:
                return ValidationResult(
                    False,
                    ERROR_IMAGE_INVALID,
                    f"压缩包条目 {info.filename} 像素尺寸无效: {width}x{height}",
                    REASON_INVALID_SIZE,
                )

    return ValidationResult(True, "", "", "")


def pack_and_validate(image_paths: list[str], zip_path: str) -> tuple[int, ValidationResult]:
    """打包 + 校验一次完成，返回 ``(条目数, 校验结果)``。"""
    count = pack_zip(image_paths, zip_path)
    return count, validate_zip(zip_path, count)
