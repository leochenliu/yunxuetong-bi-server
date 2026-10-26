"""分桶指标（按维度切分的人数分布）的测试。

覆盖需求：「按月度（自然月）为单位，抓取一个学生报名了 1/2/3/4 门课的总人数」。

关键口径（这些测试就是在锁它们）：
  1. 分组键 = 证件号码 + 姓名 + 年份 + 月份 —— 同一人**同一自然月**算一组；
  2. 组内按「课程」不同取值计数（多值列拆分后去重）；
  3. 落格的是**人数**（去重后的人数），不是人次；
  4. 跨月不复用计数 —— 同一人 3 月报 2 门、6 月报 1 门，两个月各计一次；
  5. 分桶指标按 dataset_id 隔离，不与其他数据集串味。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bi.api import app
from bi.config import MetricDef
from bi.excel_reader import Dataset
from bi.metrics import compute_bucket_metrics
from bi.store import FactStore
from bi.settings import get_settings

client = TestClient(app)


def _dataset(rows: list[dict]) -> Dataset:
    return Dataset(
        name="测试",
        sheet="s",
        columns=list(rows[0].keys()) if rows else [],
        rows=rows,
        source_file="test.xlsx",
    )


def _depth_metric(**overrides) -> MetricDef:
    params = dict(
        key="monthly_enroll_depth",
        name="月度报名门数分布",
        agg="count_by_group_depth",
        unit="人",
        group_columns=["证件号码", "姓名"],
        dimension_columns=["年份", "月份"],
        depth_column="课程",
        depth_max_bucket=5,
        dimension_name="月份",
    )
    params.update(overrides)
    return MetricDef(**params)


def _grid(buckets) -> dict[str, dict[str, float]]:
    """把分桶结果转成 {维度值: {分档: 人数}} 便于断言。"""
    out: dict[str, dict[str, float]] = {}
    for b in buckets:
        out.setdefault(b.bucket, {})[b.group_key] = b.value
    return out


# ============================================================ 算子口径
def test_counts_people_by_courses_in_same_month():
    """同一人同月报 N 门课 -> 计入 N 门档。"""
    ds = _dataset([
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "3月", "课程": "A"},
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "3月", "课程": "B"},
        {"证件号码": "2", "姓名": "乙", "年份": "2026年", "月份": "3月", "课程": "A"},
    ])
    grid = _grid(compute_bucket_metrics(ds, [_depth_metric()]))
    assert grid["2026年 3月"]["1门"] == 1
    assert grid["2026年 3月"]["2门"] == 1


def test_same_person_in_different_months_counted_per_month():
    """跨月不复用计数：同一人 3 月报 2 门、6 月报 1 门，两个月各计一次。"""
    ds = _dataset([
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "3月", "课程": "A"},
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "3月", "课程": "B"},
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "6月", "课程": "C"},
    ])
    grid = _grid(compute_bucket_metrics(ds, [_depth_metric()]))
    assert grid["2026年 3月"] == {"1门": 0, "2门": 1} or grid["2026年 3月"] == {"2门": 1}
    assert grid["2026年 6月"] == {"1门": 1}


def test_depth_above_threshold_is_merged():
    """达到 depth_max_bucket 的合并为「N门以上」。"""
    rows = [
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "1月", "课程": c}
        for c in ["A", "B", "C", "D", "E", "F"]
    ]
    grid = _grid(compute_bucket_metrics(_dataset(rows), [_depth_metric()]))
    assert "5门以上" in grid["2026年 1月"]
    assert grid["2026年 1月"]["5门以上"] == 1
    assert "6门" not in grid["2026年 1月"]


def test_multi_value_course_column_is_split_and_deduped():
    """课程列是逗号分隔多值时，先拆分再去重计数。"""
    ds = _dataset([
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "1月", "课程": "A,B"},
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "1月", "课程": "A,B"},
    ])
    metric = _depth_metric(depth_split=",")
    grid = _grid(compute_bucket_metrics(ds, [metric]))
    # 重复出现的 A,B 仍只算 2 门
    assert grid["2026年 1月"]["2门"] == 1


def test_rows_without_identity_are_excluded():
    """无身份键的行不参与统计（与全局口径一致）。"""
    ds = _dataset([
        {"证件号码": "", "姓名": "", "年份": "2026年", "月份": "1月", "课程": "A"},
        {"证件号码": "1", "姓名": "甲", "年份": "2026年", "月份": "1月", "课程": "A"},
    ])
    grid = _grid(compute_bucket_metrics(ds, [_depth_metric()]))
    assert grid["2026年 1月"]["1门"] == 1


def test_missing_dimension_goes_to_unfilled_bucket():
    """维度缺失归入「未填写」，不静默丢弃。"""
    ds = _dataset([
        {"证件号码": "1", "姓名": "甲", "年份": "", "月份": "", "课程": "A"},
    ])
    grid = _grid(compute_bucket_metrics(ds, [_depth_metric()]))
    assert "未填写 未填写" in grid


def test_metric_requires_config():
    """缺必要配置时必须显式报错，不能静默返回 0。"""
    ds = _dataset([{"证件号码": "1", "姓名": "甲", "课程": "A"}])
    for bad in (
        _depth_metric(group_columns=[]),
        _depth_metric(dimension_columns=[]),
        _depth_metric(depth_column=None),
    ):
        try:
            compute_bucket_metrics(ds, [bad])
        except ValueError:
            continue
        raise AssertionError(f"应当抛出 ValueError: {bad}")


# ============================================================ 真实数据回归
def test_real_data_monthly_depth_distribution(seeded):
    """用真实批次锁定各档人数（防止口径被改坏而不自知）。"""
    store = FactStore(get_settings())
    matrix = store.metric_bucket_matrix(
        "20261015", "monthly_enroll_depth", "course_monthly_summary")
    assert matrix["buckets"], "分桶指标应已落库"
    totals = matrix["group_totals"]
    # 真实数据核对值（20261015 批次）
    assert totals["1门"] == 117
    assert totals["2门"] == 47
    assert totals["3门"] == 58
    assert totals["4门"] == 7
    assert totals["5门以上"] == 1
    # 每月的合计 = 该月各档人数之和
    for b in matrix["buckets"]:
        assert b["total"] == sum(b["groups"].values())


def test_real_data_month_totals_match_unique_learners_per_month(seeded):
    """各月人数之和 <= 去重学员数（跨月重复计数导致 >= 才对）。

    每月去重人数之和必然 >= 全期去重人数（同一人跨月会被算多次）。
    """
    store = FactStore(get_settings())
    matrix = store.metric_bucket_matrix(
        "20261015", "monthly_enroll_depth", "course_monthly_summary")
    assert matrix["grand_total"] >= 158
    assert matrix["grand_total"] <= 418


# ============================================================ 隔离与端点
def test_bucket_metrics_are_isolated_by_dataset_id():
    """分桶指标按 dataset_id 隔离，不与其他数据集串味。"""
    store = FactStore(get_settings())
    course = store.metric_bucket_matrix(
        "20261015", "monthly_enroll_depth", "course_monthly_summary")
    roster = store.metric_bucket_matrix(
        "20261015", "monthly_enroll_depth", "personnel_roster")
    assert course["buckets"], "课程数据集应有分桶数据"
    assert not roster["buckets"], "人员名单数据集不应有该分桶数据"


def test_bucket_endpoint_returns_matrix():
    response = client.get(
        "/metrics/buckets",
        params={"dataset_id": "course_monthly_summary",
                "metric_key": "monthly_enroll_depth"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dimension"] == "月份"
    assert body["grand_total"] > 0
    assert body["buckets"]
    first = body["buckets"][0]
    assert set(first) >= {"bucket", "groups", "total"}


def test_bucket_endpoint_404_for_unknown_metric():
    response = client.get(
        "/metrics/buckets",
        params={"dataset_id": "course_monthly_summary", "metric_key": "not_exists"},
    )
    assert response.status_code == 404


def test_course_dashboard_exposes_monthly_depth_panel():
    body = client.get("/dashboard/course/data").json()
    depth = body.get("monthly_depth")
    assert depth, "课程看板应包含 monthly_depth 面板"
    assert depth["dimension"] == "月份"
    assert depth["buckets"]
    assert depth["grand_total"] > 0
    # 口径说明必须带上（这是本需求最容易被误读的地方）
    assert depth["note"]


def test_bucket_panel_has_no_pii():
    """分桶数据不得含任何个人标识。"""
    body = client.get("/dashboard/course/data").json()
    depth = body["monthly_depth"]
    text = repr(depth)
    for pii in ("证件号码", "姓名", "手机", "id_number", "phone"):
        assert pii not in text, f"分桶面板泄露 PII 字段：{pii}"
