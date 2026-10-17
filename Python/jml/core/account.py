"""账号与登录态（阶段 3）。

安全纪律（对应功能计划书 4.8 / 11.2，技术方案第六章）：

- **凭据只存在进程内存**：本模块只把账号密码放在 ``_STATE``（显式
  ``threading.RLock`` 保护），不写文件、不进日志、不进异常消息；
- 对外只回传打码后的用户名（``ab***cd`` / ``***``），任何入口都不回传
  明文密码；
- 凭据的持久化由 C# 侧负责（Windows 凭据管理器 / DPAPI），Python 只接收
  C# 读出的凭据；真实账号不硬编码；
- 登录失败按**异常类型**分类（``INVALID_CREDENTIALS`` / ``NETWORK_ERROR``
  / ``TIMEOUT`` / ``JM_ERROR`` ...），不靠异常文案猜；
- 登录过程可能耗时：``_STATE_LOCK`` 只用于快照凭据与提交状态，
  **网络请求在锁外执行**；同一时刻只允许一个登录在途，其余线程等待
  同一个结果（单飞），避免重复登录。

对数据源的依赖是鸭子类型（见 ``jm_source`` 协议）：

    source.login(username, password) -> bool
    source.logout() -> None
    source.is_logged_in() -> bool

假数据源只需实现 ``login``（抛可分类异常或返回 True/False）。
"""

from __future__ import annotations

import threading
from urllib.parse import quote, quote_plus

from jml.core import diagnostics, jm_source
from jml.core.errors import (
    ERROR_CANCELLED,
    ERROR_INVALID_ARGUMENT,
    ERROR_INVALID_CREDENTIALS,
    ERROR_JM,
    ERROR_MESSAGES,
    ERROR_NOT_AVAILABLE,
    ERROR_NOT_LOGGED_IN,
    ERROR_TIMEOUT,
    ERROR_UNKNOWN,
    classify,
    sanitize_text,
)

#: 等另一个线程完成登录的最长秒数（超时只影响等待者，不取消登录）
LOGIN_WAIT_SECONDS = 120.0

#: 状态锁：只保护 ``_STATE`` 的读写，**不得**在持锁期间访问网络
_STATE_LOCK = threading.RLock()

#: 账号模块状态。键：
#:   username        —— 内存中的明文账号（只在 set_credentials 后存在）
#:   password        —— 内存中的明文密码（绝不落盘/进日志）
#:   logged_in       —— 当前是否已登录
#:   session_masked  —— 当前登录会话的用户名打码（未登录时为空）
#:   generation      —— 凭据/登录态变更代数；在途登录据此丢弃过期结果
#:   login_event     —— 在途登录的完成事件（None 表示没有在途登录）
#:   last_error      —— 上一次登录失败的结构化原因（供等待线程复用）
_STATE: dict = {
    "username": "",
    "password": "",
    "logged_in": False,
    "session_masked": "",
    "generation": 0,
    "login_event": None,
    "last_error": None,
}


def reset_state() -> None:
    """重置账号内存状态（仅测试/开发自检用；不联网、不触盘）。

    生产路径不要调用：C# 侧的"修改账号"用 ``clear_credentials``。
    """
    with _STATE_LOCK:
        pending = _STATE.get("login_event")
        _STATE["username"] = ""
        _STATE["password"] = ""
        _STATE["logged_in"] = False
        _STATE["session_masked"] = ""
        _STATE["generation"] += 1
        _STATE["login_event"] = None
        _STATE["last_error"] = None
    # 唤醒可能正在等待的线程，避免测试重置后永久挂住。
    if pending is not None:
        pending.set()


# ---------------------------------------------------------------------------
# 对外状态形态
# ---------------------------------------------------------------------------


def _result(
    ok: bool,
    logged_in: bool,
    username_masked: str = "",
    error_code: str = "",
    error_message: str = "",
) -> dict:
    """账号类入口的统一形态（失败也带 ``logged_in`` 便于界面直接使用）。"""
    return {
        "ok": bool(ok),
        "logged_in": bool(logged_in),
        "username_masked": str(username_masked or ""),
        "error_code": str(error_code or ""),
        "error_message": str(error_message or ""),
    }


def mask_username(username: object) -> str:
    """用户名打码：``ab***cd`` / ``a***c`` / ``***``。

    - 空用户名 → 空串（未配置账号）；
    - 1~2 字符 → ``***``；
    - 3~5 字符 → 首尾各留 1 个字符；
    - 6 字符及以上 → 首尾各留 2 个字符。

    这是**唯一**允许把用户名展示给外部的形态；明文用户名不进入任何返回值。
    """
    text = "" if username is None else str(username)
    if not text:
        return ""
    length = len(text)
    if length <= 2:
        return "***"
    if length <= 5:
        return f"{text[0]}***{text[-1]}"
    return f"{text[:2]}***{text[-2:]}"


