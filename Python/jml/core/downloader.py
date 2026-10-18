"""批量下载业务（功能计划书 4.1 + 决策 #1/#2/#6 + 旧缺陷 #6/#7/#10）。

硬性规则（逐条锁定）：

- 并发：批次内 3 线程池 + 模块级全局信号量，**所有批次合计**同时最多 3 本；
- 真取消：批次级 ``threading.Event``，在"每本开始 / 每章开始 / 每图下载前"
  三个检查点轮询；一旦 set，正在跑的本子在下个检查点立即退出，
  **不等当前本子下载完**（决策 #2）；
- 单本失败不影响其它本（异常按类型分类后记入 item_finished，继续跑）；
- 每本一个 ZIP，命名 ``{标题}-JM{编号}.zip``，同名走"副本"命名（决策 #1）；
  先写 ``.part``，校验通过后 ``os.replace``；失败/取消同时删除 ``.part``
  与 0 字节占位文件；
- 批次开始前实测 ``target_dir`` 可写（``.jml-write-probe-<rand>.tmp`` 建/写/删）；
  不可写立即失败且不登记批次，绝不白跑整本才发现权限不足；
- ZIP/占位文件写入被拒时错误码仍 ``PERMISSION_DENIED``，但消息带绝对路径、
  errno/winerror、目录可写复探与"同名文件可能被占用"判断，并补一条 error 级日志；
- 打包后校验 ZIP（可打开 / 条目数 / 可解码 / 像素 / 零字节），
  不过关 → 删除半成品并判该本失败（功能计划书 4.1 质量保证）；
- 事件：item_started / item_finished / log / batch_finished，payload 字段
  与 ``protocol.EVENT_PAYLOAD_SCHEMAS`` 一致（额外带 batch_id）；
  ``download.log`` 统一走全局事件总线，C# sink 按 batch_id 分窗；
- 批次状态先提交（status/result）再发 batch_finished，事件处理器内查询
  立即可见 finished；快照/查询结果一律是副本。

线程模型：``start_batch`` 只登记批次并起一个后台线程后立即返回；
工作线程内部再用 3 线程池跑本子。本模块不感知 UI、不联网（数据源注入）。
"""

from __future__ import annotations

import copy
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from jml.core import archive, book_ids, diagnostics, jm_source, naming
from jml.core.errors import (
    ERROR_CANCELLED,
    ERROR_PERMISSION_DENIED,
    AppError,
    BatchNotFoundError,
    CancelledError,
    ImageError,
    InvalidArgumentError,
    classify,
    sanitize_text,
)
from jml.core.protocol import (
    EVENT_DOWNLOAD_BATCH_FINISHED,
    EVENT_DOWNLOAD_ITEM_FINISHED,
    EVENT_DOWNLOAD_ITEM_STARTED,
    EVENT_DOWNLOAD_LOG,
)
from jml import events

#: 同时下载的本子数（计划书 11.6 逐字锁定：3）
MAX_WORKERS = 3

#: 进程级并发槽：**所有批次合计**最多 MAX_WORKERS 本在跑。
#: 批次内部线程池也是 3，但两个批次并行时不能变成 3×批次数。
_GLOBAL_SLOTS = threading.BoundedSemaphore(MAX_WORKERS)

#: 批次状态常量
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"

#: 日志级别常量
LOG_INFO = "info"
LOG_WARN = "warn"
LOG_ERROR = "error"

#: 批次历史保留条数（按 finished_at 保留最近 N 个）
MAX_BATCH_HISTORY = 50

#: 超期临时工作目录阈值（秒）
STALE_WORK_DIR_SECONDS = 24 * 3600

#: 临时工作目录前缀（位于调用方给定的父目录下）
WORK_DIR_PREFIX = ".jml-tmp-"


# ---------------------------------------------------------------------------
# 内部异常与工具
# ---------------------------------------------------------------------------


