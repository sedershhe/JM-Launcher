"""
JML Python 侧后端入口。

定位说明（重要）：
- 本模块是 JML 新项目自己的后端代码，供 C# 通过 pythonnet 调用。
- 严禁 import 旧项目（jm_app / jmhost_api / plugins 等）下的任何模块，
  这是"零代码耦合"纪律的硬性要求。
- 阶段 1：在此挂载插件宿主与协议（api_version=1）；阶段 2 起继续挂载
  下载/搜索/详情等业务模块。

对外接口（C# 侧调用，返回一律为纯 dict，字符串必须可 JSON 序列化）：
    health_check() -> dict                          运行时与依赖库自检（保持原样）
    version() -> str                                后端代码版本（保持原样）
    bootstrap(plugins_dir: str) -> dict             宿主引导：注册主程序服务 + 装配插件
    list_plugins(plugins_dir: str) -> dict          只扫描校验不加载（插件管理界面用）
    emit_test_event(event_type, payload) -> dict    测试：派发事件并返回投递数

幂等约定：bootstrap 可重复调用，第二次起直接返回上次结果（不重复装配）；
模块级单例状态放显式 dict 并加锁保护。
"""

from __future__ import annotations

import threading

__version__ = "0.2.0"

# ---------------------------------------------------------------------------
# 模块级单例状态（显式 dict + 锁，对应旧缺陷 #10 的显式加锁纪律）
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.RLock()

#: 宿主状态。键:
#:   bootstrapped  —— 是否已执行过 bootstrap（幂等标记）
#:   result        —— 上一次 bootstrap 的返回值（重复调用时原样返回）
#:   plugins_dir   —— 上一次 bootstrap 使用的插件目录
_STATE: dict = {"bootstrapped": False, "result": None, "plugins_dir": None}


# ---------------------------------------------------------------------------
# 自检接口（保持原样，xUnit 依赖）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 插件宿主入口（阶段 1 新增）
# ---------------------------------------------------------------------------


def bootstrap(plugins_dir: str) -> dict:
    """宿主引导：注册主程序服务 + 装配插件目录，返回纯 dict。

    返回结构（字符串均可 JSON 序列化，供 C# 直接转 DTO）:
        {
          "ok": bool,
          "loaded":  [ {"id", "name", "version", "pages": [...]} ],
          "failed":  [ {"id", "reason"} ],
          "services": {"archive_reader": 1, "jm.search": 1, ...},
        }
        pages 元素形如 {"id", "title", "template"}。

    幂等：第二次调用（任意目录）直接返回上次结果，不重复装配。
    """
    with _STATE_LOCK:
        if _STATE["bootstrapped"]:
            return _STATE["result"]

        from jml.host import loader
        from jml.services.host_services import register_host_services

        # 1. 主程序服务注册（jm.search / jm.detail / jm.download / jm.account）
        register_host_services()

        # 2. 装配插件（discover → topo → import + setup）
        try:
            report = loader.load_all(plugins_dir)
            ok = True
        except Exception as exc:
            # 整体流程级失败（如 plugins_dir 不存在 / 依赖环）：
            # 返回结构化失败，不向 C# 抛 Python 异常
            report = {
                "loaded": [],
                "failed": [{"id": "(host)", "reason": str(exc)}],
                "services": {},
            }
            ok = False

        result = {
            "ok": ok,
            "loaded": report["loaded"],
            "failed": report["failed"],
            "services": report["services"],
        }
        _STATE["bootstrapped"] = True
        _STATE["result"] = result
        _STATE["plugins_dir"] = plugins_dir
        return result


def list_plugins(plugins_dir: str) -> dict:
    """只扫描 + 校验 manifest，不导入不装配（插件管理界面"重新扫描"用）。

    返回结构与 bootstrap 同构（services 为当前注册表快照，通常为空 dict
    或上次 bootstrap 的残留——不触发任何注册动作）。
    """
    from jml.host import loader
    from jml.host.registry import REGISTRY

    try:
        manifests = loader.discover(plugins_dir)
    except Exception as exc:
        return {
            "ok": False,
            "loaded": [],
            "failed": [{"id": "(host)", "reason": str(exc)}],
            "services": {},
        }

    loaded = [
        {
            "id": m["id"],
            "name": m["name"],
            "version": m["version"],
            "pages": list(m["pages"]),
            "development_sample": m["development_sample"],
        }
        for m in manifests
    ]
    # discover 阶段的坏 manifest → failed（loader 维护的错误列表）
    failed = []
    with loader._LOAD_ERRORS_LOCK:
        for err in loader._LOAD_ERRORS:
            dir_name, _, reason = err.partition(": ")
            failed.append({"id": dir_name, "reason": reason})
    return {"ok": True, "loaded": loaded, "failed": failed, "services": REGISTRY.names()}


def emit_test_event(event_type: str, payload: dict) -> dict:
    """测试入口：向事件总线派发一个事件，返回投递到的 handler 数。

    返回:
        {"ok": bool, "delivered": int, "errors": [str, ...]}
        校验失败（未知类型/缺字段/裸 dict）时 ok=False 且 errors 含原因，
        不抛异常（便于 C# 侧直接展示）。
    """
    from jml.host.events import BUS

    try:
        delivered = BUS.dispatch(event_type, payload)
    except Exception as exc:
        return {"ok": False, "delivered": 0, "errors": [f"{type(exc).__name__}: {exc}"]}
    return {
        "ok": True,
        "delivered": delivered,
        "errors": list(BUS.last_dispatch_errors()),
    }


def reset_host_state() -> None:
    """重置宿主单例状态（仅测试与开发自检用，C# 不应调用）。

    清空 bootstrap 幂等标记、服务注册表、事件总线与 loader 内部状态。
    """
    with _STATE_LOCK:
        _STATE["bootstrapped"] = False
        _STATE["result"] = None
        _STATE["plugins_dir"] = None
    from jml.host import events, loader, registry

    registry.reset()
    events.reset()
    loader.reset()


if __name__ == "__main__":
    # 允许单独在命令行执行自检：python jml_backend.py [plugins_dir]
    import json
    import sys

    print(json.dumps(health_check(), ensure_ascii=False, indent=2))
    if len(sys.argv) > 1:
        print(json.dumps(bootstrap(sys.argv[1]), ensure_ascii=False, indent=2))
