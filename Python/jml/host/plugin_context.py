"""PluginContext——传给插件 setup 的唯一对象（对应旧缺陷 #9）。

- 插件只能通过本对象与宿主交互：register_service / subscribe /
  get_service / call_service / config_get / log，以及结构化页面协议的
  register_page / register_page_action / register_page_image，**只暴露这些方法**；
  另有 plugin_id / data_dir 两个只读属性
- 插件不许摸宿主私有属性：本类不持有 registry/bus 的引用，全部通过
  注册回调闭包转发（插件在 ctx 上拿不到任何宿主内部对象）
- ctx 内保存 plugin_id，log 打日志时自动带前缀 [plugin:xxx]
- data_dir 是宿主（loader）创建并传入的**绝对路径**可写数据目录
  （%APPDATA%/JML/plugins/<plugin_id>/data），插件只拿路径、不自己推导
  APPDATA 位置（避免路径探测与硬编码）

结构化页面协议（技术方案第三节方案 A）：
插件在这里注册“页面钩子”，宿主 ``plugin_page_load`` / ``plugin_page_action`` /
``plugin_page_image`` 入口按 ``(plugin_id, page_id)`` 路由过去。plugin_id
由宿主在构造 ctx 时绑定，**方法签名里没有 plugin_id**——插件无法注册或
覆盖别人的页面；页面 id 还必须是该插件 manifest ``pages[]`` 声明过的
（由 loader 构造 ctx 时校验，非法直接让本次装配失败）。
"""

from __future__ import annotations

from typing import Callable

from jml.core.protocol import ServiceError


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
        on_register_page: Callable[[str, Callable], None],
        on_register_page_action: Callable[[str, str, Callable], None],
        on_register_page_image: Callable[[str, Callable], None],
        data_dir: str,
    ) -> None:
        # 双下划线命名 + 只在方法里用：插件不应直接访问这些字段
        self.__plugin_id = plugin_id
        self.__on_register_service = on_register_service
        self.__on_subscribe = on_subscribe
        self.__on_get_service = on_get_service
        self.__on_config_get = on_config_get
        self.__on_log = on_log
        self.__on_register_page = on_register_page
        self.__on_register_page_action = on_register_page_action
        self.__on_register_page_image = on_register_page_image
        self.__data_dir = data_dir

    # -- 插件可见的属性（只读） ---------------------------------------------

    @property
    def plugin_id(self) -> str:
        """本插件的 id（manifest 中声明）。"""
        return self.__plugin_id

    @property
    def data_dir(self) -> str:
        """本插件专属的可写数据目录（宿主创建并传入的绝对路径）。

        路径规则：%APPDATA%/JML/plugins/<plugin_id>/data（由 loader 统一
        推导并确保目录存在）。插件只在此目录下读写自己的数据文件，不得
        自行拼 %APPDATA% 路径，也不得读写别的插件目录。
        """
        return self.__data_dir

    # -- 插件可调用的方法（仅此九个） ---------------------------------------

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

    def call_service(
        self, name: str, version: int, method: str, payload: dict
    ) -> dict:
        """调用其它服务的方法（插件唯一跨服务调用路径，对应技术方案 2.2）。

        等价于 ``ctx.get_service(name, version).call(method, payload)``，
        但把"取服务 + 校验 call 方法"收口在宿主，插件无需接触注册表。

        异常:
            ServiceError —— 服务不存在 / 服务对象没有 ``call`` 方法
        """
        try:
            provider = self.__on_get_service(name, version)
        except ServiceError:
            raise
        except Exception as exc:  # 统一收口成协议错误，不让插件看到宿主内部异常
            raise ServiceError(
                f"获取服务 {name}/版本 {version} 失败: {type(exc).__name__}: {exc}"
            ) from exc
        call = getattr(provider, "call", None)
        if not callable(call):
            raise ServiceError(
                f"服务 {name}/版本 {version} 的提供者不支持 call 方法"
            )
        # 由宿主注入调用方标识，供 jm.search 等服务按调用方隔离会话；
        # 不允许插件伪造（_caller 始终由宿主覆盖），且不改写调用方原字典。
        call_payload = payload
        if isinstance(payload, dict):
            call_payload = dict(payload)
            call_payload["_caller"] = f"plugin:{self.__plugin_id}"
        return call(method, call_payload)

    def config_get(self, key: str) -> object:
        """读本插件的配置项（无值时返回 schema 默认值）。"""
        return self.__on_config_get(key)

    # -- 结构化页面协议（技术方案第三节方案 A） ---------------------------

    def register_page(self, page_id: str, loader) -> None:
        """注册页面加载钩子：``loader(params: dict) -> dict``。

        参数:
            page_id: 必须在本插件 manifest 的 ``pages[]`` 里；
            loader: 收到宿主透传的 params（``plugin_page_load`` 的
                ``params_json`` 解析结果，缺省为空 dict），返回该模板的
                payload dict（字段见契约《插件结构化页面协议》一节）。

        异常:
            PageError —— 页面 id 未声明 / loader 不是可调用对象。
                setup 期抛出会让本插件加载失败，并在插件管理界面显示原因。
        """
        self.__on_register_page(page_id, loader)

    def register_page_action(self, page_id: str, action_id: str, handler) -> None:
        """注册页面动作钩子：``handler(payload: dict) -> dict``。

        handler 返回 dict 可含 ``refresh``（bool）/ ``data``（dict|null）/
        ``message``（str）；其余键被忽略。

        异常:
            PageError —— 页面未声明 / action_id 为空 / handler 不可调用。
        """
        self.__on_register_page_action(page_id, action_id, handler)

    def register_page_image(self, page_id: str, resolver) -> None:
        """注册页面图片解析器：``resolver(index: int) -> bytes | None``。

        图片走独立入口 ``plugin_page_image`` 返回原始 bytes，**不** base64
        塞进 JSON。``index`` 是插件当前选中条目内的图片下标（0 起），
        当前选中条目由插件在 ``loader`` 中自行记录。

        异常:
            PageError —— 页面未声明 / resolver 不是可调用对象。
        """
        self.__on_register_page_image(page_id, resolver)

    def log(self, message: str) -> None:
        """打日志（宿主自动加 [plugin:xxx] 前缀）。"""
        self.__on_log(message)