class _ValidationFailed(AppError):
    """ZIP 校验未通过：错误码取自校验结果（ARCHIVE_INVALID / IMAGE_INVALID）。"""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class TargetDirectoryUnwritableError(AppError, PermissionError):
    """下载目录实测不可写（fail fast）。

    错误码固定 ``PERMISSION_DENIED``，消息由
    :func:`jml.core.archive.describe_target_unwritable` 生成（绝对路径 +
    异常类型 + errno/winerror + 换目录/检查安全软件提示）；同时是
    ``PermissionError``，方便调用方按 ``OSError`` 兜底。
    """

    error_code = ERROR_PERMISSION_DENIED

    def __init__(self, directory: str, exc: BaseException) -> None:
        absolute = os.path.abspath(os.fspath(directory))
        # 先走父类构造（OSError 会重置 errno/winerror），再补回结构化字段
        super().__init__(archive.describe_target_unwritable(absolute, exc))
        self.directory = absolute
        self.errno = getattr(exc, "errno", None)
        self.winerror = getattr(exc, "winerror", None)


def _remove_quiet(path: str) -> None:
    """删除失败产物（只用于本模块自己创建的半成品/占位文件）。"""
    try:
        os.remove(path)
    except OSError as exc:
        diagnostics.record("CLEANUP_FAILED", f"清理失败产物失败: {path}", exc)


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise CancelledError("下载已取消")


def _ensure_target_dir_writable(directory: str) -> None:
    """批次开始前实测下载目录可写（fail fast，绝不白跑整本才发现写不了）。

    探针由 :func:`jml.core.archive.write_probe` 完成（建唯一临时文件 → 写入 →
    删除，失败也清理）；不可写时抛 ``PermissionError``（错误码
    ``PERMISSION_DENIED``），消息带绝对路径、异常类型与 errno/winerror。
    """
    try:
        archive.write_probe(directory)
    except OSError as exc:
        error = TargetDirectoryUnwritableError(directory, exc)
        diagnostics.record("TARGET_DIR_UNWRITABLE", str(error), exc)
        raise error from exc


def _log_artifact_write_failure(
    batch_id: str, exc: archive.ArtifactWriteError
) -> None:
    """写产物失败时补一条可定位的 error 级下载日志（不含账号等敏感信息）。"""
    errno_value = 0 if exc.errno is None else exc.errno
    winerror_value = 0 if exc.winerror is None else exc.winerror
    _emit_log(
        batch_id,
        LOG_ERROR,
        f"写产物失败：{exc.path}（errno={errno_value}, winerror={winerror_value}）",
        exc.error_code,
    )


def _safe_rmtree(path: str) -> None:
    """删除临时目录；失败写诊断，绝不静默忽略。"""
    try:
        shutil.rmtree(path)
    except OSError as exc:
        diagnostics.record(
            "WORK_DIR_CLEANUP_FAILED",
            f"清理临时工作目录失败: {path}",
            exc,
        )


def _cleanup_stale_artifacts(directory: str) -> None:
    """清理崩溃残留：超期 ``*.zip.part`` 与 0 字节 ``*.zip``（>24h）。

    只处理本模块命名/写入形态的残留，正常 ZIP 与新鲜文件绝不触碰。
    """
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        diagnostics.record(
            "ARTIFACT_SCAN_FAILED", f"扫描下载目录残留失败: {directory}", exc
        )
        return

    now = time.time()
    for entry in entries:
        name = entry.name
        if not (name.endswith(".zip.part") or name.endswith(".zip")):
            continue
        try:
            if not entry.is_file():
                continue
            if now - entry.stat().st_mtime <= STALE_WORK_DIR_SECONDS:
                continue
            if name.endswith(".zip") and entry.stat().st_size != 0:
                continue
        except OSError as exc:
            diagnostics.record(
                "ARTIFACT_STAT_FAILED", f"读取下载残留状态失败: {entry.path}", exc
            )
            continue
        _remove_quiet(entry.path)


