"""批量下载单元测试（零联网：可控假数据源）。

覆盖：并发上限 3、真取消（set 后在检查点中断）、单本失败隔离、
每本一个 ZIP + 三位零填充 + 校验、同名副本、事件 payload、
jmcomic 日志转发携带 batch_id、start_batch 立即返回。
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import zipfile

import pytest
from PIL import Image

from jml.core import archive, downloader, jm_source
from jml.core.errors import (
    ERROR_IMAGE_INVALID,
    ERROR_PERMISSION_DENIED,
    BatchNotFoundError,
    InvalidArgumentError,
)
from jml.core.protocol import (
    EVENT_DOWNLOAD_BATCH_FINISHED,
    EVENT_DOWNLOAD_ITEM_FINISHED,
    EVENT_DOWNLOAD_ITEM_STARTED,
    EVENT_DOWNLOAD_LOG,
)
from jml.host import events
from tests.conftest import FakeSource as FakeDownloadSource

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class ActiveTracker:
    """记录同时活跃的下载本数。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def exit(self) -> None:
        with self.lock:
            self.active -= 1


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _zip_names(path: str) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


# ---------------------------------------------------------------------------
# 打包与产物
# ---------------------------------------------------------------------------


def test_max_workers_is_three():
    assert downloader.MAX_WORKERS == 3


def test_batch_creates_zip_with_padded_entries(tmp_path):
    source = FakeDownloadSource({"1": "标题 A"}, chapters=2, images_per_chapter=2)

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["ok"] is True
    assert result["total"] == 1
    assert result["success"] == 1
    assert result["failed"] == 0
    assert result["cancelled"] == 0

    zip_path = result["items"][0]["zip_path"]
    assert zip_path.endswith("标题 A-JM1.zip")
    assert os.path.isfile(zip_path)
    assert _zip_names(zip_path) == ["001.png", "002.png", "003.png", "004.png"]
    assert archive.validate_zip(zip_path, 4).ok is True

    with zipfile.ZipFile(zip_path) as zf:
        with Image.open(io.BytesIO(zf.read("001.png"))) as image:
            assert image.size == (4, 3)


def test_concurrency_capped_at_three(tmp_path):
    tracker = ActiveTracker()
    gate = threading.Event()
    books = {str(index): f"书{index}" for index in range(1, 7)}
    source = FakeDownloadSource(
        books,
        chapters=1,
        images_per_chapter=2,
        gate=gate,
        tracker=tracker,
    )

    info = downloader.start_batch(list(books), str(tmp_path), source=source)
    assert _wait_until(lambda: source.chapters_started == 3, timeout=5)
    # 第 3 本已进检查点；再等一会儿，第 4 本不得抢跑
    time.sleep(0.1)
    assert source.chapters_started == 3
    assert tracker.max_active == 3

    gate.set()
    result = downloader.wait_batch(info["batch_id"], timeout=10)
    assert result is not None
    assert result["success"] == 6
    assert tracker.max_active <= 3


def test_global_concurrency_cap_across_two_batches(tmp_path):
    """两个批次并行时，进程内总活跃本数仍 ≤ 3（不是 3×批次数）。"""
    tracker = ActiveTracker()
    gate = threading.Event()
    books = {str(index): f"书{index}" for index in range(1, 9)}
    source = FakeDownloadSource(
        books, chapters=1, images_per_chapter=2, gate=gate, tracker=tracker
    )

    first = downloader.start_batch(
        ["1", "2", "3", "4"], str(tmp_path / "batch1"), source=source
    )
    second = downloader.start_batch(
        ["5", "6", "7", "8"], str(tmp_path / "batch2"), source=source
    )

    assert _wait_until(lambda: source.chapters_started == 3, timeout=5)
    time.sleep(0.15)
    # 两个批次合计也只允许 3 个本子进入检查点（旧实现会到 6）
    assert source.chapters_started == 3
    assert tracker.max_active == 3

    gate.set()
    result1 = downloader.wait_batch(first["batch_id"], timeout=20)
    result2 = downloader.wait_batch(second["batch_id"], timeout=20)

    assert result1 is not None and result2 is not None
    assert result1["success"] + result2["success"] == 8
    assert tracker.max_active <= 3


def test_single_book_failure_does_not_block_others(tmp_path):
    received: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_ITEM_FINISHED, received.append)
    source = FakeDownloadSource(
        {"1": "好一", "2": "坏", "3": "好三"}, fail_books={"2"}
    )

    result = downloader.run_batch(["1", "2", "3"], str(tmp_path), source=source)

    assert result["success"] == 2
    assert result["failed"] == 1
    failed_item = result["items"][1]
    assert failed_item["book_id"] == "2"
    assert failed_item["ok"] is False
    assert failed_item["error_code"] == "UNKNOWN_ERROR"
    assert failed_item["zip_path"] == ""

    zips = sorted(name for name in os.listdir(tmp_path) if name.endswith(".zip"))
    assert len(zips) == 2

    finished = {payload["book_id"]: payload for payload in received}
    assert finished["1"]["ok"] is True
    assert finished["2"]["ok"] is False
    assert finished["3"]["ok"] is True


