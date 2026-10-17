"""PluginContext——传给插件 setup 的唯一对象（对应旧缺陷 #9）。

- 插件只能通过本对象与宿主交互：register_service / subscribe /
  get_service / config_get / log，**只暴露这些方法**
- 插件不许摸宿主私有属性：本类不持有 registry/bus 的引用，全部通过
  注册回调闭包转发（插件在 ctx 上拿不到任何宿主内部对象）
- ctx 内保存 plugin_id，log 打日志时自动带前缀 [plugin:xxx]
"""

from __future__ import annotations

from typing import Callable


class PluginContext:
    """插件运行上下文（宿主构造，传给插件的 setup(ctx)）。

    构造参数全部是**回调函数**而非宿主对象——这是刻意的封装：
    插件即使遍历 ctx 的属性也摸不到宿主的注册表/事件总线本体。
    """

    def __init__(
        self,
        plugin_id: str,
        *,
        on_register_service: Callable[[str, int, object], None],
        on_subscribe: Callable[[str, Callable], None],
        on_get_service: Callable[[str, int], object],
        on_config_get: Callable[[str], object],
        on_log: Callable[[str], None],
    ) -> None:
        # 双下划线命名 + 只在方法里用：插件不应直接访问这些字段
        self.__plugin_id = plugin_id
        self.__on_register_service = on_register_service
        self.__on_subscribe = on_subscribe
        self.__on_get_service = on_get_service
        self.__on_config_get = on_config_get
        self.__on_log = on_log

    # -- 插件可见的属性（只读） ---------------------------------------------

    @property
    def plugin_id(self) -> str:
        """本插件的 id（manifest 中声明）。"""
        return self.__plugin_id

    # -- 插件可调用的方法（仅此五个） ---------------------------------------

    def register_service(self, name: str, version: int, ref: object) -> None:
        """注册本插件提供的服务（转发给宿主服务注册表）。

        参数 ref: 服务对象或可调用工厂（由消费方通过 get_service 获取）。
        """
        self.__on_register_service(name, version, ref)

    def subscribe(self, event_type: str, handler) -> None:
        """订阅事件（转发给宿主事件总线）。"""
        self.__on_subscribe(event_type, handler)

    def get_service(self, name: str, version: int) -> object:
        """获取其它插件或主程序注册的服务（精确匹配版本）。"""
        return self.__on_get_service(name, version)

    def config_get(self, key: str) -> object:
        """读本插件的配置项（无值时返回 schema 默认值）。"""
        return self.__on_config_get(key)

    def log(self, message: str) -> None:
        """打日志（宿主自动加 [plugin:xxx] 前缀）。"""
        self.__on_log(message)