def _scrub(text: object, username: str, password: str) -> str:
    """先按已知凭据做字面打码，再走通用 ``sanitize_text``。

    异常消息可能把密码原样带出来（假数据源/第三方库都可能），
    ``sanitize_text`` 只能识别 ``password=...`` 这类键值形态，所以这里额外
    把本次登录实际使用的账号、密码字面量替换成 ``***``。
    """
    value = "" if text is None else str(text)
    secrets = [password, username]
    if password and len(password) >= 3:
        # 密码也可能以 URL 编码形态出现在请求/异常文本里。
        secrets.extend([quote(password), quote_plus(password)])
    for secret in secrets:
        if secret:
            value = value.replace(secret, "***")
    return sanitize_text(value)


def _error_message(error_code: str) -> str:
    """错误码对应的中文基础文案。"""
    return ERROR_MESSAGES.get(error_code, ERROR_MESSAGES[ERROR_UNKNOWN])


def _normalize_credentials(username: object, password: object) -> tuple[str, str, bool]:
    """校验并规整凭据；空账号/空密码/非字符串一律不合法。

    密码**不 strip**（首尾空格可能是密码的一部分），只拒绝真正的空串。
    """
    if not isinstance(username, str) or not isinstance(password, str):
        return "", "", False
    user = username.strip()
    if not user or not password:
        return "", "", False
    return user, password, True


# ---------------------------------------------------------------------------
# 状态快照（持锁调用）
# ---------------------------------------------------------------------------


def _status_locked() -> dict:
    """在持有 ``_STATE_LOCK`` 时构造当前状态。"""
    logged_in = bool(_STATE["logged_in"])
    if logged_in:
        masked = _STATE["session_masked"]
    else:
        # 未登录时仍展示"已配置账号"的打码，便于设置页显示；密码永不出现。
        masked = mask_username(_STATE["username"])
    return _result(True, logged_in, masked)


def login_status() -> dict:
    """查询登录态（永远 ``ok=true``；``error_code`` 仅在查询本身失败时才有值）。

    返回::

        {"ok": true, "logged_in": bool, "username_masked": str,
         "error_code": "", "error_message": ""}

    ``username_masked`` 规则见 :func:`mask_username`；**绝不回传明文密码**。
    """
    with _STATE_LOCK:
        return _status_locked()


# ---------------------------------------------------------------------------
# 凭据管理
# ---------------------------------------------------------------------------


def set_credentials(username: object, password: object) -> dict:
    """把账号密码放进进程内存（覆盖旧值），成功返回当前状态。

    空账号 / 空密码 → ``INVALID_ARGUMENT``。凭据变更会立即让旧登录态失效
    （``logged_in=false``），并让在途登录的结果作废。
    """
    user, pwd, valid = _normalize_credentials(username, password)
    if not valid:
        return _result(
            False,
            False,
            "",
            ERROR_INVALID_ARGUMENT,
            "账号和密码不能为空",
        )

    with _STATE_LOCK:
        previous_active = bool(
            _STATE["logged_in"] or _STATE["username"] or _STATE["password"]
        )
        changed = user != _STATE["username"] or pwd != _STATE["password"]
        if changed:
            _STATE["generation"] += 1
            _STATE["logged_in"] = False
            _STATE["session_masked"] = ""
        _STATE["username"] = user
        _STATE["password"] = pwd
        _STATE["last_error"] = None
        result = _status_locked()

    if changed and previous_active:
        # 凭据换了就让旧会话真正失效，避免旧 cookie 还留在客户端里。
        _logout_source()
    return result


def clear_credentials() -> dict:
    """清空内存中的凭据与登录态（用于"修改账号"前先彻底断开）。

    返回 ``credentials_cleared=true``；数据源的会话 cookie 也会在锁外清理。
    """
    with _STATE_LOCK:
        _STATE["username"] = ""
        _STATE["password"] = ""
        _STATE["logged_in"] = False
        _STATE["session_masked"] = ""
        _STATE["generation"] += 1
        _STATE["last_error"] = None
        result = _result(True, False, "")

    _logout_source()
    result["credentials_cleared"] = True
    return result


