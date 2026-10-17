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
from pydantic import BaseModel

from .attribution import attribute_metric
from .dashboard import dashboard_data
from .insights import router as insights_router
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
    s = store()
    versions = s.list_versions()
    return {
        "status": "ok",
        "db": s.backend if s.backend == "postgresql" else str(s.db_path),
        "db_backend": s.backend,
        "batches": [v["batch"] for v in versions],
        "records": {v["batch"]: v["record_count"] for v in versions},
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


@app.get("/", response_class=HTMLResponse, summary="对话页")
def index() -> HTMLResponse:
    page = get_settings().project_root / "web" / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>web/index.html 不存在</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/dashboard", response_class=HTMLResponse, summary="业务运营驾驶舱")
def dashboard_page() -> HTMLResponse:
    page = get_settings().project_root / "web" / "dashboard.html"
    if not page.exists():
        return HTMLResponse("<h1>web/dashboard.html 不存在</h1>", status_code=404)
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