def _prepare_work_base(target_dir: str, work_dir: str | None) -> str:
    """确定临时工作目录的父目录（调用方给了就用它，否则用下载目录）。

    **不因父目录不可用而整批失败**：临时目录的降级链在 ``_create_work_dir``
    里按本子执行（首选 ``work_dir`` → 下载目录 → 该本失败）。这里只负责
    尽量创建首选父目录，失败就记诊断后原样返回，交给单本降级处理。

    顺手清理同一父目录下超期的 ``.jml-tmp-*``（>24h），避免崩溃残留堆积。
    """
    try:
        base = os.fspath(work_dir) if work_dir else target_dir
    except TypeError as exc:
        raise InvalidArgumentError(
            f"临时工作目录类型不合法: {type(work_dir).__name__}"
        ) from exc
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as exc:
        diagnostics.record(
            "WORK_DIR_UNAVAILABLE", f"临时目录父目录不可用: {base}", exc
        )
        return base

    try:
        entries = list(os.scandir(base))
    except OSError as exc:
        diagnostics.record("WORK_DIR_SCAN_FAILED", f"扫描临时目录失败: {base}", exc)
        return base

    now = time.time()
    for entry in entries:
        if not entry.name.startswith(WORK_DIR_PREFIX):
            continue
        try:
            if not entry.is_dir() or now - entry.stat().st_mtime <= STALE_WORK_DIR_SECONDS:
                continue
        except OSError as exc:
            diagnostics.record(
                "WORK_DIR_STAT_FAILED", f"读取临时目录状态失败: {entry.path}", exc
            )
            continue
        _safe_rmtree(entry.path)
    return base


def _ensure_work_path(base: str, safe_id: str) -> str:
    """在 ``base`` 下建一个本子的临时工作目录；失败把 ``OSError`` 抛给调用方。"""
    path = os.path.join(base, f"{WORK_DIR_PREFIX}{safe_id}-{uuid.uuid4().hex[:8]}")
    os.makedirs(path, exist_ok=True)
    return path


def _create_work_dir(
    target_dir: str,
    book_id: str,
    work_dir: str | None = None,
    batch_id: str = "",
) -> str:
    """建本子的临时工作目录（图片暂存，打包后清理）。

    降级链（按顺序尝试，成功即止）：

    1. 调用方传入的 ``work_dir``（C# 传 ``%LOCALAPPDATA%\\JML\\tmp``）；
    2. ``<target_dir>\\.jml-tmp-<book>-<rand>``（下载目录内，用户肯定可写）；
    3. 两处都不可用 → 抛 ``PermissionError``（错误码仍为 ``PERMISSION_DENIED``），
       消息写明"两处临时目录都不可用"并带上两处路径。

    降级成功时发一条 warn 级 ``download.log``（经统一脱敏，不含任何凭据）。
    ``work_dir`` 由调用方传入（缺省 = 下载目录），不硬编码任何路径。
    刻意不用 ``tempfile.mkdtemp``：它固定用 0o700 建目录，在受限环境
    会出现"目录建得出来但写不进去"的情况。
    """
    try:
        preferred = os.fspath(work_dir) if work_dir else target_dir
    except TypeError as exc:
        raise InvalidArgumentError(
            f"临时工作目录类型不合法: {type(work_dir).__name__}"
        ) from exc

    safe_id = "".join(
        char if (char.isascii() and (char.isalnum() or char in "_-")) else "_"
        for char in str(book_id)
    )[:32] or "book"

    first_error: OSError | None = None
    try:
        return _ensure_work_path(preferred, safe_id)
    except OSError as exc:
        first_error = exc
        diagnostics.record("WORK_DIR_UNAVAILABLE", f"临时目录不可用: {preferred}", exc)

    assert first_error is not None  # 上面一定走过 except（成功已提前 return）
    fallback = os.fspath(target_dir)
    if os.path.abspath(preferred) != os.path.abspath(fallback):
        try:
            path = _ensure_work_path(fallback, safe_id)
        except OSError as second_error:
            diagnostics.record(
                "WORK_DIR_UNAVAILABLE",
                f"下载目录内临时目录不可用: {fallback}",
                second_error,
            )
            raise PermissionError(
                "两处临时目录都不可用："
                f"首选 {preferred}（{type(first_error).__name__}: {first_error}）；"
                f"降级 {fallback}（{type(second_error).__name__}: {second_error}）"
            ) from second_error
        _emit_log(
            batch_id,
            LOG_WARN,
            f"临时目录不可用，已降级到下载目录：{fallback}"
            f"（原因：{type(first_error).__name__}）",
        )
        return path

    raise PermissionError(
        "两处临时目录都不可用："
        f"首选 {preferred}（{type(first_error).__name__}: {first_error}）；"
        f"降级 {fallback}（{type(first_error).__name__}: {first_error}）"
    ) from first_error