def test_duplicate_name_uses_copy_suffix(tmp_path):
    existing = tmp_path / "同名-JM1.zip"
    existing.write_bytes(b"ORIGINAL")
    source = FakeDownloadSource({"1": "同名"})

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    zip_path = result["items"][0]["zip_path"]
    assert os.path.basename(zip_path) == "同名-JM1 - 副本.zip"
    assert existing.read_bytes() == b"ORIGINAL"
    assert archive.validate_zip(zip_path, 2).ok is True


def test_validation_failure_marks_book_failed_and_removes_zip(tmp_path):
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource({"1": "坏图"}, invalid_image_books={"1"})

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    item = result["items"][0]
    assert result["failed"] == 1
    assert item["ok"] is False
    assert item["error_code"] == ERROR_IMAGE_INVALID
    assert list(tmp_path.glob("*.zip")) == []
    assert list(tmp_path.glob("*.part")) == []
    assert not list(tmp_path.glob("*.zip"))  # 不允许 0 字节占位残留
    assert any(
        message["message"].startswith("[IMAGE_INVALID]")
        for message in logs
        if message["level"] == "error"
    )


def test_pack_failure_leaves_no_part_and_no_zero_byte_zip(tmp_path):
    """图片文件在打包前被删 → 打包失败，目录里 .part 与占位 ZIP 都不能残留。"""
    source = FakeDownloadSource({"1": "打包失败"})

    original_iter = source.iter_chapter_images

    def deleting_iter(book_id, chapter, dest_dir):
        for path in original_iter(book_id, chapter, dest_dir):
            os.remove(path)
            yield path

    source.iter_chapter_images = deleting_iter
    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["failed"] == 1
    assert list(tmp_path.glob("*.zip")) == []
    assert list(tmp_path.glob("*.part")) == []


def test_work_dir_argument_keeps_temp_out_of_target(tmp_path):
    target = tmp_path / "out"
    work = tmp_path / "work"
    source = FakeDownloadSource({"1": "书1"}, record_dest_dirs=True)

    result = downloader.run_batch(
        ["1"], str(target), source=source, work_dir=str(work)
    )

    assert result["success"] == 1
    assert source.dest_dirs
    assert all(path.startswith(str(work)) for path in source.dest_dirs)
    assert list(target.glob(".jml-tmp-*")) == []
    assert list(work.glob(".jml-tmp-*")) == []


