"""Privacy-safe aggregates used by the graphical business dashboard."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .config import load_metrics_config, load_period_config
from .period import group_by_period
from .settings import Settings
from .store import FactStore
from .values import to_number


DENGGAO = "denggao"
ROSTER = "personnel_roster"
COURSE_MONTHLY = "course_monthly_summary"
CORE_METRIC_KEYS = ("enrolled_total", "exam_scheduled", "exam_passed", "avg_score", "paid_count")
PII_FIELDS = {"姓名", "证件号码", "联系电话", "手机", "id_number", "phone", "name"}


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


def _count_values(rows: list[dict[str, Any]], field: str, *, empty_label: str = "未填写") -> list[dict[str, Any]]:
    counts = Counter(str(row.get(field) or "").strip() or empty_label for row in rows)
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _number(value: Any) -> float | None:
    return to_number(value)


def _latest(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(versions, key=lambda version: version["batch"]) if versions else None


def _metrics(
    store: FactStore,
    settings: Settings,
    versions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    latest = _latest(versions)
    if latest is None:
        return [], []

    metric_defs = load_metrics_config(settings.metrics_config_path)
    definitions = {metric.key: metric for metric in metric_defs}
    previous = max(
        (version for version in versions if version["batch"] < latest["batch"]),
        key=lambda version: version["batch"],
        default=None,
    )
    current_values = store.metric_snapshot(latest["batch"])
    previous_values = store.metric_snapshot(previous["batch"]) if previous else {}
    demo_batches = {
        version["batch"] for version in versions if version.get("is_demo")
    }

    kpis = []
    trends = []
    for key in CORE_METRIC_KEYS:
        definition = definitions.get(key)
        current = current_values.get(key)
        if definition is None or current is None:
            continue
        previous_value = previous_values.get(key, {}).get("value")
        value = current["value"]
        delta = value - previous_value if value is not None and previous_value is not None else None
        kpis.append({
            "key": key,
            "label": definition.name,
            "value": value,
            "unit": definition.unit,
            "decimals": definition.decimals,
            "delta": delta,
            "previous_value": previous_value,
        })
        points = store.metric_timeseries(key, dataset_id=DENGGAO)
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


def _anomaly_data(
    store: FactStore,
    dataset_id: str,
    versions: list[dict[str, Any]],
    settings: Settings,
) -> dict[str, Any]:
    if not versions:
        return {"total": 0, "by_rule": [], "trend": []}

    rules: dict[str, str] = {}
    config_path = (
        settings.dataset_config_path
        if dataset_id == DENGGAO
        else settings.config_dir / "datasets" / f"{dataset_id}.yaml"
    )
    if config_path.exists():
        from .config import load_dataset_config

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
            (dataset_id,),
        ).fetchall()

    latest_batch = _latest(versions)["batch"]  # versions was checked above
    latest_rules: Counter[str] = Counter()
    by_batch: dict[str, dict[str, Any]] = {
        version["batch"]: {"batch": version["batch"], "total": 0, "high": 0}
        for version in versions
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

    return {
        "total": sum(latest_rules.values()),
        "by_rule": [
            {"rule_id": rule_id, "label": rules.get(rule_id, rule_id), "count": count}
            for rule_id, count in latest_rules.most_common()
        ],
        "trend": [
            {**by_batch[version["batch"]], "is_demo": bool(version.get("is_demo"))}
            for version in versions
        ],
    }


def _denggao_data(
    store: FactStore,
    settings: Settings,
    versions: list[dict[str, Any]],
    period=None,
) -> dict[str, Any]:
    latest = _latest(versions)
    if latest is None:
        return {"batch": None, "record_count": 0, "kpis": [], "trends": []}

    rows = _load_rows(store, DENGGAO, latest["batch"])
    kpis, trends = _metrics(store, settings, versions)

    enrollment = Counter(
        str(row.get("报名月份") or "").strip()
        for row in rows if row.get("报名月份")
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

    # 自然时间周期口径（config/period.yaml，单位：天）——页面上的「按周期」分布
    enrollment_by_period: list[dict[str, Any]] = []
    period_unassigned = 0
    if period is not None:
        enrollment_by_period, period_unassigned = group_by_period(
            rows, "报名日期", period, status_field="考试状态"
        )

    scores = [score for row in rows if (score := _number(row.get("分数"))) is not None]
    score_bands = [
        {"label": "低于 60", "count": sum(score < 60 for score in scores)},
        {"label": "60–69", "count": sum(60 <= score < 70 for score in scores)},
        {"label": "70–79", "count": sum(70 <= score < 80 for score in scores)},
        {"label": "80–89", "count": sum(80 <= score < 90 for score in scores)},
        {"label": "90–100", "count": sum(90 <= score <= 100 for score in scores)},
        {"label": "超出 0–100", "count": sum(score < 0 or score > 100 for score in scores)},
        {"label": "无分数", "count": len(rows) - len(scores)},
    ]
    amounts = [_number(row.get("金额")) for row in rows]
    known_amounts = [amount for amount in amounts if amount is not None]
    attendance = []
    for label, field in (
        ("培训第一天", "培训第一天签到情况"),
        ("培训第二天", "培训第二日签到情况"),
    ):
        attendance.append({
            "label": label,
            "values": _count_values(rows, field),
        })
    retries = Counter(
        str(row.get("补考次数") or "").strip() or "未填写"
        for row in rows
    )
    return {
        "batch": latest["batch"],
        "previous_batch": max(
            (version["batch"] for version in versions if version["batch"] < latest["batch"]),
            default=None,
        ),
        "record_count": latest["record_count"],
        "dropped_rows": latest["dropped_rows"],
        "kpis": kpis,
        "trends": trends,
        "enrollment_by_month": enrollment_by_month,
        "enrollment_by_period": enrollment_by_period,
        "period_unassigned": period_unassigned,
        "exam_status": _count_values(rows, "考试状态"),
        "exam_schedule": [
            {
                "label": label,
                "count": sum(bool(str(row.get("考试日期") or "").strip()) == has_date for row in rows),
            }
            for label, has_date in (("有考试日期", True), ("无考试日期", False))
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
        "retry_distribution": [
            {"label": label, "count": count}
            for label, count in sorted(retries.items(), key=lambda item: (-item[1], item[0]))
        ],
        "deferred_cert_count": sum(
            int(_number(row.get("延期换证人数")) or 0)
            for row in rows
        ),
        "anomalies": _anomaly_data(store, DENGGAO, versions, settings),
    }


def _roster_data(
    store: FactStore,
    versions: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = _latest(versions)
    if latest is None:
        return {"batch": None, "record_count": 0}
    rows = _load_rows(store, ROSTER, latest["batch"])
    courses: Counter[str] = Counter()
    employers: Counter[str] = Counter()
    for row in rows:
        for course in str(row.get("课程") or "").split(","):
            course = course.strip()
            if course:
                courses[course] += 1
        employer = str(row.get("工作单位") or "").strip()
        if employer:
            employers[employer] += 1
    return {
        "batch": latest["batch"],
        "record_count": latest["record_count"],
        "education": _count_values(rows, "最高学历"),
        "title_level": _count_values(rows, "职称等级"),
        "payment_status": _count_values(rows, "缴费情况"),
        "courses": [
            {"label": label, "count": count}
            for label, count in courses.most_common(10)
        ],
        "employers": [
            {"label": label, "count": count}
            for label, count in employers.most_common(10)
        ],
        "employer_count": len({
            str(row.get("工作单位") or "").strip()
            for row in rows if row.get("工作单位")
        }),
    }


def _course_monthly_data(
    store: FactStore,
    versions: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = _latest(versions)
    if latest is None:
        return {"batch": None, "detail_rows": 0, "unique_learners": 0}

    rows = _load_rows(store, COURSE_MONTHLY, latest["batch"])
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
        key=lambda value: int("".join(char for char in value if char.isdigit()) or "99"),
    )
    courses = sorted(course_rows, key=lambda value: (-course_rows[value], value))[:12]
    return {
        "batch": latest["batch"],
        "detail_rows": latest["record_count"],
        "unique_learners": len(unique_people),
        "months": [
            {
                "label": month,
                "detail_rows": sum(1 for row in rows if (str(row.get("月份") or "").strip() or "未填写") == month),
                "unique_learners": len(month_people[month]),
            }
            for month in months
        ],
        "courses": [
            {
                "label": course,
                "detail_rows": course_rows[course],
                "unique_learners": len(course_people[course]),
            }
            for course in courses
        ],
        "heatmap": [
            {"course": course, "month": month, "count": heatmap[(course, month)]}
            for course in courses for month in months
        ],
        "payment_status": _count_values(rows, "缴费情况"),
    }


def dashboard_data(settings: Settings) -> dict[str, Any]:
    store = FactStore(settings)
    period = load_period_config(settings.period_config_path)
    denggao_versions = store.list_versions(DENGGAO)
    roster_versions = store.list_versions(ROSTER)
    course_versions = store.list_versions(COURSE_MONTHLY)
    return {
        "period": period.as_dict(),
        "datasets": {
            DENGGAO: _denggao_data(store, settings, denggao_versions, period),
            ROSTER: _roster_data(store, roster_versions),
            COURSE_MONTHLY: _course_monthly_data(store, course_versions),
        },
    }
