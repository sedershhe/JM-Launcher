"""后端入口（jml_backend）契约测试（阶段 2）。

锁定：C# 可调用入口返回 JSON 字符串（health_check 除外）、
start_download 立即返回、事件 sink 回调、失败形态不抛异常、
中文不转义（ensure_ascii=False）。零联网（假数据源）。
"""

from __future__ import annotations

import json
import sys
import time

import pytest

import jml_backend
from jml.core import diagnostics, downloader, jm_source
from jml.core.errors import ERROR_BATCH_NOT_FOUND, ERROR_INVALID_ARGUMENT
from jml.core.protocol import (
    EVENT_DOWNLOAD_BATCH_FINISHED,
    EVENT_DOWNLOAD_ITEM_FINISHED,
    EVENT_DOWNLOAD_ITEM_STARTED,
    EVENT_DOWNLOAD_LOG,
)
from jml.host.plugin_context import PluginContext
from jml.host.registry import REGISTRY
from jml.services.host_services import call_service, register_host_services
from tests.conftest import FakeSource


@pytest.fixture(autouse=True)
def _reset_backend_state():
    jml_backend.reset_host_state()
    yield
    jml_backend.reset_host_state()


# ---------------------------------------------------------------------------
# 返回值类型契约
# ---------------------------------------------------------------------------


def test_health_check_stays_dict_and_version_stays_str():
    health = jml_backend.health_check()
    assert isinstance(health, dict)
    assert health["ok"] is True
    assert isinstance(jml_backend.version(), str)


