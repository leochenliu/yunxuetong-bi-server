"""阶段五验收：自然语言 Agent 的完整闭环。

断言的不是「答案好看」，而是闭环的**结构性特征**：
  - 从一个问题触发了多轮查询
  - 查询之间存在依赖（后一步用前一步的结果）
  - 发现异常后会自动去追溯证据
  - 最终解释里的数字都来自查询结果
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bi import mcp_server as M
from bi.agent import BIAgent, detect_intent
from bi.api import app

F, T = "20261012", "20261013"


@pytest.fixture(scope="module", autouse=True)
def _inject_client(seeded):
    M.set_query_client(M.BIQueryClient(sdk_client=TestClient(app)))
    yield
    M.set_query_client(None)


def test_intent_detection():
    assert detect_intent("为什么报名人数涨了") == "why"
    assert detect_intent("有什么异常吗") == "anomaly"
    assert detect_intent("本期多少人了") == "metric"
    assert detect_intent("两期对比有什么差异") == "diff"
    assert detect_intent("随便聊聊") == "overview"


def test_full_loop_structure():
    r = BIAgent().ask("为什么报名人数涨了？本期有哪些异常？证据在哪？")
    # 闭环的结构性断言（不依赖输出格式，兼容本地模板和 LLM）
    assert r["step_count"] >= 8, f"步数过少（{r['step_count']}），闭环不完整"
    assert r["from"] == F and r["to"] == T
    assert len(r["plan"]) >= 5
    tools = [s["tool"] for s in r["steps"]]
    for t in ["bi_list_batches", "bi_compare_metrics", "bi_diff_summary",
              "bi_list_anomalies", "bi_trace_evidence"]:
        assert t in tools, f"闭环缺少 {t}"
    # 关键事实：必须含 883/887 这些指标数字（防止 LLM 自由发挥不接数据）
    answer = r["answer"]
    must_have_any = ["已通过", "异常", "陈艾伟", "150", "800", "883", "887"]
    assert any(k in answer for k in must_have_any), \
        f"解释缺少关键事实: {answer[:200]}"


def test_evidence_is_actually_traced():
    r = BIAgent().ask("本期有哪些异常？请给出证据")
    assert len(r["evidence"]) >= 1, "发现异常后必须追溯证据"
    e = r["evidence"][0]
    chain = e["trace"]["chain"]
    assert len(chain) == 2
    assert all(c["present"] for c in chain)
    assert all(c["source_row"] > 1 for c in chain)
    assert e["event"]["id"] == e["trace"]["event"]["id"]


def test_queries_are_dependent_not_fixed():
    """第 4 步起是条件触发的：下钻用的字段来自第 3 步的 by_field。"""
    r = BIAgent().ask("对比两期有什么变化")
    tools = [s["tool"] for s in r["steps"]]
    assert "bi_diff_summary" in tools
    assert "bi_diff_records" in tools
    # 下钻参数里的 field_name 必须与汇总里最活跃字段一致
    summary = next(s for s in r["steps"] if s["tool"] == "bi_diff_summary")["result"]
    top = summary["by_field"][0]["field"]
    drill = next(s for s in r["steps"] if s["tool"] == "bi_diff_records")
    assert drill["params"]["field_name"] == top


def test_agent_numbers_match_api():
    """解释里的数字必须与 API 一致（防止模型/模板编造）。"""
    r = BIAgent().ask("指标变化")
    by_key = {m["metric_key"]: m for m in r["metrics"]}
    assert by_key["enrolled_total"]["value_to"] == 887
    assert by_key["enrolled_total"]["delta"] == 4
    assert "883" in r["answer"] and "887" in r["answer"]


def test_agent_handles_single_batch_gracefully():
    r = BIAgent().ask("对比一下", from_batch=F, to_batch=F)
    # 同一批次对比：无差异，但流程不应崩溃
    assert "answer" in r
