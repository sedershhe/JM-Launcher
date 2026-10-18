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
    return bool(probe_writable_detailed(directory)["writable"])


def probe_writable_detailed(directory: str | os.PathLike | None) -> dict:
    """``probe_writable`` 的诊断版本：返回具体探针文件名 / 完整路径 / 失败明细。

    返回字段（全部是普通可 JSON 序列化的值）::

        {"writable": bool, "directory": str, "probe_file": str,
         "probe_path": str, "stage": str, "error_type": str,
         "errno": int | None, "winerror": int | None, "message": str}

    失败时 ``stage`` 是“创建目录 / 写探针文件 / 删除探针文件”，
    ``error_type`` 为原始异常类型名；``writable`` 的判定与旧实现完全一致。
    """
    probe_name = f".jml-write-probe-{uuid.uuid4().hex}.tmp"
    result: dict = {
        "writable": False,
        "directory": "",
        "probe_file": probe_name,
        "probe_path": "",
        "stage": "",
        "error_type": "",
        "errno": None,
        "winerror": None,
        "message": "",
    }

    normalized = _normalize(directory)
    if not normalized:
        result.update(stage="路径为空", message="目录路径为空或空白")
        return result

    result["directory"] = normalized
    result["probe_path"] = os.path.join(normalized, probe_name)

    def fail(stage: str, exc: OSError) -> dict:
        result.update(
            stage=stage,
            error_type=type(exc).__name__,
            errno=getattr(exc, "errno", None),
            winerror=getattr(exc, "winerror", None),
            message=str(exc),
        )
        return result

    try:
        os.makedirs(normalized, exist_ok=True)
    except OSError as exc:
        return fail("创建目录", exc)

    try:
        with open(result["probe_path"], "x", encoding="utf-8") as handle:
            handle.write("J")
            handle.flush()
    except OSError as exc:
        try:
            os.unlink(result["probe_path"])
        except OSError:
            pass
        return fail("写探针文件", exc)

    try:
        os.unlink(result["probe_path"])
    except OSError as exc:
        # 与旧判定一致：删除失败也算不可写（可能被杀软占用）。
        return fail("删除探针文件", exc)

    result["writable"] = True
    return result
