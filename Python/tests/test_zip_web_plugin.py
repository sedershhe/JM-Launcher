"""ZIP 网页浏览插件测试（阶段 4-3，全部离线、只连本机回环）。

覆盖：
- manifest：id / name / version / action 页面 / ``requires archive_reader/1``；
- 缺服务：临时插件目录只放 zip_web → bootstrap 的 failed 里是该插件与
  "缺少服务 archive_reader" 原因（明确失败，不崩溃）；
- 页面状态：未选目录 / 已选目录 / 正在浏览：端口 / 已停止；动作随状态切换；
- 真实 HTTP：127.0.0.1 + 端口 0 自动分配、200 HTML（「第 1 页 / 共 3 页」与
  3 个 ``<img>``）、``/img/0..2`` 原始字节可被 Pillow 解码、Content-Type 正确；
- stop：``shutdown()`` + ``server_close()`` 后连接失败、端口可重新绑定；
- 再次 start 自动分配端口并继续可读；换包/换目录释放旧服务；
- 只监听 127.0.0.1（断言 ``server.server_address[0]``）；
- ``stop_all()`` 兜底释放；last_folder 记忆与重新装配；
- 仓库离线数据 ``Draw/zip-fixture`` 的 1.zip（3 张图）。

全程不联网外网、不启动 JML.exe；APPDATA 由 conftest 重定向到临时目录。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import urllib.error
import urllib.request

import pytest

import jml_backend
from jml.host import loader

PLUGIN_ID = "zip_web"
PAGE_ID = "zip_web.main"
BROWSER_ID = "zip_browser"
SERVICE_NAME = "archive_reader"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PYTHON_ROOT = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_PYTHON_ROOT)
_PLUGINS_ROOT = os.path.join(_REPO_ROOT, "plugins")
_ZIP_WEB_SRC = os.path.join(_PLUGINS_ROOT, PLUGIN_ID)
_ZIP_BROWSER_SRC = os.path.join(_PLUGINS_ROOT, BROWSER_ID)
_FIXTURE_DIR = os.path.join(_REPO_ROOT, "Draw", "zip-fixture")

#: 只走直连（不读系统代理），保证访问的是本机回环服务
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: stop 后等待端口释放的最长时间（毫秒）
_RELEASE_TIMEOUT_MS = 5000


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend_state():
    """bootstrap 幂等标记在 jml_backend._STATE 里，跨用例必须清掉。"""
    jml_backend.reset_host_state()
    yield
    # 先停 HTTP 服务（插件模块此时仍在 sys.modules 里），再让 conftest 清宿主
    module = sys.modules.get(f"_jml_plugin_{PLUGIN_ID}")
    if module is not None:
        try:
            module.stop_all()
        except Exception:
            pass
    jml_backend.reset_host_state()


@pytest.fixture
def plugins_root(tmp_path):
    """把 zip_browser + zip_web 两个真实插件复制到临时 plugins 目录。"""
    assert os.path.isdir(_ZIP_WEB_SRC), f"插件源码目录不存在: {_ZIP_WEB_SRC}"
    assert os.path.isdir(_ZIP_BROWSER_SRC), f"插件源码目录不存在: {_ZIP_BROWSER_SRC}"
    root = tmp_path / "plugins"
    root.mkdir()
    shutil.copytree(_ZIP_BROWSER_SRC, root / BROWSER_ID)
    shutil.copytree(_ZIP_WEB_SRC, root / PLUGIN_ID)
    return str(root)


@pytest.fixture
def only_zip_web_root(tmp_path):
    """只放 zip_web（缺 archive_reader 服务提供者）。"""
    root = tmp_path / "plugins"
    root.mkdir()
    shutil.copytree(_ZIP_WEB_SRC, root / PLUGIN_ID)
    return str(root)


@pytest.fixture
def installed(plugins_root):
    """装配 zip_browser（提供 archive_reader/1）+ zip_web。"""
    report = json.loads(jml_backend.bootstrap(plugins_root))
    assert report["ok"] is True, report
    loaded = {item["id"]: item for item in report["loaded"]}
    assert set(loaded) == {BROWSER_ID, PLUGIN_ID}, report
    assert report["failed"] == [], report
    return report


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def load_page(**params) -> dict:
    raw = jml_backend.plugin_page_load(
        PLUGIN_ID, PAGE_ID, json.dumps(params, ensure_ascii=False)
    )
    return json.loads(raw)


def run_action(action_id: str, payload: dict | None = None) -> dict:
    raw = jml_backend.plugin_page_action(
        PLUGIN_ID,
        PAGE_ID,
        action_id,
        json.dumps(payload or {}, ensure_ascii=False),
    )
    return json.loads(raw)


def plugin_module():
    """取 loader 导入的插件入口模块（用于 stop_all / 内部回环断言）。"""
    name = f"_jml_plugin_{PLUGIN_ID}"
    assert name in sys.modules, "插件尚未装配或模块已被清理"
    return sys.modules[name]


def unit_module():
    """按文件路径导入插件 main.py（只测纯函数，不装配宿主）。"""
    spec = importlib.util.spec_from_file_location(
        "_zip_web_main_unit", os.path.join(_ZIP_WEB_SRC, "main.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch(url: str):
    with _OPENER.open(url, timeout=5) as response:
        return response.status, response.headers, response.read()


def decode_size(raw: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        return image.size


def assert_port_rebindable(port: int) -> None:
    """端口释放断言：能重新 bind + listen（stop 后必须通过）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
        probe.listen(1)
    finally:
        probe.close()


