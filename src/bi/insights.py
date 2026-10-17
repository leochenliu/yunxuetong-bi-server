"""4 个业务痛点的聚合端点（/insights/data）。

为「图形优先」dashboard 提供的 JSON：
  痛点 1 续费漏斗    （理论到期 + 已续 + 4 类未续分布）
  痛点 2 进度风险    （4 级分布 + 高风险与失联名单）
  痛点 3 老师画像    （24 个老师 × 5 维度）
  痛点 4 已签未买单  （5 级漏斗 + 本月新增付款率）

数据基于 D2（当前最新批次）。空数据场景（如续费 0 人）显式返回 0
并说明原因，**不允许编造数字**。
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from typing import Any

from fastapi import APIRouter, Query

from .config import load_dataset_config
from .derive import parse_class_code
from .ingest import ingest
from .settings import get_settings
from .store import FactStore

router = APIRouter()

TODAY = _dt.date(2026, 10, 17)
VALIDITY_YEARS = 3  # 行业经验：低压/登高/焊工证书 3 年


def _to_int(v: Any) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 痛点 1：续费漏斗
# ---------------------------------------------------------------------------
def _renewal_funnel(rows, cfg) -> dict[str, Any]:
    """理论到期 = 首次报名 + 3 年；D2 当期是否续费 = 仍在 D2 名单 + 状态活跃。"""
    due_count = 0
    renewed = 0
    not_renewed_by_mode: Counter = Counter()
    sample = {"已续": [], "未续培训中": [], "未续失联": [], "未续退费": []}

    for r in rows:
        code = r.get("开班编号")
        if not code:
            continue
        p = parse_class_code(code, cfg)
        if not p:
            continue
        try:
            due = _dt.date(p.year + VALIDITY_YEARS, p.month, p.day)
        except ValueError:
            continue
        if due > TODAY:
            continue
        due_count += 1
        status = str(r.get("考试状态") or "").strip()
        if status == "已通过":
            renewed += 1
            if len(sample["已续"]) < 5:
                sample["已续"].append(r.get("姓名"))
        elif status in ("待实操约考", "理论考试阶段", "实操考试阶段"):
            not_renewed_by_mode["培训中（未结业）"] += 1
            if len(sample["未续培训中"]) < 5:
                sample["未续培训中"].append(r.get("姓名"))
        elif status == "取消考试":
            not_renewed_by_mode["退费/取消"] += 1
            if len(sample["未续退费"]) < 5:
                sample["未续退费"].append(r.get("姓名"))
        elif status in ("未申请考试", ""):
            not_renewed_by_mode["失联/未启动"] += 1
            if len(sample["未续失联"]) < 5:
                sample["未续失联"].append(r.get("姓名"))
        else:
            not_renewed_by_mode["可结算/其他"] += 1

    return {
        "due_count": due_count,
        "renewed": renewed,
        "renewal_rate": (renewed / due_count * 100) if due_count else None,
        "not_renewed_modes": [
            {"mode": m, "count": c}
            for m, c in sorted(not_renewed_by_mode.items(), key=lambda kv: -kv[1])
        ],
        "sample": sample,
        "note": (
            f"基于「首次报名 + {VALIDITY_YEARS} 年」推导理论到期日"
            "（原 Excel 无「取证日期/证书编号」字段，本结果为代理）。"
            "D1 数据里所有人开班日期都在近一年，到期数为 0 是预期。",
        ),
    }


# ---------------------------------------------------------------------------
# 痛点 2：进度风险
# ---------------------------------------------------------------------------
def _progress_risk(rows) -> dict[str, Any]:
    buckets: Counter = Counter()
    high_risk: list[dict[str, Any]] = []
    lost: list[dict[str, Any]] = []
    behind: list[dict[str, Any]] = []

    for r in rows:
        status = str(r.get("考试状态") or "").strip()
        ed = r.get("考试日期")
        if status == "已通过":
            buckets["正常"] += 1
            continue
        if not ed:
            train_status = str(r.get("培训情况") or "").strip()
            if not train_status:
                buckets["失联"] += 1
                if len(lost) < 10:
                    lost.append({
                        "name": r.get("姓名"),
                        "id_masked": _mask_id(r.get("证件号码")),
                        "employer": r.get("工作单位"),
                        "subject": r.get("科目"),
                        "last_class": r.get("开班日期"),
                    })
                continue
            # 培训了但还没考
            d1 = r.get("培训第一天日期") or r.get("开班日期")
            days = _days_since(d1)
            if days is not None and days > 60:
                buckets["高风险"] += 1
                if len(high_risk) < 10:
                    high_risk.append({
                        "name": r.get("姓名"),
                        "id_masked": _mask_id(r.get("证件号码")),
                        "employer": r.get("工作单位"),
                        "subject": r.get("科目"),
                        "days_since_training": days,
                    })
            else:
                buckets["落后"] += 1
                if len(behind) < 10:
                    behind.append({
                        "name": r.get("姓名"),
                        "id_masked": _mask_id(r.get("证件号码")),
                        "employer": r.get("工作单位"),
                        "days_since_training": days or 0,
                    })
            continue
        # 有考试日期
        score = _to_int(r.get("分数"))
        if 0 < score < 60:
            buckets["落后"] += 1
        else:
            buckets["正常"] += 1
    return {
        "buckets": dict(buckets),
        "total": sum(buckets.values()),
        "high_risk": high_risk,
        "lost": lost,
        "behind_sample": behind,
        "rule_summary": (
            "高风险：培训 > 60 天未考 / 失联：无任何推进记录 / "
            "落后：有分但 < 60 / 30-60 天未考"
        ),
    }


def _days_since(date_str) -> int | None:
    if not date_str:
        return None
    s = str(date_str).strip()[:10]
    try:
        d = _dt.date.fromisoformat(s)
    except ValueError:
        return None
    return (TODAY - d).days


def _mask_id(v: Any) -> str:
    s = (str(v) if v else "").strip()
    if len(s) < 10:
        return "*" * len(s)
    return f"{s[:6]}****{s[-4:]}"


# ---------------------------------------------------------------------------
# 痛点 3：老师画像
# ---------------------------------------------------------------------------
def _teacher_workload(rows) -> dict[str, Any]:
    teachers: dict[str, list] = {}
    for r in rows:
        t = (str(r.get("负责老师") if r.get("负责老师") else "")).strip() or "(未指派)"
        teachers.setdefault(t, []).append(r)

    stats = []
    for t, rs in teachers.items():
        n_total = len(rs)
        n_class = len(set(r.get("开班编号") for r in rs if r.get("开班编号")))
        n_paid = sum(1 for r in rs if str(r.get("是否付款") or "").strip() == "已付款")
        n_passed = sum(1 for r in rs if str(r.get("考试状态") or "").strip() == "已通过")
        n_pending = sum(1 for r in rs if not r.get("考试日期"))
        amount = sum(_to_int(r.get("金额")) for r in rs)
        pass_rate = (n_passed / n_total * 100) if n_total else 0
        # 多样性指数：批次 / 学员（越大说明管得越散）
        diversity = (n_class / n_total) if n_total else 0
        stats.append({
            "teacher": t,
            "n_total": n_total,
            "n_class": n_class,
            "n_paid": n_paid,
            "n_passed": n_passed,
            "n_pending": n_pending,
            "amount": amount,
            "pass_rate": round(pass_rate, 1),
            "diversity": round(diversity * 100, 1),
        })
    stats.sort(key=lambda x: -x["n_total"])

    # 计算阈值（基于已指派老师，排除未指派类的干扰）
    assigned = [s for s in stats if s["teacher"] != "(未指派)"]
    n_totals = sorted([s["n_total"] for s in assigned])
    median = n_totals[len(n_totals) // 2] if n_totals else 0
    # 阈值 = 2 倍中位数（保守判定：超过行业经验平均太多算过载）
    high_threshold = max(median * 2, 30) if median else 30
    overloaded = [s for s in assigned if s["n_total"] >= high_threshold]
    unassigned = next((s for s in stats if s["teacher"] == "(未指派)"), None)

    return {
        "teachers": stats,
        "overloaded": overloaded,
        "unassigned": unassigned,
        "threshold": high_threshold,
        "note": f"过载阈值 = 中位数 × 2（{high_threshold} 人）",
    }


# ---------------------------------------------------------------------------
# 痛点 4：已签未买单漏斗
# ---------------------------------------------------------------------------
def _signed_unpaid_funnel(rows) -> dict[str, Any]:
    total = len(rows)
    trained = sum(1 for r in rows
                  if r.get("考试日期") and str(r.get("考试状态") or "").strip() != "取消考试")
    scored = sum(1 for r in rows if str(r.get("分数") or "").strip() not in ("",))
    passed = sum(1 for r in rows if str(r.get("考试状态") or "").strip() == "已通过")
    paid = sum(1 for r in rows if str(r.get("是否付款") or "").strip() == "已付款")

    stages = [
        {"stage": "已报名", "count": total},
        {"stage": "已培训", "count": trained},
        {"stage": "已出分", "count": scored},
        {"stage": "已通过", "count": passed},
        {"stage": "已付款", "count": paid},
    ]
    leakage = []
    for i in range(1, len(stages)):
        prev, cur = stages[i - 1]["count"], stages[i]["count"]
        if prev == 0:
            leakage.append({"from": stages[i - 1]["stage"], "to": stages[i]["stage"], "rate": None})
        else:
            leakage.append({
                "from": stages[i - 1]["stage"],
                "to": stages[i]["stage"],
                "lost": prev - cur,
                "rate": round((prev - cur) / prev * 100, 1),
            })

    return {
        "stages": stages,
        "leakage": leakage,
        "bottleneck": max(
            (l for l in leakage if l.get("rate") is not None),
            key=lambda x: x["rate"], default=None,
        ),
        "note": "原 Excel 仅为「已签约客户」数据。真正的「潜在客户池」需 CRM 接入。",
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/insights/data", summary="4 个痛点的聚合数据")
def insights_data(batch: str = Query(None, description="批次（默认最新）")) -> dict[str, Any]:
    t0 = __import__("time").perf_counter()
    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    fs = FactStore(settings)

    if batch is None:
        versions = fs.list_versions()
        if versions:
            batch = versions[-1]["batch"]

    rows = ingest(batch, settings=settings, cfg=cfg).dataset.rows

    renewal = _renewal_funnel(rows, cfg)
    progress = _progress_risk(rows)
    teachers = _teacher_workload(rows)
    funnel = _signed_unpaid_funnel(rows)

<<<<<<< HEAD
    # 时间序列：5 个核心指标 × 全部已有版本（按批次升序）
    from .config import load_metrics_config
    metric_defs = load_metrics_config(settings.metrics_config_path)
    timeseries: dict[str, list[dict[str, Any]]] = {}
    for m in metric_defs:
        ts = fs.metric_timeseries(m.key)
        if ts:
            timeseries[m.key] = ts

=======
>>>>>>> origin/main
    from .store import FactStore as _FS
    _FS(settings).audit("insights_data", {"batch": batch}, 4, duration_ms=int((__import__("time").perf_counter() - t0) * 1000))

    return {
        "batch": batch,
        "today": TODAY.isoformat(),
        "renewal": renewal,
        "progress": progress,
        "teachers": teachers,
        "funnel": funnel,
<<<<<<< HEAD
        "timeseries": timeseries,
        "demo_batches": [v["batch"] for v in fs.list_versions()
                          if v.get("is_demo")],
    }


@router.get("/metrics/timeseries", summary="单指标多期时间序列")
def metrics_timeseries(
    key: str = Query(..., description="指标 key，如 enrolled_total"),
    from_batch: str = Query(None, description="起始批次（可选）"),
    to_batch: str = Query(None, description="结束批次（可选）"),
) -> dict[str, Any]:
    """从 metric_snapshots 表读取某指标的多期值。
    例：GET /metrics/timeseries?key=enrolled_total"""
    settings = get_settings()
    fs = FactStore(settings)
    rows = fs.metric_timeseries(key, from_batch=from_batch, to_batch=to_batch)
    return {"metric_key": key, "from": from_batch, "to": to_batch, "points": rows}
=======
    }
>>>>>>> origin/main
