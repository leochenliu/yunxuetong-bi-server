"""过期日期解析：从「证书有效期」原始文本推导过期日期。

为什么单独一个模块
------------------
「证书有效期」列在证书台账里有三种写法混在一起：

    "2026/10/01"              单日期 —— 就是过期日期
    "2024/10/01-2026/10/01"   区间   —— 右端是过期日期
    "2027年1月"                只有年月 —— 取该月最后一天，精度降级为「月」

再加上「日期来源」列会说明这个日期是**证面原文**（证面上写的真实到期日）还是
**按 2 年效期推算**（推算值）。两者不能混为一谈：推算值能回答「规模有多大」，
但续证提醒必须回到证面核对。

三条必须守住的规则
------------------
1. **区间必须成对合法**。实测 256 行形如 `2026/04/17-2028/04/31`——4 月 31 日
   根本不存在。若只按"取最后一个能解析的日期"，会把**左端的开始日期**当成过期
   日期，算出"有效期 0 天"这种假数据。所以右端非法 → 整段不可解析 → 留空并报异常。
2. **不猜替代值**。4 月 31 日不自动改成 4 月 30 日：那是推测，不是事实。
3. **精度要区分**。只有年月时按月末取值，精度标记为「月」，下游必须知道这是估计值。

解析结果带**形态标签**（单日期 / 区间 / 年月 / 无法解析），让质量看板能直接
回答「这批数据的有效期字段到底有多乱」。
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any

# 形态标签
SHAPE_SINGLE = "单日期"
SHAPE_RANGE = "区间"
SHAPE_MONTH = "年月"
SHAPE_NONE = "无法解析"

# 精度
PRECISION_DAY = "日"
PRECISION_MONTH = "月"

# 日期片段：完整日期（斜杠/点/横线/中文）优先，其次中文年月
_TOKEN_RE = re.compile(
    r"\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}\s*日?"      # 2026/10/01
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?"             # 2026年10月1日
    r"|\d{4}\s*年\s*\d{1,2}\s*月"                             # 2027年1月
)

_FULL_DATE_RE = re.compile(r"^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*日?$")
_CN_FULL_DATE_RE = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$")
_CN_YEAR_MONTH_RE = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月$")


@dataclass(frozen=True)
class ExpiryInfo:
    """过期日期解析结果。"""

    date: _dt.date | None = None
    precision: str = ""        # "日" / "月" / ""
    shape: str = SHAPE_NONE    # 单日期 / 区间 / 年月 / 无法解析
    raw: str = ""
    start: _dt.date | None = None   # 区间左端（仅区间形态）
    error: str = ""                 # 失败原因（供异常信息定位）

    @property
    def ok(self) -> bool:
        return self.date is not None

    @property
    def is_estimated(self) -> bool:
        """精度不是「日」即为估计值（按月末取值）。"""
        return self.date is not None and self.precision != PRECISION_DAY


def last_day_of_month(year: int, month: int) -> _dt.date | None:
    """某年某月的最后一天；月份非法返回 None。"""
    if not 1 <= month <= 12:
        return None
    return _dt.date(year, month, calendar.monthrange(year, month)[1])


def _parse_token(token: str) -> tuple[_dt.date | None, str]:
    """把一个日期片段解析成 (日期, 精度)；不合法返回 (None, "")。"""
    text = token.strip()
    match = _FULL_DATE_RE.match(text) or _CN_FULL_DATE_RE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return _dt.date(year, month, day), PRECISION_DAY
        except ValueError:
            return None, ""
    match = _CN_YEAR_MONTH_RE.match(text)
    if match:
        year, month = (int(part) for part in match.groups())
        last = last_day_of_month(year, month)
        if last is not None:
            return last, PRECISION_MONTH
    return None, ""


def parse_expiry(raw: Any) -> ExpiryInfo:
    """解析「证书有效期」原始文本。

    返回 ``ExpiryInfo(date, precision, shape, raw, start, error)``：

    - ``"2026/10/01"``            -> ``(2026-10-01, 日, 单日期)``
    - ``"2024/10/01-2026/10/01"`` -> ``(2026-10-01, 日, 区间)``
    - ``"2027年1月"``              -> ``(2027-01-31, 月, 年月)``
    - ``"2026/04/17-2028/04/31"`` -> 不可解析（右端 4 月 31 日不存在）
    - 空 / 无法识别                -> 不可解析
    """
    if raw is None:
        return ExpiryInfo(None, "", SHAPE_NONE, "")
    if isinstance(raw, _dt.datetime):
        return ExpiryInfo(raw.date(), PRECISION_DAY, SHAPE_SINGLE, raw.date().isoformat())
    if isinstance(raw, _dt.date):
        return ExpiryInfo(raw, PRECISION_DAY, SHAPE_SINGLE, raw.isoformat())

    text = str(raw).strip()
    if not text:
        return ExpiryInfo(None, "", SHAPE_NONE, "")

    tokens = [m.group(0) for m in _TOKEN_RE.finditer(text)]
    # 片段之间的内容必须只是分隔符（"- ~ 至 到" 之类），否则形态不明
    if not tokens:
        return ExpiryInfo(None, "", SHAPE_NONE, text, error="未识别到日期片段")
    if len(tokens) > 2:
        return ExpiryInfo(None, "", SHAPE_NONE, text, error=f"出现 {len(tokens)} 个日期片段，形态不明")

    if len(tokens) == 1:
        value, precision = _parse_token(tokens[0])
        if value is None:
            return ExpiryInfo(None, "", SHAPE_NONE, text, error=f"日期片段不合法：{tokens[0]}")
        shape = SHAPE_MONTH if precision == PRECISION_MONTH else SHAPE_SINGLE
        return ExpiryInfo(value, precision, shape, text)

    # 区间：两端都必须合法，**不允许只取一端**
    left, _ = _parse_token(tokens[0])
    if left is None:
        return ExpiryInfo(None, "", SHAPE_NONE, text, error=f"区间起始日期不合法：{tokens[0]}")
    right, right_precision = _parse_token(tokens[1])
    if right is None:
        return ExpiryInfo(None, "", SHAPE_NONE, text, error=f"区间到期日期不合法：{tokens[1]}")
    if right < left:
        return ExpiryInfo(
            None, "", SHAPE_NONE, text,
            error=f"区间倒挂：到期 {right.isoformat()} 早于生效 {left.isoformat()}",
        )
    # 精度以**右端**（到期端）为准
    return ExpiryInfo(right, right_precision, SHAPE_RANGE, text, start=left)
