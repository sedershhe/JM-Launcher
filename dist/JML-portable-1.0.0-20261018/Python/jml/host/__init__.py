"""host 包：插件宿主（扫描/校验/拓扑/服务注册表/配置契约）。

刻意不做 eager import：``import jml.core.downloader`` 等 core 路径不应顺带
拉起 loader/config/manifest。需要宿主子模块时由调用方显式
``from jml.host import loader`` / ``import jml.host.loader``。
"""

from __future__ import annotations

__all__: list[str] = []
