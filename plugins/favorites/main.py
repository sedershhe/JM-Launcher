"""收藏夹插件（阶段 4-4）。

功能（功能计划书 4.5）：
- 浏览**当前登录账号**的收藏夹，列表样式复用 ``list`` 模板；
- 切换收藏夹目录、切换排序（最近收藏 ``mr`` / 最多点击 ``mv`` / 最多图片 ``mp``）；
- 每条可「详情」「下载」——插件只返回 ``intent``，由 C# 转发给主窗口的
  现有 ``DetailWindow`` / 下载页路径（C# 不做业务判断）。
- 列表行带 ``thumb_book_id``（C# 据此异步取封面），页级带
  ``row_double_click_action="detail"``（双击行等价于点「详情」）；封面字节与
  取图失败兜底全部在 C#，插件只给编号。

登录态协作（本任务的关键设计，见桥接契约 §六）::

    setup:  ctx.get_service("jm.session", 1)        # manifest requires 保证存在
    load:   ctx.call_service("jm.account", 1, "ensure_login", {})
            session.get_client()                     # 宿主已登录的 jmcomic 客户端
            client.favorite_folder(page, order_by, folder_id)

- 插件**拿不到密码**：登录由 ``jm.account`` 用主进程内存凭据完成，插件只拿到
  宿主的客户端对象；不读密码、不 import 宿主私有模块、不探测任何路径；
- 未登录：``jm.account.ensure_login`` 返回 ``NOT_LOGGED_IN`` 时抛
  :class:`NotLoggedInError`，宿主入口转成
  ``{"ok": false, "error_code": "NOT_LOGGED_IN", "error_message": "未登录，无法浏览收藏夹"}``；
- 只调用收藏夹**只读**接口 ``favorite_folder(...)``，不写入收藏夹。

jmcomic 2.7.1 收藏夹 API 查证（``D:\\python\\Lib\\site-packages\\jmcomic``）：
- ``JmApiClient.favorite_folder(page=1, order_by=JmMagicConstants.ORDER_BY_LATEST,
  folder_id='0', username='')``（``jm_client_impl.py:840-855``），内部
  ``req_api(API_FAVORITE, params={page, folder_id, o})``（``:846-853``）；
- 排序常量 ``ORDER_BY_LATEST='mr'`` / ``ORDER_BY_PICTURE='mp'`` /
  ``ORDER_BY_VIEW='mv'``（``jm_config.py:43-45``）；
- 返回 ``JmFavoritePage``（``jm_entity.py:685-707``）：``content`` 为
  ``[(album_id, info_dict), ...]``、``folder_list``（元素含 ``FID`` / ``name``）、
  ``total``；``page_size`` 取 ``JmModuleConfig.PAGE_SIZE_FAVORITE``（``=20``，
  ``jm_config.py:134``）；
- 列表接口只保证返回 ``id / name / author / ...``，**不保证**返回页数、点击量、
  更新日期；本插件按可选键尽力映射（``total_photos``/``page_count``、
  ``total_views``/``views``/``likes``、``update_date``/``updated_at``），缺省给
  空值，绝不额外发网络请求补全。
"""

from __future__ import annotations

from jml.core.errors import ERROR_NOT_LOGGED_IN, InvalidArgumentError, NotLoggedInError

#: 本插件在 manifest 中声明的页面 id
PAGE_ID = "favorites.main"

#: 登录会话服务（manifest services.requires 必须一致）
SESSION_SERVICE_NAME = "jm.session"
SESSION_SERVICE_VERSION = 1

#: 账号服务（宿主一定提供的 JSON 信封服务；ensure_login 自动用内存凭据登录）
ACCOUNT_SERVICE_NAME = "jm.account"
ACCOUNT_SERVICE_VERSION = 1

#: 默认排序：jmcomic 的「最近收藏」
DEFAULT_SORT = "mr"

#: 排序选项（value 与 jmcomic 的 ORDER_BY_* 常量一致）
SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("mr", "最近收藏"),
    ("mv", "最多点击"),
    ("mp", "最多图片"),
)

#: 排序合法值集合
SORT_VALUES = frozenset(value for value, _label in SORT_OPTIONS)

