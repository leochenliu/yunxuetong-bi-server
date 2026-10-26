"""按项目分页看板的测试。

覆盖：
  1. 项目注册表能从 config/dashboards.yaml 读出，且每个项目指向自己的配置；
  2. 每个项目的数据包 KPI 不为空、且指标互不串项目（dataset_id 隔离）；
  3. 指标快照按 dataset_id 隔离（同一批次两个数据集不互相覆盖）；
  4. 各项目页面的 HTML 存在、且引用各自的数据端点；
  5. 返回数据不含个人标识；
  6. 指标引擎的新聚合（count_equals / count_distinct_multi）行为正确。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bi.api import app
from bi.config import MetricDef
from bi.metrics import compute_metrics
from bi.projects import find_project, load_projects
from bi.settings import get_settings
from bi.store import FactStore


client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]

PII_KEYS = {
    "姓名", "证件号码", "联系电话", "手机", "id_number", "phone",
    "name", "_identity_key", "id_number_masked",
}


# ---------------------------------------------------------------- 项目注册表
def test_projects_are_registered_with_distinct_configs():
    projects = load_projects(get_settings())
    ids = [p.id for p in projects]
    assert {"denggao", "roster", "course"} <= set(ids)
    # 每个项目都必须有自己的页面文件与自己的 metrics 配置
    for project in projects:
        assert (ROOT / "web" / project.page).is_file(), f"缺少页面 {project.page}"
        assert (get_settings().config_dir / project.metrics_config).is_file()
    # dataset_id 不得重复（否则两个页面会读同一份数据）
    dataset_ids = [p.dataset_id for p in projects]
    assert len(dataset_ids) == len(set(dataset_ids))


def test_find_project_returns_none_for_unknown():
    assert find_project(get_settings(), "not-a-project") is None


# ------------------------------------------------------------- 指标快照隔离
def test_metric_snapshot_is_isolated_by_dataset_id():
    """同一批次下的两个数据集，指标快照不得互相覆盖。"""
    store = FactStore(get_settings())
    roster = store.metric_snapshot("20261015", dataset_id="personnel_roster")
    course = store.metric_snapshot("20261015", dataset_id="course_monthly_summary")
    assert roster["roster_total"]["value"] == 158
    assert course["detail_rows"]["value"] == 418
    # 汇总查询（不指定 dataset）会合并两者，但仍不应丢掉其中一个
    merged = store.metric_snapshot("20261015")
    assert "roster_total" in merged
    assert "detail_rows" in merged


# ----------------------------------------------------------------- 数据接口
def test_project_dashboard_data_has_kpis_and_no_pii(seeded):
    for project in load_projects(get_settings()):
        response = client.get(f"/dashboard/{project.id}/data")
        assert response.status_code == 200, project.id
        body = response.json()
        assert body["project"]["id"] == project.id
        assert body["kpis"], f"{project.id} 的 KPI 为空"
        # KPI 必须是该项目自己声明的指标
        assert {k["key"] for k in body["kpis"]} == set(project.kpi_metrics)

        def assert_no_pii(value):
            if isinstance(value, dict):
                assert not set(value).intersection(PII_KEYS)
                for nested in value.values():
                    assert_no_pii(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_no_pii(nested)

        assert_no_pii(body)


def test_project_dashboard_data_uses_own_metrics_not_other_projects():
    """继续教育项目的 KPI 不得出现登高指标（反之亦然）——即项目不串味。"""
    store = FactStore(get_settings())
    roster = client.get("/dashboard/roster/data").json()
    keys = {k["key"] for k in roster["kpis"]}
    assert "enrolled_total" not in keys
    assert "exam_passed" not in keys
    assert "roster_total" in keys

    course = client.get("/dashboard/course/data").json()
    ckeys = {k["key"] for k in course["kpis"]}
    assert "detail_rows" in ckeys
    assert "roster_total" not in ckeys


def test_unknown_project_returns_404():
    assert client.get("/dashboard/nope/data").status_code == 404
    assert client.get("/dashboard/nope").status_code == 404


def test_all_projects_endpoint_lists_every_project():
    response = client.get("/dashboard/all/data")
    assert response.status_code == 200
    body = response.json()
    ids = {p["id"] for p in body["projects"]}
    assert {"denggao", "roster", "course"} <= ids
    assert set(body["data"]) == ids


# ------------------------------------------------------------------- 页面
def test_each_project_page_is_served_and_targets_its_own_endpoint():
    for project in load_projects(get_settings()):
        response = client.get(f"/dashboard/{project.id}")
        assert response.status_code == 200
        text = response.text
        # 页面必须绑定自己的项目 id（数据端点由它拼出）
        assert f'const PROJECT_ID = "{project.id}"' in text
        assert "/dashboard/${PROJECT_ID}/data" in text
        assert "/assets/dashboard.css" in text
        assert "/assets/dashboard.js" in text


def test_dashboards_index_page_is_served():
    response = client.get("/dashboards")
    assert response.status_code == 200
    assert "选择业务项目" in response.text


def test_shared_assets_are_mounted():
    assert client.get("/assets/dashboard.css").status_code == 200
    assert client.get("/assets/dashboard.js").status_code == 200


def test_area_chart_accepts_theme_accent():
    """areaChart 必须支持传入主题色。

    回归防护：此前 areaChart 把蓝色 #5578f6 硬编码在函数体里，
    导致课程页（橙色主题）的月度趋势线渲染成蓝色，与页面主题不符。
    """
    src = (ROOT / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8")
    assert 'function areaChart(items, {labelKey = "month", valueKey = "count", accent = ACCENT}' \
        in src, "areaChart 未提供 accent 参数"
    # 页面必须把项目主题色传进去
    course_page = (ROOT / "web" / "dashboard_course.html").read_text(encoding="utf-8")
    assert "valueKey: \"detail_rows\", accent: data.project?.accent" in course_page
    denggao_page = (ROOT / "web" / "dashboard_denggao.html").read_text(encoding="utf-8")
    assert "areaChart(data.enrollment_by_period,\n        {labelKey: \"period\", accent: data.project?.accent})" \
        in denggao_page


def test_projects_endpoint_lists_registered_projects():
    response = client.get("/projects")
    assert response.status_code == 200
    assert response.json()["count"] >= 3


# --------------------------------------------------- 异常面板语义（关键）
def test_anomaly_payload_distinguishes_unconfigured_from_zero():
    """「未配置异常规则」与「本批次 0 异常」必须能被调用方区分。

    这两者对业务的含义完全相反：前者是「没测过」，后者是「测过了，没问题」。
    若都退化成 total=0，页面会给出误导性的「一切正常」结论。
    """
    for project in load_projects(get_settings()):
        body = client.get(f"/dashboard/{project.id}/data").json()
        anomalies = body.get("anomalies")
        assert anomalies is not None, project.id
        # rules_configured 必须显式给出来，不能靠 total 反推
        assert isinstance(anomalies["rules_configured"], bool), project.id

        if anomalies["rules_configured"] is False:
            # 没配规则 -> 不可能有异常，且 by_rule 必须为空
            assert anomalies["by_rule"] == [], project.id
            assert anomalies["total"] == 0, project.id
        else:
            # 配了规则 -> total 必须等于 by_rule 的计数之和（自洽）
            summed = sum(r["count"] for r in anomalies["by_rule"])
            assert anomalies["total"] == summed, project.id


def test_denggao_has_anomaly_rules_and_reports_them():
    """登高配了异常规则，页面必须能给出真实异常统计（而不是空态）。"""
    body = client.get("/dashboard/denggao/data").json()
    anomalies = body["anomalies"]
    assert anomalies["rules_configured"] is True
    assert anomalies["by_rule"], "登高应能报出异常规则命中情况"
    assert anomalies["total"] > 0
    # 每条规则都要有可读中文标签，不能只给 rule_id
    for row in anomalies["by_rule"]:
        assert row["label"]
        assert not row["label"].startswith(("missing_", "invalid_", "score_")), row


def test_every_raised_anomaly_rule_is_declared_in_config():
    """引擎产出的每个 rule_id 都必须在 dataset.yaml 里声明。

    回归防护：跨期规则 exam_status_regressed 曾只存在于代码里而没写进配置，
    导致页面上直接显示英文 rule_id，而不是可读的中文说明。
    """
    from bi.config import load_dataset_config

    cfg = load_dataset_config(get_settings().config_dir / "dataset.yaml")
    declared = {r.id for r in cfg.anomaly_rules}
    # 引擎里硬编码产出的 rule_id（行级 + 跨期）
    from bi.anomaly import check_cross_period_anomalies  # noqa: F401

    produced = {
        "duplicate_record_key", "missing_identity", "invalid_id_number",
        "score_out_of_range", "passed_but_no_exam_date", "passed_but_low_score",
        "passed_but_no_score", "paid_but_zero_amount", "class_code_unparsable",
        "exam_status_regressed",
    }
    missing = produced - declared
    assert not missing, f"以下异常规则未在 dataset.yaml 声明：{sorted(missing)}"


def test_roster_and_course_declare_no_anomaly_rules():
    """继续教育两个数据集当前未配异常规则 —— 这是已知状态，页面必须如实说明。"""
    for pid in ("roster", "course"):
        body = client.get(f"/dashboard/{pid}/data").json()
        assert body["anomalies"]["rules_configured"] is False, pid


# ------------------------------------------------------------- 指标引擎新算子
def _dataset(rows: list[dict]):
    from bi.excel_reader import Dataset

    return Dataset(
        name="测试",
        sheet="s",
        columns=list(rows[0].keys()) if rows else [],
        rows=rows,
        source_file="test.xlsx",
    )


def test_count_equals_matches_exact_text():
    ds = _dataset([
        {"缴费情况": "已缴费"},
        {"缴费情况": "已缴费"},
        {"缴费情况": "/"},
        {"缴费情况": ""},
    ])
    metric = MetricDef(key="paid", name="已缴费", agg="count_equals",
                       column="缴费情况", equals="已缴费")
    assert compute_metrics(ds, [metric])[0].value == 2


def test_count_equals_not_equals_and_exclusions():
    ds = _dataset([
        {"职称等级": "初级"},
        {"职称等级": "中级"},
        {"职称等级": "无"},
        {"职称等级": ""},
    ])
    metric = MetricDef(key="titled", name="有职称", agg="count_equals",
                       column="职称等级", not_equals="无", exclude_values=["无", ""])
    assert compute_metrics(ds, [metric])[0].value == 2


def test_count_equals_handles_multi_value_column():
    ds = _dataset([
        {"课程": "A,B"},
        {"课程": "B,C"},
        {"课程": "A"},
    ])
    metric = MetricDef(key="picked_b", name="选了B", agg="count_equals",
                       column="课程", equals="B", split=",")
    assert compute_metrics(ds, [metric])[0].value == 2


def test_count_distinct_multi_dedupes_on_composite_key():
    ds = _dataset([
        {"证件号码": "1", "姓名": "甲", "课程": "A"},
        {"证件号码": "1", "姓名": "甲", "课程": "B"},
        {"证件号码": "1", "姓名": "甲", "课程": "A"},
        {"证件号码": "2", "姓名": "乙", "课程": "A"},
    ])
    people = MetricDef(key="learners", name="去重学员", agg="count_distinct_multi",
                       dedupe_columns=["证件号码", "姓名"])
    pairs = MetricDef(key="pairs", name="去重组合", agg="count_distinct_multi",
                      dedupe_columns=["证件号码", "姓名", "课程"])
    assert compute_metrics(ds, [people])[0].value == 2
    assert compute_metrics(ds, [pairs])[0].value == 3
