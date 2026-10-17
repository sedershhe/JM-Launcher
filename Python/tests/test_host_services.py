"""主程序服务路由单元测试（jm.search / jm.detail / jm.download / jm.account）。

验证：统一 call_service 签名、成功/失败形态、关键词快照、账号登录态真实化、
服务不存在/方法不存在抛 ServiceError。零联网（假数据源）。
"""

from __future__ import annotations

import pytest

from jml.core import account, downloader, jm_source
from jml.core.errors import (
    ERROR_BATCH_NOT_FOUND,
    ERROR_INVALID_ARGUMENT,
    ERROR_NOT_LOGGED_IN,
)
from jml.core.protocol import ServiceError
from jml.services.host_services import (
    HOST_SERVICES,
    call_service,
    register_host_services,
)
from jml.host.registry import REGISTRY


@pytest.fixture
def fake_source(fake_source_cls):
    source = fake_source_cls(total_pages=2)
    jm_source.set_source(source)
    register_host_services()
    return source


def test_register_host_services():
    register_host_services()
    names = REGISTRY.names()
    assert names["jm.search"] == 1
    assert names["jm.detail"] == 1
    assert names["jm.download"] == 1
    assert names["jm.account"] == 1
    assert set(HOST_SERVICES) == {"jm.search", "jm.detail", "jm.download", "jm.account"}


def test_search_service_route(fake_source):
    result = call_service("jm.search", 1, "search", {"keyword": "abc", "page": 1})

    assert result["ok"] is True
    assert result["keyword"] == "abc"
    assert result["page"] == 1
    assert result["total_pages"] == 2
    assert result["results"][0]["book_id"] == "1"


def test_search_service_paging_snapshot(fake_source):
    """jm.search 服务同样遵守旧缺陷 #8 的关键词快照语义。"""
    call_service("jm.search", 1, "search", {"keyword": "旧词", "page": 1})
    result = call_service("jm.search", 1, "get_page", {"keyword": "新词", "page": 2})

    assert result["keyword"] == "旧词"
    assert fake_source.search_calls == [("旧词", 1), ("旧词", 2)]


def test_detail_service_route(fake_source):
    result = call_service("jm.detail", 1, "get_detail", {"book_id": "1"})

    assert result["ok"] is True
    assert result["title"] == "详情标题"
    assert result["description_truncated"] is True


def test_detail_service_failure_form(fake_source):
    """服务层不把 Python 异常抛给插件：空编号 → 结构化失败。"""
    result = call_service("jm.detail", 1, "get_detail", {"book_id": ""})

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_ARGUMENT


def test_download_service_lifecycle(fake_source, tmp_path):
    started = call_service(
        "jm.download",
        1,
        "start_batch",
        {"book_ids": ["1"], "target_dir": str(tmp_path)},
    )
    assert started["ok"] is True
    batch_id = started["batch_id"]

    result = downloader.wait_batch(batch_id, timeout=10)
    assert result is not None
    assert result["success"] == 1

    progress = call_service("jm.download", 1, "get_progress", {"batch_id": batch_id})
    assert progress["ok"] is True
    assert progress["status"] == "finished"
    assert progress["total"] == 1

    unknown = call_service("jm.download", 1, "get_progress", {"batch_id": "missing"})
    assert unknown["ok"] is False
    assert unknown["error_code"] == ERROR_BATCH_NOT_FOUND


def test_download_service_cancel_unknown(fake_source):
    result = call_service("jm.download", 1, "cancel_batch", {"batch_id": "missing"})
    assert result["ok"] is False
    assert result["error_code"] == ERROR_BATCH_NOT_FOUND


def test_download_service_empty_ids(fake_source, tmp_path):
    result = call_service(
        "jm.download", 1, "start_batch", {"book_ids": [], "target_dir": str(tmp_path)}
    )
    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_ARGUMENT


@pytest.mark.parametrize("bad_ids", [["abc"], ["1", "x"], ["1.5"]])
def test_download_service_rejects_non_digit_ids(fake_source, tmp_path, bad_ids):
    """服务层与后端/下载器共用同一套严格编号校验。"""
    result = call_service(
        "jm.download",
        1,
        "start_batch",
        {"book_ids": bad_ids, "target_dir": str(tmp_path)},
    )
    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_ARGUMENT


@pytest.mark.parametrize("method", ["login_status", "ensure_login", "login", "logout"])
def test_account_service_methods_return_structured_dicts(fake_source, method):
    """账号服务不再返回 NOT_AVAILABLE，也不抛业务异常。"""
    result = call_service("jm.account", 1, method, {})
    assert isinstance(result, dict)
    assert "ok" in result
    assert "logged_in" in result
    assert "username_masked" in result
    if not result["ok"]:
        assert result["error_code"]


def test_account_service_login_lifecycle(fake_source):
    """设置内存凭据 → 服务登录 → 查状态 → 登出；凭据不出现在返回里。"""
    password = "SERVICE-SECRET-9x"
    account.set_credentials("service_user", password)

    status = call_service("jm.account", 1, "login_status", {})
    assert status["logged_in"] is False
    assert status["username_masked"] == "se***er"

    logged_in = call_service("jm.account", 1, "login", {})
    assert logged_in["ok"] is True
    assert logged_in["logged_in"] is True
    assert logged_in["username_masked"] == "se***er"
    assert password not in str(logged_in)

    ensured = call_service("jm.account", 1, "ensure_login", {})
    assert ensured["logged_in"] is True
    assert len(fake_source.login_calls) == 1

    logged_out = call_service("jm.account", 1, "logout", {})
    assert logged_out["ok"] is True
    assert logged_out["logged_in"] is False
    assert logged_out["credentials_kept"] is True
    assert password not in str(logged_out)


def test_account_service_ensure_login_without_credentials(fake_source):
    result = call_service("jm.account", 1, "ensure_login", {})
    assert result["ok"] is False
    assert result["error_code"] == ERROR_NOT_LOGGED_IN
    assert fake_source.login_calls == []


def test_account_service_ignores_credentials_in_payload(fake_source):
    """插件不能通过服务 payload 偷塞账号密码（凭据只走主程序入口）。"""
    result = call_service(
        "jm.account",
        1,
        "login",
        {"username": "plugin", "password": "PLUGIN-SECRET"},
    )
    assert result["ok"] is False
    assert result["error_code"] == ERROR_NOT_LOGGED_IN
    assert fake_source.login_calls == []


def test_login_status_reports_empty_error_fields(fake_source):
    """login_status 是查询，成功查询时错误字段为空串。"""
    result = call_service("jm.account", 1, "login_status", {})
    assert result["ok"] is True
    assert result["error_code"] == ""
    assert result["error_message"] == ""
    assert result["username_masked"] == ""


def test_unknown_method_raises_service_error(fake_source):
    with pytest.raises(ServiceError):
        call_service("jm.search", 1, "no_such_method", {})
    with pytest.raises(ServiceError):
        call_service("jm.account", 1, "no_such_method", {})


def test_unknown_service_raises_service_error():
    with pytest.raises(ServiceError):
        call_service("no.such.service", 1, "search", {})