def _emit(event_type: str, payload: dict) -> None:
    """派发事件；事件层异常不得中断下载主流程，但必须写诊断。"""
    try:
        events.BUS.dispatch(event_type, payload)
    except Exception as exc:
        diagnostics.record(
            "EVENT_DISPATCH_FAILED",
            f"事件派发失败: {event_type}",
            exc,
        )


# ---------------------------------------------------------------------------
# 日志事件（统一走全局事件总线；C# sink 按 batch_id 分窗）
# ---------------------------------------------------------------------------


def _emit_log(
    batch_id: str, level: str, message: str, error_code: str = ""
) -> None:
    """发一条 download.log（已脱敏；行首统一 ``[错误码] 消息``）。"""
    text = sanitize_text(message)
    line = f"[{error_code}] {text}" if error_code else text
    payload = {
        "type": EVENT_DOWNLOAD_LOG,
        "level": level,
        "message": line,
        "batch_id": batch_id,
    }
    _emit(EVENT_DOWNLOAD_LOG, payload)


# ---------------------------------------------------------------------------
# 单本下载
# ---------------------------------------------------------------------------


def _run_one_book(
    book_id: str,
    batch_id: str,
    target_dir: str,
    cancel_event: threading.Event,
    source,
    work_dir: str | None = None,
) -> dict:
    """下载一本：返回统一结果 dict（不抛异常，异常在这里分类收口）。"""
    started = False
    title = ""
    temp_dir: str | None = None
    acquired = False

    def _cancelled_before_start() -> dict:
        """取消前尚未开始的本子：不发任何 item 事件，只计入批次取消数。"""
        return {
            "book_id": book_id,
            "title": title,
            "ok": False,
            "zip_path": "",
            "error_code": ERROR_CANCELLED,
            "error_message": "已取消",
            "cancelled": True,
        }

    def _release_slot() -> None:
        """释放全局并发槽（终态事件前调用，尽量不再占用槽）。"""
        nonlocal acquired
        if acquired:
            _GLOBAL_SLOTS.release()
            acquired = False

    def _finished_ok(zip_path: str) -> dict:
        _release_slot()
        _emit(
            EVENT_DOWNLOAD_ITEM_FINISHED,
            {
                "type": EVENT_DOWNLOAD_ITEM_FINISHED,
                "book_id": book_id,
                "title": title,
                "ok": True,
                "zip_path": zip_path,
                "error_code": "",
                "error_message": "",
                "batch_id": batch_id,
            },
        )
        return {
            "book_id": book_id,
            "title": title,
            "ok": True,
            "zip_path": zip_path,
            "error_code": "",
            "error_message": "",
        }

    def _finished_fail(error_code: str, error_message: str, cancelled: bool = False) -> dict:
        _release_slot()
        _emit(
            EVENT_DOWNLOAD_ITEM_FINISHED,
            {
                "type": EVENT_DOWNLOAD_ITEM_FINISHED,
                "book_id": book_id,
                "title": title,
                "ok": False,
                "zip_path": "",
                "error_code": error_code,
                "error_message": sanitize_text(error_message),
                "batch_id": batch_id,
            },
        )
        result = {
            "book_id": book_id,
            "title": title,
            "ok": False,
            "zip_path": "",
            "error_code": error_code,
            "error_message": sanitize_text(error_message),
        }
        if cancelled:
            result["cancelled"] = True
        return result

    jm_source.set_current_batch(batch_id)
    try:
        # 全局并发槽：所有批次合计同时最多 3 本在跑。
        # 等待期间若被取消，直接返回"未开始取消"，不产生任何 item 事件。
        while not acquired:
            if cancel_event.is_set():
                return _cancelled_before_start()
            acquired = _GLOBAL_SLOTS.acquire(timeout=0.05)

        # 检查点 1：每本开始
        _raise_if_cancelled(cancel_event)
        album = source.get_album(book_id)
        if not isinstance(album, dict):
            raise ValueError(f"下载数据源返回类型非法: {type(album).__name__}")
        title = str(album.get("title") or "")
        chapters = album.get("chapters") or []
        if not isinstance(chapters, list):
            raise ValueError("下载数据源返回的 chapters 不是列表")

        _emit(
            EVENT_DOWNLOAD_ITEM_STARTED,
            {
                "type": EVENT_DOWNLOAD_ITEM_STARTED,
                "book_id": book_id,
                "title": title,
                "batch_id": batch_id,
            },
        )
        started = True
        _emit_log(
            batch_id, LOG_INFO, f"开始下载 JM{book_id}: {title or naming.UNKNOWN_TITLE}"
        )

        temp_dir = _create_work_dir(target_dir, book_id, work_dir, batch_id)
        image_paths: list[str] = []
        for chapter_index, chapter in enumerate(chapters, start=1):
            # 检查点 2：每章开始
            _raise_if_cancelled(cancel_event)
            chapter_dir = os.path.join(temp_dir, f"chapter_{chapter_index:03d}")
            image_iter = iter(source.iter_chapter_images(book_id, chapter, chapter_dir))
            while True:
                # 检查点 3：每图下载前
                _raise_if_cancelled(cancel_event)
                try:
                    image_path = next(image_iter)
                except StopIteration:
                    break
                image_paths.append(image_path)

        _raise_if_cancelled(cancel_event)
        if not image_paths:
            raise ImageError(f"本子 {book_id} 未获取到任何图片")

        filename = naming.build_archive_filename(title, book_id)
        zip_path = ""
        part_path = ""
        try:
            zip_path = naming.reserve_archive_path(target_dir, filename)
            part_path = zip_path + ".part"
            try:
                # 先写 .part，校验通过后再原子替换到占位名；
                # 任何失败/取消都删除 .part 与 0 字节占位文件。
                entry_count, validation = archive.pack_and_validate(image_paths, part_path)
                if not validation.ok:
                    raise _ValidationFailed(validation.error_code, validation.error_message)
                if cancel_event.is_set():
                    raise CancelledError("下载已取消")
                try:
                    os.replace(part_path, zip_path)
                except OSError as exc:
                    # 原子替换也可能被"同名文件被占用/权限"挡住：同样给可诊断文案
                    if archive.is_write_permission_failure(exc):
                        raise archive.ArtifactWriteError(zip_path, exc) from exc
                    raise
                if cancel_event.is_set():
                    _remove_quiet(zip_path)
                    raise CancelledError("下载已取消")
            except BaseException:
                _remove_quiet(part_path)
                _remove_quiet(zip_path)
                raise
        except archive.ArtifactWriteError as exc:
            # 除错误码日志外补一条可定位的明细（download.log 由 C# sink 落盘）
            _log_artifact_write_failure(batch_id, exc)
            raise

        _emit_log(batch_id, LOG_INFO, f"已保存: {zip_path}（{entry_count} 页）")
        return _finished_ok(zip_path)

    except CancelledError as exc:
        if started:
            _emit_log(batch_id, LOG_WARN, f"JM{book_id} 已取消", ERROR_CANCELLED)
            return _finished_fail(ERROR_CANCELLED, str(exc) or "已取消", cancelled=True)
        return _cancelled_before_start()
    except Exception as exc:
        info = classify(exc)
        diagnostics.record(
            info.error_code,
            f"JM{book_id} 下载失败: {info.error_message}",
            exc,
        )
        message = f"JM{book_id} 下载失败: {info.error_message}"
        _emit_log(batch_id, LOG_ERROR, message, info.error_code)
        # 只要尝试过（无论是否已拿到标题）都发 item_finished，界面才能收口；
        # 取消前根本没启动的本子不算尝试，不发事件。
        return _finished_fail(info.error_code, info.error_message)
    finally:
        if acquired:
            _GLOBAL_SLOTS.release()
        if temp_dir:
            _safe_rmtree(temp_dir)
        jm_source.clear_current_batch()


