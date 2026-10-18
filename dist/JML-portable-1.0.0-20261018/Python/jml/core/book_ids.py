"""批量编号解析（对应功能计划书 4.1 / 11.6 的等价行为）。

规则（逐条锁定）：

- 分隔符：``-``、``,``、``，``、``、``、``;``、``；``，以及任意空白（含全角空格）；
- 只收**纯数字** token（``str.isdigit()``），``JM123`` / ``123a`` / ``-`` 一律丢弃；
- 含前导零的纯数字按合法编号处理，**原样保留**（``"007"`` → ``"007"``）；
- 去重保序：同一编号保留第一次出现的顺序；
- 空串 / None / 非字符串 / 无有效数字 → 空列表（不抛异常）。

本模块是纯函数，无状态、无 IO。
"""

from __future__ import annotations

import re

from jml.core.errors import InvalidArgumentError

#: 允许的分隔符（``- , ， 、 ; ；`` + 任意空白）
DELIMITERS = ("-", ",", "，", "、", ";", "；")

#: 实际使用的切分正则：分隔符连续出现视作一个分隔
_SPLIT_PATTERN = re.compile(r"[-,\uFF0C\u3001;\uFF1B\s]+")


def parse_book_ids(text: object) -> list[str]:
    """把用户输入的一串编号解析成去重保序的编号列表。

    参数:
        text: 用户输入文本（任何类型都安全，非字符串返回空列表）。

    返回:
        纯数字编号字符串列表；没有有效编号时返回 ``[]``。

    示例::

        parse_book_ids("123-456,789，101、202;303；404 505")
        # -> ["123", "456", "789", "101", "202", "303", "404", "505"]
        parse_book_ids("abc JM999 12a")  # -> []
    """
    if not isinstance(text, str):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for token in _SPLIT_PATTERN.split(text):
        if not token or not token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def normalize_ids(values: object, digits_only: bool = True) -> list[str]:
    """严格规范化编号输入（下载/服务入口的唯一实现）。

    与 ``parse_book_ids`` 的区别：本函数**不容忍非法 token**——
    ``digits_only=True`` 时任何非纯数字项都抛 ``InvalidArgumentError``，
    绝不把 "abc" 之类继续送进真实网络调用。

    参数:
        values: 编号文本（含分隔符）或 list/tuple（JSON 数组解析后结果）。
        digits_only: 是否只允许纯数字；默认 True。

    返回:
        去重保序的字符串列表（可能为空；空列表由调用方判为参数错误）。
    """
    if values is None:
        raise InvalidArgumentError("编号列表不能为空")

    if isinstance(values, str):
        tokens = [token for token in _SPLIT_PATTERN.split(values) if token]
    elif isinstance(values, (list, tuple)):
        tokens = [
            str(item).strip() if item is not None else ""
            for item in values
        ]
    else:
        raise InvalidArgumentError(
            f"编号列表类型不合法: {type(values).__name__}"
        )

    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if not token:
            raise InvalidArgumentError("编号不能为空")
        if digits_only and not token.isdigit():
            raise InvalidArgumentError(f"编号必须是纯数字: {token!r}")
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result