def test_stale_work_dirs_are_cleaned(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stale = work / ".jml-tmp-old"
    stale.mkdir()
    fresh = work / ".jml-tmp-fresh"
    fresh.mkdir()
    old_time = time.time() - 48 * 3600
    os.utime(stale, (old_time, old_time))

    source = FakeDownloadSource({"1": "书1"})
    downloader.run_batch(["1"], str(tmp_path / "out"), source=source, work_dir=str(work))

    assert not stale.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# 临时目录降级（首选 %LOCALAPPDATA%\JML\tmp 不可写）
# ---------------------------------------------------------------------------


def _deny_makedirs(monkeypatch, denied_prefix):
    """把 ``os.makedirs`` 换成"指定前缀下拒绝创建"的假实现（其余照常）。"""
    real_makedirs = os.makedirs
    denied = os.path.abspath(str(denied_prefix))

    def fake_makedirs(path, *args, **kwargs):
        target = os.path.abspath(str(path))
        if target == denied or target.startswith(denied + os.sep):
            raise PermissionError(13, "拒绝访问（测试模拟）", str(path))
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(downloader.os, "makedirs", fake_makedirs)
    return fake_makedirs


def _deny_all_new_dirs(monkeypatch):
    """把所有"新建目录"都拒绝，已存在的目录照常返回（模拟两处都不可写）。"""
    real_isdir = os.path.isdir

    def fake_makedirs(path, *args, **kwargs):
        if real_isdir(str(path)):
            return None
        raise PermissionError(13, "拒绝访问（测试模拟）", str(path))

    monkeypatch.setattr(downloader.os, "makedirs", fake_makedirs)


def test_work_dir_denied_falls_back_to_target_with_warn_log(
    monkeypatch, tmp_path
):
    """首选 work_dir 不可写 → 降级到下载目录：下载成功、有 warn 日志、无残留。"""
    target = tmp_path / "out"
    target.mkdir()
    denied_work = tmp_path / "denied-work"

    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    _deny_makedirs(monkeypatch, denied_work)

    source = FakeDownloadSource({"1": "降级书"}, record_dest_dirs=True)
    result = downloader.run_batch(
        ["1"], str(target), source=source, work_dir=str(denied_work)
    )

    assert result["success"] == 1
    item = result["items"][0]
    assert item["ok"] is True
    assert os.path.isfile(item["zip_path"])
    assert item["zip_path"].startswith(str(target))

    # 临时目录必须落在下载目录内，而不是不可写的首选目录
    assert source.dest_dirs
    assert all(path.startswith(str(target)) for path in source.dest_dirs)
    assert not denied_work.exists()

    # 降级必须发一条 warn 日志（脱敏、说明原因）
    warns = [entry for entry in logs if entry["level"] == "warn"]
    fallback_logs = [
        entry
        for entry in warns
        if "临时目录不可用，已降级到下载目录" in entry["message"]
    ]
    assert fallback_logs, f"应收到降级 warn 日志，实际 warn: {warns}"
    assert str(target) in fallback_logs[0]["message"]
    assert "PermissionError" in fallback_logs[0]["message"]
    assert fallback_logs[0]["batch_id"] == result["batch_id"]
    assert all("拒绝访问（测试模拟）" not in entry["message"] for entry in fallback_logs)

    # 批次结束后降级目录也无残留
    assert list(target.glob(".jml-tmp-*")) == []


def test_both_temp_dirs_denied_fails_with_permission_denied(
    monkeypatch, tmp_path
):
    """首选与下载目录都写不进去 → 该本失败，PERMISSION_DENIED，消息带两处路径。"""
    target = tmp_path / "out"
    target.mkdir()
    denied_work = tmp_path / "denied-work"
    _deny_all_new_dirs(monkeypatch)

    source = FakeDownloadSource({"1": "无解书"})
    result = downloader.run_batch(
        ["1"], str(target), source=source, work_dir=str(denied_work)
    )

    assert result["success"] == 0
    assert result["failed"] == 1
    item = result["items"][0]
    assert item["ok"] is False
    assert item["error_code"] == ERROR_PERMISSION_DENIED
    assert "两处临时目录都不可用" in item["error_message"]
    assert str(denied_work) in item["error_message"]
    assert str(target) in item["error_message"]
    assert list(target.glob(".jml-tmp-*")) == []


def test_normal_work_dir_does_not_log_fallback_warning(monkeypatch, tmp_path):
    """正常情况不受影响：临时目录仍在 work_dir，不出现降级 warn 日志。"""
    target = tmp_path / "out"
    work = tmp_path / "work"

    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource({"1": "正常书"}, record_dest_dirs=True)

    result = downloader.run_batch(
        ["1"], str(target), source=source, work_dir=str(work)
    )

    assert result["success"] == 1
    assert source.dest_dirs
    assert all(path.startswith(str(work)) for path in source.dest_dirs)
    assert not any(
        "临时目录不可用，已降级到下载目录" in entry["message"] for entry in logs
    )
    assert not any(entry["level"] == "warn" for entry in logs)
    assert list(work.glob(".jml-tmp-*")) == []


def test_stale_artifacts_are_cleaned_but_normal_zip_kept(tmp_path):
    old_time = time.time() - 48 * 3600
    stale_part = tmp_path / "旧-JM1.zip.part"
    stale_part.write_bytes(b"partial")
    stale_zero = tmp_path / "旧-JM2.zip"
    stale_zero.write_bytes(b"")
    old_valid = tmp_path / "旧-JM3.zip"
    old_valid.write_bytes(b"PK-valid-looking")
    fresh_part = tmp_path / "新-JM4.zip.part"
    fresh_part.write_bytes(b"partial")
    for path in (stale_part, stale_zero, old_valid):
        os.utime(path, (old_time, old_time))

    source = FakeDownloadSource({"1": "书1"})
    downloader.run_batch(["1"], str(tmp_path), source=source)

    assert not stale_part.exists()
    assert not stale_zero.exists()
    assert old_valid.exists()  # 非 0 字节的旧 ZIP 不碰
    assert fresh_part.exists()  # 新鲜的 .part 不碰


def test_rmtree_failure_writes_diagnostic(monkeypatch, tmp_path):
    from jml.core import diagnostics

    diagnostics.clear()

    class BrokenShutil:
        @staticmethod
        def rmtree(path, ignore_errors=False):
            raise OSError("locked")

    monkeypatch.setattr(downloader, "shutil", BrokenShutil)
    source = FakeDownloadSource({"1": "书1"})

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["success"] == 1
    assert any(
        "WORK_DIR_CLEANUP_FAILED" in item for item in diagnostics.recent()
    )


# ---------------------------------------------------------------------------
# 真取消
# ---------------------------------------------------------------------------


def test_cancel_interrupts_at_checkpoints(tmp_path):
    """set 取消事件后，正在跑的本子在下一个检查点退出，不等整本下完。"""
    cancel_event = threading.Event()
    source = FakeDownloadSource(
        {"1": "书1", "2": "书2", "3": "书3"},
        chapters=1,
        images_per_chapter=40,
        sleep=0.01,
    )
    holder = {}

    def runner() -> None:
        holder["result"] = downloader.run_batch(
            ["1", "2", "3"],
            str(tmp_path),
            cancel_event=cancel_event,
            source=source,
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    assert _wait_until(lambda: source.downloaded >= 3, timeout=5)

    started_at = time.time()
    cancel_event.set()
    thread.join(5)
    elapsed = time.time() - started_at

    assert not thread.is_alive(), "取消后下载线程应立即返回"
    assert elapsed < 3
    result = holder["result"]
    assert result["cancelled"] >= 1
    assert result["success"] == 0
    assert source.downloaded < 3 * 40
    assert list(tmp_path.glob("*.zip")) == []


def test_cancel_stops_before_next_chapter(tmp_path):
    """章节检查点：取消后不再开启新章节。"""
    cancel_event = threading.Event()
    source = FakeDownloadSource(
        {"1": "书1"}, chapters=5, images_per_chapter=2, sleep=0.02
    )
    holder = {}

    def runner() -> None:
        holder["result"] = downloader.run_batch(
            ["1"], str(tmp_path), cancel_event=cancel_event, source=source
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    assert _wait_until(lambda: source.downloaded >= 1, timeout=5)
    cancel_event.set()
    thread.join(5)

    assert not thread.is_alive()
    assert source.chapters_started <= 2
    assert holder["result"]["cancelled"] == 1


def test_cancel_event_semantics_started_vs_unstarted(tmp_path):
    """取消事件语义：已开始本发 item_finished(CANCELLED)，未开始本一条 item 事件都不发。"""
    started_events: list[dict] = []
    finished_events: list[dict] = []
    batch_events: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_ITEM_STARTED, started_events.append)
    events.BUS.subscribe(EVENT_DOWNLOAD_ITEM_FINISHED, finished_events.append)
    events.BUS.subscribe(EVENT_DOWNLOAD_BATCH_FINISHED, batch_events.append)

    gate = threading.Event()
    cancel_event = threading.Event()
    source = FakeDownloadSource(
        {str(index): f"书{index}" for index in range(1, 6)},
        chapters=1,
        images_per_chapter=1,
        gate=gate,
    )
    holder: dict = {}

    def runner() -> None:
        holder["result"] = downloader.run_batch(
            [str(index) for index in range(1, 6)],
            str(tmp_path),
            cancel_event=cancel_event,
            source=source,
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    # 并发上限 3：先只有 1/2/3 发 item_started 并阻塞在 gate，4/5 尚未开始
    assert _wait_until(lambda: len(started_events) == 3, timeout=5)

    cancel_event.set()
    gate.set()
    thread.join(10)

    assert not thread.is_alive()
    result = holder["result"]
    started_ids = {payload["book_id"] for payload in started_events}
    finished = {payload["book_id"]: payload for payload in finished_events}

    assert started_ids == {"1", "2", "3"}
    assert set(finished) == started_ids  # 未开始的 4/5 不发 item_finished
    for payload in finished.values():
        assert payload["ok"] is False
        assert payload["error_code"] == "CANCELLED"
        assert payload["zip_path"] == ""
    for book_id in ("4", "5"):
        assert book_id not in started_ids
        assert book_id not in finished

    assert result["success"] == 0
    assert result["failed"] == 0
    assert result["cancelled"] == 5
    assert batch_events[-1]["cancelled"] == 5
    assert batch_events[-1]["total"] == 5


def test_start_batch_returns_immediately(tmp_path):
    source = FakeDownloadSource({"1": "慢书"}, images_per_chapter=2, sleep=0.1)

    started_at = time.time()
    info = downloader.start_batch(["1"], str(tmp_path), source=source)
    elapsed = time.time() - started_at

    assert info["ok"] is True
    assert info["batch_id"]
    assert info["total"] == 1
    assert elapsed < 0.5

    result = downloader.wait_batch(info["batch_id"], timeout=10)
    assert result is not None
    assert result["success"] == 1


def test_cancel_batch_and_status(tmp_path):
    source = FakeDownloadSource(
        {"1": "书1", "2": "书2"}, chapters=1, images_per_chapter=30, sleep=0.01
    )
    info = downloader.start_batch(["1", "2"], str(tmp_path), source=source)

    assert downloader.cancel_batch(info["batch_id"]) is True
    assert downloader.cancel_batch("no-such-batch") is False

    result = downloader.wait_batch(info["batch_id"], timeout=10)
    assert result is not None
    assert result["cancelled"] >= 1

    status = downloader.get_batch_status(info["batch_id"])
    assert status["ok"] is True
    assert status["status"] == "finished"
    assert status["total"] == 2

    with pytest.raises(BatchNotFoundError):
        downloader.get_batch_status("no-such-batch")


# ---------------------------------------------------------------------------
# 事件与日志通道
# ---------------------------------------------------------------------------


def test_item_and_batch_event_payloads(tmp_path):
    started: list[dict] = []
    finished: list[dict] = []
    batch_done: list[dict] = []
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_ITEM_STARTED, started.append)
    events.BUS.subscribe(EVENT_DOWNLOAD_ITEM_FINISHED, finished.append)
    events.BUS.subscribe(EVENT_DOWNLOAD_BATCH_FINISHED, batch_done.append)
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource({"1": "事件书"}, chapters=2, images_per_chapter=1)

    result = downloader.run_batch(["1"], str(tmp_path), batch_id="evt", source=source)

    assert len(started) == 1
    assert started[0]["type"] == EVENT_DOWNLOAD_ITEM_STARTED
    assert started[0]["book_id"] == "1"
    assert started[0]["title"] == "事件书"
    assert started[0]["batch_id"] == "evt"

    assert len(finished) == 1
    payload = finished[0]
    assert payload["type"] == EVENT_DOWNLOAD_ITEM_FINISHED
    for field in ("book_id", "title", "ok", "zip_path", "error_code", "error_message"):
        assert field in payload
    assert payload["ok"] is True

    assert len(batch_done) == 1
    assert batch_done[0]["type"] == EVENT_DOWNLOAD_BATCH_FINISHED
    assert batch_done[0] == {
        "type": EVENT_DOWNLOAD_BATCH_FINISHED,
        "total": 1,
        "success": 1,
        "failed": 0,
        "cancelled": 0,
        "batch_id": "evt",
    }

    assert logs, "至少要有批次开始/保存日志"
    assert all("batch_id" in item for item in logs)
    assert result["success"] == 1


def test_download_log_is_sanitized(tmp_path):
    """日志本身保留并脱敏（不再有按批通道；C# 从 sink 按 batch_id 分窗）。"""
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource({"1": "password=SECRET-TITLE"})

    downloader.run_batch(["1"], str(tmp_path), batch_id="log-batch", source=source)

    assert logs
    assert all(item["batch_id"] == "log-batch" for item in logs)
    assert all("SECRET-TITLE" not in item["message"] for item in logs)
    assert any("***" in item["message"] for item in logs)


def test_jmcomic_warning_log_carries_current_batch_id(tmp_path):
    """下载线程里的 jmcomic WARNING 日志必须补上当前 batch_id。"""
    logger = logging.getLogger("jmcomic")
    logger.handlers.clear()
    jm_source._JM_LOG_INSTALLED = False
    jm_source._install_log_capture()
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)

    source = FakeDownloadSource({"1": "书1"})
    original_get_album = source.get_album

    def logging_get_album(book_id):
        logger.warning("下载期间的 WARN 日志")
        return original_get_album(book_id)

    source.get_album = logging_get_album

    downloader.run_batch(
        ["1"], str(tmp_path), batch_id="ctx-batch", source=source
    )

    matched = [item for item in logs if "下载期间的 WARN" in item["message"]]
    assert matched, "应收到 jmcomic 转发的 WARNING 日志"
    assert all(item["batch_id"] == "ctx-batch" for item in matched)
    # 下载线程退出后上下文必须清空
    assert jm_source.current_batch_id() == ""


# ---------------------------------------------------------------------------
# 图片级进度日志（download.log，level=info；格式/节流/上限见契约 §5.3）
# ---------------------------------------------------------------------------

_IMAGE_PROGRESS_RE = re.compile(r"^图片下载完成：(\d+)(?:/(\d+))?")
_IMAGE_SUMMARY_PREFIX = "图片下载完成：共 "


def _image_progress(logs: list[dict]) -> list[tuple[int, int | None]]:
    """从 download.log 事件里抽出图片进度行 ``(done, total|None)``（按顺序）。"""
    pairs: list[tuple[int, int | None]] = []
    for entry in logs:
        match = _IMAGE_PROGRESS_RE.match(entry["message"])
        if match is None:
            continue
        total = match.group(2)
        pairs.append((int(match.group(1)), None if total is None else int(total)))
    return pairs


def test_progress_logs_cover_every_chapter_and_image(tmp_path):
    """3 章共 6 张：三条章开始行都在 + 1/6…6/6 全序列 + 结尾汇总。"""
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource(
        {"1": "进度书"}, chapters=3, images_per_chapter=2, sleep=0.08
    )

    result = downloader.run_batch(
        ["1"], str(tmp_path), batch_id="progress", source=source
    )

    assert result["success"] == 1
    messages = [entry["message"] for entry in logs]
    # 章节开始行豁免 50ms 节流：每章都必须出现
    chapter_lines = [
        message for message in messages if message.startswith("开始下载章节：")
    ]
    assert chapter_lines == [
        "开始下载章节：第 1/3 章（第1章）",
        "开始下载章节：第 2/3 章（第2章）",
        "开始下载章节：第 3/3 章（第3章）",
    ], messages
    # 章开始行必须落在该章第一张图片之前
    first_images = [f"图片下载完成：{index}/6" for index in (1, 3, 5)]
    for chapter_line, first_image in zip(chapter_lines, first_images):
        assert messages.index(chapter_line) < next(
            index
            for index, message in enumerate(messages)
            if message.startswith(first_image)
        )
    assert _image_progress(logs) == [
        (1, 6),
        (2, 6),
        (3, 6),
        (4, 6),
        (5, 6),
        (6, 6),
    ], messages
    # 章节号随图片行一起给出（多章节本子才附）
    assert "图片下载完成：3/6（第 2/3 章）" in messages
    assert "图片下载完成：6/6（第 3/3 章）" in messages
    # 单本成功收口：额外一条"共 N 张"
    assert any(
        message.startswith(_IMAGE_SUMMARY_PREFIX) and message.endswith("s")
        for message in messages
    ), messages
    summary = next(
        message for message in messages if message.startswith(_IMAGE_SUMMARY_PREFIX)
    )
    assert "共 6 张，用时 " in summary
    # 进度行一律 info 级，且带当前 batch_id
    progress_entries = [
        entry
        for entry in logs
        if entry["message"].startswith(("图片下载完成：", "开始下载章节："))
    ]
    assert progress_entries
    assert all(entry["level"] == "info" for entry in progress_entries)
    assert all(entry["batch_id"] == "progress" for entry in progress_entries)


def test_progress_without_page_count_never_writes_slash_zero(tmp_path):
    """page_count=0：进度行只写已完成张数，结尾仍能报总张数。"""
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource(
        {"1": "零页书"}, chapters=1, images_per_chapter=3, sleep=0.08
    )
    album = source.get_album("1")
    album["page_count"] = 0
    source.album = album

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["success"] == 1
    messages = [entry["message"] for entry in logs]
    assert not any("/0" in message for message in messages), messages
    assert _image_progress(logs) == [(1, None), (2, None), (3, None)]
    assert any(message.startswith("图片下载完成：共 3 张，用时 ") for message in messages)
    assert any("开始下载章节：第 1/1 章" in message for message in messages)


def test_progress_lines_are_throttled_and_last_image_always_emitted(tmp_path):
    """相邻图片进度行 ≥50ms；被节流的图只更新计数；最后一张必发。"""
    logs: list[dict] = []
    stamps: list[float] = []

    def on_log(payload: dict) -> None:
        logs.append(payload)
        match = _IMAGE_PROGRESS_RE.match(payload["message"])
        if match is not None:
            stamps.append(time.monotonic())

    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, on_log)
    # 每张 20ms：快于 50ms 下限，中间必然有图被节流
    source = FakeDownloadSource(
        {"1": "节流书"}, chapters=1, images_per_chapter=10, sleep=0.02
    )

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["success"] == 1
    pairs = _image_progress(logs)
    assert 2 <= len(pairs) <= 10, pairs
    # 最后一张必须发（补发），计数等于真实总数
    assert pairs[-1] == (10, 10)
    assert source.downloaded == 10
    # 除"最后一张补发"这一对外，相邻进度行间隔都不小于 50ms
    gaps = [later - earlier for earlier, later in zip(stamps[:-2], stamps[1:-1])]
    assert gaps, stamps
    assert all(
        gap >= downloader.PROGRESS_MIN_INTERVAL_SECONDS - 0.005 for gap in gaps
    ), gaps