# ---------------------------------------------------------------------------
# 同批次任务编排
# ---------------------------------------------------------------------------


def run_batch(
    book_ids: list,
    target_dir: str,
    *,
    batch_id: str | None = None,
    cancel_event: threading.Event | None = None,
    source=None,
    work_dir: str | None = None,
    on_complete=None,
) -> dict:
    """同步下载一批（测试与后台线程共用），返回批次结果 dict。

    ``on_complete(result)`` 在 ``batch_finished`` 事件**之前**调用，供后台
    管理器先提交状态再发事件。
    """
    identity = _normalize_or_raise(book_ids)
    directory = _normalize_target_dir(target_dir)
    _ensure_target_dir_writable(directory)
    _cleanup_stale_artifacts(directory)
    base_work_dir = _prepare_work_base(directory, work_dir)

    batch = batch_id or uuid.uuid4().hex
    cancel = cancel_event or threading.Event()
    active_source = source or jm_source.get_source()
    started_at = time.time()

    _emit_log(
        batch, LOG_INFO, f"批次开始: 共 {len(identity)} 本，并发上限 {MAX_WORKERS}"
    )

    results_by_id: dict[str, dict] = {}
    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS, thread_name_prefix="jml-book"
    ) as executor:
        futures = {
            executor.submit(
                _run_one_book,
                book_id,
                batch,
                directory,
                cancel,
                active_source,
                base_work_dir,
            ): book_id
            for book_id in identity
        }
        for future in as_completed(futures):
            book_id = futures[future]
            try:
                results_by_id[book_id] = future.result()
            except Exception as exc:  # 兜底：单本结果异常也只算该本失败
                info = classify(exc)
                diagnostics.record(
                    info.error_code,
                    f"JM{book_id} 结果收集失败",
                    exc,
                )
                results_by_id[book_id] = {
                    "book_id": book_id,
                    "title": "",
                    "ok": False,
                    "zip_path": "",
                    "error_code": info.error_code,
                    "error_message": info.error_message,
                }

    items = [results_by_id[book_id] for book_id in identity]
    success = sum(1 for item in items if item.get("ok"))
    cancelled = sum(1 for item in items if item.get("cancelled"))
    failed = len(items) - success - cancelled

    result = {
        "ok": True,
        "batch_id": batch,
        "total": len(items),
        "success": success,
        "failed": failed,
        "cancelled": cancelled,
        "items": items,
    }

    _emit_log(
        batch,
        LOG_INFO,
        f"批次结束: 成功 {success} / 共 {len(items)}，失败 {failed}，取消 {cancelled}"
        f"（用时 {time.time() - started_at:.1f} 秒）",
    )
    # 先提交状态（on_complete），再发 batch_finished：C# 在处理器内查状态
    # 必须已经是 finished 且 result 非空。
    if on_complete is not None:
        on_complete(result)
    _emit(
        EVENT_DOWNLOAD_BATCH_FINISHED,
        {
            "type": EVENT_DOWNLOAD_BATCH_FINISHED,
            "total": len(items),
            "success": success,
            "failed": failed,
            "cancelled": cancelled,
            "batch_id": batch,
        },
    )

    return result


