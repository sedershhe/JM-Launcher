"""jmcomic 数据源适配层单元测试（不联网：用假客户端替换实例属性）。

覆盖：搜索字段映射、详情章节映射、逐图落盘、封面 bytes、默认源单例切换。
真实网络调用由验收子代理在阶段验收时单独跑。
"""

from __future__ import annotations

import io
import logging
import os
from types import SimpleNamespace

import jmcomic
import pytest
from PIL import Image

from jml.core import jm_source
from jml.core.errors import (
    ERROR_JM,
    ERROR_NETWORK,
    InvalidCredentialsError,
    classify,
)
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


# ---------------------------------------------------------------------------
# 页数（DATA-1）：jmcomic 的 JmAlbumDetail.page_count 恒为 0，
# 真实值在专辑原始 JSON 的 total_photos 里
# ---------------------------------------------------------------------------


def _raw_album(**overrides) -> dict:
    data = {
        "id": 651712,
        "name": "原始标题",
        "author": ["作者甲"],
        "tags": ["标签A"],
        "description": "原始简介",
        "likes": "10",
        "works": [],
        "actors": [],
        "related_list": [],
        "total_views": "100",
        "comment_total": "2",
        "total_photos": 42,
        "series": [{"id": "651712", "name": "第1话", "sort": "1"}],
    }
    data.update(overrides)
    return data


class FakeApiClient(FakeClient):
    """在 FakeClient 上补出 API 客户端的原始请求能力（仍然离线）。"""

    API_ALBUM = "/album"

    def __init__(self, raw: dict | None = None) -> None:
        super().__init__()
        self.raw = _raw_album() if raw is None else raw
        self.api_urls: list[str] = []

    def append_params_to_url(self, url: str, params: dict) -> str:
        return f"{url}?id={params.get('id')}"

    def req_api(self, url: str):
        self.api_urls.append(url)
        return SimpleNamespace(encoded_data='{"code": 200}', res_data=dict(self.raw))


def test_get_album_page_count_uses_raw_total_photos():
    source = jm_source.JmcomicSource()
    client = FakeApiClient()
    source._client = client

    album = source.get_album("651712")

    assert client.api_urls == ["/album?id=651712"]
    assert album["page_count"] == 42
    assert album["title"] == "原始标题"
    assert album["author"] == "作者甲"
    assert [chapter["photo_id"] for chapter in album["chapters"]] == ["651712"]


def test_get_album_falls_back_to_public_api_when_raw_shape_changes():
    source = jm_source.JmcomicSource()
    client = FakeApiClient(_raw_album(series=None))
    source._client = client

    album = source.get_album("1")

    assert album["page_count"] == 20  # 公共接口兜底值
    assert album["title"] == "详情标题"


class FakeApiClientNoTotal(FakeApiClient):
    """原始响应缺少 total_photos 时，单章节本子按章节图片数兜底。"""

    def get_photo_detail(self, photo_id, fetch_album=True, fetch_scramble_id=True):
        return SimpleNamespace(page_arr=["00001.webp"] * 42)


def test_get_album_page_count_falls_back_when_raw_total_missing():
    source = jm_source.JmcomicSource()
    client = FakeApiClientNoTotal(_raw_album(total_photos=0))
    source._client = client

    album = source.get_album("651712")

    assert album["page_count"] == 42


class FakeZeroPageClient(FakeClient):
    """公共接口也不带页数的客户端：单章节本子用图片数兜底求和。"""

    def get_album_detail(self, book_id: str):
        return SimpleNamespace(
            album_id=str(book_id),
            name="零页数标题",
            authors=["作者甲"],
            tags=[],
            description="",
            page_count=0,
            episode_list=[("11", "1", "第1章")],
        )

    def get_photo_detail(self, photo_id, fetch_album=True, fetch_scramble_id=True):
        self.photo_id = photo_id
        return SimpleNamespace(page_arr=["00001.webp"] * 34)


def test_get_album_page_count_falls_back_to_single_chapter_images():
    source = jm_source.JmcomicSource()
    client = FakeZeroPageClient()
    source._client = client

    album = source.get_album("1")

    assert client.photo_id == "11"
    assert album["page_count"] == 34


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


# ---------------------------------------------------------------------------
# 登录（jmcomic 2.7.1：JmApiClient.login，离线假客户端）
# ---------------------------------------------------------------------------