def assert_connection_refused(port: int) -> None:
    with pytest.raises(OSError):
        probe = socket.create_connection(("127.0.0.1", port), timeout=1)
        probe.close()


def image_zip(path: str | os.PathLike, sizes: dict[str, tuple[int, int]]) -> str:
    """写一个图片 ZIP（条目名 → 像素尺寸），供本文件自造测试数据。"""
    import zipfile

    from PIL import Image

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, (name, size) in enumerate(sizes.items()):
            buffer = io.BytesIO()
            Image.new(
                "RGB", size, (index * 40 % 255, 80, 160)
            ).save(buffer, format="PNG")
            archive.writestr(name, buffer.getvalue())
    return os.fspath(path)


# ---------------------------------------------------------------------------
# manifest / 缺服务
# ---------------------------------------------------------------------------


def test_manifest_declares_action_page_and_requires_service(installed):
    loaded = {item["id"]: item for item in installed["loaded"]}
    assert PLUGIN_ID in loaded
    plugin = loaded[PLUGIN_ID]
    assert plugin["name"] == "ZIP 网页浏览"
    assert plugin["version"] == "1.0.0"
    assert plugin["pages"] == [
        {"id": PAGE_ID, "title": "ZIP 网页浏览", "template": "action"}
    ]
    assert installed["services"][SERVICE_NAME] == 1
    assert plugin["development_sample"] is False