# ---------------------------------------------------------------------------
# 后台批次管理（C# 侧 start_download 立即返回的支撑）
# ---------------------------------------------------------------------------


class _BatchRecord:
    """一个后台批次的登记信息。"""

    def __init__(
        self,
        batch_id: str,
        book_ids: list[str],
        target_dir: str,
        work_dir: str | None = None,
    ) -> None:
        self.batch_id = batch_id
        self.book_ids = list(book_ids)
        self.target_dir = target_dir
        self.work_dir = work_dir
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.status = STATUS_RUNNING
        self.result: dict | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        """返回状态**副本**（插件/C# 改写返回值不影响内部记录）。"""
        with self.lock:
            return {
                "ok": True,
                "batch_id": self.batch_id,
                "status": self.status,
                "total": len(self.book_ids),
                "cancel_requested": self.cancel_event.is_set(),
                "result": copy.deepcopy(self.result),
            }


_BATCHES_LOCK = threading.RLock()
_BATCHES: dict[str, _BatchRecord] = {}


def _prune_batches() -> None:
    """按 finished_at 只保留最近 MAX_BATCH_HISTORY 个已结束批次。"""
    with _BATCHES_LOCK:
        finished = [
            record
            for record in _BATCHES.values()
            if record.finished_at is not None
        ]
        if len(finished) <= MAX_BATCH_HISTORY:
            return
        finished.sort(key=lambda record: record.finished_at or 0.0)
        for record in finished[: len(finished) - MAX_BATCH_HISTORY]:
            _BATCHES.pop(record.batch_id, None)


