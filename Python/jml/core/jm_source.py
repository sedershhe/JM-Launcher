"""禁漫数据源适配层（唯一触碰 jmcomic 的模块）。

设计目的：

- 业务模块（search / detail / cover / downloader）只依赖本模块定义的
  **数据源协议**，不直接 import jmcomic，因此单元测试可以注入假数据源，
  自动化测试零联网；
- 真实实现 ``JmcomicSource`` 才 import jmcomic，且惰性建客户端；
- 返回的字典只含 JSON 可序列化的基础类型（str / int / list / dict / bytes），
  工作线程只产出数据，不产出任何 UI 对象（对应旧缺陷 #7）。

数据源协议（假实现只需照此实现四个方法）：

    search(keyword: str, page: int) -> dict
        返回 {"keyword", "page", "total_pages", "results": [ {...} ]}
        列表接口不提供页数时，结果里的 ``page_count`` 为 0（由搜索层做有界补全）
    get_album(book_id: str) -> dict
        返回 {"book_id","title","author","page_count","tags","description",
              "cover_url","chapters":[{"photo_id","index","title"}]}
        ``page_count`` 必须是真实页数；确实取不到才为 0
    iter_chapter_images(book_id: str, chapter: dict, dest_dir: str) -> Iterator[str]
        下载本章图片到 dest_dir，**按原始顺序**产出本地文件路径
        （真实实现单本内有序并发预取，默认 8 线程，见契约 §四/§五）
    cover_bytes(book_id: str) -> bytes | None
        返回封面原图字节；失败返回 None

可选账号方法（收藏夹/账号功能使用；假实现按需提供）：

    login(username: str, password: str) -> bool
        成功返回 True；凭据错误抛 ``errors.InvalidCredentialsError``；
        网络失败抛 ``ConnectionError`` / ``TimeoutError`` /
        ``jmcomic.RequestRetryAllFailException``；其它接口错误抛 jmcomic 异常
    logout() -> None
        清理内存登录态（不联网、不落盘）
    is_logged_in() -> bool
        返回当前数据源会话是否已登录
    logged_in_client() -> object
        返回**已登录**的底层客户端对象（阶段 4-4：``jm.session`` 服务用它把
        登录会话交给插件）；未登录抛 ``errors.NotLoggedInError``
        （``NOT_LOGGED_IN``），不自动登录、不返回任何凭据

本模块不硬编码任何绝对路径；图片临时目录由调用方（downloader）传入。
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterator

from jml import events
from jml.core import diagnostics
from jml.core.errors import (
    ERROR_DOWNLOAD_PARTIAL,
    ERROR_JM,
    ERROR_NETWORK,
    ERROR_NOT_FOUND,
    ERROR_RESPONSE_INVALID,
    ImageError,
    InvalidArgumentError,
    InvalidCredentialsError,
    NotLoggedInError,
    sanitize_text,
)
from jml.core.protocol import EVENT_DOWNLOAD_LOG

# ---------------------------------------------------------------------------
# 单本内图片并发（有序预取）
# ---------------------------------------------------------------------------

#: 单本内并发取图的默认线程数（可用环境变量 ``JML_IMAGE_WORKERS`` 覆盖）。
#: 取值依据：jmcomic 自带同步下载器的图片线程数默认 30
#: （``jm_config.py:482``）；本实现取 8，进程图片级总并发上界 =
#: 下载器 3 本（``downloader.MAX_WORKERS``）× 8 = 24，仍低于 30。
#: 真实网络调参（651712，42 张，各一轮）：N=4 中位数 65.3s → N=8 32.4s，
#: 失败 0，无重试/429，因此默认定为 8。
DEFAULT_IMAGE_WORKERS = 8

#: 单本内图片并发的环境变量名；``1`` 表示退回串行（便于排查/对比测速）。
IMAGE_WORKERS_ENV = "JML_IMAGE_WORKERS"

#: 环境变量允许的最大值（防止误配把线程数拉爆；正常不需要超过 8）。
MAX_IMAGE_WORKERS = 32


def image_workers() -> int:
    """返回单本内并发取图的线程数（每次调用现读环境变量，便于测试与排查）。

    - 未设置 / 空串 / 非整数 → :data:`DEFAULT_IMAGE_WORKERS`；
    - ``<= 1`` → 1（串行，行为与旧实现一致）；
    - 其余 → 原值，但封顶 :data:`MAX_IMAGE_WORKERS`。
    """
    raw = os.environ.get(IMAGE_WORKERS_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_IMAGE_WORKERS
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_IMAGE_WORKERS
    if value <= 1:
        return 1
    return min(value, MAX_IMAGE_WORKERS)


# ---------------------------------------------------------------------------
# jmcomic 异常类型表（唯一 import jmcomic 的模块）
# ---------------------------------------------------------------------------


def exception_rules() -> list[tuple[type, str]]:
    """返回 jmcomic 异常类型 → 错误码规则（子类在前，父类兜底）。"""
    try:
        import jmcomic

        return [
            (jmcomic.MissingAlbumPhotoException, ERROR_NOT_FOUND),
            (jmcomic.RequestRetryAllFailException, ERROR_NETWORK),
            (jmcomic.PartialDownloadFailedException, ERROR_DOWNLOAD_PARTIAL),
            (jmcomic.ResponseUnexpectedException, ERROR_RESPONSE_INVALID),
            (jmcomic.JmcomicException, ERROR_JM),
        ]
    except Exception:
        return []


def jmcomic_version() -> str | None:
    """返回 jmcomic 版本（健康探测走本函数，不在 jml_backend 里 import）。"""
    try:
        import jmcomic

        return getattr(jmcomic, "__version__", None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# jmcomic logger 接管（脱敏后转 download.log 事件，禁止直打 stdout）
# ---------------------------------------------------------------------------

_JM_LOG_LOCK = threading.RLock()
_JM_LOG_INSTALLED = False

#: 当前线程正在跑的下载批次（threading.local；非下载线程为空串）。
_LOG_CONTEXT = threading.local()

_LOG_LEVELS = {
    logging.DEBUG: "info",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


def set_current_batch(batch_id: str) -> None:
    """标记当前线程正在处理的下载批次（供 jmcomic 日志补齐 batch_id）。"""
    _LOG_CONTEXT.batch_id = str(batch_id or "")


def clear_current_batch() -> None:
    """清除当前线程的批次标记（下载线程退出本子时调用）。"""
    _LOG_CONTEXT.batch_id = ""


def current_batch_id() -> str:
    """返回当前线程的批次 id；非下载线程返回空串。"""
    return getattr(_LOG_CONTEXT, "batch_id", "")


class _JmcomicLogHandler(logging.Handler):
    """接管 jmcomic 日志：WARNING 及以上转脱敏 download.log，INFO 只进诊断。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = sanitize_text(record.getMessage())
            level = _LOG_LEVELS.get(record.levelno, "info")
            batch_id = current_batch_id()
            if record.levelno < logging.WARNING:
                # INFO/DEBUG 不逐条走事件（避免数百页下载时每页一次同步回调），
                # 只进有界诊断环表，保留排查能力。
                diagnostics.record(
                    "JMCOMIC_LOG",
                    f"[{batch_id or '<host>'}] [{level}] {message}",
                )
                return
            payload = {
                "type": EVENT_DOWNLOAD_LOG,
                "level": level,
                "message": message,
                "batch_id": batch_id,
            }
            events.BUS.dispatch(EVENT_DOWNLOAD_LOG, payload)
        except Exception as exc:  # 日志转发失败绝不能影响下载
            diagnostics.record(
                "JMCOMIC_LOG_FORWARD_FAILED",
                "jmcomic 日志转发失败",
                exc,
            )


