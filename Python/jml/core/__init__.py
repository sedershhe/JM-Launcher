"""core 包：协议常量、异常与阶段 2 业务核心。

业务模块：book_ids（编号解析）/ naming（产物命名）/ archive（打包校验）/
downloader（批量下载）/ searcher（搜索）/ detail（详情）/ cover（封面字节）/
errors（错误分类与脱敏）/ jm_source（jmcomic 数据源适配）。
"""

from jml.core.protocol import (
    API_VERSION,
    EVENT_DOWNLOAD_BATCH_FINISHED,
    EVENT_DOWNLOAD_ITEM_FINISHED,
    EVENT_DOWNLOAD_ITEM_STARTED,
    EVENT_DOWNLOAD_LOG,
    EVENT_PAYLOAD_SCHEMAS,
    EVENT_PLUGIN_NOTIFICATION,
    EVENT_SEARCH_PAGE_LOADED,
    PAGE_TEMPLATES,
    ConfigError,
    CircularDependencyError,
    DependencyError,
    PluginError,
    ProtocolVersionError,
    ServiceError,
)

__all__ = [
    "API_VERSION",
    "EVENT_DOWNLOAD_ITEM_STARTED",
    "EVENT_DOWNLOAD_ITEM_FINISHED",
    "EVENT_DOWNLOAD_BATCH_FINISHED",
    "EVENT_DOWNLOAD_LOG",
    "EVENT_SEARCH_PAGE_LOADED",
    "EVENT_PLUGIN_NOTIFICATION",
    "EVENT_PAYLOAD_SCHEMAS",
    "PAGE_TEMPLATES",
    "PluginError",
    "ProtocolVersionError",
    "DependencyError",
    "ServiceError",
    "ConfigError",
    "CircularDependencyError",
]
