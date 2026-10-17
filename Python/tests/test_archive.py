"""ZIP 打包与校验单元测试（计划书 4.1 质量保证 / 11.6 条目命名）。

覆盖：三位零填充、后缀取首图、打包空列表、校验四个失败分支、成功路径。
不联网；图片用 Pillow 现场生成，测试完由 tmp_path 回收。
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from PIL import Image

from jml.core import archive
from jml.core.errors import ERROR_ARCHIVE_INVALID, ERROR_IMAGE_INVALID, ArchiveError


def _image_bytes(fmt: str = "PNG", size: tuple[int, int] = (4, 3)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def _image_file(directory, name: str, fmt: str = "PNG", size: tuple[int, int] = (4, 3)) -> str:
    path = os.path.join(str(directory), name)
    with open(path, "wb") as handle:
        handle.write(_image_bytes(fmt, size))
    return path


def test_entry_name_three_digit_padding():
    assert archive.entry_name(1, ".png") == "001.png"
    assert archive.entry_name(12, ".jpg") == "012.jpg"
    assert archive.entry_name(999, ".webp") == "999.webp"
    # 超过 999 自然进位（不再截断）
    assert archive.entry_name(1000, ".png") == "1000.png"


def test_entry_name_width_follows_total():
    """总页数 1000 时统一 4 位宽：第 1 页是 0001，最后是 1000。"""
    assert archive.entry_name(1, ".png", total=1000) == "0001.png"
    assert archive.entry_name(999, ".png", total=1000) == "0999.png"
    assert archive.entry_name(1000, ".png", total=1000) == "1000.png"
    assert archive.entry_name(1, ".png", total=12) == "001.png"


def test_entry_name_rejects_zero():
    with pytest.raises(ValueError):
        archive.entry_name(0, ".png")


def test_pack_zip_writes_padded_entries(tmp_path):
    """12 张图 → 001..012，名字与内容都可读。"""
    images = [_image_file(tmp_path, f"src_{i}.png") for i in range(1, 13)]
    target = os.path.join(str(tmp_path), "out.zip")

    count = archive.pack_zip(images, target)

    assert count == 12
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        assert names == [f"{i:03d}.png" for i in range(1, 13)]
        with Image.open(io.BytesIO(zf.read(names[0]))) as image:
            assert image.size == (4, 3)


def test_pack_zip_suffix_from_first_image(tmp_path):
    """混合后缀时统一取首图扩展名（计划书 11.6）。"""
    first = _image_file(tmp_path, "a.jpg", fmt="JPEG")
    second = _image_file(tmp_path, "b.png", fmt="PNG")
    target = os.path.join(str(tmp_path), "mixed.zip")

    archive.pack_zip([first, second], target)

    with zipfile.ZipFile(target) as zf:
        assert zf.namelist() == ["001.jpg", "002.jpg"]


def test_pack_zip_suffix_fallback(tmp_path):
    """首图无扩展名 → 回退 .jpg。"""
    path = os.path.join(str(tmp_path), "no_suffix")
    with open(path, "wb") as handle:
        handle.write(_image_bytes())
    target = os.path.join(str(tmp_path), "fallback.zip")

    archive.pack_zip([path], target)

    with zipfile.ZipFile(target) as zf:
        assert zf.namelist() == ["001.jpg"]


def test_pack_zip_empty_list_raises(tmp_path):
    with pytest.raises(ArchiveError):
        archive.pack_zip([], os.path.join(str(tmp_path), "empty.zip"))


def test_validate_zip_success(tmp_path):
    images = [_image_file(tmp_path, "1.png"), _image_file(tmp_path, "2.png")]
    target = os.path.join(str(tmp_path), "ok.zip")
    archive.pack_zip(images, target)

    result = archive.validate_zip(target, 2)

    assert result.ok is True
    assert result.error_code == ""
    assert result.reason == ""


def test_validate_zip_unopenable(tmp_path):
    """失败分支 1：不是有效 ZIP。"""
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"this is not a zip")
    result = archive.validate_zip(str(broken), 1)

    assert result.ok is False
    assert result.error_code == ERROR_ARCHIVE_INVALID
    assert result.reason == archive.REASON_UNOPENABLE


def test_validate_zip_count_mismatch(tmp_path):
    """失败分支 2：条目数与图片数不一致。"""
    images = [_image_file(tmp_path, "1.png"), _image_file(tmp_path, "2.png")]
    target = os.path.join(str(tmp_path), "count.zip")
    archive.pack_zip(images, target)

    result = archive.validate_zip(target, 3)

    assert result.ok is False
    assert result.error_code == ERROR_ARCHIVE_INVALID
    assert result.reason == archive.REASON_COUNT_MISMATCH
    assert "2" in result.error_message and "3" in result.error_message


def test_validate_zip_undecodable_image(tmp_path):
    """失败分支 3：条目内容不是图片。"""
    target = os.path.join(str(tmp_path), "bad_image.zip")
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("001.jpg", b"definitely-not-an-image")
    result = archive.validate_zip(target, 1)

    assert result.ok is False
    assert result.error_code == ERROR_IMAGE_INVALID
    assert result.reason == archive.REASON_UNDECODABLE


def test_validate_zip_zero_byte_entry(tmp_path):
    """失败分支 4：零字节条目。"""
    target = os.path.join(str(tmp_path), "empty_entry.zip")
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("001.jpg", b"")
    result = archive.validate_zip(target, 1)

    assert result.ok is False
    assert result.error_code == ERROR_IMAGE_INVALID
    assert result.reason == archive.REASON_EMPTY_ENTRY


def test_pack_and_validate(tmp_path):
    images = [_image_file(tmp_path, "1.png"), _image_file(tmp_path, "2.png")]
    target = os.path.join(str(tmp_path), "combo.zip")

    count, result = archive.pack_and_validate(images, target)

    assert count == 2
    assert result.ok is True


def test_validate_zip_encrypted_read_runtime_error_maps_to_archive_invalid(
    tmp_path, monkeypatch
):
    """加密 ZIP 读取时抛 RuntimeError → ARCHIVE_INVALID（不再逃逸）。"""
    images = [_image_file(tmp_path, "1.png")]
    target = os.path.join(str(tmp_path), "encrypted.zip")
    archive.pack_zip(images, target)

    def raise_runtime_error(self, name, pwd=None):
        raise RuntimeError("File is encrypted, password required")

    monkeypatch.setattr(zipfile.ZipFile, "read", raise_runtime_error)

    result = archive.validate_zip(target, 1)

    assert result.ok is False
    assert result.error_code == ERROR_ARCHIVE_INVALID
    assert result.reason == archive.REASON_UNOPENABLE


def test_validate_zip_memory_error_propagates(tmp_path, monkeypatch):
    """MemoryError 不能被吞成 IMAGE_INVALID，必须原样上抛。"""
    images = [_image_file(tmp_path, "1.png")]
    target = os.path.join(str(tmp_path), "oom.zip")
    archive.pack_zip(images, target)

    def raise_memory_error(self, name, pwd=None):
        raise MemoryError()

    monkeypatch.setattr(zipfile.ZipFile, "read", raise_memory_error)

    with pytest.raises(MemoryError):
        archive.validate_zip(target, 1)