def _commit_result(record: _BatchRecord, result: dict) -> None:
    """先提交批次状态（深拷贝），再允许调用方发终态事件。"""
    with record.lock:
        record.result = copy.deepcopy(result)
        record.status = STATUS_FINISHED
        record.finished_at = time.time()
    _prune_batches()


def start_batch(
    book_ids: list, target_dir: str, source=None, work_dir: str | None = None
) -> dict:
    """登记批次 + 起后台线程，**立即返回**（不阻塞调用方）。

    返回::

        {"ok": True, "batch_id": str, "total": int, "status": "running"}

    异常:
        InvalidArgumentError —— 编号列表为空 / 下载目录不可用
    """
    identity = _normalize_or_raise(book_ids)
    directory = _normalize_target_dir(target_dir)
    # fail fast：目录写不进去就立即失败，不登记批次、不取专辑/下载任何图片。
    _ensure_target_dir_writable(directory)

    batch_id = uuid.uuid4().hex
    record = _BatchRecord(batch_id, identity, directory, work_dir)
    with _BATCHES_LOCK:
        _BATCHES[batch_id] = record

    record.thread = threading.Thread(
        target=_run_batch_entry,
        args=(record, source),
        name=f"jml-download-{batch_id[:8]}",
        daemon=True,
    )
    try:
        record.thread.start()
    except BaseException:
        # 线程起不来：清掉登记，避免查询到一个永远 running 的幽灵批次
        with _BATCHES_LOCK:
            _BATCHES.pop(batch_id, None)
        raise
    return {
        "ok": True,
        "batch_id": batch_id,
        "total": len(identity),
        "status": STATUS_RUNNING,
    }