def test_bootstrap_and_list_plugins_return_json_strings(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()

    raw = jml_backend.bootstrap(str(root))
    assert isinstance(raw, str)
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["loaded"] == []
    assert data["services"]["jm.search"] == 1
    assert data["services"]["jm.download"] == 1

    listed = jml_backend.list_plugins(str(root))
    assert isinstance(listed, str)
    listed_data = json.loads(listed)
    assert listed_data["ok"] is True
    assert listed_data["loaded"] == []


def test_bootstrap_idempotent_returns_same_json(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()

    first = jml_backend.bootstrap(str(root))
    second = jml_backend.bootstrap(str(tmp_path / "does-not-exist"))

    assert second == first


def test_bootstrap_loads_plugin(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("demo", base_manifest, root=root)

    data = json.loads(jml_backend.bootstrap(str(root)))

    assert data["ok"] is True
    assert [item["id"] for item in data["loaded"]] == ["demo_plugin"]
    assert data["failed"] == []


def test_list_plugins_reports_bad_manifest(make_plugin, base_manifest, tmp_path):
    root = tmp_path / "plugins"
    make_plugin("good", base_manifest, root=root)
    bad = root / "broken"
    bad.mkdir()
    (bad / "plugin.json").write_text("{ nope", encoding="utf-8")

    data = json.loads(jml_backend.list_plugins(str(root)))

    assert data["ok"] is True
    assert [item["id"] for item in data["loaded"]] == ["demo_plugin"]
    assert [item["id"] for item in data["failed"]] == ["broken"]


def test_list_plugins_missing_dir_returns_failure_form(tmp_path):
    data = json.loads(jml_backend.list_plugins(str(tmp_path / "missing")))

    assert data["ok"] is False
    assert data["loaded"] == []
    assert data["error_code"]
    assert data["error_message"]


def test_list_plugins_survives_host_import_failure(monkeypatch):
    """宿主模块 import 失败也必须返回失败 JSON（入口不抛 ImportError）。"""
    import jml.host
    import jml.host.loader  # noqa: F401 - 确保属性存在，便于 monkeypatch 删除

    with monkeypatch.context() as patcher:
        patcher.setitem(sys.modules, "jml.host.loader", None)
        patcher.delattr(jml.host, "loader")
        data = json.loads(jml_backend.list_plugins("whatever"))

    assert data["ok"] is False
    assert data["loaded"] == []
    assert data["failed"][0]["id"] == "(host)"
    assert data["error_code"]
    assert data["error_message"]


def test_bootstrap_missing_dir_returns_failure_form(tmp_path):
    data = json.loads(jml_backend.bootstrap(str(tmp_path / "missing")))

    assert data["ok"] is False
    assert data["loaded"] == []
    assert data["failed"][0]["id"] == "(host)"
    assert data["error_code"]
    assert data["error_message"]


def test_bootstrap_failure_not_cached_allows_retry(tmp_path):
    """首次失败不永久缓存：建好目录后第二次必须能成功装配。"""
    plugins_dir = tmp_path / "plugins"
    first = json.loads(jml_backend.bootstrap(str(plugins_dir)))
    assert first["ok"] is False

    plugins_dir.mkdir()
    second = json.loads(jml_backend.bootstrap(str(plugins_dir)))
    assert second["ok"] is True
    assert second["loaded"] == []


def test_set_event_sink_not_blocked_by_bootstrap(monkeypatch, tmp_path):
    """bootstrap 读盘/装配在 _STATE_LOCK 外，UI 线程注册 sink 不得被阻塞。"""
    import threading

    from jml.host import loader

    entered = threading.Event()
    release = threading.Event()

    def slow_load_all(plugins_dir):
        entered.set()
        release.wait(5)
        return {"loaded": [], "failed": [], "services": {}}

    monkeypatch.setattr(loader, "load_all", slow_load_all)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    thread = threading.Thread(
        target=lambda: jml_backend.bootstrap(str(plugins_dir)), daemon=True
    )
    thread.start()
    assert entered.wait(5)

    started_at = time.time()
    ack = json.loads(jml_backend.set_event_sink(lambda text: None))
    elapsed = time.time() - started_at

    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert ack == {"ok": True}
    assert elapsed < 0.5


# ---------------------------------------------------------------------------
# 编号解析与下载
# ---------------------------------------------------------------------------


def test_parse_book_ids_entry():
    data = json.loads(jml_backend.parse_book_ids("1-2,3，4、5;6；7 8"))
    assert data == {
        "ok": True,
        "book_ids": ["1", "2", "3", "4", "5", "6", "7", "8"],
        "count": 8,
    }

    empty = json.loads(jml_backend.parse_book_ids("abc"))
    assert empty == {"ok": True, "book_ids": [], "count": 0}


def test_start_download_returns_immediately(tmp_path):
    source = FakeSource(sleep=0.1)
    jm_source.set_source(source)

    started_at = time.time()
    data = json.loads(jml_backend.start_download('["1"]', str(tmp_path)))
    elapsed = time.time() - started_at

    assert data["ok"] is True
    assert data["status"] == "running"
    assert data["total"] == 1
    assert data["batch_id"]
    assert elapsed < 0.5

    result = downloader.wait_batch(data["batch_id"], timeout=10)
    assert result is not None
    assert result["success"] == 1
    assert result["items"][0]["zip_path"].endswith("详情标题-JM1.zip")


def test_start_download_accepts_plain_text_ids(tmp_path):
    source = FakeSource()
    jm_source.set_source(source)

    data = json.loads(jml_backend.start_download("1-2", str(tmp_path)))
    assert data["ok"] is True
    assert data["total"] == 2
    result = downloader.wait_batch(data["batch_id"], timeout=10)
    assert result["success"] == 2
    assert sorted(source.album_calls) == ["1", "2"]


@pytest.mark.parametrize(
    ("book_ids_json", "target"),
    [
        ("[]", None),
        ("", None),
        ("abc", None),
        ('["abc"]', None),
        ('["1", "x"]', None),
        (None, None),
        ('["1"]', ""),
    ],
    ids=[
        "empty_array", "empty_text", "no_numbers", "json_non_digit",
        "json_mixed_non_digit", "none", "empty_target",
    ],
)
def test_start_download_failure_forms(book_ids_json, target, tmp_path):
    data = json.loads(
        jml_backend.start_download(
            book_ids_json, str(tmp_path) if target is None else target
        )
    )
    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_ARGUMENT
    assert data["error_message"]


def test_start_download_target_is_file(tmp_path):
    file_path = tmp_path / "not-a-dir.txt"
    file_path.write_text("x", encoding="utf-8")

    data = json.loads(jml_backend.start_download('["1"]', str(file_path)))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_ARGUMENT


def test_cancel_download_failure_forms():
    missing = json.loads(jml_backend.cancel_download("no-such-batch"))
    assert missing["ok"] is False
    assert missing["error_code"] == "BATCH_NOT_FOUND"

    empty = json.loads(jml_backend.cancel_download(""))
    assert empty["ok"] is False
    assert empty["error_code"] == ERROR_INVALID_ARGUMENT


def test_cancel_download_success(tmp_path):
    source = FakeSource(sleep=0.02)
    jm_source.set_source(source)
    started = json.loads(jml_backend.start_download('["1"]', str(tmp_path)))

    cancelled = json.loads(jml_backend.cancel_download(started["batch_id"]))
    assert cancelled == {
        "ok": True,
        "batch_id": started["batch_id"],
        "cancelled": True,
    }

    downloader.wait_batch(started["batch_id"], timeout=10)


# ---------------------------------------------------------------------------
# 下载进度入口（get_progress）
# ---------------------------------------------------------------------------


def test_get_progress_registered_batch_returns_same_id(tmp_path):
    """真登记一个批次后立刻查询：ok=true 且 batch_id 与传入值一致。"""
    jm_source.set_source(FakeSource())
    started = json.loads(jml_backend.start_download('["1"]', str(tmp_path)))
    batch_id = started["batch_id"]

    progress = json.loads(jml_backend.get_progress(batch_id))

    assert progress["ok"] is True
    assert progress["batch_id"] == batch_id
    assert progress["status"] in ("running", "finished")
    assert progress["total"] == 1
    assert progress["cancel_requested"] is False
    assert progress["result"] is None or isinstance(progress["result"], dict)

    result = downloader.wait_batch(batch_id, timeout=10)
    assert result is not None
    assert result["success"] == 1


def test_get_progress_unknown_id_keeps_id_in_error_message():
    """未知批次必须回传传入 id，证明参数没有在桥接层丢失。"""
    unknown = "no-such-batch-4f2c9d"
    data = json.loads(jml_backend.get_progress(unknown))

    assert data["ok"] is False
    assert data["error_code"] == ERROR_BATCH_NOT_FOUND
    assert unknown in data["error_message"]


def test_get_progress_empty_id_is_invalid_argument():
    for blank in ("", "   ", None):
        data = json.loads(jml_backend.get_progress(blank))
        assert data["ok"] is False
        assert data["error_code"] == ERROR_INVALID_ARGUMENT
        assert data["error_message"]


def test_bootstrap_and_list_plugins_include_description(
    make_plugin, base_manifest, tmp_path
):
    """loaded[] 必须带 description（内容来自 manifest，空则空串）。"""
    root = tmp_path / "plugins"
    make_plugin("demo", base_manifest, root=root)

    boot = json.loads(jml_backend.bootstrap(str(root)))
    assert boot["loaded"][0]["description"] == "用于单元测试的演示插件"

    listed = json.loads(jml_backend.list_plugins(str(root)))
    assert listed["loaded"][0]["description"] == "用于单元测试的演示插件"

    # manifest 缺省 description → 空串（不是缺键）
    manifest_no_description = dict(base_manifest)
    manifest_no_description.pop("description")
    manifest_no_description["id"] = "plain_plugin"
    make_plugin("plain", manifest_no_description, root=root)
    plain = json.loads(jml_backend.list_plugins(str(root)))
    by_id = {item["id"]: item for item in plain["loaded"]}
    assert by_id["demo_plugin"]["description"] == "用于单元测试的演示插件"
    assert by_id["plain_plugin"]["description"] == ""


# ---------------------------------------------------------------------------
# 搜索 / 详情 / 封面
# ---------------------------------------------------------------------------


def test_search_entry_json_and_paging_snapshot():
    source = FakeSource(
        total_pages=5,
        search_results=[
            {
                "book_id": "1",
                "title": "搜索标题",
                "author": "作者",
                "page_count": 6,
                "tags": ["全彩"],
                "cover_url": "https://example.invalid/1.jpg",
            }
        ],
    )
    jm_source.set_source(source)

    raw = jml_backend.search("旧词", 1)
    assert isinstance(raw, str)
    assert "搜索标题" in raw  # ensure_ascii=False，中文不转义
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["keyword"] == "旧词"
    assert data["page"] == 1
    assert data["total_pages"] == 5
    assert data["results"][0]["book_id"] == "1"

    paged = json.loads(jml_backend.search("新词", 2))
    assert paged["keyword"] == "旧词"
    assert source.search_calls == [("旧词", 1), ("旧词", 2)]


def test_search_entry_failure_form():
    data = json.loads(jml_backend.search("", 1))
    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_ARGUMENT


def test_search_snapshot_shared_between_service_and_backend():
    """jm.search 服务（无 caller）与后端入口共用 main 会话。"""
    source = FakeSource()
    jm_source.set_source(source)
    register_host_services()

    service_result = call_service(
        "jm.search", 1, "search", {"keyword": "旧词", "page": 1}
    )
    assert service_result["ok"] is True
    assert service_result["keyword"] == "旧词"

    # 模拟"插件搜过后，主界面点下一页且输入框已被改成新词"
    backend_result = json.loads(jml_backend.search("新词", 2))

    assert backend_result["keyword"] == "旧词"
    assert source.search_calls == [("旧词", 1), ("旧词", 2)]


def test_plugin_search_session_isolated_from_main():
    """插件经 ctx.call_service 使用 plugin:<id> 会话，不污染主界面快照。"""
    source = FakeSource()
    jm_source.set_source(source)
    register_host_services()

    def on_get_service(name: str, version: int):
        return REGISTRY.resolve(name, version)

    ctx = PluginContext(
        "p1",
        on_register_service=lambda name, version, ref: None,
        on_subscribe=lambda event_type, handler: None,
        on_get_service=on_get_service,
        on_config_get=lambda key: None,
        on_log=lambda message: None,
    )

    plugin_first = ctx.call_service(
        "jm.search", 1, "search", {"keyword": "插件词", "page": 1}
    )
    assert plugin_first["keyword"] == "插件词"

    main_first = json.loads(jml_backend.search("主词", 1))
    assert main_first["keyword"] == "主词"

    plugin_second = ctx.call_service(
        "jm.search", 1, "get_page", {"keyword": "改词", "page": 2}
    )
    main_second = json.loads(jml_backend.search("改词", 2))

    assert plugin_second["keyword"] == "插件词"
    assert main_second["keyword"] == "主词"
    assert source.search_calls == [
        ("插件词", 1),
        ("主词", 1),
        ("插件词", 2),
        ("主词", 2),
    ]


def test_get_detail_entry_truncates_description():
    jm_source.set_source(FakeSource())

    raw = jml_backend.get_detail("1")
    assert "详情标题" in raw
    data = json.loads(raw)

    assert data["ok"] is True
    assert data["book_id"] == "1"
    assert data["page_count"] == 2
    assert data["description_truncated"] is True
    assert len(data["description"]) == 201
    assert data["description"].endswith("…")


def test_get_detail_entry_failure_form():
    data = json.loads(jml_backend.get_detail(""))
    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_ARGUMENT


def test_fetch_cover_returns_bytes():
    jm_source.set_source(FakeSource(cover_data=b"COVER-BYTES"))
    assert jml_backend.fetch_cover("1") == b"COVER-BYTES"


def test_fetch_cover_returns_none_on_failure():
    jm_source.set_source(FakeSource(cover_data=None))
    assert jml_backend.fetch_cover("1") is None
    assert jml_backend.fetch_cover("") is None


# ---------------------------------------------------------------------------
# 事件 sink
# ---------------------------------------------------------------------------


def test_set_event_sink_receives_json_events():
    received: list[dict] = []

    def callback(text: str) -> None:
        received.append(json.loads(text))

    ack = json.loads(jml_backend.set_event_sink(callback))
    assert ack == {"ok": True}

    result = json.loads(
        jml_backend.emit_test_event(
            EVENT_DOWNLOAD_LOG,
            {"type": EVENT_DOWNLOAD_LOG, "level": "info", "message": "hello"},
        )
    )
    assert result["ok"] is True
    assert received[-1] == {
        "type": EVENT_DOWNLOAD_LOG,
        "payload": {
            "type": EVENT_DOWNLOAD_LOG,
            "level": "info",
            "message": "hello",
        },
    }


def test_event_sink_receives_download_events(tmp_path):
    jm_source.set_source(FakeSource())
    received: list[dict] = []
    jml_backend.set_event_sink(lambda text: received.append(json.loads(text)))

    started = json.loads(jml_backend.start_download('["1"]', str(tmp_path)))
    result = downloader.wait_batch(started["batch_id"], timeout=10)
    assert result["success"] == 1

    types = [item["type"] for item in received]
    assert EVENT_DOWNLOAD_ITEM_STARTED in types
    assert EVENT_DOWNLOAD_ITEM_FINISHED in types
    assert EVENT_DOWNLOAD_BATCH_FINISHED in types
    assert EVENT_DOWNLOAD_LOG in types

    finished = next(
        item for item in received if item["type"] == EVENT_DOWNLOAD_ITEM_FINISHED
    )
    assert finished["payload"]["ok"] is True
    assert finished["payload"]["book_id"] == "1"


def test_event_sink_callback_error_does_not_break_delivery():
    def bad_callback(text: str) -> None:
        raise RuntimeError("C# 回调炸了")

    received: list[dict] = []
    from jml.host import events

    events.BUS.subscribe(EVENT_DOWNLOAD_LOG, received.append)
    jml_backend.set_event_sink(bad_callback)

    result = json.loads(
        jml_backend.emit_test_event(
            EVENT_DOWNLOAD_LOG,
            {"type": EVENT_DOWNLOAD_LOG, "level": "info", "message": "still delivered"},
        )
    )

    assert result["ok"] is True
    assert result["delivered"] == 1
    assert len(received) == 1
    assert any("汇聚出口" in item for item in result["errors"])


def test_set_event_sink_rejects_non_callable():
    data = json.loads(jml_backend.set_event_sink(None))
    assert data["ok"] is False
    assert data["error_code"] == ERROR_INVALID_ARGUMENT


def test_clear_event_sink():
    received: list[dict] = []
    jml_backend.set_event_sink(lambda text: received.append(json.loads(text)))
    assert json.loads(jml_backend.clear_event_sink()) == {"ok": True}

    jml_backend.emit_test_event(
        EVENT_DOWNLOAD_LOG,
        {"type": EVENT_DOWNLOAD_LOG, "level": "info", "message": "after clear"},
    )
    assert received == []


def test_set_log_dir_entry_opt_in(tmp_path):
    data = json.loads(jml_backend.set_log_dir(str(tmp_path)))
    assert data["ok"] is True
    assert data["log_dir"] == str(tmp_path)

    disabled = json.loads(jml_backend.set_log_dir(""))
    assert disabled == {"ok": True, "log_dir": ""}


def test_get_diagnostics_entry_is_sanitized_and_limited():
    diagnostics.clear()
    diagnostics.record("TEST", "password=SECRET-DIAG")

    data = json.loads(jml_backend.get_diagnostics(10))
    assert data["ok"] is True
    assert data["count"] == len(data["records"]) >= 1
    assert any("TEST" in item for item in data["records"])
    assert all("SECRET-DIAG" not in item for item in data["records"])

    limited = json.loads(jml_backend.get_diagnostics(1))
    assert limited["count"] == 1


def test_emit_test_event_invalid_payload_returns_failure_form():
    data = json.loads(jml_backend.emit_test_event(EVENT_DOWNLOAD_LOG, {}))
    assert data["ok"] is False
    assert data["delivered"] == 0
    assert data["errors"]


# ---------------------------------------------------------------------------
# 入口绝不向 C# 抛异常
# ---------------------------------------------------------------------------


def test_entry_points_never_raise_and_always_json_or_bytes():
    # 假数据源：确保走真机网络的分支不会被触发（自动化测试禁止联网）
    jm_source.set_source(FakeSource())
    calls = [
        (jml_backend.search, (None, None)),
        (jml_backend.search, (object(), object())),
        (jml_backend.get_detail, (None,)),
        (jml_backend.get_detail, (object(),)),
        (jml_backend.start_download, (None, None)),
        (jml_backend.start_download, ("[]", None)),
        (jml_backend.cancel_download, (None,)),
        (jml_backend.get_progress, (None,)),
        (jml_backend.get_progress, (object(),)),
        (jml_backend.parse_book_ids, (None,)),
    ]
    for function, args in calls:
        result = function(*args)
        assert isinstance(result, str), function.__name__
        parsed = json.loads(result)
        assert "ok" in parsed

    assert jml_backend.fetch_cover(None) is None
    weird_cover = jml_backend.fetch_cover(object())
    assert weird_cover is None or isinstance(weird_cover, bytes)
