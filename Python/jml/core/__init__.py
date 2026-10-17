"""core 包：协议常量与异常定义。"""

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
