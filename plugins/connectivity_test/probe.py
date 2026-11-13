"""连通性测试插件的 HTTP 探测内核。

纯函数模块：不依赖宿主、不 import 任何 HTTP 客户端库、不自己发网络请求。
所有网络请求都由调用方通过 `fetch` 注入（main.py 注入真实实现，单测注入假函数）。
`fetch(url, timeout=..., referer="")` 返回的对象需有 `status_code`、`headers`、
`text`、`url` 四个属性；`headers` 需支持 `.get("Location")`。

返回字典的键固定为：verdict / http_status / latency_ms / final_url /
hop_chain / error / detail。
"""

from __future__ import annotations

import time
from urllib.parse import urljoin, urlsplit

# 内置常量兜底：与 Python/jml/core/jm_source.py:245-249 同步。
ENTRY_URL = "https://jm365.work/3YeBdF"
PUB_URL = "https://jmcomicgo.org"
STATIC_DOMAINS = ("comic18j-moco.work", "comic18j-nico.site", "comic18j-ada.work")

# 判定表只读响应前 8KB；异常消息截断到 200 字符。
_BODY_LIMIT = 8192
_ERROR_LIMIT = 200

# 200 页面里的异常特征（文案取自 jmcomic 的 JM_ERROR_RESPONSE_TEXT / Cloudflare 挑战页）。
_MARK_REGION = "Restricted Access!"
_MARK_MYSQL = "Could not connect to mysql"
_MARK_CHALLENGE = ("Just a moment", "cf-challenge")


def _text(value) -> str:
    """把任意值安全转成字符串，None 变空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _header(resp, name: str) -> str:
    """从响应头里取一个值，兼容大小写不同的字典实现；取不到返回空串。"""
    headers = getattr(resp, "headers", None)
    if not headers:
        return ""
    for key in (name, name.lower(), name.upper()):
        try:
            value = headers.get(key)
        except Exception:
            value = None
        if value:
            return _text(value)
    try:
        items = headers.items()
    except Exception:
        return ""
    lowered = name.lower()
    try:
        for key, value in items:
            if _text(key).lower() == lowered and value:
                return _text(value)
    except Exception:
        return ""
    return ""


def _is_https_url(url: str) -> bool:
    """判断 url 是否是带主机名的 https 地址。"""
    try:
        parts = urlsplit(_text(url))
    except Exception:
        return False
    return parts.scheme.lower() == "https" and bool(parts.netloc)


def _fail_payload(exc: BaseException, latency_ms: float, final_url: str, hop_chain: list) -> dict:
    """把任意异常收口成 fail 结果，不向外抛。"""
    name = type(exc).__name__
    message = _text(exc)[:_ERROR_LIMIT]
    return {
        "verdict": "fail",
        "http_status": 0,
        "latency_ms": int(round(latency_ms)),
        "final_url": _text(final_url),
        "hop_chain": list(hop_chain),
        "error": f"{name}: {message}" if message else name,
        "detail": name,
    }


def classify(status_code: int, headers, body_text) -> tuple:
    """按方案 §4.2 判定表给出 (verdict, detail)。

    输入：状态码、响应头、响应正文（内部只取前 8KB）。
    输出：verdict 为 ok/restricted；detail 为细分文案，ok 时为空串。
    异常/超时不在本函数处理，由调用方收口成 fail。
    """
    status = int(status_code or 0)
    body = _text(body_text)[:_BODY_LIMIT]
    location = ""
    try:
        location = _text(headers.get("Location") if hasattr(headers, "get") else "") or _text(
            headers.get("location") if hasattr(headers, "get") else ""
        )
    except Exception:
        location = ""

    # 正文异常特征优先：这些页面即使状态码是 200（或 403/503）也说明站点不可用。
    if _MARK_REGION in body:
        return "restricted", "地区受限"
    if _MARK_MYSQL in body:
        return "restricted", "服务端异常"
    if any(mark in body for mark in _MARK_CHALLENGE):
        return "restricted", "人机验证"

    if status == 200:
        return "ok", ""
    if status in (301, 302) and _is_https_url(location):
        return "ok", ""
    if status == 403:
        return "restricted", "访问被拒"
    if status == 429:
        return "restricted", "请求过多"
    if 400 <= status < 500:
        return "restricted", "访问被拒"
    if 500 <= status < 600:
        return "restricted", "服务端异常"
    # 其它状态码（1xx、3xx 非 301/302、0 等）按"不可用"处理，避免误判为可用。
    return "restricted", "访问被拒"


def _normalize_proxy(value):
    """把 ProxyBuilder 的 host:port 补成带协议的 URL；空值返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    # ProxyBuilder 返回的是 "host:port"，curl_cffi 需要带协议的 URL。
    return text if "://" in text else f"http://{text}"


