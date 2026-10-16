"""归一化与解析的边界测试——整个差分正确性的地基。

若这里错了，上面所有层都会产生「假差异」。
"""

from __future__ import annotations

import datetime as _dt

from bi.derive import parse_class_code
from bi.values import canon_key, canon_value, fmt_date, to_date, to_number


def test_numeric_canonical():
    r = {"numeric_canonical": True, "strip_whitespace": True, "case_sensitive": False}
    assert canon_value(80, r) == "80"
    assert canon_value(80.0, r) == "80"
    assert canon_value("80", r) == "80"
    assert canon_value("80.0", r) == "80"
    assert canon_value(" 80 ", r) == "80"
    # 不同值不能被压成同一个
    assert canon_value("80.5", r) == "80.5"
    assert canon_value(79, r) != canon_value(80, r)


def test_dates_normalized_to_same_form():
    r = {"numeric_canonical": True}
    assert canon_value(_dt.datetime(2026, 9, 7), r) == "2026-09-07"
    assert canon_value(_dt.date(2026, 9, 7), r) == "2026-09-07"
    assert canon_value("2026-09-07 00:00:00", r) == "2026-09-07"
    assert canon_value("2026/9/7", r) == "2026-09-07"
    assert canon_value("2026年9月7日", r) == "2026-09-07"


def test_empty_tokens_collapse_to_empty():
    r = {"empty_tokens": ["", "-", "/", "N/A", "null", "None", "nan"], "strip_whitespace": True}
    for v in [None, "", "  ", "-", "/", "N/A", "null", "None", "nan"]:
        assert canon_value(v, r) == "", v


def test_id_number_never_numeric_canonicalized():
    """18 位证件号绝不能被当成数字处理——否则精度丢失会让不同人撞成同一个键。"""
    id1 = "320826197306181638"
    id2 = "320826197306181639"
    assert canon_key(id1) == id1
    assert canon_key(int(id1)) == id1
    assert canon_key(id1) != canon_key(id2)
    # 值归一化路径也不应破坏证件号（长度 > 15 时不走数值规范化）
    assert canon_value(id1, {"numeric_canonical": True}) == id1


def test_to_number_loose():
    assert to_number("1,234") == 1234
    assert to_number("￥56") == 56
    assert to_number("12%") == 0.12
    assert to_number("-") is None
    assert to_number(None) is None
    assert to_number("") is None
    assert to_number(True) == 1.0


def test_to_date_loose():
    assert fmt_date(to_date("2026年1月5日")) == "2026-01-05"
    assert fmt_date(to_date("2026-01-05")) == "2026-01-05"
    assert fmt_date(to_date(_dt.datetime(2026, 1, 5))) == "2026-01-05"
    assert to_date("-") is None
    assert to_date(None) is None


def test_parse_class_code(cfg):
    # 0706782 + 6(2026) + 0511 + 5  -> 2026-05-11 第 5 班
    p = parse_class_code("0706782605115", cfg)
    assert (p.year, p.month, p.day, p.seq) == (2026, 5, 11, 5)
    # 0706782 + 5(2025) + 0914 + 1  -> 2025-09-14 第 1 班
    p = parse_class_code("0706782509141", cfg)
    assert (p.year, p.month, p.day, p.seq) == (2025, 9, 14, 1)
    # Excel 存成数字丢了前导 0，仍应能解析
    p = parse_class_code(706782605115, cfg)
    assert p is not None and p.month == 5
    # 浮点形态
    p = parse_class_code(706782605115.0, cfg)
    assert p is not None and p.day == 11
    # 非法输入
    assert parse_class_code(None, cfg) is None
    assert parse_class_code("", cfg) is None
    assert parse_class_code("9999999999999", cfg) is None
    assert parse_class_code("abc", cfg) is None
