"""阶段三：固定查询 API（FastAPI）。

四类查询能力，按「从粗到细、最后落到证据」的顺序组织：

  /metrics/compare   指标对比 —— 指标是多少、变了多少、为什么变（归因）
  /diffs/summary     差分汇总 —— 本期一共发生了多少事、哪个字段最活跃
  /diffs/records     明细下钻 —— 具体是哪些人、哪些字段、怎么变的（分页/过滤）
  /evidence/{id}     证据追溯 —— 这条事件来自哪个文件、第几行、当时的值是什么

所有端点都是**只读**。查询上限在 API 层与 MCP 层双重收口。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .attribution import attribute_metric
from .dashboard import dashboard_data
from .insights import router as insights_router
from .project_dashboard import build_all_projects, build_project_data
from .projects import find_project, load_projects, project_page_path
from .settings import get_settings
from .store import FactStore

MAX_LIMIT = 500


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    store().init_schema()
    yield


app = FastAPI(
    title="云学通 BI 差分服务",
    version="0.1.0",
    description="Excel 差分 -> 事实存储 -> 固定查询 API -> MCP -> 自然语言 Agent",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# 各项目看板共享的设计令牌与图表函数（放在 web/assets 下）
_ASSETS_DIR = get_settings().project_root / "web" / "assets"
if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")


def store() -> FactStore:
    return FactStore(get_settings())


def _audit(tool: str, params: Any, rows: int | None, t0: float,
           actor: str = "http", status: str = "ok", message: str | None = None) -> None:
    try:
        store().audit(tool, params, rows, status=status, actor=actor,
                      duration_ms=int((time.perf_counter() - t0) * 1000), message=message)
    except Exception:
        pass  # 审计失败绝不影响主流程


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# --------------------------------------------------------------------------
@app.get("/health", summary="健康检查")
def health() -> dict[str, Any]:
    from .jobs import JOBS, job_health

    s = store()
    versions = s.list_versions()
    jobs = []
    for job_id in JOBS:
        try:
            health_info = job_health(s, job_id, days=7)
        except Exception:  # 健康检查不能因为一块坏了整体 500
            continue
        jobs.append({
            "job_id": job_id,
            "healthy": health_info["healthy"],
            "last_run_date": health_info["last_run_date"],
            "missing": health_info["missing"],
            "unhealthy": health_info["unhealthy"],
            # 从未跑过（新库/首次部署）不算故障；否则只有「最近一个应跑日期缺记录」
            # 才拉低 status —— 历史缺口请用 /jobs/status 看日历，不该让 /health 长期告警。
            "never_run": health_info["last_run_date"] is None,
            "latest_due": health_info["latest_due"],
            "lagging": health_info["latest_due"] in health_info["missing"],
        })
    ran_jobs = [j for j in jobs if not j["never_run"]]
    healthy = all((not j["lagging"]) and not j["unhealthy"] for j in ran_jobs)
    return {
        "status": "ok" if healthy else "degraded",
        "db": s.backend if s.backend == "postgresql" else str(s.db_path),
        "db_backend": s.backend,
        "batches": [v["batch"] for v in versions],
        "records": {v["batch"]: v["record_count"] for v in versions},
        "jobs": jobs,
    }


@app.get("/dashboard/data", summary="驾驶舱图表聚合数据")
def dashboard_data_endpoint() -> dict[str, Any]:
    t0 = time.perf_counter()
    settings = get_settings()
    result = dashboard_data(settings)
    row_count = sum(
        dataset.get("record_count", dataset.get("detail_rows", 0))
        for dataset in result["datasets"].values()
    )
    _audit("dashboard_data", {}, row_count, t0)
    return result


@app.get("/versions", summary="版本列表")
def versions() -> dict[str, Any]:
    t0 = time.perf_counter()
    items = store().list_versions()
    _audit("list_versions", {}, len(items), t0)
    return {"items": items, "count": len(items)}


@app.get("/courses/enrollments", summary="按课程数查询学员报名明细")
def course_enrollments(
    batch: str | None = Query(None, description="课程明细批次；省略时使用最新批次"),
    min_courses: int = Query(2, ge=1, le=100, description="至少报名的不同课程数"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        result = store().course_enrollment_stats(batch, min_courses, limit, offset)
    except LookupError as exc:
        _audit(
            "course_enrollments",
            {"batch": batch, "min_courses": min_courses, "limit": limit, "offset": offset},
            None,
            t0,
            status="error",
            message=str(exc),
        )
        raise HTTPException(404, str(exc)) from exc
    _audit(
        "course_enrollments",
        {"batch": result["batch"], "min_courses": min_courses, "limit": limit, "offset": offset},
        result["returned"],
        t0,
    )
    return result


@app.get("/metrics/buckets", summary="分桶指标（按维度切分的分布，如每月报名门数人数）")
def metric_buckets(
    dataset_id: str = Query("course_monthly_summary",
                            description="数据集 id（分桶指标按数据集隔离）"),
    metric_key: str = Query("monthly_enroll_depth", description="分桶指标 key"),
    batch: str | None = Query(None, description="批次；省略时使用该数据集最新批次"),
) -> dict[str, Any]:
    """返回「维度值 × 分档 -> 人数」的交叉分布。

    例：metric_key=monthly_enroll_depth 返回
      {"buckets":[{"bucket":"2026年 3月","groups":{"1门":9,"2门":2,...},"total":18}],
       "group_totals":{"1门":117,...},"grand_total":230}
    """
    t0 = time.perf_counter()
    s = store()
    if batch is None:
        versions = s.list_versions(dataset_id)
        if not versions:
            raise HTTPException(404, f"数据集没有已入库批次: {dataset_id}")
        batch = max(v["batch"] for v in versions)

    matrix = s.metric_bucket_matrix(batch, metric_key, dataset_id)
    if not matrix["buckets"]:
        raise HTTPException(
            404,
            f"该批次没有分桶指标 {metric_key}（数据集 {dataset_id} / 批次 {batch}）；"
            f"请先运行 python -m bi.cli seed 重新入库",
        )
    _audit("metric_buckets",
           {"dataset_id": dataset_id, "metric_key": metric_key, "batch": batch},
           len(matrix["buckets"]), t0)
    return {**matrix, "batch": batch, "dataset_id": dataset_id}


@app.get("/metrics/compare", summary="指标对比（含变化归因）")
def metrics_compare(
    from_batch: str = Query(..., alias="from", description="基期批次，如 20261012"),
    to_batch: str = Query(..., alias="to", description="对比期批次，如 20261013"),
    attribute: bool = Query(True, description="是否计算变化归因"),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    s = store()
    m_from = s.metric_snapshot(from_batch)
    m_to = s.metric_snapshot(to_batch)
    if not m_from:
        raise HTTPException(404, f"批次不存在或无指标快照: {from_batch}")
    if not m_to:
        raise HTTPException(404, f"批次不存在或无指标快照: {to_batch}")

    items = []
    for key, cur in m_to.items():
        prev = m_from.get(key, {})
        v1, v2 = prev.get("value"), cur.get("value")
        delta = round(v2 - v1, 6) if (v1 is not None and v2 is not None) else None
        pct = round(delta / v1 * 100, 2) if (delta is not None and v1) else None
        item: dict[str, Any] = {
            "metric_key": key,
            "metric_name": cur.get("metric_name"),
            "unit": cur.get("unit") or "",
            "decimals": cur.get("decimals") or 0,
            "value_from": v1,
            "value_to": v2,
            "delta": delta,
            "delta_pct": pct,
            "direction": ("up" if (delta or 0) > 0 else "down" if (delta or 0) < 0 else "flat"),
            "desc": cur.get("descr") or "",
        }
        if attribute:
            item["attribution"] = attribute_metric(s, key, from_batch, to_batch)
        items.append(item)

    _audit("metrics_compare", {"from": from_batch, "to": to_batch, "attribute": attribute},
           len(items), t0)
    return {"from": from_batch, "to": to_batch, "items": items}


@app.get("/diffs/summary", summary="差分汇总")
def diffs_summary(
    from_batch: str = Query(..., alias="from"),
    to_batch: str = Query(..., alias="to"),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    s = store()
    if not s.version_id_of(from_batch) or not s.version_id_of(to_batch):
        raise HTTPException(404, "批次不存在")
    out = s.diff_summary(from_batch, to_batch)
    out["total_events"] = sum(out["by_type"].values())
    _audit("diffs_summary", {"from": from_batch, "to": to_batch}, out["total_events"], t0)
    return out


@app.get("/diffs/records", summary="差分明细（下钻）")
def diffs_records(
    from_batch: str = Query(..., alias="from"),
    to_batch: str = Query(..., alias="to"),
    event_type: str | None = Query(None, description="added / removed / changed"),
    field_name: str | None = Query(None, description="如 分数、考试状态"),
    severity: str | None = Query(None, description="high / medium / low / info"),
    record_key: str | None = Query(None, description="按人过滤：证件号码|姓名"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    s = store()
    if event_type and event_type not in {"added", "removed", "changed"}:
        raise HTTPException(400, "event_type 必须是 added / removed / changed")

    rows, total = s.diff_events(from_batch, to_batch, event_type=event_type,
                                field_name=field_name, record_key=record_key,
                                limit=limit, offset=offset)
    if severity:
        rows = [r for r in rows if r["severity"] == severity]
    _audit("diffs_records",
           {"from": from_batch, "to": to_batch, "event_type": event_type,
            "field_name": field_name, "limit": limit, "offset": offset},
           len(rows), t0)
    return {"total": total, "limit": limit, "offset": offset,
            "returned": len(rows), "items": rows}


@app.get("/evidence/{event_id}", summary="证据追溯")
def evidence(event_id: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    s = store()
    out = s.evidence(event_id)
    if not out:
        raise HTTPException(404, f"事件不存在: {event_id}")
    _audit("evidence", {"event_id": event_id}, 1, t0)
    return out


@app.get("/anomalies", summary="异常清单")
def anomalies(
    batch: str = Query(..., description="批次"),
    severity: str | None = Query(None),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    s = store()
    items = s.list_anomalies(batch, limit=limit, severity=severity)
    counts = s.anomaly_counts(batch)
    _audit("anomalies", {"batch": batch, "severity": severity}, len(items), t0)
    return {"batch": batch, "counts": counts, "total": sum(counts.values()),
            "returned": len(items), "items": items}


@app.get("/audit", summary="审计日志")
def audit(limit: int = Query(50, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    items = store().recent_audit(limit)
    return {"items": items, "count": len(items)}


@app.get("/jobs/status", summary="定时任务执行状态（漏跑 = 日历里的 missed）")
def jobs_status(
    job_id: str | None = Query(None, description="任务 id；省略则返回全部已注册任务"),
    days: int = Query(14, ge=1, le=90, description="回看天数"),
) -> dict[str, Any]:
    from .jobs import JOBS, job_health

    t0 = time.perf_counter()
    s = store()
    items = []
    for jid in ([job_id] if job_id else list(JOBS)):
        try:
            items.append(job_health(s, jid, days))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
    _audit("jobs_status", {"job_id": job_id, "days": days}, len(items), t0)
    return {
        "items": items,
        "count": len(items),
        "healthy": all(item["healthy"] for item in items),
    }


# --------------------------------------------------------------------------
# 阶段五：自然语言 Agent
# --------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str
    from_batch: str | None = None
    to_batch: str | None = None


@app.post("/agent/ask", summary="自然语言提问（多轮查询闭环）")
def agent_ask(req: AskRequest) -> dict[str, Any]:
    from .agent import BIAgent

    t0 = time.perf_counter()
    out = BIAgent().ask(req.question, req.from_batch, req.to_batch)
    _audit("agent_ask", {"question": req.question}, out.get("step_count"), t0,
           status="ok" if not out.get("error") else "error")
    return out


def _serve_page(filename: str) -> HTMLResponse:
    """读取 web/ 下的静态页面；文件缺失时返回 404。"""
    page = get_settings().project_root / "web" / filename
    if not page.exists():
        return HTMLResponse(f"<h1>web/{filename} 不存在</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse, summary="首页（业务运营驾驶舱）")
def index() -> HTMLResponse:
    """站点首页 = 业务运营驾驶舱；对话页（智能问答）移至 /chat。"""
    return _serve_page("dashboard.html")


@app.get("/dashboard", response_class=HTMLResponse, summary="业务运营驾驶舱")
def dashboard_page() -> HTMLResponse:
    return _serve_page("dashboard.html")


@app.get("/chat", response_class=HTMLResponse, summary="对话页（智能问答）")
def chat_page() -> HTMLResponse:
    return _serve_page("index.html")


# --------------------------------------------------------------------------
# 阶段三·扩展：按项目分页面看板
# --------------------------------------------------------------------------
@app.get("/projects", summary="看板项目清单")
def projects_list() -> dict[str, Any]:
    """所有已注册的业务项目（每个项目一张独立看板页面）。"""
    t0 = time.perf_counter()
    items = [p.as_dict() for p in load_projects(get_settings())]
    _audit("projects_list", {}, len(items), t0)
    return {"items": items, "count": len(items)}


@app.get("/dashboard/all/data", summary="所有项目的看板数据")
def all_projects_dashboard_data() -> dict[str, Any]:
    """全部项目的数据包（项目总览页用）。"""
    t0 = time.perf_counter()
    result = build_all_projects(get_settings())
    _audit("all_projects_dashboard_data", {}, len(result.get("data", {})), t0)
    return result


@app.get("/dashboard/{project_id}/data", summary="指定项目的看板数据")
def project_dashboard_data(project_id: str) -> dict[str, Any]:
    """单个项目的完整看板数据包（KPI / 趋势 / 专属面板 / 异常）。"""
    t0 = time.perf_counter()
    settings = get_settings()
    project = find_project(settings, project_id)
    if project is None:
        _audit("project_dashboard_data", {"project_id": project_id}, None, t0,
               status="error", message="未知项目")
        raise HTTPException(404, f"未知项目: {project_id}")
    result = build_project_data(settings, project)
    _audit("project_dashboard_data", {"project_id": project_id},
           result.get("record_count"), t0)
    return result


@app.get("/dashboard/{project_id}", response_class=HTMLResponse,
         summary="按项目区分的看板页面")
def project_dashboard_page(project_id: str) -> HTMLResponse:
    """每个项目一张独立页面（布局/配色/KPI 都按项目定制）。"""
    settings = get_settings()
    project = find_project(settings, project_id)
    if project is None:
        return HTMLResponse(f"<h1>未知项目: {project_id}</h1>", status_code=404)
    page = project_page_path(settings, project)
    if not page.exists():
        return HTMLResponse(f"<h1>页面不存在: {project.page}</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/dashboards", response_class=HTMLResponse, summary="项目看板总览（切换入口）")
def dashboards_index() -> HTMLResponse:
    """项目切换页：列出所有项目及其入口。"""
    settings = get_settings()
    page = settings.project_root / "web" / "dashboards.html"
    if not page.exists():
        return HTMLResponse("<h1>web/dashboards.html 不存在</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


app.include_router(insights_router)


@app.get("/insights", response_class=HTMLResponse, summary="图形优先 dashboard")
def insights_page() -> HTMLResponse:
    page = get_settings().project_root / "web" / "insights.html"
    if not page.exists():
        return HTMLResponse("<h1>web/insights.html 不存在</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _s = get_settings()
    uvicorn.run(app, host=_s.api_host, port=_s.api_port)
