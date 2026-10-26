"""自然时间周期配置（config/period.yaml，单位：天）的行为测试。

口径约定：周期 = 连续 N 天；days=7 是一周，days=30 是一个自然月。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from bi.api import app
from bi.config import PeriodConfig, load_period_config
from bi.period import group_by_period
from bi.settings import Settings

client = TestClient(app)


# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #
def test_period_config_from_project_file(settings: Settings):
    cfg = load_period_config(settings.period_config_path)
    assert cfg.unit == "day"
    assert cfg.days == 30
    assert cfg.label_text == "月"
    assert cfg.anchor == dt.date(2025, 1, 1)


def test_period_config_missing_file_falls_back_to_default(tmp_path):
    cfg = load_period_config(tmp_path / "nope.yaml")
    assert cfg.days == 30
    assert cfg.label_text == "月"


def test_period_config_rejects_non_day_unit(tmp_path):
    path = tmp_path / "period.yaml"
    path.write_text("period:\n  unit: month\n  days: 30\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unit"):
        load_period_config(path)


def test_period_config_rejects_non_positive_days(tmp_path):
    path = tmp_path / "period.yaml"
    path.write_text("period:\n  unit: day\n  days: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="days"):
        load_period_config(path)


def test_period_config_rejects_bad_anchor(tmp_path):
    path = tmp_path / "period.yaml"
    path.write_text("period:\n  unit: day\n  days: 30\n  anchor: 2025/1/1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="anchor"):
        load_period_config(path)


def test_label_is_inferred_from_days():
    assert PeriodConfig(days=7).label_text == "周"
    assert PeriodConfig(days=30).label_text == "月"
    assert PeriodConfig(days=14).label_text == "14天"
    assert PeriodConfig(days=7, label="档期").label_text == "档期"


# --------------------------------------------------------------------------- #
# 分桶边界
# --------------------------------------------------------------------------- #
def test_week_bucket_boundaries():
    """一周 = 7 天：锚点当天是第 0 期，第 7 天进入第 1 期。"""
    period = PeriodConfig(days=7, anchor=dt.date(2026, 1, 5))  # 周一
    assert period.bucket_of(dt.date(2026, 1, 5)) == "2026-01-05"
    assert period.bucket_of(dt.date(2026, 1, 11)) == "2026-01-05"
    assert period.bucket_of(dt.date(2026, 1, 12)) == "2026-01-12"
    assert period.bucket_end(dt.date(2026, 1, 12)) == dt.date(2026, 1, 18)
    assert period.window_of(dt.date(2026, 1, 12)) == "2026-01-12 ~ 2026-01-18"


def test_month_bucket_boundaries():
    """一个自然月 = 30 天：第 0 期覆盖锚点起的 30 天。"""
    period = PeriodConfig(days=30, anchor=dt.date(2025, 1, 1))
    assert period.bucket_of(dt.date(2025, 1, 1)) == "2025-01-01"
    assert period.bucket_of(dt.date(2025, 1, 30)) == "2025-01-01"
    assert period.bucket_of(dt.date(2025, 1, 31)) == "2025-01-31"
    assert period.bucket_end(dt.date(2025, 1, 31)) == dt.date(2025, 3, 1)


def test_dates_before_anchor_fall_into_negative_buckets():
    period = PeriodConfig(days=30, anchor=dt.date(2025, 1, 1))
    assert period.bucket_index(dt.date(2024, 12, 15)) == -1
    assert period.bucket_of(dt.date(2024, 12, 15)) == "2024-12-02"


# --------------------------------------------------------------------------- #
# 分组
# --------------------------------------------------------------------------- #
def test_group_by_period_counts_buckets_and_unassigned():
    period = PeriodConfig(days=7, anchor=dt.date(2026, 1, 5))
    rows = [
        {"报名日期": "2026-01-05", "考试状态": "缺考"},
        {"报名日期": "2026-01-06", "考试状态": "缺考"},
        {"报名日期": "2026-01-12", "考试状态": "合格"},
        {"报名日期": "", "考试状态": "合格"},
        {"报名日期": None, "考试状态": ""},
    ]
    buckets, unassigned = group_by_period(rows, "报名日期", period, status_field="考试状态")

    assert unassigned == 2
    assert [b["period"] for b in buckets] == ["2026-01-05", "2026-01-12"]
    assert buckets[0]["count"] == 2
    assert buckets[0]["period_end"] == "2026-01-11"
    assert buckets[0]["statuses"] == [{"label": "缺考", "count": 2}]
    assert buckets[1]["count"] == 1
    assert buckets[1]["statuses"] == [{"label": "合格", "count": 1}]


def test_group_by_period_without_status_field():
    period = PeriodConfig(days=30, anchor=dt.date(2025, 1, 1))
    buckets, unassigned = group_by_period([{"报名日期": "2026-05-11"}], "报名日期", period)
    assert unassigned == 0
    # 30 天窗口相对日历月会漂移：2026-05-11 落在 2026-04-26 ~ 2026-05-25 这一期
    assert buckets == [{"period": "2026-04-26", "period_end": "2026-05-25", "count": 1}]


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def test_dashboard_data_exposes_period_config(seeded):
    response = client.get("/dashboard/data")
    assert response.status_code == 200
    body = response.json()

    period = body["period"]
    assert period["unit"] == "day"
    assert period["days"] == 30
    assert period["label"] == "月"
    assert period["anchor"] == "2025-01-01"

    denggao = body["datasets"]["denggao"]
    buckets = denggao["enrollment_by_period"]
    assert buckets, "周期分布不应为空"
    # 周期标签必须是窗口起始日，且窗口长度 = 配置的天数
    for bucket in buckets:
        start = dt.date.fromisoformat(bucket["period"])
        end = dt.date.fromisoformat(bucket["period_end"])
        assert (end - start).days == period["days"] - 1
    # 分组总数 + 未归组 = 当前批次记录数（不允许分母悄悄变小）
    assert sum(b["count"] for b in buckets) + denggao["period_unassigned"] == denggao["record_count"]


def test_project_data_exposes_period_config(seeded):
    response = client.get("/dashboard/denggao/data")
    assert response.status_code == 200
    body = response.json()
    assert body["period"]["days"] == 30
    assert body["period"]["label"] == "月"
    assert body["enrollment_by_period"]
