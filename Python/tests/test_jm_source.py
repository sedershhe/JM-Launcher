"""jmcomic 数据源适配层单元测试（不联网：用假客户端替换实例属性）。

覆盖：搜索字段映射、详情章节映射、逐图落盘、封面 bytes、默认源单例切换。
真实网络调用由验收子代理在阶段验收时单独跑。
"""

from __future__ import annotations

import io
import logging
import os
from types import SimpleNamespace

import pytest
from PIL import Image

from jml.core import jm_source
from jml.core.protocol import EVENT_DOWNLOAD_LOG
from jml.host import events


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), (5, 6, 7)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeImage:
    def __init__(self, suffix: str = ".png") -> None:
        self.img_file_suffix = suffix
        self.download_url = "https://example.invalid/1.png"


class FakePage:
    def __init__(self) -> None:
        self.content = [
            (
                "123",
                {
                    "name": "搜索标题",
                    "author": "作者A",
                    "tags": ["全彩", "中文"],
                    "page_count": "8",
                },
            )
        ]
        self.page_count = 4


class FakeClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.photo_id: str | None = None
        self.cover_id: str | None = None
        self.cover_url: str | None = None

    def search_site(self, search_query: str, page: int):
        self.search_calls.append((search_query, page))
        return FakePage()

    def get_album_detail(self, book_id: str):
        return SimpleNamespace(
            album_id=str(book_id),
            name="详情标题",
            authors=["作者甲", "作者乙"],
            tags=["标签"],
            description="简介",
            page_count=20,
            episode_list=[("11", "1", "第1章"), ("12", "2", "第2章")],
        )

    def get_photo_detail(self, photo_id: str):
        self.photo_id = photo_id
        return [FakeImage(".png"), FakeImage(".png")]

    def download_by_image_detail(self, image, target: str, decode_image: bool = True):
        with open(target, "wb") as handle:
            handle.write(_png_bytes())

    def get_jm_image(self, url: str):
        self.cover_url = url
        return SimpleNamespace(content=b"RAW-COVER", require_success=lambda: None)


@pytest.fixture
def source_with_fake_client():
    source = jm_source.JmcomicSource()
    client = FakeClient()
    source._client = client  # 注入假客户端：仅测适配层映射，绝不联网
    return source, client


def test_search_mapping(source_with_fake_client):
    source, client = source_with_fake_client

    result = source.search("关键词", 2)

    assert client.search_calls == [("关键词", 2)]
    assert result["keyword"] == "关键词"
    assert result["page"] == 2
    assert result["total_pages"] == 4
    item = result["results"][0]
    assert item["book_id"] == "123"
    assert item["title"] == "搜索标题"
    assert item["author"] == "作者A"
    assert item["page_count"] == 8
    assert item["tags"] == ["全彩", "中文"]
    assert item["cover_url"]


def test_get_album_mapping(source_with_fake_client):
    source, _ = source_with_fake_client

    album = source.get_album("1")

    assert album["book_id"] == "1"
    assert album["title"] == "详情标题"
    assert album["author"] == "作者甲, 作者乙"
    assert album["page_count"] == 20
    assert album["tags"] == ["标签"]
    assert album["description"] == "简介"
    assert [chapter["photo_id"] for chapter in album["chapters"]] == ["11", "12"]
    assert [chapter["index"] for chapter in album["chapters"]] == [1, 2]


def test_iter_chapter_images_downloads_each(tmp_path, source_with_fake_client):
    source, client = source_with_fake_client
    chapter = {"photo_id": "11", "index": 1, "title": "第1章"}

    paths = list(source.iter_chapter_images("1", chapter, str(tmp_path / "chapter")))

    assert client.photo_id == "11"
    assert len(paths) == 2
    for path in paths:
        assert os.path.isfile(path)
        with Image.open(path) as image:
            assert image.size == (2, 3)


def test_iter_chapter_images_missing_photo_id(tmp_path, source_with_fake_client):
    source, _ = source_with_fake_client
    from jml.core.errors import InvalidArgumentError

    with pytest.raises(InvalidArgumentError):
        list(source.iter_chapter_images("1", {"photo_id": ""}, str(tmp_path)))


def test_cover_bytes(source_with_fake_client):
    source, client = source_with_fake_client

    data = source.cover_bytes("1")

    assert data == b"RAW-COVER"
    assert client.cover_url is not None and "/1" in client.cover_url


def test_default_source_singleton_switch():
    jm_source.reset_source()
    first = jm_source.get_source()
    assert isinstance(first, jm_source.JmcomicSource)
    assert jm_source.get_source() is first

    fake = object()
    jm_source.set_source(fake)
    assert jm_source.get_source() is fake

    jm_source.reset_source()
    assert jm_source.get_source() is not fake


def test_exception_rules_cover_jmcomic_types():
    import jmcomic

    rules = jm_source.exception_rules()
    mapping = dict(rules)

    assert mapping[jmcomic.MissingAlbumPhotoException] == "NOT_FOUND"
    assert mapping[jmcomic.RequestRetryAllFailException] == "NETWORK_ERROR"
    assert mapping[jmcomic.PartialDownloadFailedException] == "DOWNLOAD_PARTIAL"
    assert mapping[jmcomic.ResponseUnexpectedException] == "RESPONSE_INVALID"
    assert mapping[jmcomic.JmcomicException] == "JM_ERROR"


def test_jmcomic_version_probe():
    version = jm_source.jmcomic_version()
    assert version and isinstance(version, str)


def test_jmcomic_logger_capture_masks_and_emits_event():
    """WARNING+ 转脱敏 download.log；INFO/DEBUG 只进诊断，不逐条发事件。"""
    from jml.core import diagnostics

    logger = logging.getLogger("jmcomic")
    logger.handlers.clear()
    jm_source._JM_LOG_INSTALLED = False
    diagnostics.clear()
    received: list[dict] = []
    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, received.append)

    jm_source._install_log_capture()
    logger.info("password=INFO-SECRET")
    assert received == [], "INFO 不应逐条转事件"
    info_records = diagnostics.recent()
    assert any("JMCOMIC_LOG" in item for item in info_records)
    assert all("INFO-SECRET" not in item for item in info_records)

    logger.warning("password=WARN-SECRET token=abc123")
    assert received, "jmcomic WARNING 应转成 download.log 事件"
    payload = received[-1]
    assert payload["type"] == EVENT_DOWNLOAD_LOG
    assert payload["batch_id"] == ""  # 非下载线程：空串（契约已写明）
    assert "WARN-SECRET" not in payload["message"]
    assert "abc123" not in payload["message"]
    assert "***" in payload["message"]
    assert logger.propagate is False
    assert not any(
        isinstance(handler, logging.StreamHandler) for handler in logger.handlers
    )
