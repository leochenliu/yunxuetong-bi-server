"""阶段四验收：MCP 工具的只读性、查询限制、审计日志，以及能力与 API 一致。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bi import mcp_server as M
from bi.api import app

F, T = "20261012", "20261013"


@pytest.fixture(scope="module", autouse=True)
def _inject_client(seeded):
    """把已测试的 ASGI 应用直接注入 MCP 客户端，免起 HTTP 服务。"""
    M.set_query_client(M.BIQueryClient(sdk_client=TestClient(app)))
    yield
    M.set_query_client(None)


# ---------------- 只读 ----------------
def test_no_write_tools_registered():
    M.assert_readonly()
    for fn in M.TOOLS:
        low = fn.__name__.lower()
        assert "get" in low or "list" in low or "compare" in low or "diff" in low \
            or "trace" in low or "batch" in low, fn.__name__


def test_tool_set_is_exactly_seven_readonly_capabilities():
    names = {fn.__name__ for fn in M.TOOLS}
    assert names == {
        "bi_list_batches",
        "bi_list_course_enrollments",
        "bi_compare_metrics",
        "bi_diff_summary",
        "bi_diff_records",
        "bi_trace_evidence",
        "bi_list_anomalies",
    }


# ---------------- 能力一致 ----------------
def test_list_batches():
    out = M.bi_list_batches()
    # 至少 D1 + D2 必须存在（可能有 demo 期，但 test_timeseries 不持久化）
    assert out["count"] >= 2
    batches = {b["batch"] for b in out["batches"]}
    assert "20261012" in batches
    assert "20261013" in batches
    assert {b["batch"] for b in out["batches"]} == {F, T}


def test_compare_metrics_with_attribution():
    out = M.bi_compare_metrics(F, T)
    items = {i["metric_key"]: i for i in out["items"]}
    assert items["enrolled_total"]["delta"] == 4
    assert items["exam_passed"]["delta"] == 39
    assert items["paid_count"]["delta"] == 15
    assert items["enrolled_total"]["attribution"]["residual"] == 0
    assert items["enrolled_total"]["attribution"]["fully_explained"] is True


def test_diff_summary():
    out = M.bi_diff_summary(F, T)
    assert out["by_type"] == {"added": 12, "removed": 8, "changed": 201}
    assert out["total_events"] == 221


def test_diff_records_and_evidence_end_to_end():
    out = M.bi_diff_records(F, T, event_type="changed", field_name="分数", limit=5)
    assert out["total"] == 50
    assert out["returned"] == 5
    eid = out["items"][0]["id"]
    ev = M.bi_trace_evidence(eid)
    assert len(ev["chain"]) == 2
    assert ev["chain"][0]["source_row"] > 1


def test_list_anomalies():
    out = M.bi_list_anomalies(T)
    assert out["counts"].get("score_out_of_range", 0) >= 1


# ---------------- 查询限制 ----------------
def test_limit_hard_capped(settings):
    out = M.bi_diff_records(F, T, limit=100000)
    assert out["truncated"] is True
    assert out["limit"] == settings.mcp_max_limit
    assert out["server_max_limit"] == settings.mcp_max_limit
    assert out["returned"] <= settings.mcp_max_limit


def test_limit_not_capped_when_small():
    out = M.bi_diff_records(F, T, limit=5)
    assert out["truncated"] is False
    assert out["returned"] == 5


def test_invalid_limit_rejected():
    out = M.bi_diff_records(F, T, limit=0)
    assert "error" in out
    out = M.bi_diff_records(F, T, limit=-3)
    assert "error" in out


# ---------------- 参数白名单 ----------------
def test_param_whitelist():
    assert "error" in M.bi_diff_records(F, T, event_type="deleted")
    assert "error" in M.bi_diff_records(F, T, severity="critical")
    assert "error" in M.bi_list_anomalies("not-a-batch")
    assert "error" in M.bi_compare_metrics("2026", T)  # 批次太短
    assert "error" in M.bi_trace_evidence(0)


# ---------------- 审计 ----------------
def test_every_call_is_audited(settings):
    from bi.store import FactStore

    store = FactStore(settings)
    M.bi_list_batches()
    M.bi_diff_summary(F, T)
    logs = store.recent_audit(20)
    tools = {(l["actor"], l["tool"]) for l in logs}
    assert ("mcp", "bi_list_batches") in tools
    assert ("mcp", "bi_diff_summary") in tools
    for l in logs:
        if l["actor"] == "mcp":
            assert l["duration_ms"] is not None
            assert l["status"] in {"ok", "error"}


def test_error_calls_also_audited(settings):
    from bi.store import FactStore

    M.bi_diff_records(F, T, event_type="deleted")  # 参数非法 -> error
    logs = FactStore(settings).recent_audit(5)
    assert logs[0]["tool"] == "bi_diff_records"
    assert logs[0]["status"] == "error"


# ---------------- MCP 注册 ----------------
def test_mcp_object_has_tools():
    if not M.HAS_FASTMCP:
        pytest.skip("fastmcp 未安装")
    assert M.mcp is not None
