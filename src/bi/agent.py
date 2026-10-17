"""阶段五：自然语言 Agent —— 从一个问题自动走完「多轮查询 -> 发现异常 -> 追溯证据 -> 生成解释」。

这是一个**条件驱动的循环**，不是写死的流水线：

  1. 列批次                    （永远执行）
  2. 指标对比 + 归因            （永远执行）
  3. 差分汇总                   （永远执行）
  4. 下钻最活跃字段 / 新增 / 移除 （仅当本期确有变化时执行 —— 依赖第 3 步结果）
  5. 异常清单                   （永远执行）
  6. 证据追溯                   （仅当存在高严重度异常时执行；对每个异常按其主键回查事件，
                                 再对事件追溯回原始 Excel 行 —— 依赖第 5 步结果）
  7. 生成解释                   （本地模板；配置了 LLM 则改由模型组织语言）

每一步都被记录下来（工具名、参数、结论摘要），可直接展示「它是怎么查出来的」。

业务目标路由：每个问题先按 `config/goals.yaml` 匹配一条「业务关注点」。
命中后：① 目标名会写进解释头部；② focus / output 影响查询策略与解释深度；
③ 业务专属 `action` 钩子（缺数据时显式回绝 / 证书复查粗筛）优先于通用流程。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import mcp_server as M
from .config import Goal, PRIORITY_ORDER, load_goals_config
from .settings import get_settings

INTENT_KEYWORDS: dict[str, list[str]] = {
    "anomaly": ["异常", "问题", "错", "风险", "告警", "数据质量", "不合规", "回退"],
    "why": ["为什么", "为何", "原因", "归因", "怎么回事", "怎么变", "哪来"],
    "metric": ["多少", "几个", "人数", "指标", "情况", "怎么样", "汇总", "总量"],
    "diff": ["变了", "差异", "对比", "变动", "区别", "新增", "减少", "变化", "差"],
}

_COURSE_NUMBER_WORDS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def detect_intent(question: str) -> str:
    for intent, kws in INTENT_KEYWORDS.items():
        if any(k in question for k in kws):
            return intent
    return "overview"


def _course_query_minimum(question: str) -> int | None:
    """识别按报名课程门数查询的问法，并返回包含边界的最小门数。"""
    if not any(word in question for word in ("课程", "科目")):
        return None
    if not any(word in question for word in ("报名", "报了", "报考", "参加")):
        return None

    number = r"(\d+|[一二两三四五六七八九十])"
    greater = re.search(rf"超过\s*{number}\s*门", question)
    if greater:
        token = greater.group(1)
        return int(token) + 1 if token.isdigit() else _COURSE_NUMBER_WORDS[token] + 1

    threshold = re.search(rf"(?:至少|不少于)?\s*{number}\s*门\s*(?:以上|及以上|或以上)?", question)
    if threshold:
        token = threshold.group(1)
        return int(token) if token.isdigit() else _COURSE_NUMBER_WORDS[token]
    if "多门" in question:
        return 2
    return None


# ============================================================================
# 业务目标路由器
# ============================================================================
def _goal_matches(goal: Goal, question: str, intent: str) -> bool:
    m = goal.match
    if m.intent:
        intents = m.intent if isinstance(m.intent, list) else [m.intent]
        if intent not in intents:
            return False
    if m.keywords:
        if not any(kw in question for kw in m.keywords):
            return False
    if m.rule_id and m.rule_id not in question:
        return False
    if m.field_name and m.field_name not in question:
        return False
    return True


def select_goal(question: str, intent: str, goals: list[Goal]) -> Goal | None:
    """按 priority 选出最高优先级的命中目标；未命中返回 None。"""
    hit = [g for g in goals if _goal_matches(g, question, intent)]
    if not hit:
        return None
    hit.sort(key=lambda g: PRIORITY_ORDER.get(g.priority, 9))
    return hit[0]


def _today_batch(versions: list[dict[str, Any]]) -> str | None:
    """取最新批次（to_batch），用于加载最新一期数据集。"""
    if not versions:
        return None
    return versions[-1].get("batch")


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass
class AgentStep:
    n: int
    tool: str
    params: dict[str, Any]
    reason: str
    summary: str
    result: Any
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "tool": self.tool, "params": self.params,
            "reason": self.reason, "summary": self.summary,
            "result": self.result, "elapsed_ms": self.elapsed_ms,
        }


class BIAgent:
    """自然语言 BI Agent。"""

    def __init__(self, max_steps: int = 12):
        self.max_steps = max_steps

    # ------------------------------------------------------------------
    def ask(self, question: str, from_batch: str | None = None,
            to_batch: str | None = None) -> dict[str, Any]:
        t_start = time.perf_counter()
        intent = detect_intent(question)
        steps: list[AgentStep] = []
        plan: list[str] = []

        # ---- 业务目标路由（goals.yaml） ----
        s = get_settings()
        goals = load_goals_config(s.goals_config_path)
        goal = select_goal(question, intent, goals)
        goal_dict = goal.as_dict() if goal else None
        # 命中目标时降低推理步数上限（防止过度展开）
        if goal and goal.output.max_steps:
            self._local_max_steps = min(self.max_steps, goal.output.max_steps)
        else:
            self._local_max_steps = self.max_steps

        def run(tool: str, fn, params: dict[str, Any], reason: str, summarizer) -> Any:
            if len(steps) >= self._local_max_steps:
                return None
            t0 = time.perf_counter()
            result = fn()
            ms = int((time.perf_counter() - t0) * 1000)
            if isinstance(result, dict) and "error" in result:
                summary = f"失败：{result['error']}"
            else:
                summary = summarizer(result)
            steps.append(AgentStep(len(steps) + 1, tool, params, reason, summary, result, ms))
            return result

        course_minimum = _course_query_minimum(question)
        if course_minimum is not None:
            batch_match = re.search(r"(?<!\d)\d{6,10}(?!\d)", question)
            batch = batch_match.group(0) if batch_match else None
            params = {"min_courses": course_minimum, "limit": 200, "offset": 0}
            if batch:
                params["batch"] = batch
            plan.append("查询课程报名明细，按学员统计不同课程数并筛选达到门数要求的人")
            stats = run(
                "bi_list_course_enrollments",
                lambda: M.bi_list_course_enrollments(
                    batch=batch, min_courses=course_minimum, limit=200
                ),
                params,
                "课程明细数据支持按学员去重并统计不同课程数",
                lambda result: (
                    f"{result.get('batch')} 批次有 {result.get('qualifying_learners', 0)} "
                    f"位学员报名至少 {course_minimum} 门不同课程"
                ),
            )
            error = (stats or {}).get("error")
            if error:
                answer = f"无法查询课程报名明细：{error}"
            else:
                names = (stats or {}).get("learners", [])[:30]
                if stats.get("qualifying_learners", 0):
                    lines = [
                        f"【课程报名统计】{stats['batch']} 批次共有 {stats['learner_count']} 位学员；"
                        f"报名至少 {course_minimum} 门不同课程的学员有 "
                        f"{stats['qualifying_learners']} 人。课程名称按不同课程去重，"
                        "同一课程不同月份的明细不会重复计数。"
                    ]
                    if names:
                        lines.append("名单（最多显示前 30 人）：")
                        lines.extend(
                            f"  · {learner['name'] or '未填写'}：{learner['course_count']} 门"
                            f"（{'、'.join(learner['courses'])}）"
                            for learner in names
                        )
                    if stats["qualifying_learners"] > stats["returned"]:
                        lines.append(
                            f"本次返回 {stats['returned']} 人；剩余名单可通过分页继续查询。"
                        )
                    elif stats["returned"] > len(names):
                        lines.append("完整的当前页名单见本次查询步骤中的返回数据。")
                    answer = "\n".join(lines)
                else:
                    answer = (
                        f"【课程报名统计】{stats['batch']} 批次共 {stats['learner_count']} 位学员，"
                        f"没有学员报名达到 {course_minimum} 门不同课程。"
                    )
            return {
                "question": question,
                "intent": intent,
                "goal": goal_dict,
                "from": None,
                "to": stats.get("batch") if stats and not error else batch,
                "plan": plan,
                "steps": [step.as_dict() for step in steps],
                "step_count": len(steps),
                "course_enrollments": stats if not error else None,
                "answer": answer,
                "error": error,
                "used_llm": False,
                "elapsed_ms": int((time.perf_counter() - t_start) * 1000),
            }

        # ---- 1. 批次 ----
        plan.append("列出所有已入站批次，确定可对比的时间范围")
        batches = run("bi_list_batches", M.bi_list_batches, {},
                      "先知道有哪些期数据可比",
                      lambda r: f"找到 {r.get('count', 0)} 个批次：{', '.join(b['batch'] for b in r.get('batches', []))}")
        if not batches or "error" in batches:
            return {"question": question, "intent": intent, "steps": [s.as_dict() for s in steps],
                    "answer": "无法读取批次列表：" + str((batches or {}).get("error")), "error": "no_batches"}

        bs = [b["batch"] for b in batches["batches"]]
        if len(bs) < 2 and not (from_batch and to_batch):
            return {"question": question, "intent": intent, "steps": [s.as_dict() for s in steps],
                    "answer": f"目前只有 1 个批次（{bs[0] if bs else '无'}），无法做对比。请再入站一期数据。",
                    "error": "need_two_batches"}
        f = from_batch or bs[-2]
        t = to_batch or bs[-1]

        # ---- 2. 指标对比 ----
        plan.append(f"对比 {f} 与 {t} 的核心指标，并解释每个指标变化的原因")
        metrics = run("bi_compare_metrics",
                      lambda: M.bi_compare_metrics(f, t),
                      {"from": f, "to": t, "include_attribution": True},
                      "用户想知道发生了什么变化、以及为什么",
                      lambda r: _summarize_metrics(r))
        items = (metrics or {}).get("items", [])
        changed = [i for i in items if i.get("delta")]

        # ---- 3. 差分汇总 ----
        plan.append(f"汇总 {f} -> {t} 的差分事件，找出最活跃的字段")
        diff = run("bi_diff_summary", lambda: M.bi_diff_summary(f, t), {"from": f, "to": t},
                   "需要知道整体发生了什么，而不只是指标",
                   lambda r: (f"共 {r.get('total_events', 0)} 条事件"
                              f"（新增 {r.get('by_type', {}).get('added', 0)} / "
                              f"移除 {r.get('by_type', {}).get('removed', 0)} / "
                              f"变更 {r.get('by_type', {}).get('changed', 0)}）"))
        total_events = int((diff or {}).get("total_events", 0) or 0)

        # ---- 4. 条件：有变化才下钻 ----
        drill: dict[str, Any] = {}
        if total_events > 0:
            by_field = (diff or {}).get("by_field") or []
            top_field = by_field[0]["field"] if by_field else None
            if top_field:
                plan.append(f"下钻最活跃字段「{top_field}」的具体变更")
                recs = run("bi_diff_records",
                           lambda: M.bi_diff_records(f, t, field_name=top_field, limit=8),
                           {"from": f, "to": t, "field_name": top_field, "limit": 8},
                           f"「{top_field}」变更最多，先看它",
                           lambda r: f"{top_field} 共 {r.get('total', 0)} 条变更")
                drill["top_field"] = top_field
                drill["top_field_records"] = (recs or {}).get("items", [])
                drill["top_field_total"] = (recs or {}).get("total", 0)

            if (diff or {}).get("by_type", {}).get("added"):
                plan.append("查看本期新增的报名")
                added = run("bi_diff_records",
                            lambda: M.bi_diff_records(f, t, event_type="added", limit=8),
                            {"from": f, "to": t, "event_type": "added", "limit": 8},
                            "新增是报名人数变化的直接来源",
                            lambda r: f"新增 {r.get('total', 0)} 人")
                drill["added"] = (added or {}).get("items", [])
                drill["added_total"] = (added or {}).get("total", 0)

            if (diff or {}).get("by_type", {}).get("removed"):
                plan.append("查看本期未再出现的记录")
                removed = run("bi_diff_records",
                              lambda: M.bi_diff_records(f, t, event_type="removed", limit=8),
                              {"from": f, "to": t, "event_type": "removed", "limit": 8},
                              "移除会拉低报名人数",
                              lambda r: f"移除 {r.get('total', 0)} 人")
                drill["removed"] = (removed or {}).get("items", [])
                drill["removed_total"] = (removed or {}).get("total", 0)

        # ---- 5. 异常 ----
        plan.append(f"检查 {t} 的数据质量与业务异常")
        anomalies = run("bi_list_anomalies", lambda: M.bi_list_anomalies(t, limit=50),
                        {"batch": t, "limit": 50},
                        "指标变化之外，还要看有没有不该出现的情况",
                        lambda r: f"发现 {r.get('total', 0)} 条异常（高严重度 {_high_count(r)} 条）")

        # ---- 6. 条件：有高严重度异常才追溯证据 ----
        evidence: list[dict[str, Any]] = []
        if anomalies and anomalies.get("items"):
            highs = [a for a in anomalies["items"] if a.get("severity") == "high"]
            if highs:
                plan.append("对每条高严重度异常追溯证据，一路回到原始 Excel 行")
            for a in highs[:3]:
                key = a.get("record_key")
                if not key:
                    continue
                evs = M.bi_diff_records(f, t, record_key=key, limit=5)
                ev_items = (evs or {}).get("items", [])
                if not ev_items:
                    continue
                target = next((e for e in ev_items
                               if e.get("field_name") == a.get("field_name")), ev_items[0])
                steps.append(AgentStep(
                    len(steps) + 1, "bi_diff_records",
                    {"from": f, "to": t, "record_key": key, "limit": 5},
                    f"异常「{a.get('rule_id')}」涉及 {key}，先找到它的事件",
                    f"该人有 {evs.get('total', 0)} 条事件", evs,
                ))
                ev = M.bi_trace_evidence(target["id"])
                steps.append(AgentStep(
                    len(steps) + 1, "bi_trace_evidence", {"event_id": target["id"]},
                    "把这条事件追溯到原始文件与行号",
                    _summarize_evidence(ev), ev,
                ))
                evidence.append({
                    "anomaly": a,
                    "event": target,
                    "trace": ev,
                })

        # ---- 7. 解释 ----
        # 业务目标 action 钩子：命中目标时优先用目标专属输出
        goal_action_result: dict[str, Any] | None = None
        if goal and goal.action:
            from .config import load_dataset_config
            from .goal_actions import run_action
            from .ingest import ingest

            try:
                ds = ingest(t, settings=s)
                cfg = load_dataset_config(s.dataset_config_path)
                goal_action_result = run_action(goal.as_dict(), ds.dataset, cfg)
                steps.append(AgentStep(
                    len(steps) + 1, f"goal_action:{goal.action}",
                    {"batch": t, "goal": goal.name},
                    f"命中目标「{goal.name}」，调用业务专属动作 {goal.action}",
                    _summarize_goal_action(goal_action_result),
                    goal_action_result,
                ))
            except Exception as e:
                steps.append(AgentStep(
                    len(steps) + 1, f"goal_action:{goal.action}",
                    {"batch": t, "goal": goal.name},
                    f"业务动作 {goal.action} 失败", f"失败：{e}", None,
                ))

        used_llm = False
        answer = self._explain(question, intent, f, t, items, changed, diff, drill,
                               anomalies, evidence, goal=goal,
                               goal_action=goal_action_result)
        llm_answer = self._llm_explain(question, {
            "from": f, "to": t, "metrics": items, "diff_summary": diff,
            "anomalies": anomalies, "evidence": [
                {"anomaly": e["anomaly"], "event": {k: e["event"].get(k) for k in
                 ("field_name", "value_from", "value_to", "name", "severity")},
                 "trace": [{"batch": c.get("batch"), "source_row": c.get("source_row"),
                            "file": c.get("file")} for c in (e["trace"] or {}).get("chain", [])]}
                for e in evidence],
        })
        if llm_answer:
            answer = llm_answer
            used_llm = True

        return {
            "question": question,
            "intent": intent,
            "goal": goal_dict,
            "goal_action": goal_action_result,
            "from": f,
            "to": t,
            "plan": plan,
            "steps": [s.as_dict() for s in steps],
            "step_count": len(steps),
            "metrics": items,
            "changed_metrics": changed,
            "diff_summary": diff,
            "drill": drill,
            "anomalies": anomalies,
            "evidence": evidence,
            "answer": answer,
            "used_llm": used_llm,
            "elapsed_ms": int((time.perf_counter() - t_start) * 1000),
        }

    # ------------------------------------------------------------------
    def _explain(self, question, intent, f, t, items, changed, diff, drill,
                 anomalies, evidence, goal=None, goal_action: dict | None = None) -> str:
        lines: list[str] = []

        # 业务目标命中时：解释头部显式告知
        if goal:
            lines.append(f"【目标】{goal.name}  优先级: {goal.priority}  策略: {goal.focus}")
            if goal.describe:
                lines.append(f"  关注：{goal.describe}")
            if goal.note and goal.note.strip():
                note_text = " ".join(line.strip() for line in goal.note.strip().splitlines() if line.strip())
                lines.append(f"  说明：{note_text}")
        # 业务 action 的专属输出（缺失数据 / 证书粗筛等）— 优先于通用解释
        if goal_action is not None:
            lines.append(_format_goal_action(goal_action))
            return "\n".join(lines)

        lines.append(f"\n【结论】{f} → {t} 期间，{len(items)} 个核心指标中有 {len(changed)} 个发生变化。")
        for i in changed:
            d = i.get("delta")
            arrow = "↑" if d > 0 else "↓"
            lines.append(
                f"  · {i['metric_name']}：{_num(i['value_from'])}{i.get('unit','')} → "
                f"{_num(i['value_to'])}{i.get('unit','')}（{arrow}{_num(abs(d))}{i.get('unit','')}"
                + (f"，{i['delta_pct']:+.1f}%" if i.get("delta_pct") is not None else "") + "）"
            )
        if not changed:
            lines.append("  · 所有指标保持不变。")

        # 归因
        lines.append("\n【为什么变】")
        explained_any = False
        for i in changed:
            attr = i.get("attribution") or {}
            drivers = [d for d in attr.get("drivers", []) if d.get("effect")]
            if not drivers:
                continue
            parts = []
            for d in drivers:
                sign = "+" if (d["effect"] or 0) > 0 else ""
                parts.append(f"{d['label']} {sign}{_num(d['effect'])}")
            residual = attr.get("residual")
            tail = "；残差 0，已被差分事件完全解释" if residual == 0 else (
                f"；残差 {_num(residual)}（存在未被追踪的变化，建议核查）" if residual is not None else "")
            lines.append(f"  · {i['metric_name']}：{' + '.join(parts)}{tail}")
            explained_any = True
        if not explained_any:
            lines.append("  · （无可计数的归因驱动项）")

        # 差分全局
        if diff and diff.get("total_events"):
            bt = diff.get("by_type", {})
            lines.append(
                f"\n【发生了什么】本期共 {diff['total_events']} 条事件："
                f"新增 {bt.get('added', 0)}、移除 {bt.get('removed', 0)}、字段变更 {bt.get('changed', 0)}。"
            )
            bf = diff.get("by_field", [])[:5]
            if bf:
                lines.append("  最活跃字段：" + "、".join(f"{x['field']}（{x['count']}）" for x in bf) + "。")
            bt_sev = diff.get("by_severity", {})
            if bt_sev:
                lines.append("  严重度分布：" + "、".join(
                    f"{_sev_cn(k)} {v}" for k, v in bt_sev.items()) + "。")
            if drill.get("top_field_records"):
                lines.append(f"  以「{drill['top_field']}」为例，前几条变更：")
                for r in drill["top_field_records"][:4]:
                    who = r.get("name") or r.get("id_number") or "?"
                    lines.append(
                        f"    - {who}：{r.get('value_from_raw') or '(空)'} → {r.get('value_to_raw') or '(空)'}"
                    )

        # 异常
        an_total = (anomalies or {}).get("total", 0)
        lines.append(f"\n【发现异常】{t} 共 {an_total} 条异常。")
        counts = (anomalies or {}).get("counts", {})
        if counts:
            for rule, c in list(counts.items())[:6]:
                lines.append(f"  · {rule}：{c} 条")
        highs = [a for a in (anomalies or {}).get("items", []) if a.get("severity") == "high"]
        if highs:
            lines.append(f"  其中高严重度 {len(highs)} 条，例如：")
            for a in highs[:3]:
                lines.append(f"    - {a.get('record_key')}：{a.get('message')}")

        # 证据
        if evidence:
            lines.append("\n【证据追溯】从结论回到原始文件：")
            for e in evidence[:3]:
                ev = e["event"]
                tr = e["trace"] or {}
                chain = tr.get("chain", [])
                who = ev.get("name") or ev.get("record_key")
                lines.append(
                    f"  · {who}｜{ev.get('field_name')}："
                    f"{ev.get('value_from_raw') or '(空)'} → {ev.get('value_to_raw') or '(空)'}"
                )
                for c in chain:
                    lines.append(
                        f"      来源（{c.get('batch')}）：{c.get('path') or c.get('file')} 第 {c.get('source_row')} 行"
                        f"｜证件号 {c.get('id_number_masked')}"
                    )
                if chain:
                    lines.append(f"      文件指纹 sha256：{chain[0].get('file_sha256', '')[:16]}…")

        # 建议
        lines.append("\n【建议】")
        if any(a.get("rule_id") == "exam_status_regressed" for a in (anomalies or {}).get("items", [])):
            lines.append("  · 存在「已通过 → 其他状态」的回退，请确认是成绩复核还是录入错误。")
        if any(a.get("rule_id") == "score_out_of_range" for a in (anomalies or {}).get("items", [])):
            lines.append("  · 存在分数越界（>100），请核对录入口径。")
        if any(a.get("rule_id") == "passed_but_no_score" for a in (anomalies or {}).get("items", [])):
            lines.append("  · 存在「已通过但无分数」，建议补录或确认免试规则。")
        if not any(highs):
            lines.append("  · 本期未发现高严重度异常。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def _llm_explain(self, question: str, context: dict[str, Any]) -> str | None:
        s = get_settings()
        if not (s.llm_base_url and s.llm_api_key and s.llm_model):
            return None
        system = (
            "你是数据分析师。基于给定的结构化查询结果，用简体中文回答用户的问题。"
            "要求：结论先行；每个数字都必须来自给定数据，不得编造；"
            "若指标变化有归因且残差为 0，说明已完全解释；"
            "最后给出可执行建议。不要输出 JSON。"
        )
        try:
            with httpx.Client(timeout=60.0) as c:
                r = c.post(
                    f"{s.llm_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {s.llm_api_key}",
                             "Content-Type": "application/json"},
                    json={
                        "model": s.llm_model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content":
                                f"用户问题：{question}\n\n查询结果（JSON）：\n"
                                + json.dumps(context, ensure_ascii=False, default=str)[:12000]},
                        ],
                        "temperature": 0.2,
                    },
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return None


# --------------------------------------------------------------------------
# 摘要工具
# --------------------------------------------------------------------------
def _num(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _sev_cn(s: str) -> str:
    return {"high": "高", "medium": "中", "low": "低", "info": "提示"}.get(s, s)


def _high_count(r: dict) -> int:
    return sum(1 for a in r.get("items", []) if a.get("severity") == "high")


def _summarize_metrics(r: dict) -> str:
    items = r.get("items", [])
    changed = [i for i in items if i.get("delta")]
    if not changed:
        return f"{len(items)} 个指标均无变化"
    return "；".join(
        f"{i['metric_name']} {_num(i['value_from'])}→{_num(i['value_to'])}"
        f"（{'+' if i['delta'] > 0 else ''}{_num(i['delta'])}）" for i in changed[:5]
    )


def _summarize_evidence(ev: dict) -> str:
    if not ev or "error" in ev:
        return f"失败：{(ev or {}).get('error')}"
    chain = (ev.get("chain") or [])
    rows = "、".join(f"{c.get('batch')} 第{c.get('source_row')}行" for c in chain if c.get("present"))
    return f"追溯到 {rows}" if rows else "未找到对应的原始记录"


def _summarize_goal_action(result: dict | None) -> str:
    if not result:
        return "（无输出）"
    if not result.get("answerable"):
        return f"无法可靠回答：{result.get('reason', '缺数据')}"
    title = result.get("title", "")
    count = result.get("count")
    if count is not None:
        return f"{title} → 命中 {count} 条"
    return title or "已生成结果"


def _format_goal_action(result: dict[str, Any]) -> str:
    """把业务 action 的结构化结果格式化为人类可读文本。"""
    if not result.get("answerable"):
        lines: list[str] = []
        lines.append("\n【结论】本服务当前无法可靠回答该问题。")
        lines.append(f"  原因：{result.get('reason', '缺数据')}")
        for m in result.get("missing") or []:
            lines.append(f"  缺什么：{m}")
        proxy = result.get("proxy_available")
        if proxy:
            lines.append(f"\n  弱代理视角：{proxy}")
        rec = result.get("recommendation")
        if rec:
            lines.append(f"\n  建议：{rec}")
        return "\n".join(lines)

    # 可回答：证书粗筛类
    title = result.get("title", "")
    method = result.get("method", "")
    count = result.get("count", 0)
    lines = [f"\n【结果】{title}（共 {count} 条）", f"  方法：{method}"]
    for lim in result.get("limitations") or []:
        lines.append(f"  局限：{lim}")
    by_emp = result.get("by_employer") or {}
    if by_emp:
        lines.append("\n  按单位 Top 10：")
        for i, (k, v) in enumerate(list(by_emp.items())[:10]):
            lines.append(f"    {i + 1:>2}. {k}  {v} 人")
    top = result.get("top_n") or []
    if top:
        lines.append(f"\n  前 {min(10, len(top))} 条样本：")
        for c in top[:10]:
            lines.append(
                f"    · {c.get('name')}（{c.get('id_number')}）｜{c.get('employer', '')}"
                f"｜{c.get('subject', '')}｜上次开班 {c.get('last_class_date')}（{c.get('days_since')} 天前）"
            )
    return "\n".join(lines)


def ask(question: str, **kw) -> dict[str, Any]:
    return BIAgent().ask(question, **kw)
