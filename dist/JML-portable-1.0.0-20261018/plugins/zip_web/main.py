"""ZIP 网页浏览插件（阶段 4-3）。

功能：选文件夹列出 ``*.zip``（文件名自然排序）→ 选中某个 ZIP 点「用浏览器阅读」
→ 在 **127.0.0.1** 上启动一个只读 HTTP 服务（端口由系统自动分配）→ 用外部浏览器
打开 ``http://127.0.0.1:<port>/`` 无缝纵向连续阅读（所有图片在一个长页里按顺序
``<img>``，顶部固定显示「第 n 页 / 共 N 页」）→ 点「停止浏览服务」调用
``shutdown()`` + ``server_close()`` 并释放端口。

页面状态机（``zip_web.main``，action 模板）::

    未选目录        folder=""           status_text="未选择目录"
    已选目录        有/无 ZIP            status_text="已选目录：<目录>（N 个 ZIP）"
    正在浏览        state="running"      status_text="正在浏览：http://127.0.0.1:<port>（…）"
    已停止          state="idle"         status_text="已停止"

服务消费（**唯一**跨插件路径）：
    ``ctx.get_service("archive_reader", 1)`` —— ZIP 浏览插件提供的强类型对象服务
    （合约 §六），方法 ``list_images(zip_path)`` / ``read_image(zip_path, index)`` /
    ``close(zip_path)`` / ``is_open(zip_path)``。本插件**不 import 兄弟插件模块**，
    也不探测插件目录；服务缺失由 loader 依据 manifest ``requires`` 拒绝加载
    （错误消息含"缺少服务 archive_reader"），本插件 setup 再做一次防御性校验。

HTTP 纪律：
    - 只绑 ``127.0.0.1``，端口传 **0** 由系统分配（不监听 0.0.0.0）；
    - ``/`` 返回长页 HTML，``/img/<index>`` 返回 ``read_image`` 原始字节与正确
      Content-Type；不把图片 base64 塞进 HTML / JSON；
    - ``stop`` 后端口可立即重新绑定；页面 HTML 不写临时文件、不解压到磁盘。

清理钩子：宿主当前没有 on_unload 钩子，``setup(ctx)`` 的返回值也不会被调用，
因此本插件提供模块级 :func:`stop_all`（停止本进程内所有会话的 HTTP 服务），
并在页面 ``stop``、换目录 / 换包时立即调用；另注册 ``atexit`` 作为进程退出兜底。
测试与宿主可在重新装配插件前调用 ``stop_all()``（见报告"未决"说明）。
"""

from __future__ import annotations

import atexit
import html
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

#: 本插件在 manifest 中声明的页面 id
PAGE_ID = "zip_web.main"

#: 消费的服务名与版本（manifest services.requires 必须一致）
SERVICE_NAME = "archive_reader"
SERVICE_VERSION = 1

#: 动作按钮（按状态由 Python 返回可用动作列表）
ACTION_START = "start"
ACTION_STOP = "stop"
TITLE_START = "用浏览器阅读"
TITLE_STOP = "停止浏览服务"

#: 页面状态文案前缀（计划书 4.7）
STATUS_NO_FOLDER = "未选择目录"
STATUS_STOPPED = "已停止"

#: 尚未选文件夹 / 目录里没有 ZIP 时的空态文案
FOLDER_HINT_TEXT = "选择文件夹…"
EMPTY_FOLDER_TEXT = "文件夹不存在，请重新选择"
EMPTY_ZIP_TEXT = "目录中没有 ZIP 压缩包"

#: 视为可浏览图片的条目后缀（与 archive_reader 的判定一致；仅用于 Content-Type）
_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: 自然排序：数字段按整数比较（``999`` 排在 ``1000`` 之前）
_NATURAL_SPLIT = re.compile(r"(\d+)")


# ---------------------------------------------------------------------------
# 纯函数：自然排序 / 目录扫描 / HTML 生成（可单测，不依赖宿主）
# ---------------------------------------------------------------------------


def natural_key(name: str) -> tuple:
    """自然排序键：数字段按整数比较，文本段按大小写不敏感比较。

    ``"2.zip"`` < ``"10.zip"``、``"999.png"`` < ``"1000.png"``；同位置要么
    都是数字段 ``(0, int)``、要么都是文本段 ``(1, str)``，元组不会出现
    int 与 str 互比。
    """
    parts = [part for part in _NATURAL_SPLIT.split(name or "") if part]
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in parts
    )


