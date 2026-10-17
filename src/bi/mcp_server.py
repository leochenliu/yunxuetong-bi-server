"""阶段四：把**已验证的业务 API** 暴露为 MCP 工具。

三项硬约束（阶段四的要求）：
  1. **只读**：只注册 GET 类查询工具，不存在任何写工具；`assert_readonly()` 可自检。
  2. **查询限制**：单次返回条数被 `BI_MCP_MAX_LIMIT` 硬截断，调用方无法绕过；
     参数取值也走白名单校验（如 event_type 只能是 added/removed/changed）。
  3. **审计日志**：每次调用都写 audit_log（工具名、参数、返回行数、耗时、状态、调用方）。

设计说明：工具不直接读数据库，而是调用第三阶段那套**已被 pytest 覆盖**的 HTTP API，
保证「MCP 暴露的能力」与「已验证的能力」是同一份实现。
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from .settings import get_settings
from .store import FactStore

try:  # fastmcp 是可选的：未安装时本模块的工具函数仍可被测试与直接调用
    from fastmcp import FastMCP

    HAS_FASTMCP = True
except Exception:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment]
    HAS_FASTMCP = False

# 写入类关键词：任何工具名命中即视为违反只读约束
WRITE_KEYWORDS = ("insert", "update", "delete", "drop", "create", "set_", "put", "post", "patch", "write")
ALLOWED_EVENT_TYPES = {"added", "removed", "changed"}
ALLOWED_SEVERITIES = {"high", "medium", "low", "info"}
_BATCH_RE = re.compile(r"^\d{6,10}$")


# --------------------------------------------------------------------------
# 查询客户端
# --------------------------------------------------------------------------
class BIQueryClient:
    """默认走 HTTP API；测试可注入 ASGI 客户端（免起服务）。"""

    def __init__(self, base_url: str | None = None, sdk_client: Any = None):
        s = get_settings()
        self.base_url = (base_url or f"http://{s.api_host}:{s.api_port}").rstrip("/")
        self._sdk = sdk_client

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if self._sdk is not None:
            r = self._sdk.get(path, params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"API {path} 返回 {r.status_code}: {r.text[:200]}")
            return r.json()
        with httpx.Client(timeout=15.0) as c:
            r = c.get(self.base_url + path, params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"API {path} 返回 {r.status_code}: {r.text[:200]}")
            return r.json()


_client_override: BIQueryClient | None = None


def set_query_client(c: BIQueryClient | None) -> None:
    global _client_override
    _client_override = c


def _client() -> BIQueryClient:
    return _client_override or BIQueryClient()


# --------------------------------------------------------------------------
# 审计与限额
# --------------------------------------------------------------------------
def _audit(tool: str, params: Any, rows: int | None, t0: float,
           status: str = "ok", message: str | None = None, actor: str = "mcp") -> None:
    try:
        FactStore(get_settings()).audit(
            tool, params, rows, status=status, actor=actor,
            duration_ms=int((time.perf_counter() - t0) * 1000), message=message,
        )
    except Exception:
        pass  # 审计失败不影响主流程


def _clamp_limit(limit: int | None, default: int = 50) -> tuple[int, bool, int]:
    """返回 (生效上限, 是否被截断, 配置的硬上限)。"""
    maxl = int(get_settings().mcp_max_limit or 200)
    if limit is None:
        return min(default, maxl), False, maxl
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是整数") from None
    if n < 1:
        raise ValueError("limit 必须 >= 1")
    if n > maxl:
        return maxl, True, maxl
    return n, False, maxl


def _check_batch(*batches: str) -> None:
    for b in batches:
        if not b or not _BATCH_RE.match(str(b)):
            raise ValueError(f"批次格式不合法: {b!r}（应为形如 20261012 的日期串）")


# --------------------------------------------------------------------------
# 工具实现（纯函数，可被直接测试）
# --------------------------------------------------------------------------
def bi_list_batches() -> dict[str, Any]:
    """列出全部已入站批次及其人数、源文件、采集时间。用于确认可选的时间范围。"""
    t0 = time.perf_counter()
    try:
        out = _client().get("/versions")
        items = out.get("items", [])
        _audit("bi_list_batches", {}, len(items), t0)
        return {
            "batches": [
                {"batch": v["batch"], "records": v["record_count"],
                 "file": v["filename"], "captured_at": v["captured_at"],
                 "events_to_here": v.get("event_count")}
                for v in items
            ],
            "count": len(items),
        }
    except Exception as e:
        _audit("bi_list_batches", {}, None, t0, "error", str(e))
        return {"error": str(e)}


def bi_list_course_enrollments(
    batch: str | None = None,
    min_courses: int = 2,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """查询至少报名指定门数不同课程的学员、课程名单及人数。"""
    t0 = time.perf_counter()
    params: dict[str, Any] = {
        "batch": batch,
        "min_courses": min_courses,
        "limit": limit,
        "offset": offset,
    }
    try:
        eff, truncated, maxl = _clamp_limit(limit)
        if batch is not None:
            _check_batch(batch)
        if min_courses < 1:
            raise ValueError("min_courses 必须 >= 1")
        if offset < 0:
            raise ValueError("offset 必须 >= 0")
        params = {
            "min_courses": min_courses,
            "limit": eff,
            "offset": offset,
            **({"batch": batch} if batch else {}),
        }
        out = _client().get("/courses/enrollments", params)
        out["truncated"] = truncated
        out["server_max_limit"] = maxl
        _audit("bi_list_course_enrollments", params, out.get("returned"), t0)
        return out
    except Exception as e:
        _audit("bi_list_course_enrollments", params, None, t0, "error", str(e))
        return {"error": str(e)}


def bi_compare_metrics(from_batch: str, to_batch: str,
                       include_attribution: bool = True) -> dict[str, Any]:
    """对比两个批次的核心指标：数值、变化量、变化率，并给出每个指标「为什么变」的归因。

    归因把指标变化拆成可核查的事件计数（如报名人数 +4 = 新增 12 - 移除 8），
    并给出残差 residual：为 0 表示变化已被差分事件完全解释。
    """
    t0 = time.perf_counter()
    params = {"from": from_batch, "to": to_batch, "attribute": include_attribution}
    try:
        _check_batch(from_batch, to_batch)
        out = _client().get("/metrics/compare", params)
        _audit("bi_compare_metrics", params, len(out.get("items", [])), t0)
        return out
    except Exception as e:
        _audit("bi_compare_metrics", params, None, t0, "error", str(e))
        return {"error": str(e)}


def bi_diff_summary(from_batch: str, to_batch: str) -> dict[str, Any]:
    """汇总两期差分：新增 / 移除 / 变更各多少，哪个字段最活跃，严重度分布如何。"""
    t0 = time.perf_counter()
    params = {"from": from_batch, "to": to_batch}
    try:
        _check_batch(from_batch, to_batch)
        out = _client().get("/diffs/summary", params)
        _audit("bi_diff_summary", params, out.get("total_events"), t0)
        return out
    except Exception as e:
        _audit("bi_diff_summary", params, None, t0, "error", str(e))
        return {"error": str(e)}


def bi_diff_records(from_batch: str, to_batch: str,
                    event_type: str | None = None, field_name: str | None = None,
                    severity: str | None = None, record_key: str | None = None,
                    limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """明细下钻：具体哪些人、哪些字段、从什么变成什么。支持按类型/字段/严重度/人过滤与分页。

    返回条数受服务端硬上限约束（超限会被截断，响应里 truncated=true）。
    """
    t0 = time.perf_counter()
    params: dict[str, Any] = {"from": from_batch, "to": to_batch, "event_type": event_type,
                              "field_name": field_name, "record_key": record_key,
                              "limit": limit, "offset": offset}
    try:
        eff, truncated, maxl = _clamp_limit(limit)
        params = {"from": from_batch, "to": to_batch, "event_type": event_type,
                  "field_name": field_name, "record_key": record_key,
                  "limit": eff, "offset": offset}
        _check_batch(from_batch, to_batch)
        if event_type and event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"event_type 必须是 {sorted(ALLOWED_EVENT_TYPES)} 之一")
        if severity and severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"severity 必须是 {sorted(ALLOWED_SEVERITIES)} 之一")
        if offset < 0:
            raise ValueError("offset 必须 >= 0")
        params = {k: v for k, v in params.items() if v is not None}
        out = _client().get("/diffs/records", params)
        rows = out.get("items", [])
        if severity:
            rows = [r for r in rows if r.get("severity") == severity]
        out["items"] = rows
        out["returned"] = len(rows)
        out["truncated"] = truncated
        out["server_max_limit"] = maxl
        _audit("bi_diff_records", params, len(rows), t0)
        return out
    except Exception as e:
        _audit("bi_diff_records", params, None, t0, "error", str(e))
        return {"error": str(e)}


def bi_trace_evidence(event_id: int) -> dict[str, Any]:
    """证据追溯：给定差分事件 id，返回它在上下两期的原始记录、源文件名、sha256 与 Excel 行号。

    这是「每个结论都能被核查」的最后一环——从结论一路回溯到原始 Excel 的具体某一行。
    """
    t0 = time.perf_counter()
    try:
        if int(event_id) <= 0:
            raise ValueError("event_id 必须为正整数")
        out = _client().get(f"/evidence/{int(event_id)}")
        _audit("bi_trace_evidence", {"event_id": event_id}, 1, t0)
        return out
    except Exception as e:
        _audit("bi_trace_evidence", {"event_id": event_id}, None, t0, "error", str(e))
        return {"error": str(e)}


def bi_list_anomalies(batch: str, severity: str | None = None, limit: int = 50) -> dict[str, Any]:
    """列出某批次的数据质量与业务异常（重复主键、证件号非法、已通过但无分数、状态回退等）。"""
    t0 = time.perf_counter()
    params: dict[str, Any] = {"batch": batch, "severity": severity, "limit": limit}
    try:
        eff, truncated, maxl = _clamp_limit(limit)
        params = {"batch": batch, "severity": severity, "limit": eff}
        _check_batch(batch)
        if severity and severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"severity 必须是 {sorted(ALLOWED_SEVERITIES)} 之一")
        params = {k: v for k, v in params.items() if v is not None}
        out = _client().get("/anomalies", params)
        out["truncated"] = truncated
        out["server_max_limit"] = maxl
        _audit("bi_list_anomalies", params, out.get("returned"), t0)
        return out
    except Exception as e:
        _audit("bi_list_anomalies", params, None, t0, "error", str(e))
        return {"error": str(e)}


TOOLS = (
    bi_list_batches,
    bi_list_course_enrollments,
    bi_compare_metrics,
    bi_diff_summary,
    bi_diff_records,
    bi_trace_evidence,
    bi_list_anomalies,
)


def assert_readonly() -> None:
    """自检：确认没有注册任何写操作工具。"""
    for fn in TOOLS:
        name = fn.__name__.lower()
        for kw in WRITE_KEYWORDS:
            if kw in name:
                raise AssertionError(f"工具 {fn.__name__} 疑似写操作（命中关键词 {kw}）")


# --------------------------------------------------------------------------
# MCP 注册
# --------------------------------------------------------------------------
INSTRUCTIONS = """云学通 BI 差分服务（只读）。

能力范围：
  - 列出批次
  - 查询至少报名指定门数不同课程的学员名单和人数
  - 对比两期核心指标并解释变化原因（归因到具体差分事件）
  - 汇总两期差分（新增/移除/变更）
  - 下钻到具体人与字段
  - 把任一结论追溯到原始 Excel 的具体行

约束：全部只读；单次返回条数有服务端硬上限；所有调用留审计日志。
本服务不提供任何写入、删除、修改数据的工具。
"""

mcp = None
if HAS_FASTMCP:  # pragma: no cover
    mcp = FastMCP("yunxuetong-bi", instructions=INSTRUCTIONS)
    for _fn in TOOLS:
        mcp.tool(name=_fn.__name__, description=(_fn.__doc__ or "").strip().split("\n")[0])(_fn)


def main() -> None:  # pragma: no cover
    """以 stdio 方式启动 MCP 服务。"""
    if not HAS_FASTMCP:
        raise SystemExit("未安装 fastmcp，请先 pip install fastmcp")
    assert_readonly()
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
