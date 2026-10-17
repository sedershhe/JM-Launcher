"""编号解析单元测试（计划书 11.6：7 类分隔符 + 空/非法 + 去重保序）。

全部为纯函数测试，不联网。
"""

from __future__ import annotations

import pytest

from jml.core.book_ids import normalize_ids, parse_book_ids
from jml.core.errors import InvalidArgumentError


@pytest.mark.parametrize(
    "separator",
    ["-", ",", "，", "、", ";", "；", " ", "\t", "\n", "\u3000"],
    ids=["hyphen", "comma", "fullwidth_comma", "dunhao", "semicolon", "fullwidth_semicolon",
         "space", "tab", "newline", "fullwidth_space"],
)
def test_each_separator(separator):
    """7 类分隔符 + 空白：每个都能把两个编号切开。"""
    assert parse_book_ids(f"123{separator}456") == ["123", "456"]


def test_mixed_separators():
    """混合分隔符 + 首尾空白：全部切开且保序。"""
    text = " 123 - 456,789，101、202;303；404 505\t606\n707 "
    assert parse_book_ids(text) == [
        "123", "456", "789", "101", "202", "303", "404", "505", "606", "707",
    ]


def test_repeated_separators_collapse():
    """连续分隔符视作一个（不产生空 token）。"""
    assert parse_book_ids("1--2,,3；；4   5") == ["1", "2", "3", "4", "5"]


def test_deduplicate_keeps_first_order():
    """去重保序：重复编号只保留第一次出现的位置。"""
    assert parse_book_ids("3-1-3,2,1") == ["3", "1", "2"]


def test_leading_zero_is_valid_and_kept():
    """前导零按合法数字处理，原样保留（不转 int）。"""
    assert parse_book_ids("007-08-000") == ["007", "08", "000"]


@pytest.mark.parametrize(
    "text",
    ["", "   ", "\t\n", "abc", "JM123", "12a", "a12", "1.5", "1/2", "编号", "-", "；；"],
    ids=["empty", "spaces", "whitespace", "letters", "jm_prefix", "trailing_letter",
         "leading_letter", "decimal", "slash", "chinese", "only_separator", "only_semicolon"],
)
def test_no_valid_number_returns_empty(text):
    """空串 / 无有效数字 → 空列表。"""
    assert parse_book_ids(text) == []


@pytest.mark.parametrize("value", [None, 123, 1.5, [], {}, object()])
def test_non_string_returns_empty(value):
    """非字符串输入安全返回空列表（不抛异常）。"""
    assert parse_book_ids(value) == []


def test_normalize_ids_accepts_text_and_list():
    assert normalize_ids("1-2 2,3") == ["1", "2", "3"]
    assert normalize_ids([1, "002", "002"]) == ["1", "002"]


@pytest.mark.parametrize(
    "bad",
    ["abc", "1-abc", ["abc"], ["1", "x"], ["1.5"], [""]],
    ids=["text", "text_mixed", "list", "list_mixed", "decimal", "empty_item"],
)
def test_normalize_ids_rejects_non_digit(bad):
    with pytest.raises(InvalidArgumentError):
        normalize_ids(bad)


def test_normalize_ids_rejects_non_container():
    with pytest.raises(InvalidArgumentError):
        normalize_ids(object())
