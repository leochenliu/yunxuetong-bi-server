"""指标归因：用差分事件解释「指标为什么变」。

这是本服务最有价值的一层——不只回答「报名人数涨了 4 人」，
而是回答「涨的 4 人 = 新增 12 人 - 移除 8 人，残差 0，已完全解释」。

能完全归因的前提是：指标都由被追踪的字段定义，且差分事件是字段级的。
若出现残差（residual != 0），说明存在未被追踪的变化，需人工核查——这本身就是信号。
"""

from __future__ import annotations

import json
from typing import Any

from .config import load_dataset_config
from .settings import get_settings
from .store import FactStore
from .values import canon_value, to_number

# 可直接用「事件计数」归因的指标
VALUE_RULES: dict[str, dict[str, str]] = {
    "exam_passed": {"field": "考试状态", "value": "已通过", "label": "考试状态变为「已通过」"},
    "paid_count": {"field": "是否付款", "value": "已付款", "label": "是否付款变为「已付款」"},
}
EMPTY_RULES: dict[str, dict[str, str]] = {
    "exam_scheduled": {"field": "考试日期", "label": "新增/清空考试日期"},
}


def _payload_values(store: FactStore, version_id: int, batch_from: str, batch_to: str,
                    event_type: str, field: str, limit: int) -> list[Any]:
    """取某类事件对应原始记录里指定字段的值。"""
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT r.payload_json FROM raw_records r
               WHERE r.version_id=?
                 AND r.record_key IN (SELECT record_key FROM diff_events
                                      WHERE batch_from=? AND batch_to=? AND event_type=?)
               LIMIT ?""",
            (version_id, batch_from, batch_to, event_type, limit),
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload_json"]).get(field))
        except Exception:
            out.append(None)
    return out


def attribute_metric(store: FactStore, metric_key: str,
                     batch_from: str, batch_to: str) -> dict[str, Any]:
    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    norm = cfg.normalization

    m_from = store.metric_snapshot(batch_from).get(metric_key, {})
    m_to = store.metric_snapshot(batch_to).get(metric_key, {})
    v_from, v_to = m_from.get("value"), m_to.get("value")
    delta = (v_to - v_from) if (v_from is not None and v_to is not None) else None

    from_vid = store.version_id_of(batch_from, cfg.id)
    to_vid = store.version_id_of(batch_to, cfg.id)

    result: dict[str, Any] = {
        "metric_key": metric_key,
        "metric_name": m_to.get("metric_name") or m_from.get("metric_name") or metric_key,
        "value_from": v_from,
        "value_to": v_to,
        "delta": delta,
        "unit": m_to.get("unit") or m_from.get("unit") or "",
        "drivers": [],
        "residual": None,
        "fully_explained": None,
        "note": "",
    }

    added = store.diff_events(batch_from, batch_to, event_type="added", limit=1)[1]
    removed = store.diff_events(batch_from, batch_to, event_type="removed", limit=1)[1]

    # ---- 报名总人数：完全由 新增 - 移除 决定 ----
    if metric_key == "enrolled_total":
        result["drivers"] = [
            {"type": "added", "label": "本期新录入报名", "count": added, "effect": float(added)},
            {"type": "removed", "label": "本期未再出现（退费/剔除/漏报）", "count": removed,
             "effect": float(-removed)},
        ]
        result["residual"] = None if delta is None else round(delta - (added - removed), 6)
        result["fully_explained"] = result["residual"] == 0
        return result

    # ---- 计数型指标：字段值变化 + 新增/移除中直接命中 ----
    if metric_key in VALUE_RULES:
        rule = VALUE_RULES[metric_key]
        target = canon_value(rule["value"], norm)
        events, _ = store.diff_events(batch_from, batch_to, event_type="changed",
                                      field_name=rule["field"], limit=10000)
        gain = sum(1 for e in events if e["value_to"] == target and e["value_from"] != target)
        loss = sum(1 for e in events if e["value_from"] == target and e["value_to"] != target)
        added_hit = sum(1 for v in _payload_values(store, to_vid, batch_from, batch_to, "added", rule["field"], 5000)
                        if canon_value(v, norm) == target)
        removed_hit = sum(1 for v in _payload_values(store, from_vid, batch_from, batch_to, "removed", rule["field"], 5000)
                          if canon_value(v, norm) == target)
        result["drivers"] = [
            {"type": "changed_gain", "label": f"{rule['label']}（存量变更）", "count": gain, "effect": float(gain)},
            {"type": "added_hit", "label": f"新增 = {rule['value']}", "count": added_hit, "effect": float(added_hit)},
            {"type": "changed_loss", "label": f"从「{rule['value']}」变为其他（存量变更）", "count": loss,
             "effect": float(-loss)},
            {"type": "removed_hit", "label": f"移除了原本为「{rule['value']}」的记录", "count": removed_hit,
             "effect": float(-removed_hit)},
        ]
        net = gain + added_hit - loss - removed_hit
        result["residual"] = None if delta is None else round(delta - net, 6)
        result["fully_explained"] = result["residual"] == 0
        return result

    if metric_key in EMPTY_RULES:
        rule = EMPTY_RULES[metric_key]
        events, _ = store.diff_events(batch_from, batch_to, event_type="changed",
                                      field_name=rule["field"], limit=10000)
        filled = sum(1 for e in events if e["value_to"] and not e["value_from"])
        cleared = sum(1 for e in events if e["value_from"] and not e["value_to"])
        # 新增/移除的人本身可能已带该字段（新增时已安排考试、移除时原本已安排考试）
        def _nonempty(v: Any) -> bool:
            return v is not None and str(v).strip() != ""

        added_hit = sum(1 for v in _payload_values(store, to_vid, batch_from, batch_to,
                                                   "added", rule["field"], 5000) if _nonempty(v))
        removed_hit = sum(1 for v in _payload_values(store, from_vid, batch_from, batch_to,
                                                     "removed", rule["field"], 5000) if _nonempty(v))
        result["drivers"] = [
            {"type": "filled", "label": f"首次填入{rule['field']}", "count": filled, "effect": float(filled)},
            {"type": "added_hit", "label": f"新增记录中已含{rule['field']}", "count": added_hit,
             "effect": float(added_hit)},
            {"type": "cleared", "label": f"清空{rule['field']}", "count": cleared, "effect": float(-cleared)},
            {"type": "removed_hit", "label": f"移除了原本含{rule['field']}的记录", "count": removed_hit,
             "effect": float(-removed_hit)},
        ]
        net = filled + added_hit - cleared - removed_hit
        result["residual"] = None if delta is None else round(delta - net, 6)
        result["fully_explained"] = result["residual"] == 0
        return result

    # ---- 均值型指标：给出分布变化，无法用计数完全归因 ----
    if metric_key == "avg_score":
        events, _ = store.diff_events(batch_from, batch_to, event_type="changed",
                                      field_name="分数", limit=10000)
        up = down = 0
        for e in events:
            a, b = to_number(e["value_from"]), to_number(e["value_to"])
            if a is None or b is None:
                continue
            if b > a:
                up += 1
            elif b < a:
                down += 1
        result["drivers"] = [
            {"type": "score_up", "label": "分数上调", "count": up, "effect": None},
            {"type": "score_down", "label": "分数下调", "count": down, "effect": None},
        ]
        result["note"] = "平均分属均值型指标，由分数分布变化驱动，无法用事件计数完全归因"
        return result

    result["note"] = "该指标未配置归因规则"
    return result