class FakeLoginClient:
    """只实现登录相关行为的假客户端（绝不联网）。"""

    def __init__(self, error: BaseException | None = None, result=True) -> None:
        self.error = error
        self.result = result
        self.meta: dict = {}
        self.login_calls: list[tuple[str, str]] = []
        self._username = None

    def __setitem__(self, key, value):
        self.meta[key] = value

    def get_meta_data(self, key=None, dv=None):
        if key is None:
            return self.meta
        return self.meta.get(key, dv)

    def login(self, username, password):
        self.login_calls.append((username, password))
        if self.error is not None:
            raise self.error
        if self.result is False:
            return False
        self.meta["cookies"] = {"AVS": "LOGGED-IN-SESSION"}
        return SimpleNamespace(is_success=True)


def test_source_login_sets_logged_in_without_building_option():
    source = jm_source.JmcomicSource()
    client = FakeLoginClient()
    source._client = client  # 注入假客户端：不建 JmOption、不联网

    assert source.login("user_1", "PWD-123456") is True

    assert client.login_calls == [("user_1", "PWD-123456")]
    assert source.is_logged_in() is True
    assert source._option is None, "登录不得构造/写 JmOption 配置"


def test_source_login_anonymous_avs_is_not_login_proof():
    """匿名会话也有 AVS cookie，不能据此判定已登录。"""
    source = jm_source.JmcomicSource()
    client = FakeLoginClient()
    client.meta["cookies"] = {"AVS": "ANONYMOUS-AVS"}
    source._client = client

    assert source.is_logged_in() is False


def test_source_login_html_username_is_login_signal():
    source = jm_source.JmcomicSource()
    client = FakeLoginClient()
    client._username = "html_user"
    source._client = client

    assert source.is_logged_in() is True


def test_source_login_invalid_credentials_by_exception_type():
    error = jmcomic.ResponseUnexpectedException(
        "密码错误 password=LEAKED-PASSWORD",
        {"resp": SimpleNamespace(http_code=200)},
    )
    source = jm_source.JmcomicSource()
    source._client = FakeLoginClient(error=error)

    with pytest.raises(InvalidCredentialsError) as caught:
        source.login("user_1", "LEAKED-PASSWORD")

    assert "LEAKED-PASSWORD" not in str(caught.value)
    assert classify(caught.value).error_code == "INVALID_CREDENTIALS"


def test_source_login_http_5xx_is_jm_error_not_credentials():
    error = jmcomic.ResponseUnexpectedException(
        "server error",
        {"resp": SimpleNamespace(http_code=503)},
    )
    source = jm_source.JmcomicSource()
    source._client = FakeLoginClient(error=error)

    with pytest.raises(jmcomic.JmcomicException) as caught:
        source.login("user_1", "PWD-123456")

    assert not isinstance(caught.value, InvalidCredentialsError)
    assert classify(caught.value).error_code == ERROR_JM


def test_source_login_network_error_stays_network():
    error = jmcomic.RequestRetryAllFailException("全部域名失败", {})
    source = jm_source.JmcomicSource()
    source._client = FakeLoginClient(error=error)

    with pytest.raises(jmcomic.RequestRetryAllFailException):
        source.login("user_1", "PWD-123456")


def test_source_login_false_is_invalid_credentials():
    source = jm_source.JmcomicSource()
    source._client = FakeLoginClient(result=False)

    with pytest.raises(InvalidCredentialsError):
        source.login("user_1", "PWD-123456")


def test_source_logout_clears_in_memory_cookies():
    source = jm_source.JmcomicSource()
    client = FakeLoginClient()
    source._client = client
    source.login("user_1", "PWD-123456")
    assert source.is_logged_in() is True

    source.logout()

    assert source.is_logged_in() is False
    assert client.meta["cookies"] == {}
    assert client._username is None


def test_source_failed_relogin_clears_logged_in_flag():
    source = jm_source.JmcomicSource()
    client = FakeLoginClient()
    source._client = client
    source.login("user_1", "PWD-123456")
    assert source.is_logged_in() is True

    client.error = InvalidCredentialsError("账号或密码不正确")
    with pytest.raises(InvalidCredentialsError):
        source.login("user_1", "WRONG")

    assert source.is_logged_in() is False


def test_network_error_classification_for_login():
    """``RequestRetryAllFailException`` 仍按类型归为 NETWORK_ERROR。"""
    assert (
        classify(jmcomic.RequestRetryAllFailException("x", {})).error_code
        == ERROR_NETWORK
    )


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
