"""ZIP 浏览插件（阶段 4-2）。

功能：选文件夹列出 ``*.zip``（文件名自然排序）→ 双击某个 ZIP 后**直接在包内
翻页看图**（``zipfile`` 读字节，**不解压到磁盘**）→ Esc /「返回列表」回到列表。

页面状态机（``zip_browser.main``，browser 模板）::

    未选目录        folder=""        empty_text="选择文件夹…"，allow_folder_choice=true
    已选目录未选包  item_index=-1    items=当前页 ZIP 列表，释放压缩包句柄
    包内浏览        item_index>=0    items + 当前包的 page/total/image_count
    换目录          params.folder    重新列 ZIP，并把目录记进 last_folder 配置

``item_index`` 是**当前页 items 的下标**（C# 只按当前页列表回传下标）；
``plugin_page_image(index)`` 的 index 是包内图片下标（0 起），由本插件在
``page_payload`` 里记住当前选中的压缩包（契约 §12.2 / §12.3）。

句柄生命周期：换目录 / 回列表 / 选中别的包时关闭旧句柄；同一个包再次加载但句柄
已被外部 ``close``（例如 ZIP 网页浏览插件释放）时**明确报"压缩包已关闭"**，不静默
重开——先回列表（item_index=-1）再双击即可重新打开，语义可预期。

对外服务：``archive_reader/1``（对象服务，供 ZIP 网页浏览等插件复用，杜绝路径
探测）。服务对象不持有宿主对象，四个方法共用一个显式 ``threading.RLock``。

错误文案**逐字锁定**（计划书 11.6，不得改写）；页面失败时由宿主入口
``plugin_page_load`` / ``plugin_page_action`` 统一转成
``{"ok": false, "error_code": ..., "error_message": ...}``::

    压缩包不存在 / 不是有效的 ZIP 压缩包 / 压缩包中没有可浏览的图片 /
    压缩包已关闭 / 页码超出范围
"""

from __future__ import annotations

import os
import re
import threading
import zipfile

from jml.core.errors import (
    ArchiveClosedError,
    ArchiveError,
    ArchiveNotFoundError,
    PageOutOfRangeError,
)

#: 本插件在 manifest 中声明的页面 id
PAGE_ID = "zip_browser.main"

#: 对外提供的服务名与版本（manifest services.provides 必须一致）
SERVICE_NAME = "archive_reader"
SERVICE_VERSION = 1

#: ZIP 列表每页条数（浏览器模板的 page_size）
PAGE_SIZE = 20

#: 尚未选文件夹时的页面状态文案（计划书 4.6「选文件夹列出所有 ZIP」）
FOLDER_HINT_TEXT = "选择文件夹…"

#: 目录不存在（被删除/移动）时的空态文案
EMPTY_FOLDER_TEXT = "文件夹不存在，请重新选择"

#: 目录里没有 ZIP 时的空态文案
EMPTY_ZIP_TEXT = "目录中没有 ZIP 压缩包"

# --- 五条逐字锁定的错误文案（计划书 11.6；不得改写） ---------------------
MSG_ARCHIVE_MISSING = "压缩包不存在"
MSG_ARCHIVE_INVALID = "不是有效的 ZIP 压缩包"
MSG_NO_IMAGES = "压缩包中没有可浏览的图片"
MSG_ARCHIVE_CLOSED = "压缩包已关闭"
MSG_PAGE_OUT_OF_RANGE = "页码超出范围"

#: 视为可浏览图片的条目后缀（小写比较）
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp", ".bmp")

#: 数字段切分（自然排序：``999`` 排在 ``1000`` 之前）
_NATURAL_SPLIT = re.compile(r"(\d+)")


# ---------------------------------------------------------------------------
# 纯函数：自然排序与参数清洗（可单独单元测试，不依赖宿主）
# ---------------------------------------------------------------------------


