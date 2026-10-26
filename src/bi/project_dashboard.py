"""按项目聚合看板数据：每个项目一份结构自洽的数据包。

与既有 `dashboard.py` 的区别
---------------------------
`dashboard.py` 是「一个大而全的混合包」：把 3 个数据集塞进一个 dict，
页面自己按 id 取。问题：
  ① 页面与项目耦合 —— 登高页面要认识 personnel_roster 的字段；
  ② 指标口径混淆 —— 20261015 有两个数据集，`metric_snapshot(batch)` 会互相覆盖；
  ③ KPI 硬编码 —— 5 张卡片写死是登高指标。

本模块改成「**一个项目 = 一份数据**」：
  - KPI 由项目自己的 metrics_config 决定（`config/dashboards.yaml` 的 kpi_metrics）；
  - 指标读取带 dataset_id 过滤，不会串项目；
  - 各项目只提供自己业务面上有意义的面板（登高看考试/成绩，继续教育看选课/单位）。
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter, defaultdict
from typing import Any

from .config import MetricDef, load_dataset_config, load_metrics_config, load_period_config
from .period import group_by_period
from .projects import Project, load_projects
from .settings import Settings
from .store import FactStore
from .values import to_date, to_number

PII_FIELDS = {"姓名", "证件号码", "身份证号码", "联系电话", "手机", "id_number", "phone", "name"}


def _load_rows(store: FactStore, dataset_id: str, batch: str) -> list[dict[str, Any]]:
    with store.connect() as conn:
        records = conn.execute(
            """SELECT payload_json, record_key
               FROM raw_records r
               JOIN upload_versions v ON v.id=r.version_id
               WHERE v.dataset_id=? AND v.batch=?
               ORDER BY r.id""",
            (dataset_id, batch),
        ).fetchall()
    rows = []
    for record in records:
        payload = json.loads(record["payload_json"])
        rows.append({
            key: value for key, value in payload.items()
            if key not in PII_FIELDS
        } | {"_identity_key": record["record_key"]})
    return rows


def _count_values(rows: list[dict[str, Any]], field: str, *,
                   empty_label: str = "未填写") -> list[dict[str, Any]]:
    counts = Counter(str(row.get(field) or "").strip() or empty_label for row in rows)
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _number(value: Any) -> float | None:
    return to_number(value)


def _latest(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(versions, key=lambda v: v["batch"]) if versions else None


def _kpis(store: FactStore, project: Project, versions: list[dict[str, Any]],
          settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """KPI 卡片 + 各自的时间序列（严格按 dataset_id 过滤，不串项目）。"""
    latest = _latest(versions)
    if latest is None:
        return [], []

    metric_defs = load_metrics_config(settings.config_dir / project.metrics_config)
    definitions = {m.key: m for m in metric_defs}
    previous = max(
        (v for v in versions if v["batch"] < latest["batch"]),
        key=lambda v: v["batch"], default=None,
    )
    current_values = store.metric_snapshot(latest["batch"], dataset_id=project.dataset_id)
    previous_values = (
        store.metric_snapshot(previous["batch"], dataset_id=project.dataset_id)
        if previous else {}
    )
    demo_batches = {v["batch"] for v in versions if v.get("is_demo")}

    kpis: list[dict[str, Any]] = []
    trends: list[dict[str, Any]] = []
    for key in project.kpi_metrics:
        definition = definitions.get(key)
        current = current_values.get(key)
        if definition is None or current is None:
            continue
        previous_value = previous_values.get(key, {}).get("value")
        value = current["value"]
        delta = (value - previous_value
                 if value is not None and previous_value is not None else None)
        kpis.append({
            "key": key,
            "label": definition.name,
            "value": value,
            "unit": definition.unit,
            "decimals": definition.decimals,
            "desc": definition.desc,
            "delta": delta,
            "previous_value": previous_value,
        })
        points = store.metric_timeseries(key, dataset_id=project.dataset_id)
        trends.append({
            "key": key,
            "label": definition.name,
            "unit": definition.unit,
            "decimals": definition.decimals,
            "points": [
                {
                    "batch": point["batch"],
                    "value": point["value"],
                    "is_demo": point["batch"] in demo_batches,
                }
                for point in points
            ],
        })
    return kpis, trends


def _anomaly_data(store: FactStore, project: Project,
                   versions: list[dict[str, Any]],
                   settings: Settings) -> dict[str, Any]:
    config_path = settings.config_dir / project.dataset_config
    rules: dict[str, str] = {}
    if config_path.exists():
        cfg = load_dataset_config(config_path)
        rules = {rule.id: rule.desc for rule in cfg.anomaly_rules}

    if not versions:
        # 无批次：也要如实说明「本数据集配没配异常规则」，否则前端无法区分
        # 「没数据」「没配规则」「没异常」三种截然不同的情况。
        return {"total": 0, "by_rule": [], "trend": [],
                "rules_configured": bool(rules)}

    config_path = settings.config_dir / project.dataset_config
    rules: dict[str, str] = {}
    if config_path.exists():
        cfg = load_dataset_config(config_path)
        rules = {rule.id: rule.desc for rule in cfg.anomaly_rules}

    with store.connect() as conn:
        rows = conn.execute(
            """SELECT v.batch, a.rule_id, a.severity, COUNT(*) AS count
               FROM anomalies a
               JOIN upload_versions v ON v.id=a.version_id
               WHERE v.dataset_id=?
               GROUP BY v.batch, a.rule_id, a.severity
               ORDER BY v.batch, count DESC""",
            (project.dataset_id,),
        ).fetchall()

    latest_batch = _latest(versions)["batch"]
    latest_rules: Counter[str] = Counter()
    latest_severity: Counter[str] = Counter()
    rule_severity: dict[str, str] = {}
    by_batch: dict[str, dict[str, Any]] = {
        v["batch"]: {"batch": v["batch"], "total": 0, "high": 0} for v in versions
    }
    for row in rows:
        count = int(row["count"])
        batch = row["batch"]
        by_batch.setdefault(batch, {"batch": batch, "total": 0, "high": 0})
        by_batch[batch]["total"] += count
        if row["severity"] == "high":
            by_batch[batch]["high"] += count
        if batch == latest_batch:
            latest_rules[row["rule_id"]] += count
            latest_severity[row["severity"]] += count
            rule_severity.setdefault(row["rule_id"], row["severity"])

    return {
        "total": sum(latest_rules.values()),
        # 规则是否配置过 —— 决定前端展示「未配置规则」还是「本批次无异常」
        "rules_configured": bool(rules),
        # 按严重度拆分：info 级多是「口径提示」（如过期日期来自推算），
        # 和 high 级的「数据错误」混在一个总数里会让人误判问题量级。
        "by_severity": [
            {"severity": severity, "count": count}
            for severity, count in latest_severity.most_common()
        ],
        "by_rule": [
            {
                "rule_id": rule_id,
                # 未在 dataset.yaml 声明的规则会退化显示英文 id；
                # 显式标注为「未声明」，避免用户误以为这是正常业务术语。
                "label": rules.get(rule_id, f"{rule_id}（未在配置中声明）"),
                "count": count,
                "declared": rule_id in rules,
                "severity": rule_severity.get(rule_id, "medium"),
            }
            for rule_id, count in latest_rules.most_common()
        ],
        "trend": [
            {**by_batch[v["batch"]], "is_demo": bool(v.get("is_demo"))}
            for v in versions
        ],
    }


# ---------------------------------------------------------------------------
# 各项目的专属面板
# ---------------------------------------------------------------------------
def _denggao_panels(store: FactStore, project: Project,
                    versions: list[dict[str, Any]],
                    settings: Settings) -> dict[str, Any]:
    """登高：报名 / 考试 / 成绩 / 结算 / 签到。"""
    latest = _latest(versions)
    if latest is None:
        return {}
    rows = _load_rows(store, project.dataset_id, latest["batch"])

    enrollment = Counter(
        str(row.get("报名月份") or "").strip() for row in rows if row.get("报名月份")
    )
    month_status: Counter[tuple[str, str]] = Counter()
    for row in rows:
        month = str(row.get("报名月份") or "").strip()
        status = str(row.get("考试状态") or "").strip() or "未填写"
        if month:
            month_status[(month, status)] += 1
    enrollment_by_month = [
        {
            "month": month,
            "count": count,
            "statuses": [
                {"label": status, "count": status_count}
                for (group_month, status), status_count in sorted(month_status.items())
                if group_month == month
            ],
        }
        for month, count in sorted(enrollment.items())
    ]

    # 自然时间周期口径（config/period.yaml，单位：天）
    period = load_period_config(settings.period_config_path)
    enrollment_by_period, period_unassigned = group_by_period(
        rows, "报名日期", period, status_field="考试状态"
    )

    scores = [s for row in rows if (s := _number(row.get("分数"))) is not None]
    score_bands = [
        {"label": "低于 60", "count": sum(s < 60 for s in scores)},
        {"label": "60–69", "count": sum(60 <= s < 70 for s in scores)},
        {"label": "70–79", "count": sum(70 <= s < 80 for s in scores)},
        {"label": "80–89", "count": sum(80 <= s < 90 for s in scores)},
        {"label": "90–100", "count": sum(90 <= s <= 100 for s in scores)},
        {"label": "超出 0–100", "count": sum(s < 0 or s > 100 for s in scores)},
        {"label": "无分数", "count": len(rows) - len(scores)},
    ]
    amounts = [_number(row.get("金额")) for row in rows]
    known_amounts = [a for a in amounts if a is not None]
    attendance = [
        {
            "label": label,
            "values": _count_values(rows, field),
        }
        for label, field in (
            ("培训第一天", "培训第一天签到情况"),
            ("培训第二天", "培训第二日签到情况"),
        )
    ]
    return {
        "enrollment_by_month": enrollment_by_month,
        "enrollment_by_period": enrollment_by_period,
        "period_unassigned": period_unassigned,
        "exam_status": _count_values(rows, "考试状态"),
        "exam_schedule": [
            {
                "label": label,
                "count": sum(bool(str(row.get("考试日期") or "").strip()) == has for row in rows),
            }
            for label, has in (("有考试日期", True), ("无考试日期", False))
        ],
        "score_bands": score_bands,
        "payment_status": _count_values(rows, "是否付款"),
        "invoice_status": _count_values(rows, "是否开票"),
        "reported_amount": {
            "value": sum(known_amounts),
            "records": len(known_amounts),
            "note": "按本期学员记录中的金额求和，业务口径请核验",
        },
        "attendance": attendance,
        "retry_distribution": _count_values(rows, "补考次数"),
        "deferred_cert_count": sum(
            int(_number(row.get("延期换证人数")) or 0) for row in rows
        ),
    }


def _roster_panels(store: FactStore, project: Project,
                   versions: list[dict[str, Any]],
                   settings: Settings) -> dict[str, Any]:
    """继续教育人员名单：选课结构 / 缴费 / 单位 / 画像。"""
    latest = _latest(versions)
    if latest is None:
        return {}
    rows = _load_rows(store, project.dataset_id, latest["batch"])

    courses: Counter[str] = Counter()
    selection_count: Counter[int] = Counter()
    for row in rows:
        picked = [c.strip() for c in str(row.get("课程") or "").split(",") if c.strip()]
        selection_count[len(picked)] += 1
        for course in picked:
            courses[course] += 1

    employers: Counter[str] = Counter()
    for row in rows:
        employer = str(row.get("工作单位") or "").strip()
        if employer:
            employers[employer] += 1

    avg_courses = (sum(k * v for k, v in selection_count.items()) / len(rows)) if rows else 0.0
    paid = sum(1 for row in rows if str(row.get("缴费情况") or "").strip() == "已缴费")
    titled = sum(
        1 for row in rows
        if str(row.get("职称等级") or "").strip() not in ("", "无")
    )
    return {
        "education": _count_values(rows, "最高学历"),
        "title_level": _count_values(rows, "职称等级"),
        "payment_status": _count_values(rows, "缴费情况"),
        "courses": [
            {"label": label, "count": count} for label, count in courses.most_common(10)
        ],
        "course_count_total": len(courses),
        "selection_distribution": [
            {"label": f"{k} 门", "count": v} for k, v in sorted(selection_count.items())
        ],
        "avg_courses": round(avg_courses, 2),
        "employers": [
            {"label": label, "count": count} for label, count in employers.most_common(10)
        ],
        "employer_count": len(employers),
        "summary": {
            "paid": paid,
            "unpaid": len(rows) - paid,
            "titled": titled,
            "multi_course": sum(v for k, v in selection_count.items() if k >= 2),
        },
    }


def _course_panels(store: FactStore, project: Project,
                   versions: list[dict[str, Any]],
                   settings: Settings) -> dict[str, Any]:
    """继续教育课程月份汇总：人次结构 / 热力图 / 缴费覆盖。"""
    latest = _latest(versions)
    if latest is None:
        return {}
    rows = _load_rows(store, project.dataset_id, latest["batch"])

    month_people: dict[str, set[str]] = defaultdict(set)
    course_rows: Counter[str] = Counter()
    course_people: dict[str, set[str]] = defaultdict(set)
    heatmap: Counter[tuple[str, str]] = Counter()
    unique_people: set[str] = set()
    for row in rows:
        month = str(row.get("月份") or "").strip() or "未填写"
        course = str(row.get("课程") or "").strip() or "未填写"
        identity = str(row.get("_identity_key") or "")
        month_people[month].add(identity)
        course_rows[course] += 1
        course_people[course].add(identity)
        heatmap[(course, month)] += 1
        unique_people.add(identity)

    months = sorted(
        month_people,
        key=lambda v: int("".join(ch for ch in v if ch.isdigit()) or "99"),
    )
    courses = sorted(course_rows, key=lambda c: (-course_rows[c], c))[:12]
    return {
        "detail_rows": latest["record_count"],
        "unique_learners": len(unique_people),
        "months": [
            {
                "label": month,
                "detail_rows": sum(
                    1 for row in rows
                    if (str(row.get("月份") or "").strip() or "未填写") == month
                ),
                "unique_learners": len(month_people[month]),
            }
            for month in months
        ],
        "courses": [
            {
                "label": label,
                "detail_rows": course_rows[label],
                "unique_learners": len(course_people[label]),
            }
            for label in courses
        ],
        "heatmap": [
            {"course": course, "month": month, "count": heatmap[(course, month)]}
            for course in courses for month in months
        ],
        "payment_status": _count_values(rows, "缴费情况"),
        **_monthly_depth_panel(store, project, latest, rows),
    }


def _monthly_depth_panel(store: FactStore, project: Project,
                          latest: dict[str, Any],
                          rows: list[dict[str, Any]]) -> dict[str, Any]:
    """月度报名门数分布（按自然月 × 报名门数的人数）。

    数据来源优先读已落库的分桶指标（metric_buckets）；
    若该批次尚未重跑入库（分桶表为空），则直接从当前批次原始记录现算，
    保证页面永远有数据、且口径与入库时完全一致（同一个算子）。
    """
    metric_key = "monthly_enroll_depth"
    matrix = store.metric_bucket_matrix(latest["batch"], metric_key, project.dataset_id)
    if not matrix["buckets"]:
        matrix = _compute_depth_matrix_fallback(rows)
    if not matrix["buckets"]:
        return {}

    # 维度标签从 "2026年 3月" 精简成 "3月"（年份统一时不必重复展示）
    buckets = []
    for item in matrix["buckets"]:
        label = item["bucket"]
        parts = label.split()
        if len(parts) == 2 and parts[0].endswith("年"):
            label = parts[1]
        buckets.append({**item, "label": label, "bucket": item["bucket"]})
    return {
        "monthly_depth": {
            "metric_key": metric_key,
            "metric_name": matrix.get("metric_name") or "月度报名门数分布",
            "dimension": matrix["dimension"] or "月份",
            "unit": matrix["unit"] or "人",
            "buckets": buckets,
            "group_totals": matrix["group_totals"],
            "grand_total": matrix["grand_total"],
            "note": "同一人若在多个自然月报名，会在每个月各计一次；"
                    "各档为「当月报名该门数的人数」，跨月不可直接相加当作总人数。",
        }
    }


def _compute_depth_matrix_fallback(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """直接用原始记录现算月度报名深度（与入库算子同一套口径）。

    场景：新增了分桶指标但还没重跑 seed，DB 里没有分桶格子。
    """
    from .metrics import _compute_group_depth

    metric = MetricDef(
        key="monthly_enroll_depth", name="月度报名门数分布", agg="count_by_group_depth",
        unit="人", group_columns=["证件号码", "姓名"],
        dimension_columns=["年份", "月份"], depth_column="课程",
        depth_max_bucket=5, dimension_name="月份",
    )
    ds = _FakeDataset(rows)
    buckets = _compute_group_depth(ds, metric)
    if not buckets:
        return {"metric_key": metric.key, "metric_name": metric.name, "unit": metric.unit,
                "dimension": metric.dimension_name or "月份",
                "buckets": [], "group_totals": {}, "grand_total": 0}

    by_bucket: dict[str, dict[str, float]] = {}
    group_totals: dict[str, float] = {}
    for b in buckets:
        by_bucket.setdefault(b.bucket, {})[b.group_key] = b.value
        group_totals[b.group_key] = group_totals.get(b.group_key, 0.0) + b.value
    return {
        "metric_key": metric.key,
        "metric_name": metric.name,
        "unit": metric.unit,
        "dimension": metric.dimension_name or "月份",
        "buckets": [
            {"bucket": bucket, "groups": groups, "total": sum(groups.values())}
            for bucket, groups in by_bucket.items()
        ],
        "group_totals": group_totals,
        "grand_total": sum(group_totals.values()),
    }


class _FakeDataset:
    """只带 rows 的轻量 Dataset 替身（算子只用到 .rows）。"""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.columns = list(rows[0].keys()) if rows else []


# ---------------------------------------------------------------------------
# 证书台账（小黄卡）
# ---------------------------------------------------------------------------
# 这个类目的核心是**过期日期**，所以面板围绕三件事组织：
#   ① 到期倒计时（已过期 / 30 天内 / 90 天内 / 一年以上）—— 续证动作的输入；
#   ② 到期日期的**来源与精度**（证面原文 / 按 2 年推算、精确到日 / 只有年月）——
#      推算值和月末估计值不能直接拿去提醒人，必须在页面上标出来；
#   ③ 数据质量（有效期字段有几种写法、多少条解析不出来、证件号异常多少）。
# 「距今天数」是**时点口径**，每次打开页面按当天重算，不入库（入库即过时）。
EXPIRY_BUCKETS = (
    ("已过期", None, None),
    ("30 天内", 0, 30),
    ("31–90 天", 31, 90),
    ("91–180 天", 91, 180),
    ("181–365 天", 181, 365),
    ("1 年以上", 366, None),
)


def _expiry_bucket(days: int) -> str:
    for label, low, high in EXPIRY_BUCKETS:
        if label == "已过期":
            if days < 0:
                return label
            continue
        if low is not None and days < low:
            continue
        if high is not None and days > high:
            continue
        return label
    return "1 年以上"


def _certificate_panels(store: FactStore, project: Project,
                        versions: list[dict[str, Any]],
                        settings: Settings) -> dict[str, Any]:
    """证书台账：到期倒计时 / 有效期形态 / 数据质量。"""
    latest = _latest(versions)
    if latest is None:
        return {}
    rows = _load_rows(store, project.dataset_id, latest["batch"])

    today = _dt.date.today()
    buckets: Counter[str] = Counter()
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    month_counts: Counter[str] = Counter()
    shape_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    precision_counts: Counter[str] = Counter()
    companies: Counter[str] = Counter()
    validity: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    holders: Counter[str] = Counter()

    for row in rows:
        shape = str(row.get("有效期形态") or "").strip() or "未标记"
        shape_counts[shape] += 1
        source = str(row.get("日期来源") or "").strip() or "未标注"
        source_counts[source] += 1
        precision = str(row.get("过期日期精度") or "").strip() or "无"
        precision_counts[precision] += 1

        company = str(row.get("公司名称") or "").strip()
        if company:
            companies[company] += 1

        # 持证人按身份证去重（一人多证要能看出来）。
        # 身份证号码是 PII，不进面板 payload；用主键首段（= 身份证）做去重计数。
        holder = str(row.get("_identity_key") or "").split("|")[0].strip()
        if holder:
            holders[holder] += 1

        days = _number(row.get("有效期天数"))
        if days is None:
            validity["无法计算"] += 1
        elif days <= 730:
            validity["≤ 730 天"] += 1
        elif days <= 750:
            validity["731–750 天"] += 1
        else:
            validity["751 天以上"] += 1

        expiry = to_date(row.get("过期日期"))
        if expiry is None:
            buckets["日期未知"] += 1
            by_source[source]["日期未知"] += 1
            raw = str(row.get("证书有效期") or "").strip()
            if raw:
                unresolved[raw] += 1
            continue
        delta = (expiry - today).days
        label = _expiry_bucket(delta)
        buckets[label] += 1
        by_source[source][label] += 1
        month = str(row.get("过期月份") or "").strip()
        if month:
            month_counts[month] += 1

    period = load_period_config(settings.period_config_path)
    expiry_by_period, period_unassigned = group_by_period(rows, "过期日期", period)

    bucket_order = [label for label, _, _ in EXPIRY_BUCKETS] + ["日期未知"]
    multi_holders = sum(1 for count in holders.values() if count >= 2)
    return {
        "today": today.isoformat(),
        "expiry_buckets": [
            {"label": label, "count": buckets.get(label, 0)} for label in bucket_order
        ],
        "expiry_buckets_by_source": [
            {
                "source": source,
                "buckets": [
                    {"label": label, "count": counts.get(label, 0)} for label in bucket_order
                ],
            }
            for source, counts in sorted(by_source.items(), key=lambda kv: -sum(kv[1].values()))
        ],
        "expiry_by_month": [
            {"month": month, "count": count} for month, count in sorted(month_counts.items())
        ],
        "expiry_by_period": expiry_by_period,
        "period_unassigned": period_unassigned,
        "shape_distribution": [
            {"label": label, "count": count}
            for label, count in shape_counts.most_common()
        ],
        "source_distribution": [
            {"label": label, "count": count}
            for label, count in source_counts.most_common()
        ],
        "precision_distribution": [
            {"label": label, "count": count}
            for label, count in precision_counts.most_common()
        ],
        "validity_distribution": [
            {"label": label, "count": count}
            for label, count in sorted(validity.items())
        ],
        "unresolved_samples": [
            {"value": value, "count": count}
            for value, count in unresolved.most_common(10)
        ],
        "companies": [
            {"label": label, "count": count} for label, count in companies.most_common(10)
        ],
        "company_count": len(companies),
        "holder_count": len(holders),
        "multi_holder_count": multi_holders,
    }


PANEL_BUILDERS = {
    "denggao": _denggao_panels,
    "roster": _roster_panels,
    "course": _course_panels,
    "certificates": _certificate_panels,
}


# ---------------------------------------------------------------------------
def build_project_data(settings: Settings, project: Project) -> dict[str, Any]:
    """构建单个项目的数据包。"""
    store = FactStore(settings)
    versions = store.list_versions(project.dataset_id)
    latest = _latest(versions)

    kpis, trends = _kpis(store, project, versions, settings)
    builder = PANEL_BUILDERS.get(project.id)
    panels = builder(store, project, versions, settings) if builder else {}

    return {
        "project": project.as_dict(),
        "period": load_period_config(settings.period_config_path).as_dict(),
        "batch": latest["batch"] if latest else None,
        "previous_batch": max(
            (v["batch"] for v in versions if latest and v["batch"] < latest["batch"]),
            default=None,
        ),
        "record_count": latest["record_count"] if latest else 0,
        "dropped_rows": latest["dropped_rows"] if latest else 0,
        "batch_count": len(versions),
        "batches": [v["batch"] for v in versions],
        "kpis": kpis,
        "trends": trends,
        "anomalies": _anomaly_data(store, project, versions, settings),
        **panels,
    }


def build_all_projects(settings: Settings) -> dict[str, Any]:
    """所有项目的数据包（供项目切换页 / 总览使用）。"""
    projects = load_projects(settings)
    return {
        "projects": [p.as_dict() for p in projects],
        "data": {p.id: build_project_data(settings, p) for p in projects},
    }
