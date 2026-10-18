"""ZIP 浏览插件测试（阶段 4-2，全部离线）。

覆盖：
- 用**临时插件目录 + 真实 loader** 装配仓库里的真实插件（复制后 bootstrap，
  不读写真实用户目录；APPDATA 由 conftest 重定向到临时目录）；
- 页面首个状态「选择文件夹…」+ ``allow_folder_choice=true``；
- 列 ZIP、文件名自然排序（``2.zip`` < ``10.zip``）；
- 选中 ZIP 后包内条目自然排序（``999`` 在 ``1000`` 之前）、翻页取图 bytes 可解码；
- **不写磁盘**：目录里只有原 ZIP，插件数据目录为空；
- 五条错误文案**逐字**（计划书 11.6）经 ``plugin_page_load`` / 服务实测；
- ``archive_reader/1`` 服务：四个方法、线程安全、不暴露宿主对象、可跨插件解析；
- last_folder 记住目录并在重新装配后仍生效；
- 仓库内离线数据 ``Draw/zip-fixture``（存在时）可浏览。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import threading
import zipfile

import pytest

import jml_backend
from jml.core.errors import (
    ArchiveClosedError,
    ArchiveError,
    ArchiveNotFoundError,
    PageOutOfRangeError,
)
from jml.host import loader
from jml.host import registry as service_registry

PLUGIN_ID = "zip_browser"
PAGE_ID = "zip_browser.main"
SERVICE_NAME = "archive_reader"
SERVICE_VERSION = 1

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PYTHON_ROOT = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_PYTHON_ROOT)
_PLUGIN_SRC = os.path.join(_REPO_ROOT, "plugins", PLUGIN_ID)
_FIXTURE_DIR = os.path.join(_REPO_ROOT, "Draw", "zip-fixture")

#: 五条逐字锁定的错误文案（与计划书 11.6 / 插件常量一致）
MSG_ARCHIVE_MISSING = "压缩包不存在"
MSG_ARCHIVE_INVALID = "不是有效的 ZIP 压缩包"
MSG_NO_IMAGES = "压缩包中没有可浏览的图片"
MSG_ARCHIVE_CLOSED = "压缩包已关闭"
MSG_PAGE_OUT_OF_RANGE = "页码超出范围"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend_state():
    """bootstrap 幂等标记在 jml_backend._STATE 里，跨用例必须清掉。"""
    jml_backend.reset_host_state()
    yield
    jml_backend.reset_host_state()


@pytest.fixture
def plugins_root(tmp_path):
    """把真实插件复制到临时 plugins 目录，返回该目录路径。"""
    assert os.path.isdir(_PLUGIN_SRC), f"插件源码目录不存在: {_PLUGIN_SRC}"
    root = tmp_path / "plugins"
    root.mkdir()
    shutil.copytree(_PLUGIN_SRC, root / PLUGIN_ID)
    return str(root)


@pytest.fixture
def installed(plugins_root):
    """用真实 loader 装配插件，返回 bootstrap 报告。"""
    report = json.loads(jml_backend.bootstrap(plugins_root))
    assert report["ok"] is True, report
    assert [item["id"] for item in report["loaded"]] == [PLUGIN_ID], report
    return report


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def png_bytes(width: int, height: int, color: tuple[int, int, int] = (20, 120, 200)) -> bytes:
    """生成指定尺寸的 PNG（Pillow，离线）。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def write_zip(path: str | os.PathLike, entries: dict[str, bytes]) -> str:
    """按给定条目顺序写一个 ZIP，返回路径。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return os.fspath(path)


def image_zip(path: str | os.PathLike, sizes: dict[str, tuple[int, int]]) -> str:
    """写一个图片 ZIP（条目名 → 像素尺寸）。"""
    entries = {
        name: png_bytes(width, height, (index * 10 % 255, 80, 160))
        for index, (name, (width, height)) in enumerate(sizes.items())
    }
    return write_zip(path, entries)


def load_page(**params) -> dict:
    """经宿主入口载入页面，返回解析后的 JSON。"""
    raw = jml_backend.plugin_page_load(
        PLUGIN_ID, PAGE_ID, json.dumps(params, ensure_ascii=False)
    )
    return json.loads(raw)


def load_image(index: int):
    """经宿主入口取页面图片 bytes（失败为 None）。"""
    return jml_backend.plugin_page_image(PLUGIN_ID, PAGE_ID, index)


def reader_service():
    """解析 archive_reader/1 服务对象（模拟 zip_web 等其它插件的取法）。"""
    return service_registry.resolve(SERVICE_NAME, SERVICE_VERSION)


def data_dir() -> str:
    """宿主应给本插件创建的数据目录（位于隔离后的 APPDATA 下）。"""
    return os.path.join(os.environ["APPDATA"], "JML", "plugins", PLUGIN_ID, "data")


def plugin_module():
    """取 loader 导入的插件入口模块（用于单测纯函数与模拟竞态）。"""
    name = f"_jml_plugin_{PLUGIN_ID}"
    assert name in sys.modules, "插件尚未装配或模块已被清理"
    return sys.modules[name]


def decode_size(raw: bytes) -> tuple[int, int]:
    """用 Pillow 解码 bytes 并返回像素尺寸（验证"图片可解码"）。"""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        return image.size


# ---------------------------------------------------------------------------
# manifest / 服务注册
# ---------------------------------------------------------------------------


def test_manifest_declares_browser_page_and_service(installed):
    loaded = {item["id"]: item for item in installed["loaded"]}
    assert PLUGIN_ID in loaded
    plugin = loaded[PLUGIN_ID]
    assert plugin["name"] == "ZIP 浏览"
    assert plugin["version"] == "1.0.0"
    assert plugin["pages"] == [
        {"id": PAGE_ID, "title": "ZIP 浏览", "template": "browser"}
    ]
    assert installed["services"][SERVICE_NAME] == SERVICE_VERSION
    assert installed["failed"] == []


def test_service_object_is_registered_and_complete(installed):
    reader = reader_service()
    for method in ("list_images", "read_image", "close", "is_open"):
        assert callable(getattr(reader, method, None)), method


def test_service_does_not_expose_host_objects(installed):
    from jml.host.plugin_context import PluginContext

    reader = reader_service()
    for name in ("ctx", "registry", "bus", "BUS", "REGISTRY", "loader"):
        assert not hasattr(reader, name)
    for value in vars(reader).values():
        assert not isinstance(value, PluginContext)
    # 只保存句柄与条目表，不保存宿主对象
    assert set(vars(reader)) == {"_lock", "_archives", "_entries"}
    assert isinstance(reader._lock, type(threading.RLock()))


# ---------------------------------------------------------------------------
# 页面首个状态 / 列 ZIP / 自然排序
# ---------------------------------------------------------------------------


def test_first_state_asks_for_folder(installed):
    data = load_page()

    assert data["ok"] is True
    assert data["template"] == "browser"
    assert data["title"] == "ZIP 浏览"
    payload = data["data"]
    assert payload["items"] == []
    assert payload["folder"] == ""
    assert payload["allow_folder_choice"] is True
    assert payload["empty_text"] == "选择文件夹…"


def test_lists_zip_files_with_natural_sort(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    for name in ("10.zip", "2.zip", "1.zip"):
        image_zip(folder / name, {"1.png": (8, 8)})
    (folder / "notes.txt").write_text("不是压缩包", encoding="utf-8")
    (folder / "子目录.zip").mkdir()  # 名字像 ZIP 的目录必须被忽略

    data = load_page(folder=str(folder))

    assert data["ok"] is True
    payload = data["data"]
    names = [item["name"] for item in payload["items"]]
    assert names == ["1.zip", "2.zip", "10.zip"]
    assert [item["path"] for item in payload["items"]] == [
        os.path.join(str(folder), name) for name in names
    ]
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert payload["total"] == 3
    assert payload["total_pages"] == 1
    assert payload["item_index"] == -1
    assert payload["image_count"] == 0
    assert payload["folder"] == os.path.abspath(str(folder))
    assert payload["allow_folder_choice"] is True
    assert payload["empty_text"] == ""


def test_natural_sort_puts_999_before_1000():
    assert natural_key_module()("999.png") < natural_key_module()("1000.png")
    names = ["10.zip", "2.zip", "1.zip", "a10b2.zip"]
    assert sorted(names, key=natural_key_module()) == [
        "1.zip",
        "2.zip",
        "10.zip",
        "a10b2.zip",
    ]
    assert sorted(["B.zip", "a.zip"], key=natural_key_module()) == ["a.zip", "B.zip"]


_NATURAL_MODULE = None


def natural_key_module():
    """按文件路径导入插件 main.py（不装配宿主，只测纯函数）。"""
    global _NATURAL_MODULE
    if _NATURAL_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "_zip_browser_main_unit", os.path.join(_PLUGIN_SRC, "main.py")
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _NATURAL_MODULE = module
    return _NATURAL_MODULE.natural_key


def test_zip_list_pagination(installed, tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(1, 26):
        image_zip(folder / f"{index:02d}.zip", {"1.png": (4, 4)})

    first = load_page(folder=str(folder))["data"]
    assert first["total"] == 25
    assert first["total_pages"] == 2
    assert len(first["items"]) == 20
    assert first["items"][0]["name"] == "01.zip"
    assert first["items"][-1]["name"] == "20.zip"

    second = load_page(folder=str(folder), page=2)["data"]
    assert second["page"] == 2
    assert len(second["items"]) == 5
    assert second["items"][0]["name"] == "21.zip"

    # 第 2 页的下标是"当前页 items 的下标"（C# 只回传列表条目下标）
    selected = load_page(folder=str(folder), page=2, item_index=0, image_index=0)
    assert selected["ok"] is True
    assert selected["data"]["item_index"] == 0
    assert selected["data"]["image_count"] == 1


# ---------------------------------------------------------------------------
# 包内翻页取图（不落盘）
# ---------------------------------------------------------------------------


def test_select_zip_returns_image_pagination_metadata(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    image_zip(folder / "1.zip", {"1.png": (40, 30), "2.png": (60, 50), "10.png": (80, 70)})
    image_zip(folder / "2.zip", {"1.png": (24, 24)})

    listing = load_page(folder=str(folder))["data"]
    assert [item["name"] for item in listing["items"]] == ["1.zip", "2.zip"]

    selected = load_page(folder=str(folder), item_index=0, image_index=0)["data"]
    assert selected["item_index"] == 0
    assert selected["image_index"] == 0
    assert selected["image_count"] == 3
    assert [item["name"] for item in selected["items"]] == ["1.zip", "2.zip"]
    assert selected["total"] == 2

    second = load_page(folder=str(folder), item_index=1, image_index=0)["data"]
    assert second["item_index"] == 1
    assert second["image_count"] == 1


def test_page_image_bytes_are_decodable_and_naturally_ordered(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    image_zip(
        folder / "1.zip",
        {"10.png": (80, 70), "2.png": (60, 50), "1.png": (40, 30)},
    )
    before = sorted(os.listdir(folder))

    data = load_page(folder=str(folder), item_index=0, image_index=0)
    assert data["ok"] is True
    assert data["data"]["image_count"] == 3

    first = load_image(0)
    assert isinstance(first, bytes) and first
    assert decode_size(first) == (40, 30)  # 1.png（不是 10.png）

    second = load_image(1)
    assert decode_size(second) == (60, 50)

    third = load_image(2)
    assert decode_size(third) == (80, 70)

    # 不写到磁盘：目录内容不变，插件数据目录为空
    assert sorted(os.listdir(folder)) == before
    assert os.listdir(data_dir()) == []

    # 越界取图 → None（C# 显示"图片加载失败"，不弹错误框）
    assert load_image(3) is None
    assert load_image(-1) is None


def test_zip_entries_sort_999_before_1000(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    image_zip(
        folder / "1.zip",
        {"1000.png": (10, 10), "999.png": (9, 9), "1001.png": (11, 11)},
    )

    reader = reader_service()
    names = reader.list_images(os.path.join(str(folder), "1.zip"))
    assert names == ["999.png", "1000.png", "1001.png"]

    data = load_page(folder=str(folder), item_index=0, image_index=0)["data"]
    assert data["image_count"] == 3
    assert decode_size(load_image(0)) == (9, 9)
    assert decode_size(load_image(1)) == (10, 10)


# ---------------------------------------------------------------------------
# 五条错误文案（逐字）
# ---------------------------------------------------------------------------


def test_error_invalid_zip_is_verbatim(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    (folder / "broken.zip").write_bytes(b"this is not a zip file")

    data = load_page(folder=str(folder), item_index=0, image_index=0)

    assert data["ok"] is False
    assert data["error_code"] == "ARCHIVE_INVALID"
    assert data["error_message"] == MSG_ARCHIVE_INVALID


def test_error_zip_without_images_is_verbatim(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    write_zip(folder / "no_images.zip", {"readme.txt": b"no images"})

    data = load_page(folder=str(folder), item_index=0, image_index=0)

    assert data["ok"] is False
    assert data["error_code"] == "ARCHIVE_INVALID"
    assert data["error_message"] == MSG_NO_IMAGES


def test_error_archive_missing_is_verbatim(installed, tmp_path, monkeypatch):
    """列目录与打开之间的删除竞态（选中瞬间文件被移动/删除）。"""
    folder = tmp_path / "zips"
    folder.mkdir()
    real_zip = image_zip(folder / "1.zip", {"1.png": (8, 8)})
    gone_zip = os.path.join(str(folder), "gone.zip")

    module = plugin_module()
    original = module.list_zip_files

    def fake_list(folder_path: str):
        if folder_path and os.path.isdir(folder_path):
            return [
                {"name": "gone.zip", "path": gone_zip},
                {"name": "1.zip", "path": real_zip},
            ]
        return original(folder_path)

    monkeypatch.setattr(module, "list_zip_files", fake_list)

    data = load_page(folder=str(folder), item_index=0, image_index=0)

    assert data["ok"] is False
    assert data["error_code"] == "FILE_NOT_FOUND"
    assert data["error_message"] == MSG_ARCHIVE_MISSING


def test_error_archive_closed_is_verbatim_and_recoverable(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    path = image_zip(folder / "1.zip", {"1.png": (8, 8), "2.png": (9, 9)})

    assert load_page(folder=str(folder), item_index=0, image_index=0)["ok"] is True

    # 外部（例如 ZIP 网页浏览插件）关闭句柄后再翻页 → 明确的"压缩包已关闭"
    reader_service().close(path)
    closed = load_page(folder=str(folder), item_index=0, image_index=1)
    assert closed["ok"] is False
    assert closed["error_code"] == "ARCHIVE_CLOSED"
    assert closed["error_message"] == MSG_ARCHIVE_CLOSED

    # 回列表会释放选择；再双击重新打开即可恢复
    assert load_page(folder=str(folder), item_index=-1)["ok"] is True
    reopened = load_page(folder=str(folder), item_index=0, image_index=1)
    assert reopened["ok"] is True
    assert decode_size(load_image(1)) == (9, 9)


def test_error_page_out_of_range_is_verbatim(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    image_zip(folder / "1.zip", {"1.png": (8, 8), "2.png": (9, 9)})

    overflow = load_page(folder=str(folder), item_index=0, image_index=2)
    assert overflow["ok"] is False
    assert overflow["error_code"] == "PAGE_OUT_OF_RANGE"
    assert overflow["error_message"] == MSG_PAGE_OUT_OF_RANGE

    negative = load_page(folder=str(folder), item_index=0, image_index=-1)
    assert negative["ok"] is False
    assert negative["error_message"] == MSG_PAGE_OUT_OF_RANGE


# ---------------------------------------------------------------------------
# archive_reader/1 服务
# ---------------------------------------------------------------------------


def test_service_lifecycle_and_bytes(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    path = image_zip(folder / "1.zip", {"10.png": (80, 70), "2.png": (60, 50)})
    reader = reader_service()

    assert reader.is_open(path) is False
    assert reader.list_images(path) == ["2.png", "10.png"]
    assert reader.is_open(path) is True
    assert decode_size(reader.read_image(path, 0)) == (60, 50)
    assert decode_size(reader.read_image(path, 1)) == (80, 70)

    reader.close(path)
    assert reader.is_open(path) is False
    reader.close(path)  # 幂等
    assert reader.is_open(path) is False

    with pytest.raises(ArchiveClosedError) as closed:
        reader.read_image(path, 0)
    assert str(closed.value) == MSG_ARCHIVE_CLOSED


def test_service_error_types_and_verbatim_messages(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    path = image_zip(folder / "1.zip", {"1.png": (8, 8)})
    broken = folder / "broken.zip"
    broken.write_bytes(b"not a zip")
    empty = write_zip(folder / "no_images.zip", {"a.txt": b"x"})
    reader = reader_service()

    with pytest.raises(ArchiveNotFoundError) as missing:
        reader.list_images(os.path.join(str(folder), "nope.zip"))
    assert str(missing.value) == MSG_ARCHIVE_MISSING

    with pytest.raises(ArchiveError) as invalid:
        reader.list_images(str(broken))
    assert str(invalid.value) == MSG_ARCHIVE_INVALID

    with pytest.raises(ArchiveError) as no_image:
        reader.list_images(empty)
    assert str(no_image.value) == MSG_NO_IMAGES

    reader.list_images(path)
    for bad_index in (1, -1, "x", None):
        with pytest.raises(PageOutOfRangeError) as out:
            reader.read_image(path, bad_index)
        assert str(out.value) == MSG_PAGE_OUT_OF_RANGE


def test_service_is_thread_safe(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    path = image_zip(folder / "1.zip", {"1.png": (8, 8), "2.png": (9, 9), "3.png": (10, 10)})
    reader = reader_service()
    names = reader.list_images(path)
    expected = reader.read_image(path, 2)

    results: list[bytes] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(20):
            try:
                data = reader.read_image(path, 2)
                along = reader.list_images(path)
                is_open = reader.is_open(path)
            except BaseException as exc:  # noqa: BLE001 - 线程内异常收集
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(data)
                assert along == names
                assert is_open is True

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 8 * 20
    assert all(data == expected for data in results)
    assert decode_size(expected) == (10, 10)


# ---------------------------------------------------------------------------
# last_folder 记住目录
# ---------------------------------------------------------------------------


def test_folder_is_remembered_and_survives_reassembly(installed, plugins_root, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    image_zip(folder / "1.zip", {"1.png": (8, 8)})

    load_page(folder=str(folder))

    store = loader.config_for(PLUGIN_ID)
    assert store is not None
    assert store.get("last_folder") == os.path.abspath(str(folder))

    # 重新装配（模拟重启插件体系）：不带 folder 也要回到上次目录
    jml_backend.reset_host_state()
    report = json.loads(jml_backend.bootstrap(plugins_root))
    assert report["ok"] is True

    data = load_page()["data"]
    assert data["folder"] == os.path.abspath(str(folder))
    assert [item["name"] for item in data["items"]] == ["1.zip"]


def test_missing_folder_is_reported_as_empty_state(installed, tmp_path):
    missing = tmp_path / "no_such_folder"

    data = load_page(folder=str(missing))["data"]

    assert data["items"] == []
    assert data["folder"] == os.path.abspath(str(missing))
    assert data["empty_text"] == "文件夹不存在，请重新选择"


# ---------------------------------------------------------------------------
# 仓库离线数据（Draw/zip-fixture）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.isdir(_FIXTURE_DIR),
    reason="缺少离线测试数据 Draw/zip-fixture（由 Draw/zip-fixture/_make_fixtures.py 生成）",
)
def test_repo_fixture_folder_browsing(installed):
    data = load_page(folder=_FIXTURE_DIR)
    assert data["ok"] is True
    payload = data["data"]
    assert [item["name"] for item in payload["items"]] == [
        "1.zip",
        "2.zip",
        "10.zip",
        "broken.zip",
        "no_images.zip",
    ]

    selected = load_page(folder=_FIXTURE_DIR, item_index=0, image_index=0)
    assert selected["ok"] is True
    assert selected["data"]["image_count"] == 3
    assert decode_size(load_image(0)) == (40, 30)
    assert decode_size(load_image(1)) == (60, 50)
    assert decode_size(load_image(2)) == (80, 70)

    # 同一个目录里的坏包/无图包给出锁定的错误文案
    broken = load_page(folder=_FIXTURE_DIR, item_index=3, image_index=0)
    assert broken["error_message"] == MSG_ARCHIVE_INVALID
    no_image = load_page(folder=_FIXTURE_DIR, item_index=4, image_index=0)
    assert no_image["error_message"] == MSG_NO_IMAGES
