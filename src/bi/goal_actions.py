"""业务目标的执行钩子：基于 goals.yaml 里的 `action` 字段分发具体逻辑。

每条业务目标可能需要「业务专属的查询/统计逻辑」——不是简单的 MCP 工具组合。
本模块把每种 action 实现为一个函数，输入是 dataset + cfg，输出是人可读结果。
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from typing import Any

from .config import DatasetConfig
from .excel_reader import Dataset


# 行业经验：低压/登高/焊工证书有效期（年）。可后续按项目细化。
DEFAULT_CERT_VALIDITY_YEARS = 3

# 数据当前日期的参考点：当前项目 D1 是 2026-10-12 批次。
# 改用 dt.date.today() 之前要确认 D2 也会随之更新（一致性约束）。
# 在测试里可由调用方覆盖。
def _reference_today() -> _dt.date:
    return _dt.date(2026, 10, 17)


# ---------------------------------------------------------------------------
# 目标 1：潜在流失率 → 当前数据无法可靠回答，必须显式说明缺什么
# ---------------------------------------------------------------------------
def action_missing_data(goal: dict[str, Any], ds: Dataset, cfg: DatasetConfig) -> dict[str, Any]:
    """Agent 诚实选项：承认缺数据 + 列出需补的字段/表。"""
    name = goal.get("name", "潜在流失率")
    note = goal.get("note", "")
    return {
        "title": name,
        "answerable": False,
        "reason": "当前数据无法可靠回答",
        "missing": [
            "「单位-应报课程」映射表：哪些单位必须报哪些项目（业务主表，本项目未覆盖）",
            "或：「应报项目」字段：每位学员「应报名 / 已报名」对照（目前在 Excel 里没有）",
            "或：一份可表达「同岗位同单位课程需求」的规则库（按工作单位 + 岗位推导）",
        ],
        "proxy_available": (
            "可作为代理但**不能直接当流失率**的视角："
            "同工作单位历史报名过的科目分布（共 58 个单位 ≥3 人），"
            "可作为「同单位新进员工大概率也报这个科目」的弱信号。"
        ),
        "recommendation": (
            "建议由业务经理提供「X 单位应报 Y 课程」清单（哪怕是初版），"
            "Agent 才能给出可核查的流失率数字。"
        ),
        "note_for_answer": note,
    }


# ---------------------------------------------------------------------------
# 目标 2：证书快到期 → 用行业 3 年经验粗筛 + 显式告知局限
# ---------------------------------------------------------------------------
def action_screen_due_cert(goal: dict[str, Any], ds: Dataset, cfg: DatasetConfig) -> dict[str, Any]:
    """用「开班日期距今 > 行业有效期 3 年 且 当前未在班」做粗筛。

    **重要局限**：本服务原 Excel 没有「取证日期 / 证书编号」字段，本筛选用
    「开班日期 + 行业经验 3 年」做代理——**它把"曾报名"等同于"曾取证"**，
    实际可能错。
    """
    today = _reference_today()
    validity_years = DEFAULT_CERT_VALIDITY_YEARS
    threshold_days = validity_years * 365

    in_class_ids: set[str] = set()
    for r in ds.rows:
        # 当前在任何「在进行中」班级里：不视为待复查
        exam_date = r.get("考试日期")
        if exam_date and str(r.get("考试状态") or "").strip() not in ("取消考试",):
            in_class_ids.add(str(r.get("证件号码") or "").strip())
            continue
        train_status = str(r.get("培训情况") or "").strip()
        if train_status in ("已培训", "培训中"):
            train_date = r.get("培训第一天日期") or r.get("开班日期")
            if train_date:
                in_class_ids.add(str(r.get("证件号码") or "").strip())

    candidates: list[dict[str, Any]] = []
    for r in ds.rows:
        idn = str(r.get("证件号码") or "").strip()
        if not idn or idn in in_class_ids:
            continue
        last_str = r.get("开班日期") or r.get("培训第一天日期")
        if not last_str or str(last_str).strip() in ("", "(空)"):
            continue
        try:
            last = _dt.date.fromisoformat(str(last_str).strip()[:10])
        except ValueError:
            continue
        days = (today - last).days
        if days > threshold_days:
            candidates.append({
                "id_number": idn,
                "name": r.get("姓名"),
                "phone": r.get("联系电话"),
                "employer": r.get("工作单位"),
                "subject": r.get("科目"),
                "last_class_date": str(last),
                "days_since": days,
                "urgency": "high" if days > validity_years * 365 * 1.5 else "normal",
            })

    candidates.sort(key=lambda c: -c["days_since"])

    by_employer: Counter = Counter(c["employer"] for c in candidates if c["employer"])
    return {
        "title": "证书应复查清单（粗筛，请人工核对）",
        "answerable": True,
        "method": f"上次开班 > {validity_years} 年 且 当前未在班（阈值 {threshold_days} 天）",
        "limitations": [
            "原 Excel 没有「取证日期 / 证书编号」字段，本筛选用「开班日期」+ 行业 3 年经验做代理",
            "「曾报名」≠「曾取证」：曾报名但未通过考试的人也会命中（建议人工核对）",
            "复筛建议：实际取证时间以系统里证书档案为准；本名单仅作「主动联系学员」的优先队列",
        ],
        "count": len(candidates),
        "top_n": candidates[:30],
        "by_employer": dict(by_employer.most_common(20)),
        "today": today.isoformat(),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
ACTIONS: dict[str, Any] = {
    "missing_data": action_missing_data,
    "screen_due_cert": action_screen_due_cert,
}


def run_action(goal: dict[str, Any], ds: Dataset, cfg: DatasetConfig) -> dict[str, Any] | None:
    fn = ACTIONS.get(goal.get("action") or "")
    if fn is None:
        return None
    return fn(goal, ds, cfg)
