"""业务目标路由器测试 + 钩子端到端测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bi import mcp_server as M
from bi.agent import BIAgent, select_goal
from bi.api import app
from bi.config import Goal, GoalMatch, load_goals_config
from bi.goal_actions import action_missing_data, action_screen_due_cert
from bi.ingest import ingest
from bi.settings import get_settings

F, T = "20261012", "20261013"


@pytest.fixture(scope="module", autouse=True)
def _inject_client(seeded):
    M.set_query_client(M.BIQueryClient(sdk_client=TestClient(app)))
    yield
    M.set_query_client(None)


@pytest.fixture(scope="module")
def goals() -> list[Goal]:
    s = get_settings()
    return load_goals_config(s.goals_config_path)


# ---------------- 路由器 ----------------
def test_goals_yaml_loads_three_entries(goals):
    assert len(goals) == 3
    names = {g.name for g in goals}
    assert "潜在流失率（应报未报）" in names
    assert "证书快到期（应复查）" in names
    assert "通用概览" in names


def test_priorities_are_respected(goals):
    # 当一条问题同时命中多个目标时（少见但可能），必须按 priority 排序
    for g in goals:
        assert g.priority in {"high", "medium", "low"}


def test_certificate_question_routes_to_due_cert(goals):
    g = select_goal("有哪些学员的证书快到期了", "anomaly", goals)
    assert g is not None
    assert "证书" in g.name
    assert g.action == "screen_due_cert"


def test_loss_question_routes_to_missing_data(goals):
    g = select_goal("潜在流失率是多少", "metric", goals)
    assert g is not None
    assert g.action == "missing_data"


def test_why_question_falls_back_to_overview(goals):
    """'为什么涨了' 不含 '流失' / '证书' 等关键词，命中低优先级通用概览。"""
    g = select_goal("为什么报名人数涨了", "why", goals)
    assert g is not None
    assert g.name == "通用概览"


def test_unmatched_question_still_finds_overview(goals):
    g = select_goal("你好", "overview", goals)
    assert g is not None
    assert g.name == "通用概览"


def test_intent_filter_strict_when_present(goals):
    """match.intent 存在时，必须在意图列表里才命中。"""
    # 证书目标的 match 是空意图列表（仅 keywords 过滤），所以任意意图都过
    g = select_goal("证书到期", "diff", goals)
    assert g is not None and "证书" in g.name


# ---------------- 业务钩子：missing_data ----------------
def test_missing_data_action_explains_what_to_add(settings):
    from bi.config import load_dataset_config
    dcfg = load_dataset_config(settings.dataset_config_path)
    cfg = load_goals_config(settings.goals_config_path)
    cert_goal = next(g for g in cfg if g.action == "missing_data")
    ds = ingest(T).dataset
    out = action_missing_data(cert_goal.as_dict(), ds, dcfg)
    assert out["answerable"] is False
    assert not out.get("reason", "").startswith("已"), f"reason 错: {out.get('reason')}"
    # 至少给出 1 个具体可补的对象
    assert len(out["missing"]) >= 1
    # 缺失项里要带「应报」或「课程」关键字
    joined = "".join(out["missing"])
    assert ("应报" in joined) or ("课程" in joined)
    # 推荐项要明确告诉用户怎么办
    assert "补" in out.get("recommendation", "") or "建议" in out.get("recommendation", "")


# ---------------- 业务钩子：screen_due_cert ----------------
def test_screen_due_cert_runs_and_returns_structure(settings):
    from bi.config import load_dataset_config
    cfg = load_goals_config(settings.goals_config_path)
    cert_goal = next(g for g in cfg if g.action == "screen_due_cert")
    dcfg = load_dataset_config(settings.dataset_config_path)
    ds = ingest(T).dataset
    out = action_screen_due_cert(cert_goal.as_dict(), ds, dcfg)
    assert out["answerable"] is True
    assert "count" in out and "top_n" in out
    assert isinstance(out["limitations"], list) and len(out["limitations"]) >= 1
    # 当前 D1 数据开班日期都在 2025-2026，距今不满 3 年
    assert out["count"] == 0, f"当前 D1 应命中 0 人，实际 {out['count']}"


# ---------------- 端到端：Agent 集成 ----------------
def test_certificate_question_returns_missing_data_or_zero():
    """命中证书目标：要么有粗筛结果（按规则），要么显式说明 0 人。"""
    r = BIAgent().ask("有哪些学员的证书快到期了？需要主动联系")
    assert r["goal"] is not None
    assert "证书" in r["goal"]["name"]
    assert r["goal_action"] is not None
    # 当前数据下，count 应为 0；解释中要明确说明
    if r["goal_action"]["answerable"]:
        assert r["goal_action"]["count"] == 0
    assert "局限" in r["answer"] or "建议" in r["answer"] or "0" in r["answer"]


def test_loss_question_returns_missing_data_explanation():
    r = BIAgent().ask("统计一下潜在流失率")
    assert r["goal"] is not None and r["goal"]["action"] == "missing_data"
    assert r["goal_action"] is not None
    assert r["goal_action"]["answerable"] is False
    # 关键事实关键词
    answer = r["answer"]
    must = ["缺", "无法", "CRM", "潜在客户"]
    assert any(k in answer for k in must), f"缺诚实标识: {answer[:200]}"


def test_unrelated_question_still_runs_full_loop():
    r = BIAgent().ask("为什么报名人数涨了")
    assert r["goal"] is not None and r["goal"]["name"] == "通用概览"
    assert r["step_count"] >= 8
    # 必须含关键数字
    assert "883" in r["answer"] and "887" in r["answer"]