def _install_log_capture() -> None:
    """接管 ``logging.getLogger("jmcomic")``：清 handler、换成脱敏转发。

    只处理日志泄漏，**不关闭 jmcomic 日志开关**，域名自动更新等后台行为
    保持原样。
    """
    global _JM_LOG_INSTALLED
    with _JM_LOG_LOCK:
        if _JM_LOG_INSTALLED:
            return
        logger = logging.getLogger("jmcomic")
        logger.handlers.clear()
        logger.addHandler(_JmcomicLogHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _JM_LOG_INSTALLED = True


# ---------------------------------------------------------------------------
# 真实数据源（jmcomic）
# ---------------------------------------------------------------------------


class _PrefetchStopped(Exception):
    """图片预取任务在生成器关闭后才启动：直接放弃，不再发起网络请求。"""


# ---------------------------------------------------------------------------
# 强化搜索：双通道（网页通道 + API 通道）
# ---------------------------------------------------------------------------

#: 网页通道单次请求超时（秒）。DNS / 超时 / 403 / SSL 任一发生即算该域失败。
HTML_CHANNEL_TIMEOUT = 8.0

#: 每次搜索的固定页大小。jmcomic 网页与 API 都是 80 条/页，且不受参数控制。
SEARCH_PAGE_SIZE = 80

#: 网页通道静态候选域名（P1 探针 2026-11-08 实测可用；动态列表不含 moco.work）。
_HTML_DOMAIN_CANDIDATES = (
    "comic18j-moco.work",
    "comic18j-nico.site",
    "comic18j-ada.work",
)

#: 域名池缓存与单个域失败冷却时间（秒）。
_HTML_DOMAIN_TTL = 60.0

#: 动态域名列表（发布页）最长等待时间：超时就用静态候选，保证 8 秒降级承诺。
_HTML_DOMAIN_DISCOVERY_TIMEOUT = 3.0

_HTML_DOMAIN_LOCK = threading.RLock()
_HTML_DOMAIN_CACHE: dict = {"domains": [], "expires": 0.0}
_HTML_DOMAIN_COOLDOWN: dict = {}

#: 网页搜索页 total 节点（P1 结论：结果 >10000 时显示「共10000+部」，必须容忍 +）。
_HTML_TOTAL_RE = re.compile(r'search-pagination-total">\s*共\s*(\d+)\s*(\+)?\s*部')

#: 网页「无结果」文案候选（P1 实测：空结果是 200 页面 + 「没发现A漫.」/
#: 「找不到和 xxx 相符的內容」，而错误页是独立的 <fieldset><legend> 结构）。
_HTML_EMPTY_MARKERS = (
    "沒發現A漫", "没发现A漫",
    "找不到和", "找不到與", "找不到与",
    "沒有找到", "没有找到",
    "暫無結果", "暂无结果",
    "沒有相關", "没有相关",
    "無搜尋結果", "无搜索结果",
    "No results",
)

#: API 通道 main_tag → jmcomic 客户端方法名（下标 = 协议 main_tag 0..4）。
_API_SEARCH_METHODS = (
    "search_site", "search_work", "search_author", "search_tag", "search_actor",
)

#: 网页通道降级到 API 时给用户看的一句说明。
_HTML_DEGRADED_HINT = "网页通道不可用，已降级为接口搜索（时间 / 分类筛选不生效）。"


class SearchChannelUnavailable(Exception):
    """强化搜索的某个通道不可用（由服务层收口成 error_payload）。"""


def _clamp_page_size(value) -> int:
    """把 page_size 收敛到 1..SEARCH_PAGE_SIZE；非法值回默认页大小。"""
    size = _to_int(value, SEARCH_PAGE_SIZE)
    if size <= 0:
        return SEARCH_PAGE_SIZE
    return min(size, SEARCH_PAGE_SIZE)


#: 强化搜索参数值域（与 protocol / 服务层保持一致；非法值回落默认值）。
_ORDER_BY_VALUES = ("mr", "mv", "mp", "tf")
_TIME_VALUES = ("t", "w", "m", "a")
_CATEGORY_VALUES = (
    "0", "doujin", "single", "short", "another",
    "hanman", "meiman", "doujin_cosplay", "3D", "english_site",
)
_SUB_CATEGORY_VALUES = ("chinese", "japanese", "CG", "other", "cosplay", "3d", "youth")


def _pick_value(value, allowed: tuple, default: str) -> str:
    """值域内原样返回，否则回落默认值。"""
    text = "" if value is None else str(value).strip()
    return text if text in allowed else default


def _html_domains() -> list:
    """返回当前可用网页域名（动态列表 + 静态候选，去重；60 秒缓存）。

    单个域探测失败后进入冷却，后续翻页优先换域；全部域都在冷却时忽略
    冷却（否则连续翻页会无谓降级 API）。
    """
    now = time.monotonic()
    with _HTML_DOMAIN_LOCK:
        expires = float(_HTML_DOMAIN_CACHE.get("expires") or 0.0)
        cached = list(_HTML_DOMAIN_CACHE.get("domains") or [])
        if expires <= now or not cached:
            cached = _collect_html_domains()
            _HTML_DOMAIN_CACHE["domains"] = list(cached)
            _HTML_DOMAIN_CACHE["expires"] = now + _HTML_DOMAIN_TTL
        cooling = {d for d, until in _HTML_DOMAIN_COOLDOWN.items() if until > now}
    usable = [d for d in cached if d not in cooling]
    return usable or list(cached)


def _collect_html_domains() -> list:
    """拼域名池：动态列表在前，静态候选兜底，去重并过滤非域名项。"""
    domains: list = []
    try:
        domains.extend(_dynamic_html_domains())
    except Exception as exc:  # 发布页拿不到时静态候选仍可用
        diagnostics.record(
            "HTML_DOMAIN_DISCOVERY_FAILED",
            "获取禁漫网页域名列表失败，改用静态候选",
            exc,
        )
    domains.extend(_HTML_DOMAIN_CANDIDATES)
    seen: set = set()
    result: list = []
    for item in domains:
        text = str(item or "").strip().lower()
        # 发布页列表里可能夹带 "jm365.xyz/ZNPJam" 这类带路径的 APP 下载地址
        if not text or "/" in text or ":" in text or "." not in text:
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dynamic_html_domains(
    timeout: float = _HTML_DOMAIN_DISCOVERY_TIMEOUT,
) -> list:
    """取 jmcomic 发布页动态域名列表（带总等待上限，避免拖过 8 秒）。"""
    import jmcomic

    box: dict = {}

    def run() -> None:
        try:
            box["domains"] = list(jmcomic.JmModuleConfig.get_html_domain_all())
        except Exception as exc:  # 异常在调用线程重新抛出
            box["error"] = exc

    thread = threading.Thread(target=run, name="jml-html-domains", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("获取禁漫网页域名列表超时")
    error = box.get("error")
    if error is not None:
        raise error
    return [str(item) for item in (box.get("domains") or [])]


def _mark_html_domain_failed(domain: str) -> None:
    """把探测/解析失败的域记入冷却，避免每页都先撞同一个坏域。"""
    with _HTML_DOMAIN_LOCK:
        _HTML_DOMAIN_COOLDOWN[domain] = time.monotonic() + _HTML_DOMAIN_TTL


def _reset_html_cache() -> None:
    """清空域名池缓存与冷却（单元测试与域名排查用）。"""
    with _HTML_DOMAIN_LOCK:
        _HTML_DOMAIN_CACHE["domains"] = []
        _HTML_DOMAIN_CACHE["expires"] = 0.0
        _HTML_DOMAIN_COOLDOWN.clear()


def _build_html_search_url(
    domain: str,
    keyword: str,
    page: int,
    main_tag: int,
    order_by: str,
    time_range: str,
    category,
    sub_category,
) -> str:
    """拼网页搜索 URL：/search/photos[/分类][/sub/副分类]?参数。"""
    path = "/search/photos"
    if category and str(category) != "0":
        path = f"/search/photos/{category}"
        if sub_category:
            path = f"{path}/sub/{sub_category}"
    query = urllib.parse.urlencode(
        {
            "main_tag": int(main_tag),
            "search_query": keyword,
            "page": int(page),
            "o": order_by,
            "t": time_range,
        }
    )
    return f"https://{domain}{path}?{query}"


def _fetch_html(url: str, domain: str, timeout: float = HTML_CHANNEL_TIMEOUT) -> str:
    """请求网页搜索页；非 200 视为该域失败（403 也走降级）。"""
    from curl_cffi import requests as creq

    response = creq.get(
        url,
        timeout=timeout,
        impersonate="chrome",
        allow_redirects=True,
        headers={
            "Referer": f"https://{domain}/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise SearchChannelUnavailable(f"网页搜索返回 HTTP {status}")
    return getattr(response, "text", "") or ""


def _html_has_empty_state(html: str) -> bool:
    """页面是否明确表示「搜索成功但 0 条结果」。

    只认「无结果」文案：关键字过短等错误页也有 ``well well-sm`` 容器，
    不能拿容器本身当空结果证据，否则错误页会被误判成成功的 0 条。
    """
    return any(marker in html for marker in _HTML_EMPTY_MARKERS)


def _parse_html_search_page(html: str) -> dict:
    """解析网页搜索结果，返回 ``{total, total_capped, results}``。

    P1 结论：网页 0 条 ≠ 失败。抓到 total 节点或「无结果」标识 → 成功空结果；
    两者都没有 → 解析失败，由调用方换域或降级 API。
    """
    import jmcomic

    match = _HTML_TOTAL_RE.search(html)
    total = int(match.group(1)) if match else None
    capped = bool(match and match.group(2))

    page_obj = None
    parse_error = None
    try:
        page_obj = jmcomic.JmPageTool.parse_html_to_search_page(html)
    except Exception as exc:
        parse_error = exc
    rows = None
    if page_obj is not None:
        rows = [
            _search_row(jmcomic, album_id, info)
            for album_id, info in page_obj.content
        ]

    if total is None:
        if rows:
            # 有条目但没 total 节点：结构变化，total 用本页条数作下界
            total = len(rows)
        elif _html_has_empty_state(html):
            # 空结果页可能没有 total 节点（只有「没发现A漫」文案）
            total = 0
        else:
            raise SearchChannelUnavailable(
                "网页搜索页无法解析（既无 total 节点也无无结果标识）"
            ) from parse_error
    elif not rows and total > 0:
        raise SearchChannelUnavailable(
            "网页搜索页有 total 但条目解析失败"
        ) from parse_error

    if rows and total <= 0:
        # 结构异常兜底：有结果却报 0 条时，用本页条数当下界
        total = len(rows)

    return {
        "total": int(total),
        "total_capped": capped,
        "results": rows or [],
    }


def _decode_html_text(value) -> str:
    """标题 / 作者里的 HTML 实体解到稳定（站点常见 ``&amp;#160;`` 的双重转义）。"""
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def _search_row(jmcomic_module, album_id, info) -> dict:
    """把 jmcomic 的搜索结果条目映射成与 ``search()`` 完全一致的行结构。"""
    info = info if isinstance(info, dict) else {}
    tags = info.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    author = info.get("author") or ""
    if isinstance(author, (list, tuple)):
        author = ", ".join(str(item) for item in author)
    return {
        "book_id": str(album_id),
        "title": _decode_html_text(info.get("name")),
        "author": _decode_html_text(author),
        # 列表接口若将来带上页数就直接透传（page_count / total_photos），
        # 现在多数情况没有，恒为 0，由搜索层补全。
        "page_count": _to_int(info.get("page_count") or info.get("total_photos")),
        "tags": [_decode_html_text(tag) for tag in tags],
        "cover_url": _cover_url(jmcomic_module, album_id),
    }


def _api_search_values(jmcomic_module) -> dict:
    """协议参数值 → jmcomic 常量值（唯一真源：JmMagicConstants）。"""
    c = jmcomic_module.JmMagicConstants
    return {
        "order_by": {
            "mr": c.ORDER_BY_LATEST,
            "mv": c.ORDER_BY_VIEW,
            "mp": c.ORDER_BY_PICTURE,
            "tf": c.ORDER_BY_LIKE,
        },
        "time": {
            "t": c.TIME_TODAY,
            "w": c.TIME_WEEK,
            "m": c.TIME_MONTH,
            "a": c.TIME_ALL,
        },
        "category": {
            "0": c.CATEGORY_ALL,
            "doujin": c.CATEGORY_DOUJIN,
            "single": c.CATEGORY_SINGLE,
            "short": c.CATEGORY_SHORT,
            "another": c.CATEGORY_ANOTHER,
            "hanman": c.CATEGORY_HANMAN,
            "meiman": c.CATEGORY_MEIMAN,
            "doujin_cosplay": c.CATEGORY_DOUJIN_COSPLAY,
            "3D": c.CATEGORY_3D,
            "english_site": c.CATEGORY_ENGLISH_SITE,
        },
        "sub_category": {
            "chinese": c.SUB_CHINESE,
            "japanese": c.SUB_JAPANESE,
            "CG": c.SUB_DOUJIN_CG,
            "other": c.SUB_ANOTHER_OTHER,
            "cosplay": c.SUB_ANOTHER_COSPLAY,
            "3d": c.SUB_ANOTHER_3D,
            "youth": c.SUB_SINGLE_YOUTH,
        },
    }


class JmcomicSource:
    """基于 jmcomic 的真实数据源（第三方库，允许 import）。

    客户端惰性创建并缓存；同一实例被下载线程池并发使用。线程安全依据
    （jmcomic 2.7.1 源码）：

    - jmcomic 自带的同步下载器就是**同一个 client 多线程跑图片**：
      ``jm_downloader.py:320-324`` 把 ``download_by_image_detail`` 交给
      ``execute_on_condition``，后者用 ``multi_thread_launcher`` /
      ``thread_pool_executor`` 开线程（``jm_downloader.py:369-381``），
      图片线程数默认 30（``jm_config.py:482``）；
    - 图片请求链路每次调用独立构造 kwargs：``decide_headers_and_ts``
      （``jm_client_impl.py:905-929``）按次生成 token/headers；
    - postman 每次请求都 ``meta_data.copy()``（``common/postman/postman_api.py:141-150``），
      默认 postman 是 ``curl_cffi``（``jm_config.py:490``），底层直接调用
      ``curl_cffi.requests.get``（``common/postman/postman_impl.py:40-52``），
      没有共享 Session 状态；
    - cookie 只在建客户端时初始化一次，且有类级锁
      （``jm_client_impl.py:1052-1062``），客户端缓存默认关闭
      （``jm_config.py:472``），下载期间没有 client 级共享可变状态。

    因此**不需要每线程一个 client**，共用一个客户端即可；本类只在
    ``iter_chapter_images`` 里控制单本内的有序并发。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._option = None
        self._client = None
        #: 登录态由本模块维护（jmcomic 2.7.1 的 API 客户端没有可靠的登录标记，
        #: 详见 ``login`` 的文档字符串）。
        self._logged_in = False
        self._username = ""

    # -- 客户端 -------------------------------------------------------------

    def _ensure_client(self):
        """返回 jmcomic 客户端（首次调用时建 option + client）。"""
        with self._lock:
            if self._client is None:
                import jmcomic

                _install_log_capture()
                self._option = jmcomic.JmOption.default()
                self._client = self._option.new_jm_client()
            return self._client

    # -- 账号（凭据只进内存，不写 JmOption 文件） ---------------------------

    def login(self, username: str, password: str) -> bool:
        """用 jmcomic 客户端登录，成功返回 ``True``。

        查证依据（jmcomic 2.7.1 源码）：

        - 默认客户端实现是 API（``jm_config.py:471``
          ``DEFAULT_CLIENT_IMPL = 'api'``），实际类型为
          ``jmcomic.jm_client_impl.JmApiClient``；
        - 登录入口是 ``JmApiClient.login``（``jm_client_impl.py:802``），
          内部调用 ``req_api('/login', False, data=...)``（``:829``），
          ``req_api`` 默认 ``require_success=True``（``:884``），
          响应 JSON 的 ``code != 200`` 时由 ``JmApiResp.require_success``
          抛 ``ResponseUnexpectedException``（``jm_client_interface.py:40``、
          ``:113``；``jm_exception.py:137``）——这是"凭据错误"的唯一类型信号；
        - 网络重试全部失败抛 ``RequestRetryAllFailException``
          （``jm_client_impl.py:212``），属于网络失败；
        - 登录成功后 ``JmApiClient.login`` 只把 cookies 写进内存中的
          postman 元数据（``:834-836``），**不写任何文件**，也不会设置
          ``client._username``（只有网页端 ``JmHtmlClient.login`` 在
          ``:414-415`` 设置它）。匿名会话同样带 ``AVS`` cookie，所以
          ``AVS`` 不能当作登录态证据。因此登录态由本类的 ``_logged_in``
          显式维护，``is_logged_in`` 返回它（网页端客户端的 ``_username``
          作为辅助信号）。

        本方法不把账号密码写进 ``JmOption``：``JmOption.default()`` 只在
        内存里构造配置，本模块从不调用 ``to_file``；登录凭据只作为本次
        ``client.login`` 的参数传入，不进入 option 的持久化字段。
        """
        import jmcomic

        client = self._ensure_client()

        # 重新登录前先清掉旧的“已登录”标记（cookie 先留着，登录失败再作废）；
        # 避免旧会话的 _username 让 is_logged_in 误报。
        with self._lock:
            self._logged_in = False
            self._username = ""
            if hasattr(client, "_username"):
                client._username = None

        try:
            resp = client.login(username, password)
        except jmcomic.ResponseUnexpectedException as exc:
            # 不读异常文案：HTTP 5xx 视为接口异常，其余（含 HTTP 200 +
            # JSON code != 200）视为凭据错误。响应状态是结构化字段。
            if _http_status_code(exc) >= 500:
                raise jmcomic.JmcomicException(
                    "禁漫登录接口异常（HTTP 5xx）", {}
                ) from None
            raise InvalidCredentialsError("账号或密码不正确") from None
        except (TimeoutError, ConnectionError):
            # 网络类异常交给 ``errors.classify`` 按类型映射为
            # TIMEOUT / NETWORK_ERROR。
            raise
        except jmcomic.JmcomicException:
            # RequestRetryAllFailException（网络）等交给分类器。
            raise

        if resp is False:
            raise InvalidCredentialsError("账号或密码不正确")

        with self._lock:
            self._logged_in = True
            self._username = str(username)
        return True

    def logout(self) -> None:
        """清理内存登录态（清 cookie，不联网、不落盘）。"""
        with self._lock:
            self._logged_in = False
            self._username = ""
            client = self._client
            if client is None:
                return
            try:
                # JmApiClient 的会话只存在 postman 元数据里；清成空字典即可
                # 让后续请求回到匿名状态，不触碰任何配置文件。
                client["cookies"] = {}
                if hasattr(client, "_username"):
                    client._username = None
            except Exception as exc:  # 清理失败只记诊断，不影响登出入口
                # 只记类型名：原始异常消息可能夹带 cookie / 凭据。
                diagnostics.record(
                    "ACCOUNT_LOGOUT_CLEAR_FAILED",
                    f"清理 jmcomic 内存登录 Cookie 失败: {type(exc).__name__}",
                )

    def is_logged_in(self) -> bool:
        """返回当前会话是否已登录。

        以本模块登录成功后置位的 ``_logged_in`` 为准；网页端客户端若自己
        设置了 ``_username`` 也视为已登录（API 客户端没有这个字段）。
        ``AVS`` 匿名 cookie 不能作为判据。
        """
        with self._lock:
            if self._logged_in:
                return True
            return bool(getattr(self._client, "_username", None))

    def logged_in_client(self):
        """返回**已登录**的 jmcomic 客户端对象（``jm.session`` 服务唯一入口）。

        阶段 4-4：收藏夹等需要登录态的只读功能由插件通过
        ``ctx.get_service("jm.session", 1).get_client()`` 取得这个对象，
        插件因此**不需要也没有通道**接触账号密码（密码始终只在
        ``core.account`` 的进程内存里）。

        语义锁定：
        - 未登录 → 抛 :class:`jml.core.errors.NotLoggedInError`
          （错误码 ``NOT_LOGGED_IN``），**不自动登录**——自动登录只由
          ``jm.account.ensure_login`` 决定，插件必须先确保登录；
        - 返回的是宿主缓存的**同一个**客户端实例（含内存登录 cookies），
          插件只读使用，不新建会话、不落盘；
        - 本方法不读取、不返回用户名/密码/cookie 字典，只给客户端对象本身。
        """
        with self._lock:
            logged_in = self._logged_in or bool(
                getattr(self._client, "_username", None)
            )
            if not logged_in:
                raise NotLoggedInError("尚未登录，无法提供已登录的 jmcomic 客户端")
            # 客户端在登录成功时必然已创建；_ensure_client 只在极端情况下补建
            # （RLock 可重入，仍在同一把锁内，登录态与客户端保持一致）。
            return self._ensure_client()

    # -- 协议实现 -----------------------------------------------------------

    def search(self, keyword: str, page: int) -> dict:
        """关键词搜索一页，返回统一结构。

        jmcomic 2.7.1 的搜索接口每条只返回 name / tags 等字段，**不含页数**
        （见 ``jm_toolkit.parse_api_to_search_page`` 的行为），所以这里
        ``page_count`` 通常为 0；有界并发补全由 ``searcher`` 统一负责，
        避免本模块把网络策略扩散到业务层。
        """
        import jmcomic

        client = self._ensure_client()
        page_obj = client.search_site(search_query=keyword, page=int(page))
        results: list[dict] = [
            _search_row(jmcomic, album_id, info)
            for album_id, info in page_obj.content
        ]
        return {
            "keyword": keyword,
            "page": int(page),
            "total_pages": int(page_obj.page_count),
            "results": results,
        }

    def search_advanced(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 80,
        main_tag: int = 0,
        order_by: str = "mr",
        time: str = "a",
        category: str = "0",
        sub_category: str | None = None,
        channel: str = "auto",
    ) -> dict:
        """强化搜索：优先网页通道，失败降级 API 通道。

        行结构与 :meth:`search` 完全一致（同一个 ``_search_row``），调用方
        （服务层 / 插件 / C#）不需要也不允许再做字段清洗。网页通道的
        ``time`` / ``category`` / ``sub_category`` 真正生效；API 通道只接受
        ``main_tag`` / ``order_by``，降级时 ``capabilities`` 三项全为 False
        并用 ``hint`` 告知用户。
        """
        page = max(_to_int(page, 1), 1)
        # jmcomic 网页 / API 都固定 80 条/页：page_size 只做上限收敛，通道页大小不可调
        page_size = _clamp_page_size(page_size)
        keyword = str(keyword or "")
        main_tag = main_tag if main_tag in (0, 1, 2, 3, 4) else 0
        order_by = _pick_value(order_by, _ORDER_BY_VALUES, "mr")
        time = _pick_value(time, _TIME_VALUES, "a")
        category = _pick_value(category, _CATEGORY_VALUES, "0")
        sub_category = (
            _pick_value(sub_category, _SUB_CATEGORY_VALUES, "")
            if sub_category
            else None
        ) or None
        channel = channel if channel in ("auto", "html", "api") else "auto"

        if channel == "api":
            return self._search_api(
                keyword, page, main_tag, order_by, time, category, sub_category
            )

        try:
            return self._search_html(
                keyword, page, main_tag, order_by, time, category, sub_category
            )
        except SearchChannelUnavailable as exc:
            diagnostics.record(
                "SEARCH_HTML_UNAVAILABLE",
                f"网页搜索通道不可用，降级接口通道: {keyword!r}",
                exc,
            )

        payload = self._search_api(
            keyword, page, main_tag, order_by, time, category, sub_category
        )
        payload["hint"] = _HTML_DEGRADED_HINT
        return payload

    def _search_html(
        self,
        keyword: str,
        page: int,
        main_tag: int,
        order_by: str,
        time_range: str,
        category: str,
        sub_category: str | None,
    ) -> dict:
        """网页通道：逐个可用域名尝试，全失败抛 :class:`SearchChannelUnavailable`。"""
        import jmcomic

        values = _api_search_values(jmcomic)
        category_value = None
        if category and str(category) != "0":
            category_value = values["category"].get(str(category), str(category))
        sub_value = None
        if sub_category:
            sub_value = values["sub_category"].get(str(sub_category), str(sub_category))
            if category_value is None:
                # /sub/ 只能跟在分类段后面；没有分类时忽略副分类
                sub_value = None

        failures: list = []
        for domain in _html_domains():
            url = _build_html_search_url(
                domain,
                keyword,
                page,
                main_tag,
                order_by,
                time_range,
                category_value,
                sub_value,
            )
            try:
                html = _fetch_html(url, domain)
                parsed = _parse_html_search_page(html)
            except Exception as exc:
                _mark_html_domain_failed(domain)
                failures.append(f"{domain}: {type(exc).__name__}")
                diagnostics.record(
                    "SEARCH_HTML_ATTEMPT_FAILED",
                    f"网页搜索失败: {domain} | {keyword!r} | page={page}",
                    exc,
                )
                continue

            total = int(parsed["total"])
            return {
                "channel": "html",
                "capabilities": {
                    "time": True,
                    "category": True,
                    "sub_category": True,
                },
                "total_capped": bool(parsed["total_capped"]),
                "keyword": keyword,
                "page": page,
                "page_size": SEARCH_PAGE_SIZE,
                "total": total,
                "total_pages": (
                    (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
                    if total > 0
                    else 0
                ),
                "results": parsed["results"],
                "hint": "",
            }

        raise SearchChannelUnavailable(
            "网页搜索通道全部域名不可用: " + "; ".join(failures or ["无可用域名"])
        )

    def _search_api(
        self,
        keyword: str,
        page: int,
        main_tag: int,
        order_by: str,
        time_range: str,
        category: str,
        sub_category: str | None,
    ) -> dict:
        """API 通道：参数按 main_tag 落到 search_site/work/author/tag/actor。

        time / category / sub_category 只支持网页通道（P1 结论），这里固定传
        jmcomic 的「全部」默认值，保证 API 结果不被这三个筛选影响，也与
        ``capabilities`` 三项 False 的声明一致。
        """
        import jmcomic

        client = self._ensure_client()
        values = _api_search_values(jmcomic)
        constants = jmcomic.JmMagicConstants
        method = getattr(client, _API_SEARCH_METHODS[main_tag])
        page_obj = method(
            search_query=keyword,
            page=int(page),
            order_by=values["order_by"].get(str(order_by), constants.ORDER_BY_LATEST),
            time=constants.TIME_ALL,
            category=constants.CATEGORY_ALL,
            sub_category=None,
        )
        results = [
            _search_row(jmcomic, album_id, info)
            for album_id, info in page_obj.content
        ]
        total = _to_int(getattr(page_obj, "total", 0))
        total_pages = _to_int(getattr(page_obj, "page_count", 0))
        if total <= 0:
            total = len(results)
        if total_pages <= 0 and total > 0:
            total_pages = (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
        return {
            "channel": "api",
            "capabilities": {
                "time": False,
                "category": False,
                "sub_category": False,
            },
            "total_capped": False,
            "keyword": keyword,
            "page": page,
            "page_size": SEARCH_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "results": results,
            "hint": "",
        }

    def get_album(self, book_id: str) -> dict:
        """取本子详情（含章节列表 + 真实页数），返回统一结构。

        页数取值链路见 ``_album_and_page_count``：正常情况一次请求同时拿到
        专辑实体和 ``total_photos``，不需要为页数额外请求。
        """
        import jmcomic

        album, page_count = self._album_and_page_count(book_id)
        chapters: list[dict] = []
        for index, episode in enumerate(getattr(album, "episode_list", []) or [], start=1):
            photo_id = episode[0] if len(episode) > 0 else ""
            title = episode[2] if len(episode) > 2 else ""
            chapters.append(
                {
                    "photo_id": str(photo_id),
                    "index": index,
                    "title": str(title or ""),
                }
            )
        tags = getattr(album, "tags", []) or []
        if isinstance(tags, str):
            tags = [tags]
        authors = getattr(album, "authors", []) or []
        if isinstance(authors, str):
            authors = [authors]
        author = ", ".join(str(item) for item in authors) or str(
            getattr(album, "author", "") or ""
        )
        return {
            "book_id": str(getattr(album, "album_id", book_id)),
            "title": str(getattr(album, "name", "") or ""),
            "author": author,
            "page_count": page_count,
            "tags": [str(tag) for tag in tags],
            "description": str(getattr(album, "description", "") or ""),
            "cover_url": _cover_url(jmcomic, getattr(album, "album_id", book_id)),
            "chapters": chapters,
        }

    # -- 页数解析 -----------------------------------------------------------

    def _album_and_page_count(self, book_id: str) -> tuple[object, int]:
        """取专辑实体并解析真实页数。

        **为什么不能直接用 ``album.page_count``**：jmcomic 2.7.1 的 API 适配层
        ``jm_toolkit.JmApiAdaptTool.post_adapt_album`` 把 ``page_count``
        硬编码成 ``'0'``，``JmAlbumDetail.page_count`` 恒为 0；真实页数只存在
        于专辑原始 JSON 的 ``total_photos`` 字段（实测 651712=42、565384=34，
        与 ZIP 内图片数一致）。

        所以这里直接取一次原始响应，再用 jmcomic 自己的解析器构造实体：
        网络请求数与原来 ``get_album_detail`` 相同（1 次），但能拿到真实页数。
        若当前客户端没有原始接口（例如将来换成 HTML 客户端），退回公共接口。
        """
        import jmcomic

        client = self._ensure_client()
        req_api = getattr(client, "req_api", None)
        append_params = getattr(client, "append_params_to_url", None)
        api_album = getattr(client, "API_ALBUM", None)
        if req_api is not None and append_params is not None and api_album is not None:
            try:
                album, count = self._album_from_raw(
                    jmcomic, req_api, append_params, api_album, book_id
                )
            except (AttributeError, KeyError, TypeError, IndexError) as exc:
                # 上游接口结构变化时不让详情整体失败，退回公共接口并留诊断
                diagnostics.record(
                    "ALBUM_RAW_FALLBACK",
                    f"专辑原始接口不可用，退回公共接口: book_id={book_id}",
                    exc,
                )
            else:
                if count <= 0:
                    # 原始响应没有 total_photos（不同版本/接口）时再兜底
                    count = self._page_count_from_album(client, album)
                return album, count

        album = client.get_album_detail(book_id)
        return album, self._page_count_from_album(client, album)

    @staticmethod
    def _album_from_raw(jmcomic, req_api, append_params, api_album, book_id: str):
        """用一次原始专辑请求构造 ``(实体, 页数)``。"""
        jmid = jmcomic.JmcomicText.parse_to_jm_id(book_id)
        resp = req_api(append_params(api_album, {"id": jmid}))
        if not resp.encoded_data:
            raise _missing_album(jmcomic, jmid)
        data = resp.res_data
        if data.get("name") is None:
            raise _missing_album(jmcomic, jmid)
        album = jmcomic.JmApiAdaptTool.parse_entity(data, jmcomic.JmAlbumDetail)
        return album, _to_int(data.get("total_photos"))

    @staticmethod
    def _page_count_from_album(client, album) -> int:
        """公共接口兜底：实体自带页数，或单章节本子按章节图片数求和。"""
        count = _to_int(getattr(album, "page_count", 0))
        if count > 0:
            return count
        episodes = getattr(album, "episode_list", None) or []
        # 多章节本子不逐章请求（一次详情会变成几十次网络往返），保持 0；
        # 单章节本子一次图片详情即可得到准确页数。
        if len(episodes) != 1:
            return 0
        photo_id = episodes[0][0] if len(episodes[0]) > 0 else ""
        try:
            photo = client.get_photo_detail(
                photo_id, fetch_album=False, fetch_scramble_id=False
            )
        except Exception as exc:
            diagnostics.record(
                "ALBUM_PAGE_COUNT_FAILED",
                f"页数兜底求和失败: photo_id={photo_id}",
                exc,
            )
            return 0
        return len(getattr(photo, "page_arr", None) or [])

    def iter_chapter_images(
        self, book_id: str, chapter: dict, dest_dir: str
    ) -> Iterator[str]:
        """下载一章的图片到 dest_dir，**按原始顺序**产出本地路径。

        并发模型（契约 §四/§五）：

        - 章节详情（``get_photo_detail``）仍是一次同步请求，先拿到图片清单；
        - 之后用 ``ThreadPoolExecutor`` 预取：先提交前 N 张，再**按索引顺序**
          逐个等队首完成并 yield，完成一张立刻补提交下一张，保证输出顺序
          与 ``src_0001``… 严格一致，与网络完成顺序无关；
        - N 取 :func:`image_workers`（默认
          :data:`DEFAULT_IMAGE_WORKERS`=8，``JML_IMAGE_WORKERS`` 可覆盖，
          ``1`` 退回串行）；
        - 取消：消费者停止拉取后生成器被关闭，``finally`` 里置停止标记、
          取消排队任务并 ``shutdown(wait=False, cancel_futures=True)``，
          不再发起新请求；已发出的请求自行结束、结果丢弃；
        - 单张失败在它对应的索引位置抛出（沿用"该本失败"语义），池在
          ``finally`` 里释放，不会卡死后续下载。
        """
        client = self._ensure_client()
        photo_id = str(chapter.get("photo_id") or "")
        if not photo_id:
            raise InvalidArgumentError(f"本子 {book_id} 的章节缺少 photo_id")

        photo = client.get_photo_detail(photo_id)
        os.makedirs(dest_dir, exist_ok=True)
        # 图片清单已在详情响应里，物化不会额外发请求（DetailEntity.__iter__
        # 只从 page_arr 构造实体，见 jm_entity.py:63-65）。
        images = list(photo)

        workers = image_workers()
        if workers <= 1 or len(images) <= 1:
            # 串行路径：与并发改造前完全一致，便于对比测速/排查。
            for index, image in enumerate(images, start=1):
                yield self._download_one_image(
                    client, photo_id, image, index, dest_dir
                )
            return

        yield from self._iter_images_concurrently(
            client, photo_id, images, dest_dir, workers
        )

    def _download_one_image(
        self, client, photo_id: str, image, index: int, dest_dir: str
    ) -> str:
        """下载第 ``index`` 张（1 起）到 dest_dir，返回本地路径。"""
        suffix = getattr(image, "img_file_suffix", "") or ".jpg"
        target = os.path.join(dest_dir, f"src_{index:04d}{suffix}")
        decode = True
        try:
            if self._option is not None:
                decode = bool(self._option.decide_download_image_decode(image))
        except Exception as exc:
            diagnostics.record(
                "IMAGE_DECODE_FALLBACK",
                f"图片解码策略回退为默认: photo_id={photo_id}",
                exc,
            )
            decode = True
        client.download_by_image_detail(image, target, decode_image=decode)
        if not os.path.isfile(target) or os.path.getsize(target) <= 0:
            raise ImageError(f"图片下载结果为空: {photo_id} / 第 {index} 张")
        return target

    def _iter_images_concurrently(
        self,
        client,
        photo_id: str,
        images: list,
        dest_dir: str,
        workers: int,
    ) -> Iterator[str]:
        """有序并发预取：按索引顺序 yield，池内最多 ``workers`` 个在跑。"""
        total = len(images)
        stop = threading.Event()
        pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="jml-image"
        )
        pending: dict[int, Future] = {}
        next_to_submit = 0

        def submit(index: int) -> None:
            image = images[index - 1]

            def run():
                # 生成器被取消/关闭后，排队任务即便抢到线程也不再发请求。
                if stop.is_set():
                    raise _PrefetchStopped()
                return self._download_one_image(
                    client, photo_id, image, index, dest_dir
                )

            pending[index] = pool.submit(run)

        try:
            while next_to_submit < total and len(pending) < workers:
                next_to_submit += 1
                submit(next_to_submit)
            for index in range(1, total + 1):
                future = pending.pop(index)
                yield future.result()
                if next_to_submit < total:
                    next_to_submit += 1
                    submit(next_to_submit)
        finally:
            # 不阻塞消费者：cancel 排队任务，正在跑的请求自行结束、结果丢弃。
            stop.set()
            for future in pending.values():
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    def cover_bytes(self, book_id: str) -> bytes | None:
        """取封面原图字节（不解码、不转 UI 对象、不落临时文件）。"""
        import jmcomic

        client = self._ensure_client()
        url = jmcomic.JmcomicText.get_album_cover_url(book_id)
        resp = client.get_jm_image(url)
        resp.require_success()
        return bytes(resp.content) or None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _to_int(value: object, default: int = 0) -> int:
    """尽力把外部值转成 int（失败回退默认值，不抛异常）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _http_status_code(exc: BaseException) -> int:
    """从 jmcomic 响应异常里取 HTTP 状态码（取不到返回 0）。

    只读结构化字段 ``exc.resp.http_code``，不解析异常消息文案。
    """
    try:
        resp = getattr(exc, "resp", None)
        return int(getattr(resp, "http_code", 0) or 0)
    except Exception:
        return 0


def _missing_album(jmcomic_module, jmid: object) -> Exception:
    """构造"本子不存在"异常（类型与 jmcomic 自身一致，错误码映射为 NOT_FOUND）。"""
    return jmcomic_module.MissingAlbumPhotoException(
        f"请求的本子不存在！(JM{jmid})", {"album_id": str(jmid)}
    )


def _cover_url(jmcomic_module, album_id: object) -> str:
    """生成封面 URL（失败回退空串，并写诊断；C# 显示暗红底 X）。"""
    try:
        return str(jmcomic_module.JmcomicText.get_album_cover_url(album_id))
    except Exception as exc:
        diagnostics.record(
            "COVER_URL_FAILED",
            f"封面 URL 生成失败: book_id={album_id}",
            exc,
        )
        return ""


# ---------------------------------------------------------------------------
# 模块级默认数据源（测试可替换；替换后所有业务模块一起生效）
# ---------------------------------------------------------------------------

_SOURCE_LOCK = threading.RLock()
_SOURCE = None


def get_source():
    """返回当前数据源；未设置时创建真实 ``JmcomicSource``。"""
    global _SOURCE
    with _SOURCE_LOCK:
        if _SOURCE is None:
            _SOURCE = JmcomicSource()
        return _SOURCE


def set_source(source) -> None:
    """替换数据源（测试注入假数据源；传 None 等价于 reset_source）。"""
    global _SOURCE
    with _SOURCE_LOCK:
        _SOURCE = source


def reset_source() -> None:
    """恢复默认（下次 get_source 会新建真实数据源）。"""
    set_source(None)


def get_logged_in_client():
    """返回当前数据源里**已登录**的客户端对象（``jm.session`` 服务实现）。

    数据源协议的可选方法 ``logged_in_client()`` 由数据源自己实现：真实实现是
    :meth:`JmcomicSource.logged_in_client`，测试注入的假数据源同样可以实现。
    未实现或未登录都抛 :class:`NotLoggedInError`（``NOT_LOGGED_IN``），
    本函数不自动登录、不读凭据、不做任何路径探测。
    """
    source = get_source()
    getter = getattr(source, "logged_in_client", None)
    if not callable(getter):
        raise NotLoggedInError("当前数据源不支持提供已登录客户端")
    return getter()