#: 「全部」收藏夹（jmcomic 用 folder_id='0' 表示全部）
ALL_FOLDER_ID = "0"

#: 「全部」收藏夹显示名
ALL_FOLDER_NAME = "全部"

#: 收藏夹接口默认每页条数（jmcomic PAGE_SIZE_FAVORITE = 20）
DEFAULT_PAGE_SIZE = 20

#: 空收藏夹文案（已登录但一条都没有）
EMPTY_TEXT = "收藏夹是空的"

#: 未登录文案（与页面错误文案逐字一致）
NOT_LOGGED_IN_TEXT = "未登录，无法浏览收藏夹"

#: 缺字段时的兜底标题 / 作者（与旧收藏夹插件一致）
UNKNOWN_TITLE = "未知标题"
UNKNOWN_AUTHOR = "未知作者"


# ---------------------------------------------------------------------------
# 纯函数：字段清洗（可单独单元测试）
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    """把外部值转成去掉首尾空白的字符串（None → 空串）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _to_int(value: object) -> int:
    """尽力把外部值转成非负 int（失败 / 负数返回 0，bool 不算数字）。"""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _positive_int(value: object, default: int) -> int:
    """把分页参数转成正整数；非法 / 非正数返回 default（bool 不算数字）。"""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _first_value(source: dict, keys: tuple[str, ...]) -> object:
    """按顺序取第一个存在且非 None / 非空串的字段（保留数字 0）。"""
    for key in keys:
        if key in source:
            value = source[key]
            if value is not None and value != "":
                return value
    return None


def _author_text(value: object) -> str:
    """作者字段可能是 list / tuple / 标量，统一成 ``"a, b"`` 形态。"""
    if isinstance(value, (list, tuple)):
        return ", ".join(item for item in (_text(part) for part in value) if item)
    return _text(value)


def _normalize_folders(folder_list: object) -> list[dict]:
    """把 ``JmFavoritePage.folder_list`` 规范成 ``[{"id", "name"}]``。

    接口元素含 ``FID`` / ``name``（网页客户端还会出现数字键别名）；缺 id 的
    条目跳过，重复 id 只保留第一条。返回空列表表示接口没有给目录清单
    （移动端 API 就是这样，调用方补「全部」兜底）。
    """
    folders: list[dict] = []
    seen: set[str] = set()
    if not isinstance(folder_list, (list, tuple)):
        return folders
    for item in folder_list:
        if not isinstance(item, dict):
            continue
        folder_id = _text(
            _first_value(item, ("FID", "folder_id", "id", "0"))
        )
        if not folder_id or folder_id in seen:
            continue
        name = _text(_first_value(item, ("name", "2"))) or folder_id
        seen.add(folder_id)
        folders.append({"id": folder_id, "name": name})
    return folders


def _fallback_folders(requested_folder: str) -> list[dict]:
    """接口没给目录清单时的兜底：只保留当前目录（缺省为「全部」）。"""
    folder_id = requested_folder or ALL_FOLDER_ID
    name = ALL_FOLDER_NAME if folder_id == ALL_FOLDER_ID else f"收藏夹 {folder_id}"
    return [{"id": folder_id, "name": name}]


def _pick_folder(folders: list[dict], requested_folder: str) -> str:
    """选当前目录：参数/配置的目录有效就用它，否则用第一个目录。"""
    if not folders:
        return requested_folder or ALL_FOLDER_ID
    ids = {folder["id"] for folder in folders}
    if requested_folder and requested_folder in ids:
        return requested_folder
    return folders[0]["id"]


def _row_from_item(item: object) -> dict | None:
    """把 ``(album_id, info)`` 转成 list 模板的一行；畸形条目返回 None。"""
    try:
        album_id, info = item  # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    book_id = _text(album_id)
    if not book_id:
        return None
    data = info if isinstance(info, dict) else {}
    title = _text(_first_value(data, ("name", "title"))) or (
        _text(info) if not isinstance(info, dict) else ""
    )
    updated = _text(
        _first_value(
            data,
            (
                "update_date",
                "updated_at",
                "update_at",
                "update_time",
            ),
        )
    )
    return {
        "book_id": book_id,
        # 阶段 4-4 协议扩展：行级可选字段，C# 用它去取封面（插件不传图片字节）。
        "thumb_book_id": book_id,
        "title": title or UNKNOWN_TITLE,
        "author": _author_text(_first_value(data, ("author", "authors")))
        or UNKNOWN_AUTHOR,
        # 收藏夹列表接口不保证带页数 / 点击量；缺省 0 只表示"列表未提供"，
        # 详情窗会单独拉真实页数（与旧收藏夹插件行为一致）。
        "page_count": _to_int(
            _first_value(data, ("total_photos", "page_count", "image_count"))
        ),
        "click_count": _to_int(
            _first_value(data, ("total_views", "views", "likes", "click_count"))
        ),
        "updated": updated,
    }


def _row_book_id(payload: dict) -> str:
    """从动作参数里取本子号（C# 回传整行 JSON 的 ``row``）。"""
    row = payload.get("row")
    if isinstance(row, dict):
        book_id = _text(row.get("book_id"))
        if book_id:
            return book_id
    return _text(payload.get("book_id"))