def list_zip_files(folder: str) -> list[dict]:
    """列出目录下一层的 ``*.zip``（按文件名自然排序），返回 ``[{name,path}]``。

    目录不存在 / 空串 → 返回空列表（页面显示"选择文件夹…"或目录失联提示，
    不抛异常）；除 ZIP 以外的文件与子目录一律忽略。
    """
    if not folder or not os.path.isdir(folder):
        return []
    items: list[dict] = []
    for name in os.listdir(folder):
        if not name.lower().endswith(".zip"):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        items.append({"name": name, "path": path})
    items.sort(key=lambda item: natural_key(item["name"]))
    return items


def content_type_for(entry_name: str) -> str:
    """按图片条目后缀给出 HTTP Content-Type（未知后缀用二进制流兜底）。"""
    extension = os.path.splitext(entry_name or "")[1].lower()
    return _IMAGE_CONTENT_TYPES.get(extension, "application/octet-stream")


def build_page_html(title: str, image_count: int) -> str:
    """生成无缝纵向连续阅读页面（所有图片在一个长页里，顶部固定页码条）。

    - 图片地址 ``/img/<index>``（index 0 起），原图 bytes 由服务提供；
    - 顶部固定条初始文案**逐字**为「第 1 页 / 共 N 页」，滚动时用简单 JS
      按当前视口锚点更新（只改页码，不改变图片布局）；
    - 全部内容由本函数拼出，不写临时文件。
    """
    total = max(0, int(image_count))
    safe_title = html.escape(title or "ZIP 网页浏览", quote=True)
    images = "\n".join(
        f'<img src="/img/{index}" data-index="{index}" '
        f'alt="第 {index + 1} 页">'
        for index in range(total)
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title} - ZIP 网页浏览</title>
<style>
  html, body {{ margin: 0; padding: 0; background: #1e1e1e; }}
  #progress-bar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 10;
    padding: 10px 16px; box-sizing: border-box;
    background: rgba(0, 0, 0, 0.72); color: #fff;
    font: 14px/1.4 "Microsoft YaHei", sans-serif; text-align: center;
  }}
  #pages {{ padding-top: 48px; }}
  #pages img {{
    display: block; width: 100%; max-width: 1000px; height: auto;
    margin: 0 auto 8px; background: #fff;
  }}
</style>
</head>
<body>
<div id="progress-bar">第 1 页 / 共 {total} 页</div>
<div id="pages">
{images}
</div>
<script>
(function () {{
  var bar = document.getElementById('progress-bar');
  var images = Array.prototype.slice.call(
    document.querySelectorAll('#pages img'));
  if (!bar || images.length === 0) {{ return; }}
  var total = images.length;
  function currentIndex() {{
    var anchor = window.scrollY + window.innerHeight * 0.4;
    var index = 0;
    for (var i = 0; i < images.length; i++) {{
      if (images[i].offsetTop <= anchor) {{ index = i; }} else {{ break; }}
    }}
    return index;
  }}
  function update() {{
    bar.textContent = '第 ' + (currentIndex() + 1) + ' 页 / 共 ' + total + ' 页';
  }}
  window.addEventListener('scroll', update, {{ passive: true }});
  window.addEventListener('resize', update);
  window.addEventListener('load', update);
  update();
}})();
</script>
</body>
</html>
"""


def _as_index(value: object) -> int | None:
    """把参数转成整数下标；非法值返回 None（bool 不算数字）。"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _same_path(left: str, right: str) -> bool:
    """Windows 路径比较（忽略大小写与分隔符差异）。"""
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


# ---------------------------------------------------------------------------
# 本机回环 HTTP 服务
# ---------------------------------------------------------------------------


