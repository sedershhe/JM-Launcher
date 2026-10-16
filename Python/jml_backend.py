"""
JML Python 侧后端入口。

定位说明（重要）：
- 本模块是 JML 新项目自己的后端代码，供 C# 通过 pythonnet 调用。
- 严禁 import 旧项目（jm_app / jmhost_api / plugins 等）下的任何模块，
  这是"零代码耦合"纪律的硬性要求。
- 当前为环境配置阶段的骨架，仅提供能力自检接口，业务逻辑待后续设计文档确定后实现。

对外接口（C# 侧调用）：
    health_check() -> dict   返回 Python 运行时与依赖库的可用性信息
"""

from __future__ import annotations

__version__ = "0.1.0"


def health_check() -> dict:
    """返回 Python 运行时与关键依赖的可用性。

    供 C# 侧在启动时做桥接冒烟验证，不发起任何网络请求。
    """
    import platform
    import sys

    result = {
        "ok": True,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "backend_version": __version__,
        "packages": {},
        "errors": [],
    }

    # 逐个探测依赖，单个库缺失不影响其它库的探测结果
    for name in ("jmcomic", "PIL", "yaml", "curl_cffi", "Crypto"):
        try:
            module = __import__(name)
            version = getattr(module, "__version__", None)
            result["packages"][name] = version if version else "available"
        except Exception as exc:  # 只记类型与消息，不打印堆栈（避免泄漏细节）
            result["packages"][name] = None
            result["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            result["ok"] = False

    return result


def version() -> str:
    """返回后端代码版本。"""
    return __version__


if __name__ == "__main__":
    # 允许单独在命令行执行自检：python jml_backend.py
    import json

    print(json.dumps(health_check(), ensure_ascii=False, indent=2))