# ---------------------------------------------------------------------------
# 页面逻辑
# ---------------------------------------------------------------------------


class FavoritesPage:
    """收藏夹页面：登录检查 → 拉取一页收藏 → 组装 list 模板 payload。"""

    def __init__(self, ctx, get_client) -> None:
        self._ctx = ctx
        self._get_client = get_client

    # -- 登录态 -------------------------------------------------------------

    def _ensure_login(self) -> None:
        """确保宿主已登录；未登录统一转成 ``NOT_LOGGED_IN`` 页面错误。

        自动登录只由主程序的 ``jm.account.ensure_login`` 负责（用内存凭据），
        插件**不碰**账号密码。任何登录失败都按"未登录"处理，让页面显示明确
        提示而不是空白列表。
        """
        try:
            status = self._ctx.call_service(
                ACCOUNT_SERVICE_NAME, ACCOUNT_SERVICE_VERSION, "ensure_login", {}
            )
        except Exception:
            # 宿主没有 jm.account 服务（部署异常）：同样按未登录提示。
            raise NotLoggedInError(NOT_LOGGED_IN_TEXT) from None
        if isinstance(status, dict) and status.get("ok") is True:
            return

        message = ""
        if isinstance(status, dict):
            code = _text(status.get("error_code"))
            message = _text(status.get("error_message"))
            if code and code != ERROR_NOT_LOGGED_IN:
                # 例如 INVALID_CREDENTIALS / NETWORK_ERROR：附上已脱敏的原因，
                # 让用户知道"为什么没登录"，但仍按未登录处理。
                raise NotLoggedInError(
                    f"{NOT_LOGGED_IN_TEXT}（{message or code}）"
                )
        raise NotLoggedInError(NOT_LOGGED_IN_TEXT)

    # -- 页面加载 -----------------------------------------------------------

    def load(self, params: dict) -> dict:
        """``plugin_page_load`` 钩子：返回 list 模板 payload。"""
        self._ensure_login()

        page = _positive_int(params.get("page"), 1)
        requested_folder = _text(params.get("folder_id")) or _text(
            self._ctx.config_get("last_folder_id")
        )
        sort = _text(params.get("sort")) or _text(self._ctx.config_get("sort"))
        if sort not in SORT_VALUES:
            sort = DEFAULT_SORT

        used_folder = requested_folder or ALL_FOLDER_ID
        client = self._get_client()
        result = self._fetch(client, page, used_folder, sort)

        folders = _normalize_folders(getattr(result, "folder_list", ()))
        if folders:
            folder_id = _pick_folder(folders, requested_folder)
        else:
            folders = _fallback_folders(requested_folder)
            folder_id = folders[0]["id"]
        if folder_id != used_folder:
            # 参数/配置的目录为空或已失效：按选中的目录重新拉一页
            # （只在首次进入或收藏夹被删除时发生，不会形成循环）。
            result = self._fetch(client, page, folder_id, sort)
            fresh = _normalize_folders(getattr(result, "folder_list", ()))
            if fresh:
                folders = fresh

        self._persist("last_folder_id", folder_id)
        self._persist("sort", sort)

        content = getattr(result, "content", ()) or ()
        rows = [row for row in (_row_from_item(item) for item in content) if row]
        total = _to_int(getattr(result, "total", 0))
        page_size = _to_int(getattr(result, "page_size", 0)) or DEFAULT_PAGE_SIZE
        total_pages = max(1, (total + page_size - 1) // page_size)

        return {
            "columns": [
                {"key": "title", "title": "标题"},
                {"key": "author", "title": "作者", "width": 160},
                {"key": "page_count", "title": "页数", "width": 80},
                {"key": "click_count", "title": "收藏数", "width": 90},
            ],
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "row_actions": [
                {"id": "detail", "title": "详情", "primary": True},
                {"id": "download", "title": "下载"},
            ],
            # 阶段 4-4 协议扩展：页级可选字段。双击行时 C# 以
            # {row, row_index, page} 调 plugin_page_action，action id 就是这里的值；
            # 与行按钮走同一套 intent 机制（detail 打开详情窗）。
            "row_double_click_action": "detail",
            "empty_text": EMPTY_TEXT,
            # 阶段 4-4 协议扩展：list 模板可选 filters（C# 渲染成下拉框，
            # 变更后以 {page: 1, <key>: <value>} 重新 plugin_page_load）。
            "filters": [
                {
                    "key": "folder_id",
                    "label": "收藏夹",
                    "options": [
                        {"value": folder["id"], "label": folder["name"]}
                        for folder in folders
                    ],
                    "value": folder_id,
                },
                {
                    "key": "sort",
                    "label": "排序",
                    "options": [
                        {"value": value, "label": label}
                        for value, label in SORT_OPTIONS
                    ],
                    "value": sort,
                },
            ],
            # 任务要求同时给出 data.folders 供 C# 下拉使用（filters 之外
            # 的冗余字段，便于不支持 filters 的旧渲染器降级）。
            "folders": folders,
        }

    @staticmethod
    def _fetch(client, page: int, folder_id: str, sort: str):
        """调用 jmcomic 收藏夹只读接口（API 客户端不接受 username，不传）。"""
        return client.favorite_folder(
            page=page,
            order_by=sort,
            folder_id=folder_id,
        )

    def _persist(self, key: str, value: str) -> None:
        """记住目录 / 排序（只写自己 schema 内的配置项，失败不影响页面）。"""
        try:
            if self._ctx.config_get(key) == value:
                return
            self._ctx.config_set(key, value)
        except Exception as exc:  # 配置写失败不能把收藏夹页面整个打挂
            self._ctx.log(
                f"收藏夹配置 {key} 写入失败（不影响浏览）: {type(exc).__name__}: {exc}"
            )

    # -- 行动作 -------------------------------------------------------------

    def action_detail(self, payload: dict) -> dict:
        """「详情」：只回传 intent，由 C# 打开现有 DetailWindow。"""
        book_id = _row_book_id(payload)
        if not book_id:
            raise InvalidArgumentError("详情动作缺少 book_id")
        return {"ok": True, "intent": {"kind": "detail", "book_id": book_id}}

    def action_download(self, payload: dict) -> dict:
        """「下载」：只回传 intent，由 C# 走现有下载页 + StartWithIds 路径。"""
        book_id = _row_book_id(payload)
        if not book_id:
            raise InvalidArgumentError("下载动作缺少 book_id")
        return {"ok": True, "intent": {"kind": "download", "book_ids": [book_id]}}


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------


def setup(ctx) -> None:
    """插件入口：解析 jm.session 服务并注册收藏夹页面与两个行动作。"""
    session = ctx.get_service(SESSION_SERVICE_NAME, SESSION_SERVICE_VERSION)
    get_client = getattr(session, "get_client", None)
    if not callable(get_client):
        raise RuntimeError(
            f"服务 {SESSION_SERVICE_NAME}/版本 {SESSION_SERVICE_VERSION} "
            "缺少 get_client() 方法"
        )

    page = FavoritesPage(ctx, get_client)
    ctx.register_page(PAGE_ID, page.load)
    ctx.register_page_action(PAGE_ID, "detail", page.action_detail)
    ctx.register_page_action(PAGE_ID, "download", page.action_download)
