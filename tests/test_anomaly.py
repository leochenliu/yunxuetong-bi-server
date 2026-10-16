"""异常检测器测试：真实数据 + 注入式自检。"""

from __future__ import annotations

import copy

from bi.anomaly import check_cross_period_anomalies, check_row_anomalies, summarize
from bi.diff_engine import diff_dataset_against_snapshot


def test_real_data_passed_but_no_score(batches, cfg):
    """真实数据里 3 人「已通过」但没有分数——必须被检出。"""
    d1, _ = batches
    found = check_row_anomalies(d1.dataset, cfg)
    ids = {a.rule_id for a in found}
    assert "passed_but_no_score" in ids
    assert sum(1 for a in found if a.rule_id == "passed_but_no_score") == 3


def test_detector_catches_injected_bad_rows(batches, cfg):
    """注入 6 类坏数据 -> 必须全部检出（防止检测器「永远返回 0」）。"""
    d1, _ = batches
    bad = copy.deepcopy(d1.dataset)
    bad.rows[0]["分数"] = 999
    bad.rows[1]["考试状态"] = "已通过"; bad.rows[1]["分数"] = 30
    bad.rows[2]["考试状态"] = "已通过"; bad.rows[2]["考试日期"] = None
    bad.rows[3]["证件号码"] = "12345"
    bad.rows[4]["是否付款"] = "已付款"; bad.rows[4]["金额"] = 0
    bad.rows[5]["开班编号"] = "9999999999999"

    found = check_row_anomalies(bad, cfg)
    got = {a.rule_id for a in found}
    for rule in ["score_out_of_range", "passed_but_low_score", "passed_but_no_exam_date",
                 "invalid_id_number", "paid_but_zero_amount", "class_code_unparsable"]:
        assert rule in got, f"未检出 {rule}，实际检出 {sorted(got)}"


def test_cross_period_regression_detected(batches, cfg):
    """状态从「已通过」回退 -> 必须产出跨期异常。"""
    d1, d2 = batches
    r = diff_dataset_against_snapshot(d1.snapshot, d2.dataset, cfg, "20261012", "20261013")
    cross = check_cross_period_anomalies(r, cfg)
    assert any(a.rule_id == "exam_status_regressed" for a in cross)


def test_severity_summary(batches, cfg):
    d1, _ = batches
    s = summarize(check_row_anomalies(d1.dataset, cfg))
    assert s["total"] == 3
    assert s["high_severity"] == 0
    assert s["by_severity"].get("medium") == 3