def _run_batch_entry(record: _BatchRecord, source) -> None:
    """后台线程入口：跑完整个批次并记录结果（异常也收口成结构化结果）。

    状态提交顺序：``on_complete`` 在 ``batch_finished`` 之前把
    status/result 写进 record，C# 在事件处理器里查状态能立刻看到 finished。
    """
    committed = {"done": False}

    def on_complete(result: dict) -> None:
        _commit_result(record, result)
        committed["done"] = True

    try:
        result = run_batch(
            record.book_ids,
            record.target_dir,
            batch_id=record.batch_id,
            cancel_event=record.cancel_event,
            source=source,
            work_dir=record.work_dir,
            on_complete=on_complete,
        )
        if not committed["done"]:
            _commit_result(record, result)
    except BaseException as exc:  # noqa: BLE001 - 后台线程必须收口，不能静默死掉
        info = classify(exc)
        diagnostics.record(
            info.error_code, f"批次 {record.batch_id} 异常终止", exc
        )
        total = len(record.book_ids)
        result = {
            "ok": False,
            "batch_id": record.batch_id,
            "total": total,
            "success": 0,
            "failed": total,
            "cancelled": 0,
            "items": [],
            "error_code": info.error_code,
            "error_message": info.error_message,
        }
        # 先提交状态，再发批次终态事件
        _commit_result(record, result)
        _emit_log(
            record.batch_id,
            LOG_ERROR,
            f"批次异常终止: {info.error_message}",
            info.error_code,
        )
        _emit(
            EVENT_DOWNLOAD_BATCH_FINISHED,
            {
                "type": EVENT_DOWNLOAD_BATCH_FINISHED,
                "total": total,
                "success": 0,
                "failed": total,
                "cancelled": 0,
                "batch_id": record.batch_id,
            },
        )


def cancel_batch(batch_id: str) -> bool:
    """请求取消一个批次（只 set 事件，不等待）；批次不存在返回 False。"""
    with _BATCHES_LOCK:
        record = _BATCHES.get(str(batch_id))
    if record is None:
        return False
    record.cancel_event.set()
    return True


def get_batch_status(batch_id: str) -> dict:
    """查询批次状态（未知批次抛 ``BatchNotFoundError``）。"""
    with _BATCHES_LOCK:
        record = _BATCHES.get(str(batch_id))
    if record is None:
        raise BatchNotFoundError(f"找不到下载批次: {batch_id}")
    return record.snapshot()


def batch_history_size() -> int:
    """返回当前保留的批次登记数（测试/诊断用）。"""
    with _BATCHES_LOCK:
        return len(_BATCHES)


def wait_batch(batch_id: str, timeout: float | None = None) -> dict | None:
    """等待批次线程结束并返回**结果副本**（测试/内部用；C# 不调用）。"""
    with _BATCHES_LOCK:
        record = _BATCHES.get(str(batch_id))
    if record is None or record.thread is None:
        return None
    record.thread.join(timeout)
    with record.lock:
        return copy.deepcopy(record.result)


def reset() -> None:
    """清空批次登记（测试隔离用；不等待运行中的线程）。"""
    with _BATCHES_LOCK:
        _BATCHES.clear()


# ---------------------------------------------------------------------------
# 参数规范化
# ---------------------------------------------------------------------------


def _normalize_or_raise(values) -> list[str]:
    """统一编号校验：调用 ``book_ids.normalize_ids``，空列表也判参数错误。"""
    identity = book_ids.normalize_ids(values)
    if not identity:
        raise InvalidArgumentError("编号列表为空")
    return identity


def _normalize_target_dir(target_dir) -> str:
    """校验并准备下载目录（不硬编码任何路径，全部来自调用方）。"""
    try:
        directory = os.fspath(target_dir) if target_dir is not None else ""
    except TypeError as exc:
        raise InvalidArgumentError(f"下载目录类型不合法: {type(target_dir).__name__}") from exc
    if not directory:
        raise InvalidArgumentError("下载目录不能为空")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise InvalidArgumentError(f"下载目录不可用: {directory}（{exc}）") from exc
    return os.path.abspath(directory)
