"""派生列：补出原表没有的字段，并做口径过滤。

为什么需要
----------
原表**没有「报名日期」列**，可用的时间列只有考试日期 / 培训日期，都回答不了
「每月报了多少人」。报名 / 编班时间实际编码在「开班编号」里：

    0706782 + 年份位(5=2025, 6=2026) + MMDD(4) + 班序号(1)   共 13 位
    例 0706782605115 -> 2026-05-11 第 5 班
    例 0706782509141 -> 2025-09-14 第 1 班

该解读经一致性校验：报名月份从未晚于考试月份（违例 0 条）。

口径过滤
--------
只保留有证件号码的行（约占 2.4% 的行被剔除）。剔除数量写入 `ds.notes`，
保证「分母变小」永远有痕迹，不静默减少。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

from .cleaning import clean_rows
from .config import DatasetConfig
from .excel_reader import Dataset
from .expiry import parse_expiry
from .values import fmt_date, month_key, to_date

# 派生列名（固定契约，供指标 / 差分 / API 引用）
COL_ENROLL_DATE = "报名日期"
COL_ENROLL_MONTH = "报名月份"
COL_CLASS_DATE = "开班日期"
COL_CLASS_SEQ = "班序号"
COL_HEADCOUNT = "人数"
COL_EXAM_SCHEDULED = "已考试人数"
COL_EXAM_UNSCHEDULED = "未考试人数"
COL_PASSED = "已通过人数"
COL_EXAM_MONTH = "考试月份"
COL_PAID = "已付款人数"
COL_DEFERRED_CERT = "延期换证人数"

# 证书类目的派生列（过期日期是核心口径）
COL_CERT_START_DATE = "开始日期"
COL_CERT_EXPIRY_DATE = "过期日期"
COL_CERT_EXPIRY_PRECISION = "过期日期精度"
COL_CERT_VALIDITY_DAYS = "有效期天数"
COL_CERT_EXPIRY_MONTH = "过期月份"
COL_CERT_EXPIRY_SHAPE = "有效期形态"

DERIVED_COLUMNS = [
    COL_ENROLL_DATE,
    COL_ENROLL_MONTH,
    COL_CLASS_DATE,
    COL_CLASS_SEQ,
    COL_HEADCOUNT,
    COL_EXAM_SCHEDULED,
    COL_EXAM_UNSCHEDULED,
    COL_PASSED,
    COL_EXAM_MONTH,
    COL_PAID,
    COL_DEFERRED_CERT,
]

# 业务枚举（与 Excel 中的实际取值一致）
EXAM_STATUS_PASSED = "已通过"
PAYMENT_PAID = "已付款"
CERT_TYPE_DEFERRED = "延期换证"


@dataclass
class ParsedClass:
    year: int
    month: int
    day: int
    seq: int

    @property
    def date(self) -> _dt.date:
        return _dt.date(self.year, self.month, self.day)


def parse_class_code(raw: Any, cfg: DatasetConfig) -> ParsedClass | None:
    """解析开班编号。兼容 Excel 把编号存成数字（丢前导 0）或浮点（带 .0）。"""
    cc = cfg.derive.class_code or {}
    if not cc or raw is None or raw == "":
        return None

    if isinstance(raw, float) and raw.is_integer():
        s = str(int(raw))
    else:
        s = str(raw).strip()
        if s.endswith(".0"):
            s = s[:-2]

    length = int(cc.get("length", 13))
    prefix = str(cc.get("prefix", ""))
    s = s.zfill(length)
    if len(s) != length or not s.isdigit():
        return None
    if prefix and not s.startswith(prefix):
        return None

    yp = int(cc.get("year_pos", 7))
    mmdd_pos = int(cc.get("mmdd_pos", 8))
    seq_pos = int(cc.get("seq_pos", 12))
    year_map = {int(k): int(v) for k, v in (cc.get("year_map") or {}).items()}

    y_digit = int(s[yp])
    if year_map and y_digit not in year_map:
        return None
    year = year_map.get(y_digit, 2000 + y_digit)

    month = int(s[mmdd_pos : mmdd_pos + 2])
    day = int(s[mmdd_pos + 2 : mmdd_pos + 4])
    try:
        seq = int(s[seq_pos:])
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= seq <= 9):
        return None
    return ParsedClass(year=year, month=month, day=day, seq=seq)


def require_identity(ds: Dataset, cfg: DatasetConfig) -> int:
    """口径过滤：剔除无证件号码的行。返回被剔除的行数。幂等。"""
    if not cfg.identity.require_identity or cfg.id_column not in ds.columns:
        return 0
    before = len(ds.rows)
    id_col = cfg.id_column
    kept = []
    for r in ds.rows:
        v = r.get(id_col)
        if v is not None and str(v).strip() != "":
            kept.append(r)
    ds.rows = kept
    dropped = before - len(kept)
    if dropped > 0:
        msg = (
            f"口径：只统计有「{id_col}」的学员，已剔除 {dropped} 行"
            f"（{dropped / before * 100:.1f}%），计入 {len(kept)} 人"
        )
        ds.notes = [n for n in ds.notes if not n.startswith("口径：只统计")] + [msg]
    return dropped


def derive_expiry(ds: Dataset, cfg: DatasetConfig) -> None:
    """证书类目：从「证书有效期」派生过期日期等列（配置驱动）。

    原始列写法很杂（单日期 / 区间 / 只有年月），解析规则见 `bi.expiry`。
    解析不出就留空——**不允许拿别的东西顶替**，下游据此提示「未计入」。
    """
    rule = cfg.derive.expiry or {}
    if not rule:
        return
    column = str(rule.get("column", "证书有效期"))
    start_column = str(rule.get("start_column", "证书开始日期"))
    outputs = [str(c) for c in (rule.get("outputs") or [])]
    if not outputs or column not in ds.columns:
        return

    for r in ds.rows:
        info = parse_expiry(r.get(column))
        start = to_date(r.get(start_column))
        values = {
            COL_CERT_START_DATE: fmt_date(start) if start else None,
            COL_CERT_EXPIRY_DATE: fmt_date(info.date) if info.date else None,
            COL_CERT_EXPIRY_PRECISION: info.precision or None,
            COL_CERT_VALIDITY_DAYS: (info.date - start).days if (info.date and start) else None,
            COL_CERT_EXPIRY_MONTH: month_key(info.date) if info.date else None,
            COL_CERT_EXPIRY_SHAPE: info.shape,
        }
        for col in outputs:
            r[col] = values.get(col)

    for c in outputs:
        if c not in ds.columns:
            ds.columns.append(c)


def clean(ds: Dataset, cfg: DatasetConfig) -> None:
    """字段清洗（配置驱动）。清洗命中情况写进 `ds.notes`，不静默改数。

    清洗在口径过滤**之前**做：证件号带尾噪字符时（如 `310229199403034414‘`），
    先去噪再判空，才不会把一条有效记录当成"缺证件号"剔掉。
    """
    if not cfg.cleaning:
        return
    reports = clean_rows(ds.rows, cfg.cleaning)
    notes = [report.as_note() for report in reports if report.as_note()]
    if notes:
        ds.notes = [n for n in ds.notes if not n.startswith("清洗列")] + notes


def derive(ds: Dataset, cfg: DatasetConfig) -> Dataset:
    """就地追加派生列。幂等：已含派生列则直接返回。"""
    clean(ds, cfg)
    require_identity(ds, cfg)
    derive_expiry(ds, cfg)
    code_col = (cfg.derive.class_code or {}).get("column", "开班编号")
    if code_col not in ds.columns:
        return ds
    if COL_CLASS_DATE in ds.columns:
        return ds

    exam_date_col = "考试日期"
    exam_status_col = "考试状态"
    payment_col = "是否付款"
    cert_type_col = "取证类型"

    for r in ds.rows:
        parsed = parse_class_code(r.get(code_col), cfg)
        enroll_date = parsed.date if parsed else None
        exam_date = to_date(r.get(exam_date_col))

        r[COL_ENROLL_DATE] = enroll_date
        r[COL_ENROLL_MONTH] = month_key(enroll_date) if enroll_date else None
        r[COL_CLASS_DATE] = fmt_date(enroll_date) if enroll_date else None
        r[COL_CLASS_SEQ] = f"第{parsed.seq}班" if parsed else None
        r[COL_HEADCOUNT] = 1
        r[COL_EXAM_SCHEDULED] = 1 if exam_date else None
        r[COL_EXAM_UNSCHEDULED] = None if exam_date else 1
        r[COL_PASSED] = 1 if str(r.get(exam_status_col) or "").strip() == EXAM_STATUS_PASSED else None
        r[COL_EXAM_MONTH] = month_key(exam_date) if exam_date else None
        r[COL_PAID] = 1 if str(r.get(payment_col) or "").strip() == PAYMENT_PAID else None
        r[COL_DEFERRED_CERT] = 1 if str(r.get(cert_type_col) or "").strip() == CERT_TYPE_DEFERRED else None

    for c in DERIVED_COLUMNS:
        if c not in ds.columns:
            ds.columns.append(c)
    return ds
