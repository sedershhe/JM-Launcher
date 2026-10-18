"""jml 包初始化：JML Python 后端业务包。

分层（见 docs/20261016_JML技术实现方案_v1.0.md 第五节）：
- core/    常量、协议定义、异常
- host/    插件宿主（扫描/校验/拓扑/服务注册表/事件总线/配置契约）
- services/ 主程序注册的对外服务（jm.search / jm.detail / jm.download / jm.account）
"""

from jml.core import protocol

__all__ = ["protocol"]
