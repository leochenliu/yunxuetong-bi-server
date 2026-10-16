"""指标口径执行：把派生列聚合成 3–5 个核心指标。

每个指标都能被差分事件解释：指标为什么变 = 哪些 added/removed/changed 事件
贡献了多少。这是第三阶段「指标对比」与第五阶段「异常归因」的共同地基。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import MetricDef
from .excel_reader import Dataset
from .values import to_number


@dataclass
class MetricValue:
    key: str
    name: str
    value: float | None
    unit: str = ""
    decimals: int = 0
    desc: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_key": self.key,
            "name": self.name,
            "value": self.value,
            "display": self.display(),
            "unit": self.unit,
            "decimals": self.decimals,
            "desc": self.desc,
        }

    def display(self) -> str:
        if self.value is None:
            return "-"
        if self.decimals <= 0 and float(self.value).is_integer():
            return f"{int(self.value)}{self.unit}"
        return f"{self.value:.{self.decimals}f}{self.unit}"


def compute_metrics(ds: Dataset, metrics: list[MetricDef]) -> list[MetricValue]:
    out: list[MetricValue] = []
    for m in metrics:
        value = _compute_one(ds, m)
        out.append(MetricValue(key=m.key, name=m.name, value=value, unit=m.unit,
                               decimals=m.decimals, desc=m.desc))
    return out


def _compute_one(ds: Dataset, m: MetricDef) -> float | None:
    if m.is_row_count:
        return float(len(ds.rows))

    col = m.column or ""
    if m.agg == "count_distinct":
        vals = {str(r.get(col)).strip() for r in ds.rows if r.get(col) not in (None, "")}
        return float(len(vals))

    nums = [x for x in (to_number(r.get(col)) for r in ds.rows) if x is not None]
    if not nums:
        return None
    if m.agg == "sum":
        return float(sum(nums))
    if m.agg == "avg":
        return float(sum(nums) / len(nums))
    if m.agg == "min":
        return float(min(nums))
    if m.agg == "max":
        return float(max(nums))
    if m.agg == "count":
        return float(len(nums))
    raise ValueError(f"不支持的聚合函数: {m.agg}（指标 {m.key}）")


def metrics_to_dict(values: list[MetricValue]) -> dict[str, float | None]:
    return {v.key: v.value for v in values}
