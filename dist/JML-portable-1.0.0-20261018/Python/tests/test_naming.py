"""产物命名单元测试（计划书 11.6：命名 / 剥字符 / 首尾空白与点 / 同名副本三级）。

不联网、不碰真实用户目录（tmp_path 由 conftest 提供）。
"""

from __future__ import annotations

import os
import threading

import pytest

from jml.core.naming import (
    build_archive_filename,
    reserve_archive_path,
    sanitize_filename_part,
    unique_archive_path,
)


def test_build_archive_filename_standard():
    assert build_archive_filename("标题", 123456) == "标题-JM123456.zip"


def test_build_archive_filename_strips_invalid_chars():
    """剥除 \\ / * ? : " < > | 九类非法字符。"""
    title = 'a\\b/c*d?e:f"g<h>i|j'
    assert sanitize_filename_part(title) == "abcdefghij"
    assert build_archive_filename(title, 1) == "abcdefghij-JM1.zip"


def test_build_archive_filename_strips_edges():
    """首尾空白与点全部去掉（Windows 会静默吃掉的字符）。"""
    assert sanitize_filename_part("  .标题.  ") == "标题"
    assert build_archive_filename("  .标题.  ", 1) == "标题-JM1.zip"


@pytest.mark.parametrize(
    "title",
    [None, "", "   ", "...", '\\/*?:"<>|', " . "],
    ids=["none", "empty", "spaces", "dots", "invalid_only", "dot_space"],
)
def test_build_archive_filename_unknown_title(title):
    """标题取不到 → 未知标题-{编号}.zip。"""
    assert build_archive_filename(title, "42") == "未知标题-42.zip"


def test_build_archive_filename_keeps_leading_zero_id():
    assert build_archive_filename("T", "007") == "T-JM007.zip"


def test_build_archive_filename_sanitizes_book_id_path_chars():
    """编号来自外部时也不允许把路径分隔符带进文件名（防路径穿越）。"""
    name = build_archive_filename("T", "../../evil")
    assert "/" not in name and "\\" not in name
    assert name.endswith(".zip")


def test_unique_archive_path_no_conflict(tmp_path):
    target = os.path.join(str(tmp_path), "abc.zip")
    assert unique_archive_path(str(tmp_path), "abc.zip") == target


def test_unique_archive_path_copy_three_levels(tmp_path):
    """同名副本三级命名：xxx.zip → xxx - 副本.zip → xxx - 副本 (2).zip → (3)。"""
    base = tmp_path / "xxx.zip"
    base.write_bytes(b"first")
    assert os.path.basename(unique_archive_path(str(tmp_path), "xxx.zip")) == "xxx - 副本.zip"

    (tmp_path / "xxx - 副本.zip").write_bytes(b"second")
    assert (
        os.path.basename(unique_archive_path(str(tmp_path), "xxx.zip"))
        == "xxx - 副本 (2).zip"
    )

    (tmp_path / "xxx - 副本 (2).zip").write_bytes(b"third")
    assert (
        os.path.basename(unique_archive_path(str(tmp_path), "xxx.zip"))
        == "xxx - 副本 (3).zip"
    )


def test_reserve_does_not_overwrite_existing(tmp_path):
    """已存在的同名文件内容绝不被静默覆盖（决策 #1）。"""
    existing = tmp_path / "标题-JM1.zip"
    existing.write_bytes(b"ORIGINAL")
    reserved = reserve_archive_path(str(tmp_path), "标题-JM1.zip")

    assert os.path.basename(reserved) == "标题-JM1 - 副本.zip"
    assert existing.read_bytes() == b"ORIGINAL"


def test_reserve_creates_placeholder(tmp_path):
    reserved = reserve_archive_path(str(tmp_path), "a.zip")
    assert os.path.isfile(reserved)
    assert os.path.getsize(reserved) == 0


def test_reserve_concurrent_names_are_unique(tmp_path):
    """并发占位（8 线程）不会撞名。"""
    names: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        path = reserve_archive_path(str(tmp_path), "same.zip")
        with lock:
            names.append(os.path.basename(path))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(names) == 8
    assert len(set(names)) == 8
    assert "same.zip" in names
    assert "same - 副本.zip" in names


def test_reserve_missing_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        reserve_archive_path(str(tmp_path / "does-not-exist"), "a.zip")
