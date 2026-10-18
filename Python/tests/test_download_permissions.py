"""下载目录可写预检与写产物失败诊断（全部离线）。

覆盖四条硬性行为：

1. 批次开始前实测下载目录可写：不可写立即失败、批次不登记、不取专辑；
2. 探针文件 ``.jml-write-probe-<rand>.tmp`` 成功/失败都不残留；
3. ZIP/占位文件写入被拒时仍 ``PERMISSION_DENIED``，但消息带路径、
   errno/winerror、目标目录是否可写、同名文件是否被占用；
4. 写产物失败补一条 error 级日志（``写产物失败：<path>（errno=..., winerror=...）``）。

不联网：数据源用 ``tests.conftest.FakeSource``。
"""

from __future__ import annotations

import json
import os

import pytest

import jml_backend
from jml import events
from jml.core import archive, downloader, jm_source
from jml.core.errors import ERROR_PERMISSION_DENIED
from jml.core.protocol import EVENT_DOWNLOAD_LOG
from tests.conftest import FakeSource


def _probe_files(directory) -> list[str]:
    """返回目录下残留的写探针文件。"""
    return [
        entry.name
        for entry in os.scandir(str(directory))
        if entry.name.startswith(archive.WRITE_PROBE_PREFIX)
        and entry.name.endswith(archive.WRITE_PROBE_SUFFIX)
    ]


# ---------------------------------------------------------------------------
# 1. 目标目录可写预检（fail fast）
# ---------------------------------------------------------------------------


def test_start_download_fails_fast_when_target_dir_unwritable(
    monkeypatch, tmp_path
):
    """探针抛 PermissionError → start_download 立即失败、批次未登记、不取专辑。"""
    source = FakeSource({"1": "书1"})
    jm_source.set_source(source)
    target = tmp_path / "out"
    target.mkdir()

    def denied_probe(directory):
        raise PermissionError(13, "拒绝访问（测试模拟）", str(directory), 5)

    monkeypatch.setattr(archive, "write_probe", denied_probe)

    data = json.loads(jml_backend.start_download('["1"]', str(target)))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_PERMISSION_DENIED
    message = data["error_message"]
    assert "下载目录不可写" in message
    assert str(target) in message
    assert "PermissionError" in message
    assert "errno=13" in message
    assert "winerror=5" in message
    assert "请换一个目录或检查安全软件" in message

    # 关键顺序：不登记批次，也不调用数据源取专辑/图片
    assert downloader.batch_history_size() == 0
    assert source.album_calls == []
    assert source.chapters_started == 0
    assert _probe_files(target) == []


def test_run_batch_precheck_runs_before_album(monkeypatch, tmp_path):
    """同步入口同样先探针后取专辑；失败抛 PermissionError 且无产物。"""
    source = FakeSource({"1": "书1"})

    def denied_probe(directory):
        raise PermissionError(13, "拒绝访问（测试模拟）", str(directory))

    monkeypatch.setattr(archive, "write_probe", denied_probe)

    with pytest.raises(PermissionError) as info:
        downloader.run_batch(["1"], str(tmp_path), source=source)

    assert "下载目录不可写" in str(info.value)
    assert str(tmp_path) in str(info.value)
    assert source.album_calls == []
    assert list(tmp_path.glob("*.zip")) == []


# ---------------------------------------------------------------------------
# 2. 探针文件命名与清理
# ---------------------------------------------------------------------------


