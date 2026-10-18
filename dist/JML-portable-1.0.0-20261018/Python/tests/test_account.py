"""账号模块单元测试（阶段 3；全部离线，禁止真实登录）。

覆盖：凭据设置/清空、用户名打码、空参数、登录成功/失败分类、
``ensure_login`` 三态、并发单飞、一次性登录、日志与返回文本脱敏。
"""

from __future__ import annotations

import json
import threading

import pytest

from jml.core import account, diagnostics, jm_source
from jml.core.errors import (
    ERROR_CANCELLED,
    ERROR_INVALID_ARGUMENT,
    ERROR_INVALID_CREDENTIALS,
    ERROR_NETWORK,
    ERROR_NOT_LOGGED_IN,
    InvalidCredentialsError,
)
from tests.conftest import FakeSource


@pytest.fixture
def fake_source():
    source = FakeSource()
    jm_source.set_source(source)
    return source


# ---------------------------------------------------------------------------
# 用户名打码
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("", ""),
        (None, ""),
        ("a", "***"),
        ("ab", "***"),
        ("abc", "a***c"),
        ("abcde", "a***e"),
        ("abcdef", "ab***ef"),
        ("account_name", "ac***me"),
    ],
)
def test_mask_username(raw, masked):
    assert account.mask_username(raw) == masked


# ---------------------------------------------------------------------------
# 凭据设置 / 清空
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("", "x"),
        ("   ", "x"),
        ("user", ""),
        (None, "x"),
        ("user", None),
        (123, "x"),
        ("user", 123),
    ],
)
def test_set_credentials_rejects_invalid(username, password):
    result = account.set_credentials(username, password)
    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_ARGUMENT
    assert result["error_message"]


def test_set_credentials_stores_masked_only_in_public_result(fake_source):
    password = "ACCOUNT-SECRET-1"
    username = "account_name"

    result = account.set_credentials(username, password)

    assert result["ok"] is True
    assert result["logged_in"] is False
    assert result["username_masked"] == "ac***me"
    text = json.dumps(result, ensure_ascii=False)
    assert password not in text
    assert username not in text
    assert fake_source.login_calls == []


def test_set_credentials_same_values_keeps_login_state(fake_source):
    account.set_credentials("user_1", "PWD-123456")
    account.login()
    assert account.login_status()["logged_in"] is True

    account.set_credentials("user_1", "PWD-123456")
    assert account.login_status()["logged_in"] is True

    account.set_credentials("user_2", "PWD-123456")
    assert account.login_status()["logged_in"] is False


def test_set_credentials_change_clears_source_session(fake_source):
    """换账号必须让旧会话真正失效，不能只改账号模块里的布尔值。"""
    account.set_credentials("user_1", "PWD-123456")
    account.login()
    assert fake_source.logged_in is True

    account.set_credentials("user_2", "PWD-654321")

    assert account.login_status()["logged_in"] is False
    assert fake_source.logged_in is False
    assert fake_source.logout_calls == 1


def test_clear_credentials_clears_login_and_source_session(fake_source):
    account.set_credentials("user_1", "PWD-123456")
    account.login()
    assert fake_source.logged_in is True

    result = account.clear_credentials()

    assert result["ok"] is True
    assert result["logged_in"] is False
    assert result["username_masked"] == ""
    assert result["credentials_cleared"] is True
    assert fake_source.logout_calls == 1
    assert fake_source.logged_in is False

    after = account.ensure_login()
    assert after["ok"] is False
    assert after["error_code"] == ERROR_NOT_LOGGED_IN
    assert fake_source.login_calls == [("user_1", "PWD-123456")]


# ---------------------------------------------------------------------------
# 登录成功 / 失败分类
# ---------------------------------------------------------------------------


def test_login_success_sets_state_and_masks_username(fake_source):
    password = "LOGIN-SECRET-2"
    account.set_credentials("ab_cd_ef", password)

    result = account.login()

    assert result["ok"] is True
    assert result["logged_in"] is True
    assert result["username_masked"] == "ab***ef"
    assert result["error_code"] == ""
    assert password not in json.dumps(result, ensure_ascii=False)
    assert fake_source.login_calls == [("ab_cd_ef", password)]

    status = account.login_status()
    assert status["logged_in"] is True
    assert status["username_masked"] == "ab***ef"


def test_login_without_credentials_is_not_logged_in(fake_source):
    result = account.login()
    assert result["ok"] is False
    assert result["error_code"] == ERROR_NOT_LOGGED_IN
    assert fake_source.login_calls == []


def test_login_invalid_credentials_classified_and_sanitized(fake_source):
    password = "WRONG-SECRET-3"
    fake_source.login_error = InvalidCredentialsError(f"密码错误 {password}")
    account.set_credentials("wrong_user", password)

    result = account.login()

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_CREDENTIALS
    assert password not in json.dumps(result, ensure_ascii=False)
    assert "***" in result["error_message"]

    records = "\n".join(diagnostics.recent())
    assert password not in records
    assert "***" in records


def test_login_network_failure_classified_by_type(fake_source):
    password = "NET-SECRET-4"
    fake_source.login_error = ConnectionError(f"网络断开 {password}")
    account.set_credentials("net_user", password)

    result = account.login()

    assert result["ok"] is False
    assert result["error_code"] == ERROR_NETWORK
    assert password not in json.dumps(result, ensure_ascii=False)


