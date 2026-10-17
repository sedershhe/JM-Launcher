"""插件协议核心定义（api_version = 1）。

本模块只放纯常量与异常类，不放任何可变状态，供宿主与各模块共同引用。

约定（对应技术方案 2.1 ~ 2.5 节）：
- 插件目录含 plugin.json（manifest），必填字段：id / name / version /
  api_version / entry / services / pages；
- services.provides / requires 形如 [{"name": str, "version": int}]；
- pages 形如 [{"id", "title", "template"}]，template 必须是本模块
  PAGE_TEMPLATES 中的一种；
- 所有事件 = 事件类型常量 + 按类型固定的 payload 字段表（禁止裸 dict），
  字段表见 EVENT_PAYLOAD_SCHEMAS。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 协议版本
# ---------------------------------------------------------------------------

#: 宿主支持的插件协议版本（当前为 1，不兼容即拒绝加载，不做降级）
API_VERSION = 1

# ---------------------------------------------------------------------------
# 页面模板类型（结构化页面协议，方案 A；插件不得返回任意界面描述）
# ---------------------------------------------------------------------------

#: list: 列定义 + 行数据 + 行按钮 + 翻页（下载历史、收藏夹）
#: browser: 文件夹选择 → 文件列表 + 翻页图片 + 返回（ZIP 浏览）
#: action: 文件列表 + 启动/停止动作 + 状态文案（ZIP 网页浏览）
#: form: 字段定义（文本/密码/下拉/路径选择）（设置项、账号录入）
PAGE_TEMPLATES = ("list", "browser", "action", "form")

# ---------------------------------------------------------------------------
# 事件类型常量（禁止裸 dict，payload 必须携带 type 字段）
# ---------------------------------------------------------------------------

#: 单本下载开始。payload: book_id, title
EVENT_DOWNLOAD_ITEM_STARTED = "download.item_started"

#: 单本下载结束。payload: book_id, title, ok, zip_path, error_code, error_message
EVENT_DOWNLOAD_ITEM_FINISHED = "download.item_finished"

#: 批量下载完成。payload: total, success, failed, cancelled
EVENT_DOWNLOAD_BATCH_FINISHED = "download.batch_finished"

#: 下载日志（已脱敏）。payload: level, message
EVENT_DOWNLOAD_LOG = "download.log"

#: 搜索页加载完成。payload: keyword, page, total_pages, results
EVENT_SEARCH_PAGE_LOADED = "search.page_loaded"

#: 插件通知。payload: level, title, message
EVENT_PLUGIN_NOTIFICATION = "plugin.notification"

#: 每个事件类型的 payload 必填字段表。
#: dispatch 前按表校验，缺字段抛 ValueError（含事件类型与缺失字段名）。
#: 注意：表里只列出必填字段名；字段的具体类型由各业务模块自行约定。
EVENT_PAYLOAD_SCHEMAS: dict[str, list[str]] = {
    EVENT_DOWNLOAD_ITEM_STARTED: ["book_id", "title"],
    EVENT_DOWNLOAD_ITEM_FINISHED: [
        "book_id",
        "title",
        "ok",
        "zip_path",
        "error_code",
        "error_message",
    ],
    EVENT_DOWNLOAD_BATCH_FINISHED: ["total", "success", "failed", "cancelled"],
    EVENT_DOWNLOAD_LOG: ["level", "message"],
    EVENT_SEARCH_PAGE_LOADED: ["keyword", "page", "total_pages", "results"],
    EVENT_PLUGIN_NOTIFICATION: ["level", "title", "message"],
}


# ---------------------------------------------------------------------------
# 异常体系（全部继承 PluginError 基类）
# ---------------------------------------------------------------------------


class PluginError(Exception):
    """插件体系相关错误的基类。

    所有宿主侧可预期的插件错误都用本类或其子类抛出，
    错误消息要求：中文、含插件 id 或目录名（便于插件管理界面展示）。
    """


class ProtocolVersionError(PluginError):
    """manifest 的 api_version 与宿主支持的协议版本不一致（拒绝加载）。"""


class DependencyError(PluginError):
    """依赖插件缺失，或所需服务无人提供。"""


class ServiceError(PluginError):
    """服务注册/解析失败（重复注册、找不到服务等）。"""


class ConfigError(PluginError):
    """插件配置非法（类型/枚举/范围不符），写入被拒绝并回退默认值。"""


class CircularDependencyError(PluginError):
    """插件依赖出现环（拒绝整环加载，消息列出环成员）。"""