def natural_key(name: str) -> tuple:
    """自然排序键：数字段按整数比较，文本段按大小写不敏感比较。

    ``"2.zip"`` < ``"10.zip"``、``"999.png"`` < ``"1000.png"``；
    同位置要么都是数字段 ``(0, int)``、要么都是文本段 ``(1, str)``，
    元组比较不会出现 int 与 str 互比。
    """
    parts = [part for part in _NATURAL_SPLIT.split(name or "") if part]
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in parts
    )


def _as_index(value: object) -> int | None:
    """把参数转成整数下标；非法值返回 None（bool 不算数字）。"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: object, default: int) -> int:
    """把页码参数转成正整数；非法 / 非正数返回 default（bool 不算数字）。"""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _same_path(left: str, right: str) -> bool:
    """Windows 路径比较（忽略大小写与分隔符差异）。"""
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def is_image_entry(info: zipfile.ZipInfo) -> bool:
    """ZIP 条目是否是可直接展示的图片（跳过目录、隐藏卷与 __MACOSX 垃圾）。"""
    if info.is_dir():
        return False
    name = info.filename
    if not name or name.startswith("__MACOSX/"):
        return False
    base = os.path.basename(name)
    return bool(base) and base.lower().endswith(IMAGE_EXTENSIONS)


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


# ---------------------------------------------------------------------------
# archive_reader/1：压缩包读取服务
# ---------------------------------------------------------------------------


class ArchiveReader:
    """``archive_reader/1`` 服务对象：只读 ZIP 内图片，不解压落盘。

    方法（线程安全，全部可在任意线程调用）::

        list_images(zip_path) -> [entry_name, ...]   # 打开（已开则复用）+ 自然排序
        read_image(zip_path, index) -> bytes         # 需已打开；越界抛"页码超出范围"
        close(zip_path)                              # 幂等；未打开时静默忽略
        is_open(zip_path) -> bool

    纪律：
    - 所有公开方法共用一把显式 ``threading.RLock``（不靠 GIL 侥幸）；
    - 对象内只有路径 → ``ZipFile`` 句柄与条目表，**不持有任何宿主对象**
      （没有 ctx / 注册表 / 事件总线），跨插件复用不会泄漏宿主内部；
    - 打开与条目筛选在锁内完成；``read`` 返回原始 bytes，不写临时文件。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: 规范化路径 → 已打开的 ZipFile
        self._archives: dict[str, zipfile.ZipFile] = {}
        #: 规范化路径 → 自然排序后的图片条目名
        self._entries: dict[str, list[str]] = {}

    # -- 公开接口 -----------------------------------------------------------

    def list_images(self, zip_path: str) -> list[str]:
        """列出包内可浏览图片条目（自然排序；未打开则先打开）。

        异常:
            ArchiveNotFoundError —— 路径不存在
            ArchiveError         —— 不是有效 ZIP / 没有可浏览图片
        """
        key, path = _archive_key(zip_path)
        with self._lock:
            self._ensure_open_locked(key, path)
            return list(self._entries[key])

    def read_image(self, zip_path: str, index: int) -> bytes:
        """读取包内第 ``index`` 张图片（0 起）的**原始字节**。

        异常:
            ArchiveClosedError    —— 压缩包未打开或已被 close（文案"压缩包已关闭"）
            PageOutOfRangeError   —— 下标越界/非法（文案"页码超出范围"）
            ArchiveError          —— 读取中途发现包结构损坏
        """
        key, _path = _archive_key(zip_path)
        with self._lock:
            archive = self._archives.get(key)
            if archive is None:
                raise ArchiveClosedError(MSG_ARCHIVE_CLOSED)
            names = self._entries.get(key, [])
            position = _as_index(index)
            if position is None or not 0 <= position < len(names):
                raise PageOutOfRangeError(MSG_PAGE_OUT_OF_RANGE)
            try:
                return archive.read(names[position])
            except (zipfile.BadZipFile, RuntimeError) as exc:
                raise ArchiveError(MSG_ARCHIVE_INVALID) from exc

    def close(self, zip_path: str) -> None:
        """关闭压缩包句柄（幂等；未打开时静默忽略）。"""
        key, _path = _archive_key(zip_path)
        with self._lock:
            archive = self._archives.pop(key, None)
            self._entries.pop(key, None)
        if archive is not None:
            try:
                archive.close()
            except OSError:
                pass

    def is_open(self, zip_path: str) -> bool:
        """该压缩包当前是否保持打开（``read_image`` 的前置条件）。"""
        key, _path = _archive_key(zip_path)
        with self._lock:
            return key in self._archives

    # -- 内部实现（调用方须持锁） -------------------------------------------

    def _ensure_open_locked(self, key: str, path: str) -> None:
        """打开压缩包并缓存图片条目；已打开则直接复用。"""
        if key in self._archives:
            return
        if not os.path.isfile(path):
            raise ArchiveNotFoundError(MSG_ARCHIVE_MISSING)
        try:
            archive = zipfile.ZipFile(path, "r")
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ArchiveError(MSG_ARCHIVE_INVALID) from exc
        try:
            names = sorted(
                (info.filename for info in archive.infolist() if is_image_entry(info)),
                key=natural_key,
            )
        except BaseException:
            archive.close()
            raise
        if not names:
            archive.close()
            raise ArchiveError(MSG_NO_IMAGES)
        self._archives[key] = archive
        self._entries[key] = names