def test_write_probe_uses_expected_name_and_leaves_nothing(monkeypatch, tmp_path):
    seen: list[str] = []
    real_open = open

    def recording_open(path, mode="r", *args, **kwargs):
        seen.append(str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(archive, "open", recording_open, raising=False)

    archive.write_probe(str(tmp_path))

    assert len(seen) == 1
    name = os.path.basename(seen[0])
    assert name.startswith(archive.WRITE_PROBE_PREFIX)
    assert name.endswith(archive.WRITE_PROBE_SUFFIX)
    assert not os.path.exists(seen[0])
    assert _probe_files(tmp_path) == []


def test_write_probe_cleans_up_after_failure(monkeypatch, tmp_path):
    """写入阶段失败 → 探针文件必须被删除（异常原样上抛）。"""
    created: dict[str, str] = {}
    real_open = open

    def failing_open(path, mode="r", *args, **kwargs):
        created["path"] = str(path)
        with real_open(path, "w", encoding="utf-8") as handle:
            handle.write("partial")
        raise PermissionError(13, "拒绝访问（测试模拟）", str(path), 5)

    monkeypatch.setattr(archive, "open", failing_open, raising=False)

    with pytest.raises(PermissionError):
        archive.write_probe(str(tmp_path))

    assert created["path"]
    assert not os.path.exists(created["path"])
    assert _probe_files(tmp_path) == []


# ---------------------------------------------------------------------------
# 3. ZIP 写入失败：可诊断消息 + error 日志
# ---------------------------------------------------------------------------


def test_pack_permission_error_reports_details_and_logs(
    monkeypatch, tmp_path
):
    """预检通过后 pack 阶段抛 PermissionError → 该本失败但消息可定位。"""
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeSource({"1": "写失败书"})

    class DeniedZipFile:
        """模拟 ZIP 半成品已落地后被安全软件拒绝继续写。"""

        def __init__(self, file, *args, **kwargs):
            with open(file, "wb") as handle:
                handle.write(b"PARTIAL")
            raise PermissionError(13, "拒绝访问（测试模拟）", str(file), 32)

    monkeypatch.setattr(archive.zipfile, "ZipFile", DeniedZipFile)

    result = downloader.run_batch(
        ["1"], str(tmp_path), batch_id="perm-batch", source=source
    )

    assert result["failed"] == 1
    item = result["items"][0]
    assert item["ok"] is False
    assert item["error_code"] == ERROR_PERMISSION_DENIED

    message = item["error_message"]
    assert "写产物失败" in message
    assert str(tmp_path) in message
    assert "PermissionError" in message
    assert "errno=13" in message
    assert "winerror=32" in message
    assert "目标目录可写" in message
    assert ("同名文件可能被占用" in message) or ("同名文件可打开" in message)

    # download.log：必须多一条 error 级、带路径与 errno/winerror 的明细
    write_logs = [
        entry
        for entry in logs
        if entry["level"] == "error" and "写产物失败：" in entry["message"]
    ]
    assert write_logs, f"应有写产物失败 error 日志，实际: {logs}"
    assert "[PERMISSION_DENIED]" in write_logs[0]["message"]
    assert "errno=13" in write_logs[0]["message"]
    assert "winerror=32" in write_logs[0]["message"]
    assert write_logs[0]["batch_id"] == "perm-batch"

    # 半成品不留：无 .part、无 0 字节占位
    assert list(tmp_path.glob("*.zip")) == []
    assert list(tmp_path.glob("*.part")) == []


def test_describe_write_failure_reports_locked_same_name_file(
    monkeypatch, tmp_path
):
    """同名文件存在但 os.open(O_RDWR) 失败 → 提示"同名文件可能被占用"。"""
    target = tmp_path / "标题-JM1.zip.part"
    target.write_bytes(b"LOCKED")
    real_open = os.open
    absolute = os.path.abspath(str(target))

    def denied_open(path, flags, *args, **kwargs):
        if os.path.abspath(str(path)) == absolute and flags == os.O_RDWR:
            raise PermissionError(13, "拒绝访问（测试模拟）", str(path), 32)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(archive.os, "open", denied_open)

    exc = PermissionError(13, "拒绝访问（测试模拟）", str(target), 32)
    message = archive.describe_write_failure(str(target), exc)

    assert absolute in message
    assert "errno=13" in message
    assert "winerror=32" in message
    assert "目标目录可写" in message
    assert "同名文件可能被占用" in message
    assert "写产物失败" in message


def test_replace_permission_error_is_diagnosed(monkeypatch, tmp_path):
    """原子替换被拒同样走写产物诊断，错误码仍为 PERMISSION_DENIED。"""
    logs: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, logs.append)
    source = FakeSource({"1": "替换失败书"})

    def denied_replace(src, dst, *args, **kwargs):
        raise PermissionError(13, "拒绝访问（测试模拟）", str(dst), 32)

    monkeypatch.setattr(downloader.os, "replace", denied_replace)

    result = downloader.run_batch(
        ["1"], str(tmp_path), batch_id="replace-batch", source=source
    )

    item = result["items"][0]
    assert item["ok"] is False
    assert item["error_code"] == ERROR_PERMISSION_DENIED
    assert "写产物失败" in item["error_message"]
    assert "errno=13" in item["error_message"]
    assert any(
        entry["level"] == "error" and "写产物失败：" in entry["message"]
        for entry in logs
    )
    assert list(tmp_path.glob("*.zip")) == []
    assert list(tmp_path.glob("*.part")) == []


# ---------------------------------------------------------------------------
# 4. 正常路径：无额外探针残留、无回归
# ---------------------------------------------------------------------------


def test_normal_batch_succeeds_without_probe_residue(tmp_path):
    target = tmp_path / "out"
    work = tmp_path / "work"
    source = FakeSource({"1": "正常书"})

    result = downloader.run_batch(
        ["1"], str(target), source=source, work_dir=str(work)
    )

    assert result["success"] == 1
    item = result["items"][0]
    assert item["ok"] is True
    assert os.path.isfile(item["zip_path"])
    assert _probe_files(target) == []
    assert _probe_files(work) == []
    assert list(target.glob(".jml-tmp-*")) == []
