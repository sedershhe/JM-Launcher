"""下载产物命名（功能计划书 4.1 / 11.6、技术方案第七节的等价行为锚点）。

三条固定规则：

1. 产物名 ``{标题}-JM{编号}.zip``；标题为空（或清洗后为空）时用
   ``未知标题-{编号}.zip``（两个分支都不带多余的空白/点）；
2. 标题里剥除 Windows 非法字符 ``\\ / * ? : " < > |``，并去掉首尾空白与点；
3. 同名不覆盖，按 Windows 资源管理器"同名副本"命名法递增：
   ``xxx.zip`` → ``xxx - 副本.zip`` → ``xxx - 副本 (2).zip`` → ``xxx - 副本 (3).zip`` …

``reserve_archive_path`` 在模块级锁内完成"选名 + 独占创建"，并发下载时
两个同标题本子不会拿到同一个路径（对应决策 #1：不得静默覆盖）。
"""

from __future__ import annotations

import os
import threading

#: Windows 文件名禁用字符（计划书 11.6 逐字锁定）
INVALID_FILENAME_CHARS = '\\/*?:"<>|'

#: 标题缺失时的占位标题
UNKNOWN_TITLE = "未知标题"

#: 副本命名法的固定中缀
COPY_SUFFIX = " - 副本"

#: 选名 + 占位的模块级锁（并发安全，对应旧缺陷 #10 的显式加锁纪律）
_RESERVE_LOCK = threading.RLock()


def sanitize_filename_part(text: object) -> str:
    """清洗一个文件名片段：剥非法字符，再去掉首尾空白与点。

    参数:
        text: 任意对象，内部转成字符串处理；None 视为空串。

    返回:
        清洗后的字符串（可能为空串）。
    """
    if text is None:
        return ""
    value = str(text)
    for char in INVALID_FILENAME_CHARS:
        value = value.replace(char, "")
    # Windows 会静默吃掉文件名结尾的空格与点，这里主动去掉首尾空白/点
    return value.strip(" \t\r\n\u3000.")


def build_archive_filename(title: object, book_id: object) -> str:
    """生成 ``{标题}-JM{编号}.zip``（标题缺失回退 ``未知标题-{编号}.zip``）。

    参数:
        title: 本子标题（可为 None / 空串 / 全是非法字符）。
        book_id: 本子编号（转字符串并去首尾空白）。

    返回:
        只含文件名的字符串（不含目录），调用方再与下载目录拼接。
    """
    safe_id = sanitize_filename_part(book_id)
    if not safe_id:
        safe_id = "未知编号"
    safe_title = sanitize_filename_part(title)
    if not safe_title:
        return f"{UNKNOWN_TITLE}-{safe_id}.zip"
    return f"{safe_title}-JM{safe_id}.zip"


def unique_archive_path(directory: str, filename: str) -> str:
    """返回目录下第一个不存在的产物路径（不创建文件）。

    同名递增规则（Windows 资源管理器）：

    - ``xxx.zip`` 存在 → ``xxx - 副本.zip``
    - 还存在 → ``xxx - 副本 (2).zip``、``xxx - 副本 (3).zip`` …
    """
    directory = os.fspath(directory)
    stem, ext = os.path.splitext(os.fspath(filename))
    candidate = os.path.join(directory, filename)
    if not os.path.exists(candidate):
        return candidate

    candidate = os.path.join(directory, f"{stem}{COPY_SUFFIX}{ext}")
    if not os.path.exists(candidate):
        return candidate

    index = 2
    while True:
        candidate = os.path.join(directory, f"{stem}{COPY_SUFFIX} ({index}){ext}")
        if not os.path.exists(candidate):
            return candidate
        index += 1


def reserve_archive_path(directory: str, filename: str) -> str:
    """在目录内"选一个不重名的路径并立刻独占创建空文件"，返回该路径。

    占用与选名在同一把锁内完成，因此同一进程内并发调用不会撞名；
    调用方负责后续写入内容（ZIP），写入失败时必须自行删除占位文件。
    """
    directory = os.fspath(directory)
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"下载目录不存在: {directory}")
    with _RESERVE_LOCK:
        while True:
            path = unique_archive_path(directory, filename)
            try:
                # 'x' 模式：已存在即失败，避免"检查后、创建前"被外部进程抢占
                with open(path, "x", encoding="utf-8"):
                    pass
                return path
            except FileExistsError:
                # 极端情况下被外部进程抢先创建：重新选名，绝不覆盖
                continue