def logout() -> dict:
    """退出登录：**默认保留内存凭据**，只清登录态。

    返回 ``credentials_kept`` 告诉调用方内存里是否还留着可再次登录的凭据。
    """
    with _STATE_LOCK:
        _STATE["logged_in"] = False
        _STATE["session_masked"] = ""
        # 让在途登录的结果作废：用户已经明确要求退出。
        _STATE["generation"] += 1
        _STATE["last_error"] = None
        credentials_kept = bool(_STATE["username"] and _STATE["password"])
        result = _status_locked()

    _logout_source()
    result["credentials_kept"] = credentials_kept
    return result


def _logout_source() -> None:
    """尽力清理数据源的内存登录态；失败只记诊断（不抛异常）。"""
    source = jm_source.get_source()
    logout_callable = getattr(source, "logout", None)
    if not callable(logout_callable):
        return
    try:
        logout_callable()
    except Exception as exc:
        # 不把原始异常挂进诊断（异常消息可能带凭据），只留类型名。
        diagnostics.record(
            "ACCOUNT_LOGOUT_FAILED",
            f"清理数据源登录态失败: {type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def login() -> dict:
    """用**内存中的凭据**登录；成功后置登录态与打码用户名。"""
    return _login_flow(force=True)


def ensure_login() -> dict:
    """确保处于登录态。

    - 已登录：直接返回当前状态（不重复登录）；
    - 未登录且内存里有凭据：尝试登录；
    - 未登录且无凭据：``NOT_LOGGED_IN``（不抛异常、不联网）。

    并发语义：同一时刻只允许一个登录在途，其余线程等待同一个结果，
    不会重复调用数据源。
    """
    with _STATE_LOCK:
        if _STATE["logged_in"]:
            return _status_locked()
        if not (_STATE["username"] and _STATE["password"]):
            return _result(
                False,
                False,
                mask_username(_STATE["username"]),
                ERROR_NOT_LOGGED_IN,
                "尚未登录，且内存中没有账号或密码",
            )

    return _login_flow(force=False)


def login_with(username: object, password: object) -> dict:
    """一次性登录：本次凭据**不写入内存**（返回后只保留登录会话）。

    供 C# 直接把凭据管理器/DPAPI 读出的账号密码递进来。成功后会话仍在
    数据源客户端里（收藏夹需要），但 Python 侧不再保留密码；因此之后
    调用 ``ensure_login()`` 不会用这次凭据自动重登。
    """
    user, pwd, valid = _normalize_credentials(username, password)
    if not valid:
        return _result(
            False,
            False,
            "",
            ERROR_INVALID_ARGUMENT,
            "账号和密码不能为空",
        )

    _wait_for_inflight_login()

    event, generation = _claim_login_slot()
    try:
        result = _perform_login(user, pwd)

        with _STATE_LOCK:
            if _STATE["generation"] != generation:
                result = _result(
                    False,
                    False,
                    "",
                    ERROR_CANCELLED,
                    "登录过程中凭据已变更，本次登录结果已丢弃",
                )
            elif result["ok"]:
                _STATE["logged_in"] = True
                _STATE["session_masked"] = result["username_masked"]
                _STATE["last_error"] = None
            else:
                _STATE["logged_in"] = False
                _STATE["session_masked"] = ""
                _STATE["last_error"] = {
                    "error_code": result["error_code"],
                    "error_message": result["error_message"],
                }
        return result
    finally:
        with _STATE_LOCK:
            if _STATE.get("login_event") is event:
                _STATE["login_event"] = None
        event.set()


def _claim_login_slot() -> tuple[threading.Event, int]:
    """抢占一次登录资格：没有在途登录时登记事件，否则等它结束再抢。"""
    while True:
        with _STATE_LOCK:
            event = _STATE.get("login_event")
            if event is None:
                event = threading.Event()
                _STATE["login_event"] = event
                return event, _STATE["generation"]
        if event.is_set():
            # 已结束但标志还没清（异常路径）：先清掉再抢，避免死等。
            with _STATE_LOCK:
                if _STATE.get("login_event") is event:
                    _STATE["login_event"] = None
            continue
        event.wait(LOGIN_WAIT_SECONDS)


def _wait_for_inflight_login() -> None:
    """等其它线程的在途登录结束（最多 ``LOGIN_WAIT_SECONDS`` 秒）。"""
    with _STATE_LOCK:
        event = _STATE.get("login_event")
    if event is not None:
        event.wait(LOGIN_WAIT_SECONDS)


def _login_flow(force: bool) -> dict:
    """单飞登录：快照凭据 → 出锁 → 登录 → 回锁提交。"""
    with _STATE_LOCK:
        if not force and _STATE["logged_in"]:
            return _status_locked()

        if not (_STATE["username"] and _STATE["password"]):
            message = (
                "内存中没有账号或密码，无法登录"
                if force
                else "尚未登录，且内存中没有账号或密码"
            )
            return _result(
                False,
                False,
                mask_username(_STATE["username"]),
                ERROR_NOT_LOGGED_IN,
                message,
            )

        # 单飞：已有在途登录就等它，不重复请求网络。
        event = _STATE.get("login_event")
        if event is not None:
            owner = False
        else:
            owner = True
            event = threading.Event()
            _STATE["login_event"] = event

        username = _STATE["username"]
        password = _STATE["password"]
        generation = _STATE["generation"]
        masked = mask_username(username)

    if not owner:
        event.wait(LOGIN_WAIT_SECONDS)
        if not event.is_set():
            return _result(
                False,
                False,
                "",
                ERROR_TIMEOUT,
                "等待登录结果超时",
            )
        with _STATE_LOCK:
            if _STATE["logged_in"]:
                return _status_locked()
            last = _STATE.get("last_error")
        if last:
            return _result(
                False,
                False,
                "",
                last["error_code"],
                last["error_message"],
            )
        # 事件已结束却没有结果（理论上不会发生）：有凭据就再走一轮。
        return _login_flow(force=force)

    result: dict | None = None
    try:
        result = _perform_login(username, password)
    except Exception as exc:  # 兜底：绝不让等待线程永久挂住
        info = classify(exc)
        result = _result(
            False,
            False,
            "",
            info.error_code,
            _scrub(info.error_message, username, password),
        )
    finally:
        with _STATE_LOCK:
            if _STATE["generation"] != generation:
                result = _result(
                    False,
                    False,
                    "",
                    ERROR_CANCELLED,
                    "登录过程中凭据已变更，本次登录结果已丢弃",
                )
                _STATE["logged_in"] = False
                _STATE["session_masked"] = ""
                _STATE["last_error"] = {
                    "error_code": ERROR_CANCELLED,
                    "error_message": result["error_message"],
                }
            elif result is not None and result.get("ok"):
                _STATE["logged_in"] = True
                _STATE["session_masked"] = result["username_masked"] or masked
                _STATE["last_error"] = None
            else:
                _STATE["logged_in"] = False
                _STATE["session_masked"] = ""
                if result is None:
                    result = _result(
                        False,
                        False,
                        "",
                        ERROR_UNKNOWN,
                        "登录失败（数据源没有返回结果）",
                    )
                _STATE["last_error"] = {
                    "error_code": result["error_code"],
                    "error_message": result["error_message"],
                }
            pending = _STATE.get("login_event")
            if pending is event:
                _STATE["login_event"] = None
            event.set()

    return result


def _perform_login(username: str, password: str) -> dict:
    """调用数据源登录；返回账号状态形态，**绝不把原始异常文案透给外部**。"""
    source = jm_source.get_source()
    login_callable = getattr(source, "login", None)
    if not callable(login_callable):
        message = "当前数据源不支持登录"
        diagnostics.record("ACCOUNT_LOGIN", f"账号登录失败: {message}")
        return _result(False, False, "", ERROR_NOT_AVAILABLE, message)

    try:
        outcome = login_callable(username, password)
    except Exception as exc:
        info = classify(exc)
        message = _scrub(info.error_message, username, password)
        # 只记录脱敏后的类型与文案；不把原始异常（可能带凭据）挂进诊断。
        diagnostics.record(
            info.error_code,
            f"账号登录失败（{type(exc).__name__}）: {message}",
        )
        return _result(False, False, "", info.error_code, message)

    # 数据源可能返回 bool 或结构化 dict；False / {"ok": false} 都算失败。
    if outcome is False:
        message = _error_message(ERROR_INVALID_CREDENTIALS)
        diagnostics.record(ERROR_INVALID_CREDENTIALS, f"账号登录失败: {message}")
        return _result(False, False, "", ERROR_INVALID_CREDENTIALS, message)

    if isinstance(outcome, dict) and outcome.get("ok") is False:
        code = str(outcome.get("error_code") or ERROR_JM)
        message = _scrub(
            outcome.get("error_message") or _error_message(code),
            username,
            password,
        )
        diagnostics.record(code, f"账号登录失败: {message}")
        return _result(False, False, "", code, message)

    masked = mask_username(username)
    diagnostics.record("ACCOUNT_LOGIN", f"账号登录成功: {masked}")
    return _result(True, True, masked)