def test_book_progress_throttles_middle_images_and_forces_last(monkeypatch):
    """用假时钟精确验证：中间图只累加计数，最后一张即使未到 50ms 也补发。"""
    clock = [1000.0]
    monkeypatch.setattr(downloader.time, "monotonic", lambda: clock[0])
    lines: list[str] = []
    progress = downloader._BookProgress(lines.append, 10, 1)

    progress.image_done(1)  # 首张：发
    for _ in range(3):  # 第 2~4 张：都在 50ms 内，只累加
        clock[0] += 0.01
        progress.image_done(1)
    clock[0] += 0.04  # 第 5 张：距上一条 0.07s，发
    progress.image_done(1)
    for _ in range(4):  # 第 6~9 张：又在 50ms 内
        clock[0] += 0.01
        progress.image_done(1)
    # 第 10 张（最后一张）：距上一条 0.04s（<50ms），仍必须补发
    progress.image_done(1)

    assert progress.done == 10
    assert lines == [
        "图片下载完成：1/10",
        "图片下载完成：5/10",
        "图片下载完成：10/10",
    ]
    assert progress.emitted == 3


def test_book_progress_chapter_lines_bypass_throttle_and_cap(monkeypatch):
    """章节开始行豁免 50ms 与 120 条上限：同一毫秒连发也一条不少。"""
    clock = [1000.0]
    monkeypatch.setattr(downloader.time, "monotonic", lambda: clock[0])
    lines: list[str] = []
    progress = downloader._BookProgress(lines.append, 40, 30)
    # 上限压到 2，用来证明章节行不占图片行额度（步长在构造时已算好）
    monkeypatch.setattr(downloader, "PROGRESS_MAX_IMAGE_LINES", 2)

    for index in range(1, 31):  # 30 章连续开始，同一毫秒
        assert progress.chapter_started(index, f"第{index}章") is True

    assert len(lines) == 30
    assert lines[0] == "开始下载章节：第 1/30 章（第1章）"
    assert lines[-1] == "开始下载章节：第 30/30 章（第30章）"
    assert progress.emitted == 0  # 章节行不计入图片进度行数

    # 章节行会刷新节流时间戳：紧随其后的图片行仍在 50ms 内，被节流
    assert progress.image_done(30) is False
    clock[0] += 0.06  # 越过 50ms（避开浮点边界）
    assert progress.image_done(30) is True
    assert progress.emitted == 1