def proxies_from_jmcomic() -> dict | None:
    """只读 jmcomic 的系统代理配置，返回 curl_cffi 可用的 proxies 字典或 None。

    惰性 import jmcomic，只读 JmModuleConfig.DEFAULT_PROXIES，不调用任何方法、不写字段。
    返回 None 表示直连（jmcomic 不可用或系统没配代理）。
    """
    try:
        import jmcomic
        raw = getattr(jmcomic.JmModuleConfig, "DEFAULT_PROXIES", None)
    except Exception:
        return None  # jmcomic 不可用：直连
    if not raw:
        return None  # 系统没配代理：直连
    if isinstance(raw, dict):
        pairs = {k: _normalize_proxy(v) for k, v in raw.items()}
        pairs = {k: v for k, v in pairs.items() if v}
        return pairs or None
    if isinstance(raw, str):
        value = _normalize_proxy(raw)
        return {"http": value, "https": value} if value else None
    return None


def redirect_probe(entry_url: str, fetch, max_hops: int = 4, hop_timeout: float = 3.0,
                   stop_on_302: bool = True) -> dict:
    """手动跟随入口 URL 的重定向链，返回一条完整链路的探测结果。

    输入：入口 URL、注入的 fetch、最多跳数、单跳超时、是否在 302 处收口。
    输出：verdict/http_status/latency_ms/final_url/hop_chain/error/detail。
    location 用 urljoin 补全相对路径；Referer 固定为入口 host；任何异常都不外抛。
    """
    started = time.perf_counter()
    hops = []
    url = _text(entry_url)
    referer = f"https://{urlsplit(url).netloc}/"
    try:
        for _ in range(max_hops):
            resp = fetch(url, timeout=hop_timeout, referer=referer)
            hop_url = _text(getattr(resp, "url", "")) or url
            status = int(getattr(resp, "status_code", 0) or 0)
            hops.append({"url": hop_url, "status": status})
            location = _header(resp, "Location")
            location = urljoin(hop_url, location) if location else ""

            if status in (301, 302):
                if not location:
                    latency = (time.perf_counter() - started) * 1000
                    return {
                        "verdict": "fail",
                        "http_status": status,
                        "latency_ms": int(round(latency)),
                        "final_url": hop_url,
                        "hop_chain": hops,
                        "error": f"重定向链中断：HTTP {status} 缺少 Location",
                        "detail": "重定向链中断",
                    }
                if stop_on_302 and status == 302:
                    if _is_https_url(location):
                        latency = (time.perf_counter() - started) * 1000
                        return {
                            "verdict": "ok",
                            "http_status": status,
                            "latency_ms": int(round(latency)),
                            "final_url": location,
                            "hop_chain": hops,
                            "error": "",
                            "detail": "",
                        }
                    # Location 不是 https 网页域：不认作可用，按非 30x 规则判定。
                    verdict, detail = classify(status, getattr(resp, "headers", None), getattr(resp, "text", ""))
                    latency = (time.perf_counter() - started) * 1000
                    return {
                        "verdict": verdict,
                        "http_status": status,
                        "latency_ms": int(round(latency)),
                        "final_url": hop_url,
                        "hop_chain": hops,
                        "error": "" if verdict == "ok" else detail,
                        "detail": detail,
                    }
                url = location
                continue

            # 200 或其它：就地收口。
            verdict, detail = classify(status, getattr(resp, "headers", None), getattr(resp, "text", ""))
            latency = (time.perf_counter() - started) * 1000
            return {
                "verdict": verdict,
                "http_status": status,
                "latency_ms": int(round(latency)),
                "final_url": hop_url,
                "hop_chain": hops,
                "error": "" if verdict == "ok" else detail,
                "detail": detail,
            }

        latency = (time.perf_counter() - started) * 1000
        return {
            "verdict": "fail",
            "http_status": hops[-1]["status"] if hops else 0,
            "latency_ms": int(round(latency)),
            "final_url": hops[-1]["url"] if hops else url,
            "hop_chain": hops,
            "error": f"重定向超过 {max_hops} 跳或链未收口",
            "detail": "超过跳数上限",
        }
    except Exception as exc:
        latency = (time.perf_counter() - started) * 1000
        final_url = hops[-1]["url"] if hops else url
        return _fail_payload(exc, latency, final_url, hops)


def probe_domain(domain: str, fetch, timeout: float = 8.0) -> dict:
    """对单个裸域名做一次不跟随重定向的连通性采样。

    输入：裸域名、注入的 fetch、单次请求超时。
    输出：verdict/http_status/latency_ms/final_url/hop_chain/error/detail。
    请求 https://{domain}/，Referer 同为 https://{domain}/；任何异常都不外抛。
    """
    started = time.perf_counter()
    domain = _text(domain).strip()
    url = f"https://{domain}/"
    referer = f"https://{domain}/"
    try:
        resp = fetch(url, timeout=timeout, referer=referer)
        status = int(getattr(resp, "status_code", 0) or 0)
        final_url = _text(getattr(resp, "url", "")) or url
        verdict, detail = classify(status, getattr(resp, "headers", None), getattr(resp, "text", ""))
        latency = (time.perf_counter() - started) * 1000
        return {
            "verdict": verdict,
            "http_status": status,
            "latency_ms": int(round(latency)),
            "final_url": final_url,
            # 单跳请求不是重定向链，按方案 §5.3 的示例留空数组。
            "hop_chain": [],
            "error": "" if verdict == "ok" else detail,
            "detail": detail,
        }
    except Exception as exc:
        latency = (time.perf_counter() - started) * 1000
        return _fail_payload(exc, latency, url, [])
