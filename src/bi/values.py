"""值归一化：Excel 单元格 -> 可稳定比较的规范字符串。

为什么必须归一化
----------------
同一业务含义在 Excel 里有多种形态：
  - 分数：数字 80 / 文本 "80" / 浮点 80.0
  - 日期：datetime 对象 / "2026-09-07 00:00:00" / "2026/9/7" / Excel 序列号 46000
  - 空：None / "" / "-" / "N/A"
不归一化就会产生大量「假差异」——看起来每天都变，实际什么都没变。

**关键约束**：证件号码是 18 位数字，绝不能走浮点转换（int64 以上的精度会丢），
因此数值规范化只在字符串长度 <= 15 时启用。
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_EMPTY_TOKENS = {"", "-", "/", "N/A", "null", "None", "nan", "NaN", "无"}

_NUM_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
# 日期样字符串：2026-09-07 / 2026/9/7 / 2026.9.7 / 2026年9月7日
_DATE_LIKE_RE = re.compile(r"^\d{4}(?:[-/.]\d{1,2}[-/.]\d{1,2}|年\d{1,2}月\d{1,2}日)")
# 安全数值规范化的最大字符长度：18 位证件号不会被误判为数字
_SAFE_NUMERIC_LEN = 15


def is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def to_number(v: Any) -> float | None:
    """宽松取数：支持 1234 / "1,234" / "￥56" / "12%" / " 3 "。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, _dt.datetime | _dt.date):
        return None
    s = str(v).strip()
    if s in DEFAULT_EMPTY_TOKENS:
        return None
    pct = s.endswith("%")
    cleaned = re.sub(r"[,，\s¥￥$]", "", s)
    cleaned = cleaned.rstrip("%")
    try:
        n = float(cleaned)
    except ValueError:
        return None
    return n / 100 if pct else n


def to_date(v: Any) -> _dt.date | None:
    """宽松取日期：datetime / date / Excel 序列号 / ISO / 中文格式。"""
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        # Excel 日期序列号，1900 基准（25569 = 1970-01-01）
        return _dt.date(1970, 1, 1) + _dt.timedelta(days=int(round(v - 25569)))
    s = str(v).strip()
    if s in DEFAULT_EMPTY_TOKENS:
        return None
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def fmt_date(d: _dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def month_key(d: _dt.date) -> str:
    """形如 2026-08，用于按月聚合。"""
    return f"{d.year:04d}-{d.month:02d}"


def _canon_number_str(s: str) -> str:
    """把数字样字符串规范化："080"->"80"，"80.0"->"80"，"1.50"->"1.5"。"""
    if len(s) > _SAFE_NUMERIC_LEN or not _NUM_RE.match(s):
        return s
    try:
        d = Decimal(s)
    except InvalidOperation:
        return s
    return str(int(d)) if d == d.to_integral_value() else str(d.normalize())


def canon_key(v: Any, separator_unused: str = "|") -> str:
    """主键字段的规范化。

    刻意**不做数值规范化**：证件号码是 18 位数字串，转 float 会丢精度；
    姓名也不能做任何大小写/空格之外的改动（同姓名的两个人必须保持可区分）。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (_dt.datetime, _dt.date)):
        return fmt_date(v.date() if isinstance(v, _dt.datetime) else v)
    s = str(v)
    return s.strip()


def canon_value(v: Any, rules: dict[str, Any] | None = None) -> str:
    """追踪字段的规范化（比较用）。"""
    rules = rules or {}
    strip_ws = rules.get("strip_whitespace", True)
    case_sensitive = rules.get("case_sensitive", False)
    empty_tokens = set(rules.get("empty_tokens", DEFAULT_EMPTY_TOKENS) or [])
    numeric_canonical = rules.get("numeric_canonical", True)

    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, _dt.datetime):
        return fmt_date(v.date())
    if isinstance(v, _dt.date):
        return fmt_date(v)
    if isinstance(v, (int, float)):
        d = Decimal(str(v))
        return str(int(d)) if d == d.to_integral_value() else str(d.normalize())

    s = str(v)
    if strip_ws:
        s = s.strip()
    if s in empty_tokens:
        return ""
    # 日期样字符串统一成 YYYY-MM-DD
    if _DATE_LIKE_RE.match(s):
        dt = to_date(s)
        if dt is not None:
            return fmt_date(dt)
    if numeric_canonical:
        s = _canon_number_str(s)
    if not case_sensitive:
        s = s.lower()
    return s


def display_value(v: Any) -> str:
    """人类可读的展示值（证据追溯时回显原始值用）。"""
    if v is None:
        return ""
    if isinstance(v, _dt.datetime):
        return fmt_date(v.date())
    if isinstance(v, _dt.date):
        return fmt_date(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