def test_progress_capped_for_large_book(monkeypatch, tmp_path):
    """200 张的大本子：进度行 ≤120，最后一张计数仍为 200。"""
    monkeypatch.setattr(downloader, "PROGRESS_MIN_INTERVAL_SECONDS", 0.0)
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource(
        {"1": "大书"}, chapters=1, images_per_chapter=200
    )

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["success"] == 1
    pairs = _image_progress(logs)
    assert len(pairs) <= downloader.PROGRESS_MAX_IMAGE_LINES, len(pairs)
    assert pairs[-1] == (200, 200)
    # total≥200 → 步长 total//100=2（第 1 张不发，从第 2 张开始）
    assert pairs[0][0] == 2
    assert all(total == 200 for _, total in pairs)


@pytest.mark.parametrize("total", [1, 120, 121, 199, 200, 241, 5000])
def test_progress_step_keeps_line_count_bounded(total):
    step = downloader._progress_step(total)
    assert step >= 1
    assert -(-total // step) <= downloader.PROGRESS_MAX_IMAGE_LINES


def test_progress_step_unknown_total_is_one():
    assert downloader._progress_step(0) == 1
    assert downloader._progress_step(-5) == 1


def test_progress_title_is_sanitized_and_truncated():
    masked = downloader._progress_title("password=SECRET-TITLE")
    assert masked and "SECRET-TITLE" not in masked

    long_title = "长" * (downloader.PROGRESS_TITLE_MAX_CHARS + 10)
    shortened = downloader._progress_title(long_title)
    assert len(shortened) == downloader.PROGRESS_TITLE_MAX_CHARS
    assert shortened.endswith("…")
    assert downloader._progress_title("") == ""
    assert downloader._progress_title(None) == ""


def test_cancel_stops_new_progress_lines(tmp_path):
    """取消后不再出现新的进度行，也不发成功汇总。"""
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    cancel_event = threading.Event()
    source = FakeDownloadSource(
        {"1": "取消书"}, chapters=1, images_per_chapter=60, sleep=0.05
    )
    holder: dict = {}

    def runner() -> None:
        holder["result"] = downloader.run_batch(
            ["1"], str(tmp_path), cancel_event=cancel_event, source=source
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    assert _wait_until(lambda: len(_image_progress(logs)) >= 1, timeout=5)

    cancel_event.set()
    thread.join(10)

    assert not thread.is_alive()
    result = holder["result"]
    assert result["cancelled"] == 1
    assert result["success"] == 0

    messages = [entry["message"] for entry in logs]
    cancel_index = next(
        index for index, message in enumerate(messages) if message.endswith("已取消")
    )
    after_cancel = messages[cancel_index + 1 :]
    assert not any(_IMAGE_PROGRESS_RE.match(message) for message in after_cancel), (
        after_cancel
    )
    assert not any(message.startswith(_IMAGE_SUMMARY_PREFIX) for message in messages)


def test_failed_book_does_not_emit_success_summary(tmp_path):
    """失败的本子不发"共 N 张"成功汇总（进度行可以有，收口只走 error 日志）。"""
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeDownloadSource({"1": "坏图"}, invalid_image_books={"1"})

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["failed"] == 1
    messages = [entry["message"] for entry in logs]
    assert not any(message.startswith(_IMAGE_SUMMARY_PREFIX) for message in messages)
    assert any(entry["level"] == "error" for entry in logs)


def test_batch_status_committed_before_batch_finished_event(tmp_path):
    """batch_finished 处理器内查状态必须已经 finished 且 result 非空。"""
    observed: dict = {}

    def on_batch_finished(payload: dict) -> None:
        observed["status"] = downloader.get_batch_status(payload["batch_id"])

    events.BUS.subscribe(EVENT_DOWNLOAD_BATCH_FINISHED, on_batch_finished)
    source = FakeDownloadSource({"1": "书1"})

    info = downloader.start_batch(["1"], str(tmp_path), source=source)
    downloader.wait_batch(info["batch_id"], timeout=10)

    status = observed["status"]
    assert status["status"] == downloader.STATUS_FINISHED
    assert status["result"] is not None
    assert status["result"]["success"] == 1


def test_status_and_wait_return_copies(tmp_path):
    source = FakeDownloadSource({"1": "书1"})
    info = downloader.start_batch(["1"], str(tmp_path), source=source)
    result = downloader.wait_batch(info["batch_id"], timeout=10)

    status = downloader.get_batch_status(info["batch_id"])
    status["result"]["items"][0]["ok"] = "tampered"
    result["items"][0]["ok"] = "tampered"

    fresh_status = downloader.get_batch_status(info["batch_id"])
    fresh_result = downloader.wait_batch(info["batch_id"], timeout=10)
    assert fresh_status["result"]["items"][0]["ok"] is True
    assert fresh_result["items"][0]["ok"] is True


def test_batch_history_keeps_last_n(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "MAX_BATCH_HISTORY", 2)
    source = FakeDownloadSource({str(i): f"书{i}" for i in range(1, 4)})
    batch_ids = []

    for book_id in ("1", "2", "3"):
        info = downloader.start_batch([book_id], str(tmp_path), source=source)
        downloader.wait_batch(info["batch_id"], timeout=10)
        batch_ids.append(info["batch_id"])

    assert downloader.batch_history_size() == 2
    with pytest.raises(BatchNotFoundError):
        downloader.get_batch_status(batch_ids[0])
    assert downloader.get_batch_status(batch_ids[-1])["status"] == downloader.STATUS_FINISHED


def test_start_batch_cleans_registration_when_thread_start_fails(
    monkeypatch, tmp_path
):
    def fail_start(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    source = FakeDownloadSource({"1": "书1"})

    with pytest.raises(RuntimeError):
        downloader.start_batch(["1"], str(tmp_path), source=source)

    assert downloader.batch_history_size() == 0


def test_normalize_ids_is_strict_at_downloader_boundary(tmp_path):
    source = FakeDownloadSource({"1": "书1"})
    for bad in (["abc"], ["1", "x"], ["1.5"]):
        with pytest.raises(InvalidArgumentError):
            downloader.run_batch(bad, str(tmp_path), source=source)
        with pytest.raises(InvalidArgumentError):
            downloader.start_batch(bad, str(tmp_path), source=source)


def test_emit_failure_writes_diagnostic(monkeypatch, tmp_path):
    from jml.core import diagnostics

    diagnostics.clear()

    def broken_dispatch(event_type, payload):
        raise RuntimeError("event bus broken")

    monkeypatch.setattr(events.BUS, "dispatch", broken_dispatch)
    source = FakeDownloadSource({"1": "书1"})

    result = downloader.run_batch(["1"], str(tmp_path), source=source)

    assert result["success"] == 1
    assert any("EVENT_DISPATCH_FAILED" in item for item in diagnostics.recent())


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def test_empty_book_ids_raise(tmp_path):
    with pytest.raises(InvalidArgumentError):
        downloader.run_batch([], str(tmp_path), source=FakeDownloadSource({}))
    with pytest.raises(InvalidArgumentError):
        downloader.start_batch([], str(tmp_path), source=FakeDownloadSource({}))


def test_missing_target_dir_creates_it(tmp_path):
    target = tmp_path / "nested" / "dir"
    source = FakeDownloadSource({"1": "书"})

    result = downloader.run_batch(["1"], str(target), source=source)

    assert result["success"] == 1
    assert target.is_dir()


def test_target_is_file_raises(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(InvalidArgumentError):
        downloader.run_batch(["1"], str(file_path), source=FakeDownloadSource({}))


def test_wait_unknown_batch_returns_none():
    assert downloader.wait_batch("missing") is None
