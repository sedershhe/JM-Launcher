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
        逐张下载本章图片到 dest_dir，产出本地文件路径
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

本模块不硬编码任何绝对路径；图片临时目录由调用方（downloader）传入。
"""

from __future__ import annotations

import logging
import os
import threading
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
    sanitize_text,
)
from jml.core.protocol import EVENT_DOWNLOAD_LOG

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


class JmcomicSource:
    """基于 jmcomic 的真实数据源（第三方库，允许 import）。

    客户端惰性创建并缓存；同一实例被下载线程池并发使用（jmcomic 官方支持
    并发调用，与旧项目行为一致）。
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
        results: list[dict] = []
        for album_id, info in page_obj.content:
            tags = info.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            author = info.get("author") or ""
            if isinstance(author, (list, tuple)):
                author = ", ".join(str(item) for item in author)
            results.append(
                {
                    "book_id": str(album_id),
                    "title": str(info.get("name") or ""),
                    "author": str(author),
                    # 列表接口若将来带上页数就直接透传（page_count / total_photos），
                    # 现在两者都没有，恒为 0，由搜索层补全。
                    "page_count": _to_int(
                        info.get("page_count") or info.get("total_photos")
                    ),
                    "tags": [str(tag) for tag in tags],
                    "cover_url": _cover_url(jmcomic, album_id),
                }
            )
        return {
            "keyword": keyword,
            "page": int(page),
            "total_pages": int(page_obj.page_count),
            "results": results,
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
        """逐张下载一章的图片到 dest_dir，产出本地路径。

        每张图片下载前不做事；取消检查点由 downloader 在"拉取下一张之前"完成。
        """
        client = self._ensure_client()
        photo_id = str(chapter.get("photo_id") or "")
        if not photo_id:
            raise InvalidArgumentError(f"本子 {book_id} 的章节缺少 photo_id")

        photo = client.get_photo_detail(photo_id)
        os.makedirs(dest_dir, exist_ok=True)
        for index, image in enumerate(photo, start=1):
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
            yield target

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
