"""异常检测：数据质量 + 业务异常。

规则声明在 config/dataset.yaml 的 `anomaly_rules`，本模块是规则的执行者。
每条异常都带：规则 id、严重度、定位键（哪个人）、可读说明、相关值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import DatasetConfig
from .derive import COL_EXAM_MONTH, EXAM_STATUS_PASSED, PAYMENT_PAID, parse_class_code
from .excel_reader import Dataset
from .values import display_value, to_number

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass
class Anomaly:
    rule_id: str
    severity: str
    record_key: str
    message: str
    field_name: str = ""
    value: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "record_key": self.record_key,
            "message": self.message,
            "field": self.field_name,
            "value": self.value,
        }


# --------------------------------------------------------------------------
# 个体规则
# --------------------------------------------------------------------------

def _id_number_valid(s: str) -> bool:
    """18 位身份证：前 17 位数字 + 校验位（ISO 7064:1983 MOD 11-2）。"""
    s = (s or "").strip().upper()
    if len(s) != 18 or not s[:17].isdigit():
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check_map = "10X98765432"
    total = sum(int(s[i]) * weights[i] for i in range(17))
    return check_map[total % 11] == s[17]


def _rule_duplicate_keys(snap_dict: dict[str, Any], cfg: DatasetConfig) -> list[Anomaly]:
    return [
        Anomaly("duplicate_record_key", "high", k, f"复合主键重复：同一「证件号码+姓名」出现多行（{k}）", "", "")
        for k in snap_dict.get("duplicate_keys", [])
    ]


def check_row_anomalies(ds: Dataset, cfg: DatasetConfig) -> list[Anomaly]:
    """行级（单期）异常检测。"""
    out: list[Anomaly] = []
    id_col = cfg.id_column
    code_col = (cfg.derive.class_code or {}).get("column", "开班编号")

    for row in ds.rows:
        key = f"{row.get(id_col, '')}|{row.get('姓名', '')}".strip("|")
        idv = str(row.get(id_col) or "").strip()
        if idv and not _id_number_valid(idv):
            out.append(Anomaly("invalid_id_number", "high", key,
                               f"证件号码校验位不合法：{idv}", "证件号码", idv))

        status = str(row.get("考试状态") or "").strip()
        exam_date = row.get("考试日期")
        score = to_number(row.get("分数"))
        if status == EXAM_STATUS_PASSED:
            if not exam_date:
                out.append(Anomaly("passed_but_no_exam_date", "high", key,
                                   "考试状态=已通过 但无考试日期", "考试日期", ""))
            if score is None:
                out.append(Anomaly("passed_but_no_score", "medium", key,
                                   "考试状态=已通过 但没有分数（未出分或漏录）", "分数", ""))
            elif score < 60:
                out.append(Anomaly("passed_but_low_score", "high", key,
                                   f"考试状态=已通过 但分数为 {display_value(row.get('分数'))}（<60）",
                                   "分数", display_value(row.get("分数"))))
        if score is not None and not (0 <= score <= 100):
            out.append(Anomaly("score_out_of_range", "high", key,
                               f"分数超出 [0,100]：{display_value(row.get('分数'))}",
                               "分数", display_value(row.get("分数"))))

        if str(row.get("是否付款") or "").strip() == PAYMENT_PAID:
            amount = to_number(row.get("金额"))
            if amount is None or amount == 0:
                out.append(Anomaly("paid_but_zero_amount", "medium", key,
                                   "是否付款=已付款 但金额为空或 0", "金额", display_value(row.get("金额"))))

        if code_col in ds.columns and row.get(code_col) not in (None, ""):
            if parse_class_code(row.get(code_col), cfg) is None:
                out.append(Anomaly("class_code_unparsable", "medium", key,
                                   f"开班编号无法解析：{display_value(row.get(code_col))}",
                                   code_col, display_value(row.get(code_col))))

    return out


def check_cross_period_anomalies(diff_result: Any, cfg: DatasetConfig) -> list[Anomaly]:
    """跨期异常：状态回退等。"""
    out: list[Anomaly] = []
    for rc in getattr(diff_result, "changed", []):
        for ch in rc.changes:
            if ch.field == "考试状态" and ch.from_value == "已通过" and ch.to_value != "已通过":
                out.append(Anomaly(
                    "exam_status_regressed", "high", rc.key,
                    f"考试状态从「已通过」回退为「{ch.to_display or ch.to_value}」",
                    "考试状态", f"{ch.from_display}->{ch.to_display}",
                ))
    return out


def summarize(anomalies: list[Anomaly]) -> dict[str, Any]:
    by_rule: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for a in anomalies:
        by_rule[a.rule_id] = by_rule.get(a.rule_id, 0) + 1
        by_sev[a.severity] = by_sev.get(a.severity, 0) + 1
    return {
        "total": len(anomalies),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_severity": {k: by_sev[k] for k in sorted(by_sev, key=lambda k: SEV_ORDER.get(k, 9))},
        "high_severity": sum(1 for a in anomalies if a.severity == "high"),
    }
