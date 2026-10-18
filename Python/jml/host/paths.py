"""数据目录解析（唯一真源，Python 侧不各自读环境变量）。

背景：C# 启动时按三级顺序实测选择数据根（%APPDATA%/%LOCALAPPDATA% →
系统临时目录\\JML → 程序目录 runtime_data），然后通过
``jml_backend.set_data_root(config_root, plugin_data_root, log_dir)`` 注入本模块。
之后插件配置、disabled.json、插件数据目录与日志都只从本模块取路径，
不再各自读 ``os.environ["APPDATA"]``。

默认值保持与旧实现一致（兼容测试与单独运行 Python 的场景）：
- ``config_root()``       → ``%APPDATA%/JML``（APPDATA 缺失时回退用户主目录）
- ``plugin_data_root()``  → ``<config_root>/plugins``
- ``log_dir()``           → 未配置时为 ``None``（不落盘，沿用 diagnostics 旧语义）

线程安全：显式 ``threading.RLock``。模块级只保存“被注入的覆盖值”，
默认值在每次读取时按当前环境计算，因此测试里 monkeypatch APPDATA 仍生效。
"""

from __future__ import annotations

import os
import threading
import uuid

_LOCK = threading.RLock()
_config_root: str | None = None
_plugin_data_root: str | None = None
_log_dir: str | None = None


def _normalize(path: str | os.PathLike | None) -> str | None:
    """空值 → None；其余转绝对路径（不创建目录）。"""
    if path is None:
        return None
    text = os.fspath(path).strip()
    if not text:
        return None
    return os.path.abspath(text)


def configure(
    config_root: str | os.PathLike | None = None,
    plugin_data_root: str | os.PathLike | None = None,
    log_dir: str | os.PathLike | None = None,
) -> None:
    """整体替换三个根（未传/空值表示回到默认值）。

    由 ``jml_backend.set_data_root`` 在校验可写后调用；测试也可直接调用。
    ``configure()`` 无参数等价于 :func:`reset`。
    """
    global _config_root, _plugin_data_root, _log_dir
    with _LOCK:
        _config_root = _normalize(config_root)
        _plugin_data_root = _normalize(plugin_data_root)
        _log_dir = _normalize(log_dir)


def reset() -> None:
    """清空注入值，恢复“按环境变量推导”的默认行为（测试隔离用）。"""
    configure(None, None, None)


def config_root() -> str:
    """配置/状态根目录：注入值优先，否则 ``%APPDATA%/JML``。"""
    with _LOCK:
        if _config_root is not None:
            return _config_root
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JML")


def plugin_data_root() -> str:
    """插件配置与插件数据根：注入值优先，否则 ``<config_root>/plugins``。"""
    with _LOCK:
        if _plugin_data_root is not None:
            return _plugin_data_root
    return os.path.join(config_root(), "plugins")


def log_dir() -> str | None:
    """诊断日志目录：未注入时返回 ``None``（表示不落盘）。"""
    with _LOCK:
        return _log_dir


def probe_writable(directory: str | os.PathLike | None) -> bool:
    """实测目录可写：创建目录 → 写临时文件 → 删除。

    与 C# ``AppPaths.ProbeWritable`` 同一判定口径；不允许只看
    ``os.path.isdir``。任何一步失败返回 False，不抛异常、尽力清理探针文件。
    """
    normalized = _normalize(directory)
    if not normalized:
        return False

    probe = os.path.join(normalized, f".jml-write-probe-{uuid.uuid4().hex}.tmp")
    try:
        os.makedirs(normalized, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("J")
            handle.flush()
        os.unlink(probe)
        return True
    except OSError:
        try:
            os.unlink(probe)
        except OSError:
            pass
        return False