class _ZipWebHandler(BaseHTTPRequestHandler):
    """只读 HTTP 处理器：``/`` 长页 HTML，``/img/<index>`` 图片原始字节。"""

    server_version = "JMLZipWeb/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        try:
            path = urlsplit(self.path).path
            if path in ("", "/"):
                self._send_html()
            elif path.startswith("/img/"):
                self._send_image(path[len("/img/") :])
            else:
                self._send_text(404, "Not Found")
        except Exception:
            # 读图失败 / 客户端提前断开：尽量回 500，绝不把异常抛给宿主线程
            try:
                self._send_text(500, "Internal Server Error")
            except Exception:
                pass

    def _send_html(self) -> None:
        server = self.server
        body = build_page_html(server.title, len(server.entries)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_image(self, raw_index: str) -> None:
        server = self.server
        index = _as_index(raw_index)
        if index is None or not 0 <= index < len(server.entries):
            self._send_text(404, "Not Found")
            return
        data = server.reader.read_image(server.zip_path, index)
        payload = bytes(data)
        entry_name = server.entries[index]
        self.send_response(200)
        self.send_header("Content-Type", content_type_for(entry_name))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(self, code: int, text: str) -> None:
        body = (text or "").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """静默访问日志（宿主 stdout 只保留插件自己的状态日志）。"""


class ZipWebServer(ThreadingHTTPServer):
    """本机回环只读服务器（端口 0 = 系统自动分配，只绑 127.0.0.1）。"""

    #: 让停止后同端口可立即重新绑定（Windows/Linux 一致）
    allow_reuse_address = True

    def __init__(self, reader, zip_path: str, entries: list[str], title: str) -> None:
        self.reader = reader
        self.zip_path = zip_path
        self.entries = list(entries)
        self.title = title
        # 只传回环地址；端口 0 由系统分配（绝不监听 0.0.0.0）
        super().__init__(("127.0.0.1", 0), _ZipWebHandler)

    @property
    def port(self) -> int:
        """实际监听端口（server_address 的第二项）。"""
        return int(self.server_address[1])


# ---------------------------------------------------------------------------
# 页面状态机
# ---------------------------------------------------------------------------


class ZipWebSession:
    """``zip_web.main`` 页面状态机（线程安全）。

    只依赖 ``ctx`` 的 config_get / config_set / log，以及 ``archive_reader/1``
    服务对象；HTTP 服务句柄、当前目录、当前压缩包都在同一把显式 RLock 内读写，
    页面动作与 HTTP 请求不会串状态。
    """

    def __init__(self, ctx, reader) -> None:
        self._ctx = ctx
        self._reader = reader
        self._lock = threading.RLock()
        #: 当前目录（绝对路径；空串 = 还没选过）
        self._folder = self._stored_folder()
        #: 最近一次列出的 ZIP（start 时按 item_index 兜底解析）
        self._items: list[dict] = []
        #: 运行中的服务与线程（None = 未启动）
        self._server: ZipWebServer | None = None
        self._thread: threading.Thread | None = None
        self._port = 0
        self._url = ""
        #: 当前浏览的压缩包（绝对路径 / 文件名 / 图片条目）
        self._selected_path = ""
        self._selected_name = ""
        self._entries: list[str] = []
        #: 最近一次 stop 后显示的状态文案（换目录 / 再次 start 时清除）
        self._stopped_text = ""

    # -- 页面加载 -----------------------------------------------------------

    def page_payload(self, params: dict) -> dict:
        """构造 action 模板 payload（字段见合约 §12.2 的 action 段）。"""
        with self._lock:
            self._update_folder_locked(params.get("folder"))
            self._items = list_zip_files(self._folder)
            return self._payload_locked()

    def _payload_locked(self) -> dict:
        """在锁内构造 action payload（状态、动作列表、items、url/port）。"""
        running = self._server is not None
        folder = self._folder
        items = list(self._items)
        if running:
            state = "running"
            status_text = (
                f"正在浏览：http://127.0.0.1:{self._port}"
                f"（{self._selected_name}，共 {len(self._entries)} 页）"
            )
        else:
            state = "idle"
            status_text = self._idle_status_text_locked(folder, items)

        actions = [
            {"id": ACTION_STOP, "title": TITLE_STOP}
            if running
            else {"id": ACTION_START, "title": TITLE_START}
        ]
        return {
            "items": items,
            "state": state,
            "status_text": status_text,
            "actions": actions,
            # 尚未启动时 url 为空串、port 为 0（C# 只按 url 是否存在决定按钮）
            "url": self._url if running else "",
            "port": self._port if running else 0,
            "folder": folder,
            "allow_folder_choice": True,
            "empty_text": _empty_text(folder, len(items)),
        }

    def _idle_status_text_locked(self, folder: str, items: list[dict]) -> str:
        """未运行时的状态文案（未选目录 / 已选目录 / 已停止）。"""
        if self._stopped_text:
            return self._stopped_text
        if not folder:
            return STATUS_NO_FOLDER
        if not os.path.isdir(folder):
            return EMPTY_FOLDER_TEXT
        if not items:
            return f"已选目录：{folder}（目录中没有 ZIP 压缩包）"
        return f"已选目录：{folder}（{len(items)} 个 ZIP）"

    # -- 动作：start / stop --------------------------------------------------

    def handle_start(self, payload: dict) -> dict:
        """启动本机回环 HTTP 服务（换包/重启前先释放旧服务与旧句柄）。"""
        with self._lock:
            path, name = self._resolve_selected_locked(payload)
            if not path:
                return {
                    "refresh": False,
                    "data": None,
                    "message": "请先在左侧选择要浏览的 ZIP 压缩包",
                }
            try:
                entries = list(self._reader.list_images(path))
            except Exception as exc:  # AppError 的文案已是逐字锁定的中文
                self._ctx.log(f"打开压缩包失败：{type(exc).__name__}: {exc}")
                return {"refresh": False, "data": None, "message": str(exc)}

            # 新包已通过校验：先停旧服务（同包复用句柄，不 close）
            self._stop_locked(keep_archive=path)
            self._stopped_text = ""
            self._selected_path = path
            self._selected_name = name or os.path.basename(path)
            self._entries = entries

            try:
                server = ZipWebServer(self._reader, path, entries, self._selected_name)
            except OSError as exc:
                self._release_selected_locked()
                self._ctx.log(f"启动浏览服务失败：{type(exc).__name__}: {exc}")
                return {
                    "refresh": False,
                    "data": None,
                    "message": f"启动浏览服务失败：{exc}",
                }

            thread = threading.Thread(
                target=server.serve_forever,
                name="zip-web-http",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread
            self._port = server.port
            self._url = f"http://127.0.0.1:{self._port}/"
            self._ctx.log(f"浏览服务已启动：{self._url}（{self._selected_name}）")
            return {
                "refresh": True,
                "data": self._payload_locked(),
                "message": f"浏览服务已启动：{self._url}",
            }

    def handle_stop(self, payload: dict) -> dict:
        """停止 HTTP 服务并释放端口（幂等）。"""
        with self._lock:
            if self._server is None:
                return {
                    "refresh": False,
                    "data": None,
                    "message": "浏览服务未在运行",
                }
            self._stop_locked()
            self._stopped_text = STATUS_STOPPED
            self._ctx.log("浏览服务已停止，端口已释放")
            return {
                "refresh": True,
                "data": self._payload_locked(),
                "message": "浏览服务已停止",
            }

    # -- 生命周期（页面 stop / 换目录 / 换包 / 测试清理共用） ----------------

    def stop_all(self) -> None:
        """停止本会话的 HTTP 服务并释放端口（幂等；不影响其它会话）。"""
        with self._lock:
            self._stop_locked()
            self._stopped_text = STATUS_STOPPED

    # -- 内部实现（调用方须持锁） -------------------------------------------

    def _resolve_selected_locked(self, payload: dict) -> tuple[str, str]:
        """从动作 payload 解析选中的 ZIP（``item`` 优先，其次 ``item_index``）。"""
        item = payload.get("item")
        if isinstance(item, dict):
            raw_path = item.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                path = os.path.abspath(raw_path.strip())
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    name = os.path.basename(path)
                return path, name

        index = _as_index(payload.get("item_index"))
        if index is not None and 0 <= index < len(self._items):
            entry = self._items[index]
            return os.path.abspath(entry["path"]), entry["name"]
        return "", ""

    def _update_folder_locked(self, requested: object) -> str:
        """处理 ``params.folder``：换目录时停服务、关旧包，并记住上次目录。"""
        folder = requested.strip() if isinstance(requested, str) else ""
        if not folder:
            return self._folder
        folder = os.path.abspath(folder)
        if not _same_path(folder, self._folder):
            self._stop_locked()
            self._folder = folder
            self._stopped_text = ""
        if os.path.isdir(folder):
            self._remember_folder(folder)
        return self._folder

    def _stored_folder(self) -> str:
        """读配置里的 last_folder（目录已不存在时视为未设置）。"""
        try:
            value = self._ctx.config_get("last_folder")
        except Exception:
            return ""
        if not isinstance(value, str) or not value.strip():
            return ""
        folder = os.path.abspath(value.strip())
        return folder if os.path.isdir(folder) else ""

    def _remember_folder(self, folder: str) -> None:
        """把当前目录写回 last_folder（失败只写日志，不影响浏览）。"""
        try:
            stored = self._ctx.config_get("last_folder")
            if isinstance(stored, str) and _same_path(stored, folder):
                return
            self._ctx.config_set("last_folder", folder)
        except Exception as exc:
            self._ctx.log(f"记住上次打开的文件夹失败: {type(exc).__name__}: {exc}")

    def _stop_locked(self, keep_archive: str = "") -> None:
        """停止 HTTP 服务并释放端口/句柄；``keep_archive`` 指定的包不关闭。"""
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        path = self._selected_path
        self._selected_path = ""
        self._selected_name = ""
        self._entries = []
        self._port = 0
        self._url = ""

        if server is not None:
            try:
                server.shutdown()
            except Exception as exc:  # 已在别的线程关闭 / 未 start 都只记日志
                self._ctx.log(f"停止浏览服务失败: {type(exc).__name__}: {exc}")
            try:
                server.server_close()
            except Exception as exc:
                self._ctx.log(f"关闭浏览服务套接字失败: {type(exc).__name__}: {exc}")
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

        if path and not (keep_archive and _same_path(path, keep_archive)):
            try:
                self._reader.close(path)
            except Exception as exc:
                self._ctx.log(f"关闭压缩包失败: {type(exc).__name__}: {exc}")

    def _release_selected_locked(self) -> None:
        """启动失败时清掉选择（句柄由 archive_reader 的 list_images 打开）。"""
        path = self._selected_path
        self._selected_path = ""
        self._selected_name = ""
        self._entries = []
        if path:
            try:
                self._reader.close(path)
            except Exception:
                pass


def _empty_text(folder: str, total: int) -> str:
    """action 模板空态文案（不是错误文案，锁定错误由服务异常给出）。"""
    if not folder:
        return FOLDER_HINT_TEXT
    if not os.path.isdir(folder):
        return EMPTY_FOLDER_TEXT
    if total == 0:
        return EMPTY_ZIP_TEXT
    return ""


# ---------------------------------------------------------------------------
# 进程内会话登记与清理
# ---------------------------------------------------------------------------

#: 本进程内所有已装配会话（stop_all 一次性关闭，避免端口/线程泄漏）
_SESSIONS: set[ZipWebSession] = set()
_SESSIONS_LOCK = threading.RLock()


def stop_all() -> None:
    """停止本进程内所有 ZIP 网页浏览会话的 HTTP 服务（测试/重新装配前调用）。

    宿主当前没有插件卸载钩子，``setup`` 的返回值也不会被调用；这是按任务书
    的兜底要求提供的显式清理入口：页面 ``stop`` 与换目录/换包已经即时释放，
    重新装配插件前调用本函数可确保旧端口也被释放。进程退出另由 ``atexit``
    兜底（daemon 线程随进程结束）。
    """
    with _SESSIONS_LOCK:
        # 只停止、不注销：已装配会话仍可继续被页面动作使用（再次 start 会重建服务）
        sessions = list(_SESSIONS)
    for session in sessions:
        try:
            session.stop_all()
        except Exception:
            pass


atexit.register(stop_all)


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------


def setup(ctx) -> None:
    """插件入口：取 ``archive_reader/1`` 服务并注册页面与 start/stop 动作。"""
    data_dir = getattr(ctx, "data_dir", "")
    if not isinstance(data_dir, str) or not data_dir:
        raise RuntimeError("插件上下文缺少 data_dir（宿主未提供可写数据目录）")

    try:
        reader = ctx.get_service(SERVICE_NAME, SERVICE_VERSION)
    except Exception as exc:
        raise RuntimeError(
            f"缺少服务 {SERVICE_NAME}/{SERVICE_VERSION}（ZIP 浏览插件未加载）: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    for method in ("list_images", "read_image", "close", "is_open"):
        if not callable(getattr(reader, method, None)):
            raise RuntimeError(
                f"服务 {SERVICE_NAME}/{SERVICE_VERSION} 缺少方法 {method}，拒绝加载"
            )

    session = ZipWebSession(ctx, reader)
    ctx.register_page(PAGE_ID, session.page_payload)
    ctx.register_page_action(PAGE_ID, ACTION_START, session.handle_start)
    ctx.register_page_action(PAGE_ID, ACTION_STOP, session.handle_stop)
    with _SESSIONS_LOCK:
        _SESSIONS.add(session)
    ctx.log(f"ZIP 网页浏览插件已就绪（依赖 {SERVICE_NAME}/{SERVICE_VERSION}）")