def test_manifest_json_matches_task_contract():
    with open(os.path.join(_ZIP_WEB_SRC, "plugin.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)

    assert manifest["id"] == "zip_web"
    assert manifest["name"] == "ZIP 网页浏览"
    assert manifest["version"] == "1.0.0"
    assert manifest["api_version"] == 1
    assert manifest["services"]["provides"] == []
    assert manifest["services"]["requires"] == [
        {"name": "archive_reader", "version": 1}
    ]
    assert manifest["pages"] == [
        {"id": PAGE_ID, "title": "ZIP 网页浏览", "template": "action"}
    ]

    with open(
        os.path.join(_ZIP_WEB_SRC, "config.schema.json"), encoding="utf-8"
    ) as handle:
        schema = json.load(handle)
    assert schema["last_folder"]["type"] == "string"
    assert schema["last_folder"]["default"] == ""


def test_missing_archive_reader_service_fails_plugin_not_crash(only_zip_web_root):
    report = json.loads(jml_backend.bootstrap(only_zip_web_root))

    assert report["ok"] is True, report
    assert report["loaded"] == []
    failed = {item["id"]: item["reason"] for item in report["failed"]}
    assert PLUGIN_ID in failed
    assert "缺少服务 archive_reader" in failed[PLUGIN_ID]
    # 明确失败：页面入口按"插件未加载"返回 NOT_FOUND，而不是抛异常
    page = load_page()
    assert page["ok"] is False
    assert page["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 页面状态
# ---------------------------------------------------------------------------


def test_first_state_asks_for_folder_and_offers_start(installed):
    data = load_page()

    assert data["ok"] is True
    assert data["template"] == "action"
    assert data["title"] == "ZIP 网页浏览"
    payload = data["data"]
    assert payload["state"] == "idle"
    assert payload["status_text"] == "未选择目录"
    assert payload["items"] == []
    assert payload["url"] == ""
    assert payload["port"] == 0
    assert payload["folder"] == ""
    assert payload["allow_folder_choice"] is True
    assert payload["empty_text"] == "选择文件夹…"
    assert payload["actions"] == [{"id": "start", "title": "用浏览器阅读"}]


def test_lists_zip_files_naturally_and_remembers_folder(installed, plugins_root, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    for name in ("10.zip", "2.zip", "1.zip"):
        image_zip(folder / name, {"1.png": (8, 8)})
    (folder / "notes.txt").write_text("不是压缩包", encoding="utf-8")
    (folder / "子目录.zip").mkdir()

    payload = load_page(folder=str(folder))["data"]

    assert [item["name"] for item in payload["items"]] == [
        "1.zip",
        "2.zip",
        "10.zip",
    ]
    assert payload["folder"] == os.path.abspath(str(folder))
    assert payload["status_text"].startswith("已选目录：")
    assert "3 个 ZIP" in payload["status_text"]
    assert payload["empty_text"] == ""
    assert payload["actions"] == [{"id": "start", "title": "用浏览器阅读"}]

    store = loader.config_for(PLUGIN_ID)
    assert store is not None
    assert store.get("last_folder") == os.path.abspath(str(folder))

    # 重新装配（模拟重启插件体系）：不带 folder 也要回到上次目录
    plugin_module().stop_all()
    jml_backend.reset_host_state()
    report = json.loads(jml_backend.bootstrap(plugins_root))
    assert report["ok"] is True, report
    reloaded = load_page()["data"]
    assert reloaded["folder"] == os.path.abspath(str(folder))
    assert [item["name"] for item in reloaded["items"]] == [
        "1.zip",
        "2.zip",
        "10.zip",
    ]


def test_empty_or_missing_folder_status(installed, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    payload = load_page(folder=str(empty))["data"]
    assert payload["items"] == []
    assert payload["empty_text"] == "目录中没有 ZIP 压缩包"
    assert "已选目录" in payload["status_text"]

    missing = tmp_path / "missing"
    payload = load_page(folder=str(missing))["data"]
    assert payload["items"] == []
    assert payload["empty_text"] == "文件夹不存在，请重新选择"


def test_start_without_selection_returns_message(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    image_zip(folder / "1.zip", {"1.png": (8, 8)})
    load_page(folder=str(folder))

    result = run_action("start", {"item": None, "item_index": -1})

    assert result["ok"] is True
    assert result["refresh"] is False
    assert result["data"] is None
    assert "请先" in result["message"]
    assert load_page()["data"]["state"] == "idle"


def test_start_broken_zip_reports_locked_message(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    broken = folder / "broken.zip"
    broken.write_bytes(b"this is not a zip file")
    load_page(folder=str(folder))

    result = run_action(
        "start",
        {"item": {"name": "broken.zip", "path": str(broken)}, "item_index": 0},
    )

    assert result["ok"] is True
    assert result["refresh"] is False
    assert result["message"] == "不是有效的 ZIP 压缩包"
    assert load_page()["data"]["state"] == "idle"


# ---------------------------------------------------------------------------
# 真实 HTTP：启动 / HTML / 图片 / 停止 / 端口释放
# ---------------------------------------------------------------------------


def _start_fixture_browsing() -> tuple[dict, dict]:
    """选 Draw/zip-fixture → start 1.zip，返回 (start 结果 data, 页面 payload)。"""
    listing = load_page(folder=_FIXTURE_DIR)["data"]
    item = next(item for item in listing["items"] if item["name"] == "1.zip")
    result = run_action("start", {"item": item, "item_index": 0})
    assert result["ok"] is True, result
    data = result["data"]
    assert isinstance(data, dict), result
    return data, load_page()["data"]


@pytest.mark.skipif(
    not os.path.isdir(_FIXTURE_DIR),
    reason="缺少离线测试数据 Draw/zip-fixture（由 Draw/zip-fixture/_make_fixtures.py 生成）",
)
def test_http_page_and_images_then_stop_releases_port(installed):
    data, payload = _start_fixture_browsing()

    port = int(data["port"])
    assert port > 0
    assert data["url"] == f"http://127.0.0.1:{port}/"
    assert data["state"] == "running"
    assert payload["state"] == "running"
    assert payload["url"] == data["url"]
    assert payload["port"] == port
    assert payload["actions"] == [{"id": "stop", "title": "停止浏览服务"}]
    assert "正在浏览" in payload["status_text"]
    assert str(port) in payload["status_text"]

    # 只绑回环：直接看服务器的 bind 地址（不是 0.0.0.0 / 外网地址）
    module = plugin_module()
    sessions = list(module._SESSIONS)
    assert sessions, "插件没有登记会话"
    server = sessions[0]._server
    assert server is not None
    assert server.server_address[0] == "127.0.0.1"
    assert server.server_address[1] == port

    # 长页 HTML：第 1 页 / 共 3 页 + 3 个 <img>
    status, headers, body = fetch(data["url"])
    assert status == 200
    assert headers.get_content_type() == "text/html"
    assert "utf-8" in (headers.get("Content-Type") or "").lower()
    html_text = body.decode("utf-8")
    assert "第 1 页 / 共 3 页" in html_text
    assert html_text.count("<img ") == 3
    for index in range(3):
        # 原版路由 /page/<序号>；每张图懒加载（大包不一次性拉全部）
        assert f'src="/page/{index}"' in html_text
        assert 'loading="lazy"' in html_text
    assert "position: fixed" in html_text

    # 三张原图：Pillow 解码成功，尺寸与 fixture 一致，Content-Type 正确
    expected_sizes = [(40, 30), (60, 50), (80, 70)]
    for index, expected in enumerate(expected_sizes):
        image_status, image_headers, image_body = fetch(data["url"] + f"page/{index}")
        assert image_status == 200
        assert image_headers.get_content_type() == "image/png"
        assert decode_size(image_body) == expected
        # /img/<序号> 是兼容别名，两种地址拿到同样的字节
        alias_status, _alias_headers, alias_body = fetch(data["url"] + f"img/{index}")
        assert alias_status == 200
        assert alias_body == image_body

    # stop：refresh=true + 新 payload；之后端口必须已释放
    stopped = run_action("stop", {})
    assert stopped["ok"] is True
    assert stopped["refresh"] is True
    assert stopped["message"] == "浏览服务已停止"
    assert stopped["data"]["state"] == "idle"
    assert stopped["data"]["port"] == 0
    assert stopped["data"]["url"] == ""
    assert stopped["data"]["status_text"] == "已停止"
    assert stopped["data"]["actions"] == [
        {"id": "start", "title": "用浏览器阅读"}
    ]

    assert_connection_refused(port)
    assert_port_rebindable(port)

    # 再次 start：自动分配端口并继续可读（同包可复用句柄）
    again = run_action(
        "start",
        {"item": {"name": "1.zip", "path": os.path.join(_FIXTURE_DIR, "1.zip")}},
    )
    assert again["ok"] is True
    second = again["data"]
    assert int(second["port"]) > 0
    assert second["state"] == "running"
    assert fetch(second["url"])[0] == 200

    run_action("stop", {})
    assert_connection_refused(int(second["port"]))
    assert_port_rebindable(int(second["port"]))


def test_stop_when_not_running_is_idempotent(installed):
    result = run_action("stop", {})

    assert result["ok"] is True
    assert result["refresh"] is False
    assert result["data"] is None
    assert result["message"] == "浏览服务未在运行"


def test_stop_all_releases_port(installed):
    module = plugin_module()
    folder = os.path.join(_REPO_ROOT, "Draw", "zip-fixture")
    if not os.path.isdir(folder):
        pytest.skip("缺少离线测试数据 Draw/zip-fixture")
    item = next(
        item for item in load_page(folder=folder)["data"]["items"] if item["name"] == "1.zip"
    )
    data = run_action("start", {"item": item, "item_index": 0})["data"]
    port = int(data["port"])
    assert fetch(data["url"])[0] == 200

    module.stop_all()

    assert_connection_refused(port)
    assert_port_rebindable(port)
    # 页面动作仍可用（会话未注销），但再次 start 会重建服务
    assert load_page()["data"]["state"] == "idle"


def test_switching_folder_stops_running_service(installed, tmp_path):
    folder = os.path.join(_REPO_ROOT, "Draw", "zip-fixture")
    if not os.path.isdir(folder):
        pytest.skip("缺少离线测试数据 Draw/zip-fixture")
    item = next(
        item for item in load_page(folder=folder)["data"]["items"] if item["name"] == "1.zip"
    )
    data = run_action("start", {"item": item, "item_index": 0})["data"]
    port = int(data["port"])
    assert fetch(data["url"])[0] == 200

    other = tmp_path / "other"
    other.mkdir()
    image_zip(other / "2.zip", {"1.png": (5, 6)})
    payload = load_page(folder=str(other))["data"]

    assert payload["state"] == "idle"
    assert payload["port"] == 0
    assert payload["url"] == ""
    assert [entry["name"] for entry in payload["items"]] == ["2.zip"]
    assert_connection_refused(port)
    assert_port_rebindable(port)


def test_start_new_package_stops_previous_service(installed, tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    first = image_zip(folder / "1.zip", {"1.png": (8, 8)})
    second = image_zip(folder / "2.zip", {"1.png": (9, 9), "2.png": (10, 10)})
    load_page(folder=str(folder))

    data1 = run_action(
        "start", {"item": {"name": "1.zip", "path": first}, "item_index": 0}
    )["data"]
    port1 = int(data1["port"])
    assert fetch(data1["url"])[0] == 200

    data2 = run_action(
        "start", {"item": {"name": "2.zip", "path": second}, "item_index": 1}
    )["data"]
    port2 = int(data2["port"])
    assert fetch(data2["url"])[0] == 200

    assert_connection_refused(port1)
    assert_port_rebindable(port1)
    status, _headers, body = fetch(data2["url"])
    assert status == 200
    assert "第 1 页 / 共 2 页" in body.decode("utf-8")
    assert body.decode("utf-8").count("<img ") == 2

    run_action("stop", {})
    assert_connection_refused(port2)


# ---------------------------------------------------------------------------
# 纯函数：自然排序 / HTML / Content-Type
# ---------------------------------------------------------------------------


def test_natural_key_puts_999_before_1000():
    unit = unit_module()
    assert unit.natural_key("999.png") < unit.natural_key("1000.png")
    assert sorted(["10.zip", "2.zip", "1.zip"], key=unit.natural_key) == [
        "1.zip",
        "2.zip",
        "10.zip",
    ]


def test_build_page_html_is_seamless_vertical_long_page():
    unit = unit_module()
    page = unit.build_page_html("1.zip", 3)

    assert "第 1 页 / 共 3 页" in page
    assert page.count("<img ") == 3
    for index in range(3):
        # 原版 web_viewer.py 的图片路由是 /page/<序号>
        assert f'src="/page/{index}"' in page
        assert 'loading="lazy"' in page
        assert 'decoding="async"' in page
    # 所有图片在同一个长页容器里（不是分页/iframe），页码条固定
    assert '<div id="pages">' in page
    assert "position: fixed" in page
    assert "addEventListener('scroll'" in page
    # 无缝：图片块级、无外边距/内边距、容器 line-height:0（原版 README 的四层成因）
    assert "margin: 0 auto; padding: 0" in page
    assert "line-height: 0" in page
    # 没有任何图片被 base64 内联
    assert "base64" not in page


def test_content_type_mapping():
    unit = unit_module()
    assert unit.content_type_for("a.JPG") == "image/jpeg"
    assert unit.content_type_for("a.jpeg") == "image/jpeg"
    assert unit.content_type_for("a.jfif") == "image/jpeg"
    assert unit.content_type_for("a.png") == "image/png"
    assert unit.content_type_for("a.gif") == "image/gif"
    assert unit.content_type_for("a.webp") == "image/webp"
    assert unit.content_type_for("a.bmp") == "image/bmp"
    assert unit.content_type_for("a.txt") == "application/octet-stream"


# ---------------------------------------------------------------------------
# 生命周期：页面刷新 / 翻页 / 同包重复 start 都不得停服务
# ---------------------------------------------------------------------------


def test_page_reload_keeps_service_reachable(installed):
    """模拟 C# 在 refresh=true 之后的整页重载（再次 plugin_page_load）。

    C# 重载时会带上当前 folder（PluginPageHost.AddFolderParameter）；旧缺陷若在
    loader 里把服务停掉，这里 GET / 就会连接失败——用户"手动粘贴端口打不开"
    正是这个形态。
    """
    data, _payload = _start_fixture_browsing()
    port = int(data["port"])
    assert fetch(data["url"])[0] == 200

    # 与 C# 重载同样的参数（page + folder）
    reloaded = load_page(page=1, folder=_FIXTURE_DIR)["data"]
    assert reloaded["state"] == "running"
    assert reloaded["url"] == data["url"]
    assert reloaded["port"] == port

    status, headers, body = fetch(data["url"])
    assert status == 200
    assert headers.get_content_type() == "text/html"
    assert "第 1 页 / 共 3 页" in body.decode("utf-8")
    assert fetch(data["url"] + "page/0")[0] == 200

    run_action("stop", {})


def test_page_turn_keeps_service_reachable(installed):
    """翻页（page=2）只是页面参数，不允许影响正在跑的服务。"""
    data, _payload = _start_fixture_browsing()
    port = int(data["port"])

    turned = load_page(page=2, folder=_FIXTURE_DIR)["data"]
    assert turned["state"] == "running"
    assert turned["port"] == port
    assert fetch(data["url"])[0] == 200

    run_action("stop", {})


def test_repeat_start_same_package_reuses_running_port(installed):
    """同一个包再次 start：复用正在运行的服务，不换端口、不中断浏览器页。"""
    data, _payload = _start_fixture_browsing()
    url, port = data["url"], int(data["port"])
    assert fetch(url)[0] == 200

    again = run_action(
        "start",
        {"item": {"name": "1.zip", "path": os.path.join(_FIXTURE_DIR, "1.zip")}},
    )
    assert again["ok"] is True
    assert again["refresh"] is True
    assert again["data"]["url"] == url
    assert int(again["data"]["port"]) == port
    assert "已在运行" in again["message"]
    # 端口没换、服务没断：同一地址仍然能拿到页面
    assert fetch(url)[0] == 200

    run_action("stop", {})


# ---------------------------------------------------------------------------
# 路由与文案：逐字对齐原版 web_viewer.py / zip_archive.py / plugin.py
# ---------------------------------------------------------------------------


def test_original_routes_and_error_texts(installed):
    data, _payload = _start_fixture_browsing()
    url = data["url"]

    # /index.html 与 / 等价（原版 web_viewer.py:90）
    assert fetch(url + "index.html")[0] == 200
    # 未知路由 → 404「未找到该资源」（原版 web_viewer.py:97）
    with pytest.raises(urllib.error.HTTPError) as missing:
        fetch(url + "no-such-route")
    assert missing.value.code == 404
    assert missing.value.read().decode("utf-8") == "未找到该资源"
    # 页码非整数 → 400「页码必须是整数」（原版 web_viewer.py:143）
    with pytest.raises(urllib.error.HTTPError) as bad:
        fetch(url + "page/abc")
    assert bad.value.code == 400
    assert bad.value.read().decode("utf-8") == "页码必须是整数"
    # 越界 → 404「页码超出范围」（原版 web_viewer.py:157）
    with pytest.raises(urllib.error.HTTPError) as out:
        fetch(url + "page/999")
    assert out.value.code == 404
    assert out.value.read().decode("utf-8") == "页码超出范围"

    run_action("stop", {})


def test_head_request_has_no_body(installed):
    data, _payload = _start_fixture_browsing()
    request = urllib.request.Request(data["url"], method="HEAD")
    with _OPENER.open(request, timeout=5) as response:
        assert response.status == 200
        assert response.read() == b""

    run_action("stop", {})


def test_closed_archive_is_reopened_when_serving_images(installed):
    """另一个插件把同一个压缩包 close() 后，看图不能整页 500。"""
    data, _payload = _start_fixture_browsing()
    module = plugin_module()
    # 取当前真正持有服务的会话（多次装配时 _SESSIONS 里可能有历史会话）
    session = next(s for s in module._SESSIONS if s._server is not None)
    reader = session._reader
    reader.close(os.path.join(_FIXTURE_DIR, "1.zip"))
    assert reader.is_open(os.path.join(_FIXTURE_DIR, "1.zip")) is False

    status, headers, body = fetch(data["url"] + "page/0")
    assert status == 200
    assert headers.get_content_type() == "image/png"
    assert decode_size(body) == (40, 30)

    run_action("stop", {})


def test_start_failure_message_matches_original_prefix(installed, monkeypatch):
    """服务起不来时文案对齐原版「无法启动本地服务: …」并附端口/异常。"""
    listing = load_page(folder=_FIXTURE_DIR)["data"]
    item = next(item for item in listing["items"] if item["name"] == "1.zip")

    class _BoomServer:
        def __init__(self, *_args, **_kwargs):
            raise OSError(10048, "通常每个套接字地址只允许使用一次。")

    monkeypatch.setattr(plugin_module(), "ZipWebServer", _BoomServer)
    result = run_action("start", {"item": item, "item_index": 0})

    assert result["ok"] is True
    assert result["refresh"] is False
    assert result["data"] is None
    assert result["message"].startswith("无法启动本地服务：")
    assert "OSError" in result["message"]
    assert load_page()["data"]["state"] == "idle"


def test_no_images_message_matches_original(installed):
    no_images = os.path.join(_FIXTURE_DIR, "no_images.zip")
    if not os.path.isfile(no_images):
        pytest.skip("缺少离线测试数据 Draw/zip-fixture/no_images.zip")
    load_page(folder=_FIXTURE_DIR)

    result = run_action(
        "start",
        {"item": {"name": "no_images.zip", "path": no_images}, "item_index": 0},
    )
    assert result["ok"] is True
    assert result["refresh"] is False
    # 原版 zip_archive.py:183 逐字（由 archive_reader/1 的 zip_browser 提供）
    assert result["message"] == "压缩包中没有可浏览的图片"


def test_start_without_selection_uses_original_hint(installed):
    load_page(folder=_FIXTURE_DIR)
    result = run_action("start", {"item": None, "item_index": -1})
    # 原版 plugin.py:36 逐字
    assert result["message"] == "请先选择一个压缩包"