def test_login_false_return_is_invalid_credentials(fake_source):
    fake_source.login_result = False
    account.set_credentials("user_1", "PWD-123456")

    result = account.login()

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_CREDENTIALS


def test_login_without_source_support_is_not_available():
    class NoLoginSource:
        pass

    jm_source.set_source(NoLoginSource())
    account.set_credentials("user_1", "PWD-123456")

    result = account.login()
    assert result["ok"] is False
    assert result["error_code"] == "NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# ensure_login 三态
# ---------------------------------------------------------------------------


def test_ensure_login_already_logged_in_does_not_call_source(fake_source):
    account.set_credentials("user_1", "PWD-123456")
    account.login()
    assert len(fake_source.login_calls) == 1

    result = account.ensure_login()

    assert result["ok"] is True
    assert result["logged_in"] is True
    assert len(fake_source.login_calls) == 1


def test_ensure_login_with_credentials_logs_in(fake_source):
    account.set_credentials("user_1", "PWD-123456")

    result = account.ensure_login()

    assert result["ok"] is True
    assert result["logged_in"] is True
    assert result["username_masked"] == "us***_1"
    assert len(fake_source.login_calls) == 1


def test_ensure_login_without_credentials_returns_not_logged_in(fake_source):
    result = account.ensure_login()

    assert result["ok"] is False
    assert result["logged_in"] is False
    assert result["error_code"] == ERROR_NOT_LOGGED_IN
    assert fake_source.login_calls == []


# ---------------------------------------------------------------------------
# 并发：两个线程同时 ensure_login 只登录一次
# ---------------------------------------------------------------------------


def test_concurrent_ensure_login_logs_in_once():
    source = FakeSource(login_delay=0.15)
    jm_source.set_source(source)
    account.set_credentials("user_1", "PWD-123456")

    barrier = threading.Barrier(3)
    results: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(5)
        result = account.ensure_login()
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(5)
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert len(source.login_calls) == 1
    assert len(results) == 2
    assert all(item["ok"] is True and item["logged_in"] is True for item in results)


def test_concurrent_ensure_login_failure_is_shared():
    source = FakeSource(
        login_error=InvalidCredentialsError("账号或密码不正确"),
        login_delay=0.15,
    )
    jm_source.set_source(source)
    account.set_credentials("user_1", "PWD-123456")

    barrier = threading.Barrier(3)
    results: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(5)
        result = account.ensure_login()
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(5)
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert len(source.login_calls) == 1
    assert len(results) == 2
    assert all(item["error_code"] == ERROR_INVALID_CREDENTIALS for item in results)


def test_clear_during_login_discards_result():
    """登录过程中清空凭据：在途结果作废，不能把旧账号会话提交上去。"""
    entered = threading.Event()
    release = threading.Event()

    class GatedSource(FakeSource):
        def login(self, username, password):
            entered.set()
            release.wait(5)
            return True

    source = GatedSource()
    jm_source.set_source(source)
    account.set_credentials("user_1", "PWD-123456")

    result_box: list[dict] = []
    thread = threading.Thread(
        target=lambda: result_box.append(account.ensure_login()), daemon=True
    )
    thread.start()
    assert entered.wait(5)

    account.clear_credentials()
    release.set()
    thread.join(10)
    assert not thread.is_alive()

    assert result_box[0]["ok"] is False
    assert result_box[0]["error_code"] == ERROR_CANCELLED
    assert account.login_status()["logged_in"] is False


# ---------------------------------------------------------------------------
# 登出 / 一次性登录
# ---------------------------------------------------------------------------


def test_logout_keeps_credentials_and_can_login_again(fake_source):
    account.set_credentials("user_1", "PWD-123456")
    account.login()

    result = account.logout()

    assert result["ok"] is True
    assert result["logged_in"] is False
    assert result["credentials_kept"] is True
    assert fake_source.logout_calls == 1

    again = account.ensure_login()
    assert again["ok"] is True
    assert len(fake_source.login_calls) == 2


def test_login_with_does_not_store_credentials(fake_source):
    password = "ONE-SHOT-SECRET-5"

    result = account.login_with("once_user", password)

    assert result["ok"] is True
    assert result["logged_in"] is True
    assert result["username_masked"] == "on***er"
    assert password not in json.dumps(result, ensure_ascii=False)
    assert fake_source.login_calls == [("once_user", password)]

    logged_out = account.logout()
    assert logged_out["credentials_kept"] is False
    assert account.login_status()["username_masked"] == ""

    after = account.login()
    assert after["error_code"] == ERROR_NOT_LOGGED_IN


def test_login_with_rejects_empty_credentials(fake_source):
    result = account.login_with("", "")
    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_ARGUMENT
    assert fake_source.login_calls == []


# ---------------------------------------------------------------------------
# 真实登录验证辅助（仅测试；产品代码不读取该文件）
# ---------------------------------------------------------------------------


def test_load_test_credentials_helper_is_test_only(tmp_path, monkeypatch):
    from tests.conftest import load_test_credentials

    monkeypatch.delenv("JML_TEST_CREDENTIALS_FILE", raising=False)
    assert load_test_credentials() is None

    path = tmp_path / "local-creds.json"
    path.write_text(
        json.dumps({"username": "helper_user", "password": "HELPER-FAKE-PWD"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("JML_TEST_CREDENTIALS_FILE", str(path))

    assert load_test_credentials() == ("helper_user", "HELPER-FAKE-PWD")

    path.write_text(json.dumps({"username": "", "password": ""}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_test_credentials()
