"""连通性测试插件：页面对象与 ``setup(ctx)``。

本插件回答一个问题：当前这台机器、这条网络，现在能访问哪个禁漫网页域名？

分工（方案 §3.1）：
- ``probe.py``：单域 HTTP 探测与 301/302 重定向链，纯函数，``fetch`` 由本模块注入；
- 本模块：组装候选、4 路并发调度、28 秒总预算、排序去重、拼 ``list`` 模板 JSON、
  单行重测动作。

只读红线（方案 §8）：
- 不调用 jmcomic 的 ``get_html_domain()`` / ``get_html_domain_all()``（会写域名缓存）；
- 不读写 ``DOMAIN_HTML`` / ``DOMAIN_HTML_LIST``，不 import ``jml.core.jm_source``；
- 不写 ``ctx.config_set`` / ``ctx.data_dir``；
- 只读 ``JmModuleConfig`` 的 ``DEFAULT_PROXIES`` / ``JM_REDIRECT_URL`` / ``JM_PUB_URL``。

候选来源（方案 §2.6）只接前 3 行：永久网域入口、发布页入口、3 个静态兜底；
第 4/5 行（``DOMAIN_HTML`` / ``DOMAIN_HTML_LIST``）默认关闭，见 ``_candidates`` 的注释。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlsplit

import probe

# 错误类与其它插件保持一致（favorites 同款），宿主能转成 INVALID_ARGUMENT。
from jml.core.errors import InvalidArgumentError

#: 本插件在 manifest 中声明的页面 id（必须与 plugin.json 完全一致）。
PAGE_ID = "connectivity_test.main"

#: 一轮测试的总预算（秒）：入口链最坏 20 秒（4 跳 × 5 秒），
#: 再加 redirect_target 的 8 秒（方案 §5.1；单跳超时 2026-11-13 拍板提到 5 秒）。
TOTAL_BUDGET = 28.0

#: 单个裸域名的请求超时（秒），与 jm_source.HTML_CHANNEL_TIMEOUT 一致。
DOMAIN_TIMEOUT = 8.0

#: 入口重定向链的单跳超时（秒）。用户 2026-11-13 拍板从 3 秒提到 5 秒：
#: 实测发布页耗时 0.6–3.0 秒波动，3 秒会偶发误判（已实测到 3006ms 被误杀）。
#: 4 跳最坏 20 秒，仍在 28 秒总预算内（静态域 8 秒并发、redirect_target 再 8 秒）。
HOP_TIMEOUT = 5.0

#: 入口重定向链最多跟随的跳数。
MAX_HOPS = 4

#: 组装阶段的候选上限（含两个入口）；redirect_target 是运行期追加，不计入。
MAX_CANDIDATES = 8

#: 并发线程数。
MAX_WORKERS = 4

#: 每页条数（结果最多 9 行，不真正翻页）。
PAGE_SIZE = 20

#: 等待线程池完成时的轮询上限（秒），保证预算到点能及时返回。
POLL_INTERVAL = 0.2

#: 角色中文文案（方案 §14.3 固定值，界面 ``author`` 字段用）。
ROLE_TEXT = {
    "redirect_entry": "永久网域（入口）",
    "publish_entry": "发布页入口",
    "redirect_target": "重定向目标",
    "static_fallback": "静态兜底",
    "jmcomic_cache": "jmcomic 缓存",
    "jmcomic_list": "jmcomic 配置",
}

#: 判定中文文案（方案 §14.3 固定值，界面 ``result`` 字段用）。
RESULT_TEXT = {
    "ok": "可用",
    "restricted": "受限",
    "fail": "不可用",
    "pending": "未完成",
}

#: 同域去重的角色优先级（方案 §4.3 第 6 条，数字越小越优先）。
#: redirect_entry 不参与竞争，见 ``_dedupe``。
_ROLE_PRIORITY = {
    "redirect_target": 0,
    "publish_entry": 1,
    "jmcomic_list": 2,
    "jmcomic_cache": 3,
    "static_fallback": 4,
}

#: 排序分组（方案 §4.3 第 1-3 条）：0/1/2 固定在前，其余按判定权重。
_SORT_GROUP = {
    "redirect_entry": 0,
    "publish_entry": 1,
    "redirect_target": 2,
}

#: 其余行的判定权重（方案 §4.3 第 4 条）：ok > restricted > fail > pending。
_VERDICT_WEIGHT = {
    "ok": 0,
    "restricted": 1,
    "fail": 2,
    "pending": 3,
}


# ---------------------------------------------------------------------------
# 纯函数：字段清洗（全部只产出 JSON 基本类型）
# ---------------------------------------------------------------------------


def _text(value) -> str:
    """把外部值转成去掉首尾空白的字符串（None → 空串）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def _to_int(value) -> int:
    """尽力把外部值转成非负 int（失败 / 负数返回 0，bool 不算数字）。"""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _positive_int(value, default: int) -> int:
    """把分页参数转成正整数；非法 / 非正数返回 default（bool 不算数字）。"""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _now_text() -> str:
    """本轮开始时刻，格式 ``YYYY-MM-DD HH:MM:SS``（写进 description 用）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _normalize_domain(value) -> str:
    """域名规范化：去空白转小写；含 ``/`` 或 ``:`` 的跳过；不含 ``.`` 的跳过。"""
    text = _text(value).strip().lower()
    if not text:
        return ""
    if "/" in text or ":" in text:
        return ""
    if "." not in text:
        return ""
    return text


def _host_text(url) -> str:
    """从 URL 里取规范化后的 host；取不到返回空串。"""
    text = _text(url)
    if not text:
        return ""
    try:
        return _normalize_domain(urlsplit(text).hostname or "")
    except Exception:
        return ""


def _entry_endpoint(url) -> str:
    """入口 URL 规范化：没有路径时补一个 ``/``（与方案示例的写法一致）。"""
    text = _text(url)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    if parts.path:
        return text
    return f"{parts.scheme}://{parts.netloc}/"


def _jmcomic_value(name: str):
    """只读 jmcomic 的模块级常量；导入失败 / 字段缺失返回 None。

    只做属性读取，不调用任何 jmcomic 方法（方案 §8 允许的最小破例）。
    """
    try:
        import jmcomic
        return getattr(jmcomic.JmModuleConfig, name, None)
    except Exception:
        return None


def _clean_hops(value) -> list:
    """把 ``hop_chain`` 清洗成 ``[{"url": str, "status": int}]``，丢弃畸形项。"""
    hops = []
    if not isinstance(value, (list, tuple)):
        return hops
    for hop in value:
        if isinstance(hop, dict):
            hops.append(
                {"url": _text(hop.get("url")), "status": _to_int(hop.get("status"))}
            )
    return hops


# ---------------------------------------------------------------------------
# HTTP 注入点：curl_cffi 惰性 import（方案 §7.2）
# ---------------------------------------------------------------------------


def _ensure_curl_cffi() -> None:
    """确认 curl_cffi 可用；缺失时抛 RuntimeError（宿主显示 UNKNOWN_ERROR）。"""
    try:
        import curl_cffi  # noqa: F401  仅探活，真正的客户端在 _fetch 里取
    except Exception:
        raise RuntimeError("缺少 curl_cffi，无法执行连通性测试") from None


def _fetch(url, timeout: float = DOMAIN_TIMEOUT, referer: str = ""):
    """真实 HTTP 实现，注入给 ``probe`` 的 fetch 参数（方案 §4.1）。

    - ``impersonate="chrome"``：沿用 jmcomic 的 TLS 指纹，少一个被 403 的理由；
    - ``allow_redirects=False``：本插件手动跟 301/302；
    - ``proxies``：只读 jmcomic 的 DEFAULT_PROXIES，跟随系统代理；None 表示直连。
    """
    # 惰性 import：缺库时不能让插件在管理页直接"加载失败"（方案 §7.2）。
    from curl_cffi import requests as creq

    return creq.get(
        url,
        timeout=timeout,
        impersonate="chrome",
        allow_redirects=False,
        proxies=probe.proxies_from_jmcomic(),
        headers={
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
        },
    )


# ---------------------------------------------------------------------------
# 单行构造：把 probe 的返回值收口成方案 §5.3 的数据模型
# ---------------------------------------------------------------------------


def _row_from_result(item: dict, result) -> dict:
    """把 ``probe`` 返回的字典拼成本行（字段全部为 JSON 基本类型）。"""
    payload = result if isinstance(result, dict) else {}
    verdict = _text(payload.get("verdict"))
    if verdict not in RESULT_TEXT:
        # probe 只会给 ok/restricted/fail；未知值一律按不可用处理，避免界面空白。
        verdict = "fail"
    return {
        "domain": _text(item.get("domain")),
        "role": _text(item.get("role")),
        "endpoint": _text(item.get("endpoint")),
        "verdict": verdict,
        "http_status": _to_int(payload.get("http_status")),
        "latency_ms": _to_int(payload.get("latency_ms")),
        "final_url": _text(payload.get("final_url")),
        "hop_chain": _clean_hops(payload.get("hop_chain")),
        "error": _text(payload.get("error"))[:200],
        # 内部字段：只用来拼 description，不进最终行（方案 §5.3 / §6.3）。
        "detail": _text(payload.get("detail"))[:200],
        # 行自己的测试时刻（内部字段，不进行）。用户 2026-11-13 拍板：
        # 每行独立记录时间，重测只刷新该行，满足验收 §9.3 第 5 条。
        "checked_at": "",
    }


def _exception_row(item: dict, exc: BaseException) -> dict:
    """任务抛出意外异常时收口成 fail 行，不影响其它行（方案 §7.1）。"""
    name = type(exc).__name__
    message = _text(exc)[:200]
    return {
        "domain": _text(item.get("domain")),
        "role": _text(item.get("role")),
        "endpoint": _text(item.get("endpoint")),
        "verdict": "fail",
        "http_status": 0,
        "latency_ms": 0,
        "final_url": "",
        "hop_chain": [],
        "error": f"{name}: {message}" if message else name,
        "detail": name,
        "checked_at": "",
    }


def _pending_row(item: dict) -> dict:
    """到总预算仍未完成的任务：标记 pending，不阻塞页面返回（方案 §4.2）。"""
    return {
        "domain": _text(item.get("domain")),
        "role": _text(item.get("role")),
        "endpoint": _text(item.get("endpoint")),
        "verdict": "pending",
        "http_status": 0,
        "latency_ms": 0,
        "final_url": "",
        "hop_chain": [],
        "error": "",
        "detail": "超过预算",
        "checked_at": "",
    }


def _resolve_row(item: dict, future) -> dict:
    """取一个 future 的结果并收口；异常只影响本行（方案 §7.1）。"""
    try:
        return _row_from_result(item, future.result())
    except Exception as exc:
        return _exception_row(item, exc)


# ---------------------------------------------------------------------------
# 页面模板映射：去重、排序、拼行
# ---------------------------------------------------------------------------


def _dedupe(rows: list) -> list:
    """同域去重（方案 §4.3 第 6 条）。

    - ``redirect_entry`` 与 ``publish_entry`` **都不参与去重**，固定保留：
      依据验收 §1.3「表里至少有入口行和发布页行」+ 用户 2026-11-13 拍板
      （发布页最终域恰好等于 redirect_target 时，两行都要在）；
    - 其余角色按 ``redirect_target > jmcomic_list > jmcomic_cache >
      static_fallback`` 保留优先级最高的一行
      （``_ROLE_PRIORITY`` 里的 ``publish_entry`` 条目已走不到，留作完整对照）；
    - domain 为空的行不参与去重（正常不会出现）。
    """
    kept = []
    best: dict[str, tuple[int, dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = _text(row.get("role"))
        domain = _text(row.get("domain")).lower()
        if role in ("redirect_entry", "publish_entry"):
            kept.append(row)
            continue
        if not domain:
            kept.append(row)
            continue
        priority = _ROLE_PRIORITY.get(role, 99)
        current = best.get(domain)
        if current is None or priority < current[0]:
            best[domain] = (priority, row)
    kept.extend(item[1] for item in best.values())
    return kept


def _sort_key(row: dict) -> tuple:
    """排序键（方案 §4.3 第 1-5 条）。

    入口第一、发布页第二、重定向目标第三；其余按判定权重，
    同权重按 latency_ms 升序（再同则按域名，保证结果稳定）。
    """
    role = _text(row.get("role"))
    domain = _text(row.get("domain")).lower()
    group = _SORT_GROUP.get(role)
    if group is not None:
        return (group, 0, 0, domain)
    return (
        3,
        _VERDICT_WEIGHT.get(_text(row.get("verdict")), 9),
        _to_int(row.get("latency_ms")),
        domain,
    )


def _map_row(row: dict, description: str) -> dict:
    """把内部行映射成 list 模板行（方案 §6.3）。

    只输出协议认识的字段：``title/author/result/description`` 加随行回传的
    ``domain/role/endpoint/verdict/http_status/latency_ms/final_url/hop_chain/error``；
    不放 ``time`` / ``updated`` / ``detail``。
    """
    role = _text(row.get("role"))
    verdict = _text(row.get("verdict"))
    return {
        "title": _text(row.get("domain")),
        "author": ROLE_TEXT.get(role, role),
        "result": RESULT_TEXT.get(verdict, ""),
        "description": _text(description),
        "domain": _text(row.get("domain")),
        "role": role,
        "endpoint": _text(row.get("endpoint")),
        "verdict": verdict,
        "http_status": _to_int(row.get("http_status")),
        "latency_ms": _to_int(row.get("latency_ms")),
        "final_url": _text(row.get("final_url")),
        "hop_chain": _clean_hops(row.get("hop_chain")),
        "error": _text(row.get("error"))[:200],
    }


# ---------------------------------------------------------------------------
# 页面对象
# ---------------------------------------------------------------------------


class ConnectivityPage:
    """连通性测试页面：一轮测试 → 去重排序 → list JSON；支持单行重测。

    行时间戳按行独立保存（用户 2026-11-13 拍板）：``load`` 把本轮时间刷到每行，
    ``action_retest`` 只刷新被重测的那一行，满足验收 §9.3 第 5 条。
    """

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        # 本轮结果（只存 JSON 基本类型）与本轮开始时刻（行时间的兜底值）。
        self._rows: list[dict] = []
        self._checked_at = ""

    # -- 候选组装 -----------------------------------------------------------

    def _candidates(self) -> list[dict]:
        """组装本轮候选 ``[{"role","domain","endpoint"}]``（方案 §2.6）。

        只接前 3 行（入口、发布页、3 个静态兜底），按 domain 去重，
        截断到 ``MAX_CANDIDATES``（入口永远保留，因为它在最前面加入）。

        第 4/5 行（``DOMAIN_HTML`` / ``DOMAIN_HTML_LIST``）**默认关闭**：
        方案 §2.6 把这两个来源列为可选，§8 红线又明确禁止读写这两个字段，
        因此这里不接、也不实现读取代码——不 import ``jml_source``，
        不 ``getattr`` 这两个字段。
        """
        items: list[dict] = []
        seen: set[str] = set()

        # 1. 永久网域入口（只读 JM_REDIRECT_URL，失败用内置常量）。
        entry_url = _entry_endpoint(_jmcomic_value("JM_REDIRECT_URL") or probe.ENTRY_URL)
        entry_domain = _host_text(entry_url)
        if entry_domain:
            items.append(
                {"role": "redirect_entry", "domain": entry_domain, "endpoint": entry_url}
            )
            seen.add(entry_domain)

        # 2. 发布页入口（只读 JM_PUB_URL，失败用内置常量）。
        pub_url = _entry_endpoint(_jmcomic_value("JM_PUB_URL") or probe.PUB_URL)
        pub_domain = _host_text(pub_url)
        if pub_domain and pub_domain not in seen:
            items.append(
                {"role": "publish_entry", "domain": pub_domain, "endpoint": pub_url}
            )
            seen.add(pub_domain)

        # 3. 插件内置静态兜底（与 Python/jml/core/jm_source.py:245-249 同步）。
        for raw in probe.STATIC_DOMAINS:
            if len(items) >= MAX_CANDIDATES:
                break
            domain = _normalize_domain(raw)
            if not domain or domain in seen:
                continue
            items.append(
                {
                    "role": "static_fallback",
                    "domain": domain,
                    "endpoint": f"https://{domain}/",
                }
            )
            seen.add(domain)

        return items[:MAX_CANDIDATES]

    # -- 单任务 -------------------------------------------------------------

    @staticmethod
    def _probe_item(item: dict):
        """按角色选择一个探测函数（方案 §4.1）。"""
        role = _text(item.get("role"))
        endpoint = _text(item.get("endpoint"))
        if role == "redirect_entry":
            # 302 收口：Location 就是当前漫画站域名。
            return probe.redirect_probe(
                endpoint, _fetch, MAX_HOPS, HOP_TIMEOUT, True
            )
        if role == "publish_entry":
            # 301/302 都继续跟，直到 200 收口（发布页入口目前是 301 → 200）。
            return probe.redirect_probe(
                endpoint, _fetch, MAX_HOPS, HOP_TIMEOUT, False
            )
        return probe.probe_domain(item.get("domain"), _fetch, DOMAIN_TIMEOUT)

    # -- 一轮调度 -----------------------------------------------------------

    def _run_round(self) -> list[dict]:
        """跑一轮：4 路并发 + 28 秒总预算，返回内部行列表（方案 §3.2 / §5.1）。

        - 线程池手动 ``shutdown(wait=False, cancel_futures=True)``，不用 ``with``；
        - 入口链完成后追加一个 ``redirect_target`` 任务，同一轮里等它；
        - 到 deadline 仍未完成的任务记为 ``pending``，不阻塞返回。
        """
        candidates = self._candidates()
        if not self._checked_at:
            self._checked_at = _now_text()

        deadline = time.monotonic() + TOTAL_BUDGET
        rows: list[dict] = []
        # 入口 / 发布页自身不重复追加成 redirect_target；target 之间也不重复。
        # 与静态候选同域时仍要追加：去重时 redirect_target 优先（方案 §1.3 / §4.3）。
        reserved_domains = {
            item["domain"]
            for item in candidates
            if item["role"] in ("redirect_entry", "publish_entry")
        }
        target_domains: set[str] = set()
        pending: dict = {}

        # 不用 with：with 退出会 wait=True，预算到点后仍要等最慢的任务（方案 §5.1）。
        pool = ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix="jml-conn"
        )
        try:
            for item in candidates:
                future = pool.submit(self._probe_item, item)
                pending[future] = item

            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _not_done = wait(
                    list(pending), timeout=min(remaining, POLL_INTERVAL)
                )
                if not done:
                    continue
                for future in done:
                    item = pending.pop(future)
                    row = _resolve_row(item, future)
                    rows.append(row)
                    if item["role"] != "redirect_entry":
                        continue
                    # 入口链解析出最终域名：合法、不是入口自身、且未追加过才追加。
                    host = _host_text(row.get("final_url"))
                    if not host or host in reserved_domains or host in target_domains:
                        continue
                    target_domains.add(host)
                    target_item = {
                        "role": "redirect_target",
                        "domain": host,
                        "endpoint": f"https://{host}/",
                    }
                    target_future = pool.submit(self._probe_item, target_item)
                    pending[target_future] = target_item
        finally:
            # 已运行的任务最多再跑 8 秒（受 curl 超时约束），不写共享状态。
            pool.shutdown(wait=False, cancel_futures=True)

        # 预算到点 / 被取消（含 cancel_futures 取消的）任务统一记 pending。
        for item in pending.values():
            rows.append(_pending_row(item))
        return rows

    # -- 模板 payload -------------------------------------------------------

    def _clock_text(self, checked_at=None) -> str:
        """description 里用的时分秒（``HH:MM:SS``）。

        优先用该行自己的 ``checked_at``，取不到才回退本轮开始时刻
        （用户 2026-11-13 拍板：行时间独立，重测只刷新该行）。
        """
        stamp = _text(checked_at) or _text(self._checked_at)
        if len(stamp) >= 19:
            return stamp[11:19]
        return stamp or "--:--:--"

    def _description(self, row: dict) -> str:
        """按角色与判定拼 description（方案 §6.2 / §6.3，用户 2026-11-13 拍板）。

        - 入口 / 发布页：
          - ok：``N 跳 · HTTP 状态 · HH:MM:SS · 最终 host``；
          - 非 ok：``N 跳 · 标签 · HH:MM:SS``（标签取行 ``detail``，如
            ``DNSError`` / ``Timeout``；为空回退「失败」）——失败时没有最终地址，
            不再显示"最终 host"。
        - 普通 ok：``HTTP 状态 · 耗时ms · HH:MM:SS · 最终地址``
        - restricted：``细分文案 · HTTP 状态 · HH:MM:SS``
        - 普通 fail：``标签 · HH:MM:SS``（标签取行 ``detail``，为空回退「不可用」；
          不把完整 error 消息塞进 description，error 仍随行回传）
        - pending：``超过预算 · HH:MM:SS``

        时间取本行 ``checked_at``（重测只改这一行），不再统一用 ``self._checked_at``。
        """
        role = _text(row.get("role"))
        verdict = _text(row.get("verdict"))
        stamp = self._clock_text(row.get("checked_at"))
        status = _to_int(row.get("http_status"))
        detail = _text(row.get("detail"))

        if role in ("redirect_entry", "publish_entry"):
            hops = row.get("hop_chain") or []
            if verdict == "ok":
                final_host = (
                    _host_text(row.get("final_url"))
                    or _text(row.get("final_url"))
                    or "未知"
                )
                return f"{len(hops)} 跳 · HTTP {status} · {stamp} · 最终 {final_host}"
            # 失败/受限：没有最终地址，只报跳数与异常标签。
            return f"{len(hops)} 跳 · {detail or '失败'} · {stamp}"

        if verdict == "ok":
            latency = _to_int(row.get("latency_ms"))
            final_url = _text(row.get("final_url")) or _text(row.get("endpoint"))
            return f"HTTP {status} · {latency}ms · {stamp} · {final_url}"

        if verdict == "restricted":
            return f"{detail or '受限'} · HTTP {status} · {stamp}"

        if verdict == "fail":
            # 只显示异常类名（detail），error 的完整消息留给行数据，界面不展示。
            return f"{detail or '不可用'} · {stamp}"

        return f"超过预算 · {stamp}"

    def _page_payload(self, page) -> dict:
        """把 ``_rows`` 拼成 list 模板 JSON（方案 §6.2 字段，§6.3 行映射）。"""
        with self._lock:
            snapshot = list(self._rows)

        kept = _dedupe(snapshot)
        kept.sort(key=_sort_key)
        rows = [_map_row(row, self._description(row)) for row in kept]

        total = len(rows)
        page_no = _positive_int(page, 1)
        return {
            "columns": [
                {"key": "http_status", "title": "HTTP"},
                {"key": "latency_ms", "title": "耗时(ms)"},
            ],
            "rows": rows,
            "page": page_no,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "row_actions": [{"id": "retest", "title": "重测", "primary": True}],
            "empty_text": "没有可测试的网页域名。",
        }

    # -- 页面加载 -----------------------------------------------------------

    def load(self, params: dict) -> dict:
        """``plugin_page_load`` 钩子：跑一轮测试并返回整页数据。"""
        _ensure_curl_cffi()
        params = params if isinstance(params, dict) else {}
        page = _positive_int(params.get("page"), 1)

        self._checked_at = _now_text()
        self._ctx.log("连通性测试：开始测试")

        rows = self._run_round()
        # 每行独立记下本轮时间；重测只刷新被重测的那一行（用户 2026-11-13 拍板）。
        for row in rows:
            row["checked_at"] = self._checked_at
        with self._lock:
            self._rows = rows

        ok_count = sum(1 for row in rows if row.get("verdict") == "ok")
        # 只写汇总，不逐域打印（方案 §7.3）。
        self._ctx.log(f"连通性测试：完成 {len(rows)} 项，可用 {ok_count} 项")
        return self._page_payload(page)

    # -- 单行重测 -----------------------------------------------------------

    @staticmethod
    def _endpoint_for(role: str, domain: str) -> str:
        """endpoint 缺失时按角色重建（方案 §6.4 第 3 条）。"""
        if role == "redirect_entry":
            url = _entry_endpoint(_jmcomic_value("JM_REDIRECT_URL") or probe.ENTRY_URL)
            if url:
                return url
        elif role == "publish_entry":
            url = _entry_endpoint(_jmcomic_value("JM_PUB_URL") or probe.PUB_URL)
            if url:
                return url
        return f"https://{domain}/"

    @staticmethod
    def _locate(payload: dict, rows: list, domain: str, role: str):
        """在 ``_rows`` 里定位待替换行，返回下标；找不到返回 None。

        以 ``(domain, role)`` 为主键；``row_index`` 只作提示（方案 §6.4 / 审阅 H2）。
        """
        try:
            row_index = int(payload.get("row_index", -1))
        except (TypeError, ValueError):
            row_index = -1
        if 0 <= row_index < len(rows):
            candidate = rows[row_index]
            if isinstance(candidate, dict) and (
                _text(candidate.get("domain")).lower() == domain
            ):
                return row_index
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            if _text(item.get("domain")).lower() != domain:
                continue
            if _text(item.get("role")) == role:
                return index
        for index, item in enumerate(rows):
            if isinstance(item, dict) and (
                _text(item.get("domain")).lower() == domain
            ):
                return index
        return None

    def action_retest(self, payload: dict) -> dict:
        """``retest`` 动作：只重测该行，然后用 ``data`` 替换整页（方案 §6.4）。

        找不到对应行时追加到末尾，不抛错（一次过期点击不该让整页报错）。
        """
        _ensure_curl_cffi()
        payload = payload if isinstance(payload, dict) else {}
        row = payload.get("row")
        row = row if isinstance(row, dict) else {}

        domain = _text(row.get("domain")).lower()
        if not domain:
            raise InvalidArgumentError("重测参数缺少 domain")

        # role 缺失时按静态兜底处理（任务规格；不按入口处理）。
        role = _text(row.get("role")) or "static_fallback"
        endpoint = _text(row.get("endpoint")) or self._endpoint_for(role, domain)
        item = {"domain": domain, "role": role, "endpoint": endpoint}

        # 网络探测放在锁外：入口链最长 20 秒，不能阻塞下一次 load 的读取。
        new_row = _row_from_result(item, self._probe_item(item))
        # 只刷新这一行的时间（用户 2026-11-13 拍板）；self._checked_at 保持本轮值。
        new_row["checked_at"] = _now_text()

        if not _text(self._checked_at):
            self._checked_at = _now_text()

        page = _positive_int(payload.get("page"), 1)
        with self._lock:
            rows = self._rows
            index = self._locate(payload, rows, domain, role)
            if index is None:
                rows.append(new_row)
            else:
                rows[index] = new_row

        return {
            "ok": True,
            "data": self._page_payload(page),
            "message": f"已重测 {domain}",
        }


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------


def setup(ctx) -> None:
    """注册页面与单行重测动作（不联网、不起线程，方案 §14.4）。"""
    page = ConnectivityPage(ctx)
    ctx.register_page(PAGE_ID, page.load)
    ctx.register_page_action(PAGE_ID, "retest", page.action_retest)
