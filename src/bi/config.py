"""业务契约加载：config/dataset.yaml + config/metrics.yaml -> 强类型对象。

第一阶段要求「定义主键、字段映射、差分规则和异常处理」。这些定义全部落在
YAML 里，本模块只负责把它们读成对象——引擎不硬编码任何字段名。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .cleaning import CleaningRule


@dataclass
class IdentityConfig:
    columns: list[str]
    separator: str = "|"
    require_identity: bool = True
    id_column: str | None = None
    name_column: str | None = None


@dataclass
class AnomalyRule:
    id: str
    desc: str = ""
    severity: str = "medium"


@dataclass
class DeriveConfig:
    class_code: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    # 过期日期派生（证书类目用）：{column, start_column, outputs}
    expiry: dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasetConfig:
    id: str
    name: str
    sheet: str | None
    identity: IdentityConfig
    pii_columns: list[str]
    field_mapping: dict[str, str]
    tracked_fields: list[str]
    aux_tracked_fields: list[str]
    severity: dict[str, str]
    derive: DeriveConfig
    diff_rules: dict[str, Any]
    anomaly_rules: list[AnomalyRule]
    # 字段清洗规则（配置驱动，引擎只实现通用动作，见 src/bi/cleaning.py）
    cleaning: list[CleaningRule] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ---------- 便捷访问 ----------
    @property
    def key_columns(self) -> list[str]:
        return list(self.identity.columns)

    @property
    def id_column(self) -> str:
        return self.identity.id_column or self.identity.columns[0]

    @property
    def name_column(self) -> str:
        return self.identity.name_column or (
            self.identity.columns[-1] if len(self.identity.columns) > 1 else self.identity.columns[0]
        )

    @property
    def normalization(self) -> dict[str, Any]:
        return dict(self.diff_rules.get("normalization", {}))

    @property
    def classify(self) -> dict[str, str]:
        return dict(self.diff_rules.get("classify", {}))

    @property
    def all_tracked(self) -> list[str]:
        """状态字段 + 辅助字段（都参与比对，只是严重度不同）。"""
        out = list(self.tracked_fields)
        for f in self.aux_tracked_fields:
            if f not in out:
                out.append(f)
        return out

    def severity_of(self, field_name: str) -> str:
        return self.severity.get(field_name, "info")

    def standard_name(self, excel_column: str) -> str:
        """Excel 列名 -> 标准字段名。"""
        return self.field_mapping.get(excel_column, excel_column)

    def excel_column(self, standard: str) -> str | None:
        """标准字段名 -> Excel 列名。"""
        for k, v in self.field_mapping.items():
            if v == standard:
                return k
        return None

    def anomaly_rule(self, rule_id: str) -> AnomalyRule | None:
        for r in self.anomaly_rules:
            if r.id == rule_id:
                return r
        return None


@dataclass
class MetricDef:
    key: str
    name: str
    agg: str
    unit: str = ""
    decimals: int = 0
    desc: str = ""
    column: str | None = None
    equals: str | None = None
    not_equals: str | None = None
    exclude_values: list[str] = field(default_factory=list)
    split: str | None = None
    dedupe_columns: list[str] = field(default_factory=list)
    # --- 分桶指标（agg=count_by_group_depth）---
    # group_columns:    构成「一个组」的列（通常是 证件号码 + 姓名）
    # dimension_columns: 维度切分列（通常是 年份 + 月份）
    # depth_column:     组内计数的列（通常是 课程）
    # depth_split:      depth_column 的多值分隔符（如 ","）
    # depth_max_bucket: 从第几门起合并为「N门以上」（默认 5）
    # dimension_name:   维度在展示时的名字（默认取最后一列）
    group_columns: list[str] = field(default_factory=list)
    dimension_columns: list[str] = field(default_factory=list)
    depth_column: str | None = None
    depth_split: str | None = None
    depth_max_bucket: int = 5
    dimension_name: str | None = None

    @property
    def is_row_count(self) -> bool:
        return self.agg == "count_rows"

    @property
    def is_bucket_metric(self) -> bool:
        return self.agg == "count_by_group_depth"


# ============================================================================
# 业务关注点 / 工作改进目标
# ----------------------------------------------------------------------------
# 「业务用户」与 BIAgent 之间的契约：把日常的关注点从自由文本沉淀为可版本化、
# 可被 Agent 自动读取的目标清单。每条目标 = 一个可命名的关注点。
# ============================================================================

from typing import Optional  # noqa: E402  (与本模块其他 dataclass 保持一致)

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class GoalMatch:
    """命中条件：意图 / 关键词 / 规则 id / 字段，全部为 None 时全命中。"""
    intent: list[str] | str | None = None
    keywords: list[str] | None = None
    rule_id: str | None = None
    field_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent, "keywords": self.keywords,
            "rule_id": self.rule_id, "field_name": self.field_name,
        }


@dataclass
class GoalOutput:
    """对 Agent 解释的产出要求。"""
    show_evidence: bool = True        # 是否必须给文件+行号
    show_attribution: bool = True     # 是否必须给归因残差
    max_steps: int = 12               # 推理步数上限（防止目标过度展开）

    def as_dict(self) -> dict[str, Any]:
        return {
            "show_evidence": self.show_evidence,
            "show_attribution": self.show_attribution,
            "max_steps": self.max_steps,
        }


@dataclass
class Goal:
    name: str
    describe: str
    match: GoalMatch
    priority: str = "low"
    focus: str = "top_n_events"       # 决定 Agent 走哪条路径
    output: GoalOutput = field(default_factory=GoalOutput)
    # 业务专属能力：只对某类目标生效的可选 hook
    action: str | None = None         # 预置动作（"screen_due_cert" / "missing_data" / None）
    note: str = ""                    # 写在解释里的人类可读说明

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "describe": self.describe, "match": self.match.as_dict(),
            "priority": self.priority, "focus": self.focus, "output": self.output.as_dict(),
            "action": self.action, "note": self.note,
        }


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ============================================================================
# 自然时间周期（单位：天）
# ----------------------------------------------------------------------------
# 「自然时间」在本项目里是一个**配置项**，不是硬编码的自然月：
#   周期 = 连续 N 天（N = config/period.yaml 的 days）
#   days=7  -> 一周；days=30 -> 一个自然月
# 窗口从 anchor 起每 N 天切一刀（固定长度，不是日历月）。
# ============================================================================
@dataclass
class PeriodConfig:
    """自然时间周期配置。单位固定为「天」。"""

    unit: str = "day"
    days: int = 30
    anchor: _dt.date = _dt.date(2025, 1, 1)
    label: str = ""

    # ---------- 展示 ----------
    @property
    def label_text(self) -> str:
        """展示名：显式配置优先，否则按天数推断（7→周，30→月）。"""
        if self.label:
            return self.label
        return {7: "周", 30: "月"}.get(self.days, f"{self.days}天")

    @property
    def unit_text(self) -> str:
        return "天"

    # ---------- 分桶 ----------
    def bucket_index(self, d: _dt.date) -> int:
        """日期落在第几个周期（从 anchor 起算，anchor 本身是第 0 期）。"""
        return (d - self.anchor).days // self.days

    def bucket_start(self, d: _dt.date) -> _dt.date:
        """周期窗口起始日。"""
        return self.anchor + _dt.timedelta(days=self.bucket_index(d) * self.days)

    def bucket_end_for(self, start: _dt.date) -> _dt.date:
        """给定窗口起始日，返回窗口结束日（含）。"""
        return start + _dt.timedelta(days=self.days - 1)

    def bucket_end(self, d: _dt.date) -> _dt.date:
        return self.bucket_end_for(self.bucket_start(d))

    def bucket_of(self, d: _dt.date) -> str:
        """周期标签 = 窗口起始日（YYYY-MM-DD）。"""
        return self.bucket_start(d).isoformat()

    def window_of(self, d: _dt.date) -> str:
        """周期窗口的可读形式："2025-01-01 ~ 2025-01-30"。"""
        start = self.bucket_start(d)
        return f"{start.isoformat()} ~ {self.bucket_end_for(start).isoformat()}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "unit_text": self.unit_text,
            "days": self.days,
            "label": self.label_text,
            "anchor": self.anchor.isoformat(),
            "example_window": self.window_of(self.anchor),
            "description": (
                f"统计周期 = 连续 {self.days} {self.unit_text}"
                f"（从 {self.anchor.isoformat()} 起每 {self.days} 天切一刀）"
            ),
        }


def load_period_config(path: str | Path) -> PeriodConfig:
    """加载自然时间周期配置。文件不存在时返回默认（30 天）。"""
    p = Path(path)
    if not p.exists():
        return PeriodConfig()
    data = _read_yaml(p)
    raw = data.get("period", {}) or {}

    unit = str(raw.get("unit", "day")).strip().lower()
    if unit not in {"day", "天"}:
        raise ValueError(f"period.unit 只支持「天」(day)：{p}（当前 {unit}）")

    try:
        days = int(raw.get("days", 30))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"period.days 必须是正整数：{p}") from exc
    if days <= 0:
        raise ValueError(f"period.days 必须是正整数：{p}（当前 {days}）")

    anchor_raw = str(raw.get("anchor", "2025-01-01")).strip()
    try:
        anchor = _dt.date.fromisoformat(anchor_raw)
    except ValueError as exc:
        raise ValueError(f"period.anchor 必须是 YYYY-MM-DD：{p}（当前 {anchor_raw}）") from exc

    return PeriodConfig(
        unit="day",
        days=days,
        anchor=anchor,
        label=str(raw.get("label", "") or ""),
    )


def load_dataset_config(path: str | Path) -> DatasetConfig:
    path = Path(path)
    data = _read_yaml(path)
    ds = data.get("dataset", {}) or {}
    ident = data.get("identity", {}) or {}
    derive_raw = data.get("derive", {}) or {}
    return DatasetConfig(
        id=str(ds.get("id", "default")),
        name=str(ds.get("name", ds.get("id", "default"))),
        sheet=ds.get("sheet"),
        identity=IdentityConfig(
            columns=list(ident.get("columns", [])),
            separator=str(ident.get("separator", "|")),
            require_identity=bool(ident.get("require_identity", True)),
            id_column=ident.get("id_column"),
            name_column=ident.get("name_column"),
        ),
        pii_columns=list(data.get("pii_columns", ["联系电话"]) or []),
        field_mapping=dict(data.get("field_mapping", {}) or {}),
        tracked_fields=list(data.get("tracked_fields", []) or []),
        aux_tracked_fields=list(data.get("aux_tracked_fields", []) or []),
        severity=dict(data.get("severity", {}) or {}),
        derive=DeriveConfig(
            class_code=dict(derive_raw.get("class_code", {}) or {}),
            outputs=list(derive_raw.get("outputs", []) or []),
            expiry=dict(derive_raw.get("expiry", {}) or {}),
        ),
        diff_rules=dict(data.get("diff_rules", {}) or {}),
        anomaly_rules=[
            AnomalyRule(id=str(r.get("id")), desc=str(r.get("desc", "")), severity=str(r.get("severity", "medium")))
            for r in (data.get("anomaly_rules", []) or [])
        ],
        cleaning=[
            CleaningRule(
                column=str(r["column"]),
                trim=bool(r.get("trim", True)),
                full_to_half=bool(r.get("full_to_half", False)),
                remove_chars=str(r.get("remove_chars", "") or ""),
                collapse_whitespace=bool(r.get("collapse_whitespace", False)),
                uppercase=bool(r.get("uppercase", False)),
                uppercase_x=bool(r.get("uppercase_x", False)),
            )
            for r in (data.get("cleaning", []) or [])
        ],
        raw=data,
    )


def load_metrics_config(path: str | Path) -> list[MetricDef]:
    data = _read_yaml(Path(path))
    out: list[MetricDef] = []
    for m in data.get("metrics", []) or []:
        out.append(
            MetricDef(
                key=str(m["key"]),
                name=str(m.get("name", m["key"])),
                agg=str(m.get("agg", "sum")),
                unit=str(m.get("unit", "")),
                decimals=int(m.get("decimals", 0)),
                desc=str(m.get("desc", "")),
                column=m.get("column"),
                equals=(str(m["equals"]) if m.get("equals") is not None else None),
                not_equals=(str(m["not_equals"]) if m.get("not_equals") is not None else None),
                exclude_values=[str(v) for v in (m.get("exclude_values") or [])],
                split=(str(m["split"]) if m.get("split") is not None else None),
                dedupe_columns=[str(c) for c in (m.get("dedupe_columns") or [])],
                group_columns=[str(c) for c in (m.get("group_columns") or [])],
                dimension_columns=[str(c) for c in (m.get("dimension_columns") or [])],
                depth_column=(str(m["depth_column"]) if m.get("depth_column") is not None else None),
                depth_split=(str(m["depth_split"]) if m.get("depth_split") is not None else None),
                depth_max_bucket=int(m.get("depth_max_bucket", 5)),
                dimension_name=(str(m["dimension_name"]) if m.get("dimension_name") is not None else None),
            )
        )
    if not out:
        raise ValueError(f"指标配置为空: {path}")
    return out


# Goals YAML 的 match 字段允许只填部分键；这里走一个直接 dict -> dataclass
# 路径，保持配置文件的简洁。output / priority 走同样的容忍式解析。
def load_goals_config(path: str | Path) -> list[Goal]:
    p = Path(path)
    if not p.exists():
        return []
    data = _read_yaml(p)
    raw = data.get("goals", []) or []
    if not isinstance(raw, list):
        raise ValueError(f"goals 配置必须是列表: {p}")
    out: list[Goal] = []
    for g in raw:
        m = g.get("match") or {}
        match = GoalMatch(
            intent=m.get("intent"),
            keywords=list(m["keywords"]) if m.get("keywords") else None,
            rule_id=m.get("rule_id"),
            field_name=m.get("field_name"),
        )
        o = g.get("output") or {}
        output = GoalOutput(
            show_evidence=bool(o.get("show_evidence", True)),
            show_attribution=bool(o.get("show_attribution", True)),
            max_steps=int(o.get("max_steps", 12)),
        )
        priority = str(g.get("priority", "low"))
        if priority not in PRIORITY_ORDER:
            raise ValueError(f"未知 priority: {priority}（{g.get('name')}）")
        out.append(Goal(
            name=str(g["name"]),
            describe=str(g.get("describe", "")),
            match=match,
            priority=priority,
            focus=str(g.get("focus", "top_n_events")),
            output=output,
            action=g.get("action"),
            note=str(g.get("note", "")),
        ))
    return out
