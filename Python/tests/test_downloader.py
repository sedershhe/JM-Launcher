"""批量下载单元测试（零联网：可控假数据源）。

覆盖：并发上限 3、真取消（set 后在检查点中断）、单本失败隔离、
每本一个 ZIP + 三位零填充 + 校验、同名副本、事件 payload、
jmcomic 日志转发携带 batch_id、start_batch 立即返回。
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
import zipfile

import pytest
from PIL import Image

from jml.core import archive, downloader, jm_source
from jml.core.errors import ERROR_IMAGE_INVALID, BatchNotFoundError, InvalidArgumentError
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