def _archive_key(zip_path: str) -> tuple[str, str]:
    """把调用方传入的路径规范化成 ``(字典键, 绝对路径)``。

    空值 / 非路径类型按"压缩包不存在"处理（与文件被删除同一类失败）。
    """
    try:
        raw = os.fspath(zip_path)
    except TypeError:
        raw = ""
    if not isinstance(raw, str) or not raw.strip():
        raise ArchiveNotFoundError(MSG_ARCHIVE_MISSING)
    path = os.path.abspath(raw)
    return os.path.normcase(path), path


# ---------------------------------------------------------------------------
# 页面状态机
# ---------------------------------------------------------------------------


class ZipBrowserSession:
    """``zip_browser.main`` 页面状态机（线程安全）。

    只依赖 ``ctx`` 的 config_get / config_set / log 三个方法；页面状态（当前
    目录、当前压缩包、条目表）全部在锁内读写，C# 多次翻页不会串状态。
    """

    def __init__(self, ctx, reader: ArchiveReader) -> None:
        self._ctx = ctx
        self._reader = reader
        self._lock = threading.RLock()
        #: 当前目录（绝对路径；空串 = 还没选过）
        self._folder = self._stored_folder()
        #: 当前选中的压缩包绝对路径 + 条目快照（空串 = 只看列表）
        self._selected_path = ""
        self._selected_entries: list[str] = []

    # -- 页面加载 -----------------------------------------------------------

    def page_payload(self, params: dict) -> dict:
        """构造 browser 模板 payload（契约 §12.2，含阶段 4-2 的 folder 扩展）。"""
        with self._lock:
            folder = self._update_folder_locked(params.get("folder"))
            items = list_zip_files(folder)

            page = _positive_int(params.get("page"), 1)
            total = len(items)
            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, total_pages)
            start = (page - 1) * PAGE_SIZE
            page_items = items[start : start + PAGE_SIZE]

            item_index = _as_index(params.get("item_index"))
            if item_index is None:
                item_index = -1

            payload = {
                "items": page_items,
                "page": page,
                "page_size": PAGE_SIZE,
                "total": total,
                "total_pages": total_pages,
                "item_index": -1,
                "image_index": 0,
                "image_count": 0,
                "empty_text": _empty_text(folder, total),
                # 阶段 4-2 协议小扩展：C# 据此显示「选择文件夹…」按钮
                "folder": folder,
                "allow_folder_choice": True,
            }

            if 0 <= item_index < len(page_items):
                entries = self._select_locked(page_items[item_index]["path"])
                image_count = len(entries)
                image_index = _as_index(params.get("image_index"))
                if image_index is None:
                    image_index = 0
                if image_index < 0 or image_index >= image_count:
                    raise PageOutOfRangeError(MSG_PAGE_OUT_OF_RANGE)
                payload["item_index"] = item_index
                payload["image_index"] = image_index
                payload["image_count"] = image_count
            else:
                # 未选中 / 回列表：释放句柄（下次选中会重新打开）
                self._release_locked()
            return payload

    # -- 图片入口 -----------------------------------------------------------

    def image_bytes(self, index: int) -> bytes | None:
        """``plugin_page_image`` 解析器：直接返回包内图片原始字节。

        任何失败都返回 None（宿主要求 ``bytes | None``，C# 显示"图片加载失败"），
        失败原因写插件日志；页面级错误（不存在/已关闭/页码越界）由
        ``page_payload`` 给出明确 ``error_code``。
        """
        with self._lock:
            if not self._selected_path:
                return None
            position = _as_index(index)
            if position is None or not 0 <= position < len(self._selected_entries):
                self._ctx.log(f"取图下标非法，已忽略: {index!r}")
                return None
            try:
                return self._reader.read_image(self._selected_path, position)
            except Exception as exc:  # AppError / OSError 都在这里收口
                self._ctx.log(f"取图失败：{type(exc).__name__}: {exc}")
                return None

    # -- 内部实现（调用方须持锁） -------------------------------------------

    def _update_folder_locked(self, requested: object) -> str:
        """处理 ``params.folder``：换目录时释放旧包，并记住上次打开的目录。"""
        folder = requested.strip() if isinstance(requested, str) else ""
        if not folder:
            return self._folder
        folder = os.path.abspath(folder)
        if not _same_path(folder, self._folder):
            self._release_locked()
            self._folder = folder
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

    def _select_locked(self, path: str) -> list[str]:
        """选中压缩包：同包翻页复用句柄，换包先关旧的。

        同一个包再次加载但句柄已被外部 ``close`` → 报"压缩包已关闭"（不静默
        重开）：调用方应先回列表（item_index=-1）再重新双击，语义可预期。
        """
        if self._selected_path and _same_path(path, self._selected_path):
            if not self._reader.is_open(self._selected_path):
                raise ArchiveClosedError(MSG_ARCHIVE_CLOSED)
            return list(self._selected_entries)
        self._release_locked()
        entries = self._reader.list_images(path)
        self._selected_path = path
        self._selected_entries = list(entries)
        return entries

    def _release_locked(self) -> None:
        """释放当前压缩包句柄（幂等）。"""
        path, self._selected_path = self._selected_path, ""
        self._selected_entries = []
        if not path:
            return
        try:
            self._reader.close(path)
        except Exception as exc:
            self._ctx.log(f"关闭压缩包失败: {type(exc).__name__}: {exc}")


def _empty_text(folder: str, total: int) -> str:
    """browser 模板空态文案（不是错误文案，五种锁定错误由异常给出）。"""
    if not folder:
        return FOLDER_HINT_TEXT
    if not os.path.isdir(folder):
        return EMPTY_FOLDER_TEXT
    if total == 0:
        return EMPTY_ZIP_TEXT
    return ""


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------


def setup(ctx) -> None:
    """插件入口：注册页面、图片钩子与 ``archive_reader/1`` 服务。"""
    data_dir = getattr(ctx, "data_dir", "")
    if not isinstance(data_dir, str) or not data_dir:
        raise RuntimeError("插件上下文缺少 data_dir（宿主未提供可写数据目录）")

    reader = ArchiveReader()
    session = ZipBrowserSession(ctx, reader)

    ctx.register_page(PAGE_ID, session.page_payload)
    ctx.register_page_image(PAGE_ID, session.image_bytes)
    ctx.register_service(SERVICE_NAME, SERVICE_VERSION, reader)
    ctx.log("ZIP 浏览插件已就绪（archive_reader/1）")
