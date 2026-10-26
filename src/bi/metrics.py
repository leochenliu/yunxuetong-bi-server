"""指标口径执行：把派生列聚合成 3–5 个核心指标。

每个指标都能被差分事件解释：指标为什么变 = 哪些 added/removed/changed 事件
贡献了多少。这是第三阶段「指标对比」与第五阶段「异常归因」的共同地基。
"""

from __future__ import annotations

import re
from collections import defaultdict
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


@dataclass
class MetricBucket:
    """分桶指标的一格：某个维度取值下、某个分档的人数/量。

    例：dimension="月份", bucket="3月", group_key="2门", value=12
    表示「2026 年 3 月报名了 2 门课的人数 = 12」。

    与 `MetricValue` 的分工：标量指标进 `metric_snapshots`（每期一行），
    需要按维度切分的指标进 `metric_buckets`（每期多行）。
    """
    key: str
    name: str
    dimension: str
    bucket: str
    group_key: str
    value: float
    unit: str = ""
    desc: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_key": self.key,
            "name": self.name,
            "dimension": self.dimension,
            "bucket": self.bucket,
            "group_key": self.group_key,
            "value": self.value,
            "unit": self.unit,
            "desc": self.desc,
        }


def compute_metrics(ds: Dataset, metrics: list[MetricDef]) -> list[MetricValue]:
    out: list[MetricValue] = []
    for m in metrics:
        value = _compute_one(ds, m)
        out.append(MetricValue(key=m.key, name=m.name, value=value, unit=m.unit,
                               decimals=m.decimals, desc=m.desc))
    return out


def compute_bucket_metrics(ds: Dataset, metrics: list[MetricDef]) -> list[MetricBucket]:
    """计算分桶指标（按维度切分的人数分布）。

    与 `compute_metrics` 分开：标量指标每期一个值，分桶指标每期多行。
    """
    out: list[MetricBucket] = []
    for m in metrics:
        if m.agg != "count_by_group_depth":
            continue
        out.extend(_compute_group_depth(ds, m))
    return out


def _compute_group_depth(ds: Dataset, m: MetricDef) -> list[MetricBucket]:
    """按「维度分组内，不同对象数」的分布。

    业务含义（以本项目的月度报名深度为例）：
      配置 group_columns=[证件号码, 姓名]（谁来）、dimension_columns=[年份, 月份]（哪个月）、
      depth_column=课程（数什么）、depth_split=","（课程列是多值逗号分隔）。
      引擎会：先把行按 (证件号码,姓名,年份,月份) 归组 -> 组内把「课程」取值收集成集合 ->
      集合大小即该人当月报名的门数 -> 再把人数按门数分档（1门/2门/3门/4门/5门以上）落格。

    关键口径：
      - **同一人同一自然月**算作一组（跨月不复用计数，这是「按月」口径的本质）。
      - 组内按 depth_column 的**不同取值**计数。多值列（逗号分隔）先拆再去重。
      - 维度列为空的行归入「未填写」桶，不会被静默丢弃。
      - 只统计**有身份键**的组（与全局口径一致：无证件号不参与统计）。
    """
    group_cols = m.group_columns or []
    dim_cols = m.dimension_columns or []
    depth_col = m.depth_column or m.column or ""
    if not group_cols:
        raise ValueError(f"指标 {m.key} 使用 count_by_group_depth 但未配置 group_columns")
    if not dim_cols:
        raise ValueError(f"指标 {m.key} 使用 count_by_group_depth 但未配置 dimension_columns")
    if not depth_col:
        raise ValueError(f"指标 {m.key} 使用 count_by_group_depth 但未配置 depth_column")

    # 维度值 -> 身份键 -> 该人当月命中的不同深度取值集合
    # 结构：{dimension_label: {identity: set(depth_value)}}
    groups: dict[str, dict[str, set[str]]] = defaultdict(dict)

    for row in ds.rows:
        # 身份键：全空的组不计入（与「只统计有证件号码的行」口径一致）
        identity_parts = []
        for c in group_cols:
            v = row.get(c)
            text = "" if v is None else str(v).strip()
            if text:
                identity_parts.append(text)
        if not identity_parts:
            continue
        identity = "|".join(identity_parts)

        # 维度取值：缺失统一归到「未填写」，不静默丢弃
        dim_parts = []
        for c in dim_cols:
            v = row.get(c)
            text = "" if v is None else str(v).strip()
            dim_parts.append(text or "未填写")
        dimension = " ".join(dim_parts)

        # 组内深度取值：多值列拆分后并入该人的集合
        raw = row.get(depth_col)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        if m.depth_split:
            values = {p.strip() for p in text.split(m.depth_split) if p.strip()}
        else:
            values = {text}
        if not values:
            continue
        bucket = groups[dimension].setdefault(identity, set())
        bucket |= values

    out: list[MetricBucket] = []
    for dimension, per_identity in groups.items():
        # 门数 -> 人数
        by_depth: dict[int, int] = defaultdict(int)
        for values in per_identity.values():
            by_depth[len(values)] += 1
        for depth, count in by_depth.items():
            out.append(MetricBucket(
                key=m.key,
                name=m.name,
                dimension=m.dimension_name or dim_cols[-1],
                bucket=dimension,
                group_key=_depth_label(depth, m),
                value=float(count),
                unit=m.unit,
                desc=m.desc,
            ))
    # 维度按自然顺序（月份数字优先），组内按门数升序
    out.sort(key=lambda b: (_bucket_sort_key(b.bucket), _depth_sort_key(b.group_key)))
    return out


def _depth_label(depth: int, m: MetricDef) -> str:
    """把「报名门数」转成可读分档标签。"""
    if depth >= m.depth_max_bucket:
        return f"{m.depth_max_bucket}门以上"
    return f"{depth}门"


def _depth_sort_key(label: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)", label)
    return (int(m.group(1)) if m else 999, 0)


def _bucket_sort_key(bucket: str) -> tuple[int, str]:
    """月份类维度按数字排序，「未填写」永远排最后。"""
    if bucket == "未填写":
        return (9999, bucket)
    nums = [int(ch) for ch in bucket if ch.isdigit()]
    return (int("".join(str(n) for n in nums)) if nums else 9999, bucket)


def _compute_one(ds: Dataset, m: MetricDef) -> float | None:
    if m.is_row_count:
        return float(len(ds.rows))

    col = m.column or ""
    if m.agg == "count_distinct":
        vals = {str(r.get(col)).strip() for r in ds.rows if r.get(col) not in (None, "")}
        return float(len(vals))

    if m.agg == "count_distinct_multi":
        """按多个列组成的复合键去重计数。

        `dedupe_columns` 指定参与去重的列（如 证件号码/姓名/课程/年份/月份）。
        全部列都为空/缺值的行不计入。
        """
        cols = m.dedupe_columns or ([col] if col else [])
        if not cols:
            raise ValueError(f"指标 {m.key} 使用 count_distinct_multi 但未配置 dedupe_columns")
        keys: set[tuple[str, ...]] = set()
        for r in ds.rows:
            parts = []
            empty = True
            for c in cols:
                v = r.get(c)
                text = "" if v is None else str(v).strip()
                if text:
                    empty = False
                parts.append(text)
            if not empty:
                keys.add(tuple(parts))
        return float(len(keys))

    if m.agg == "count_equals":
        """统计某列取值满足条件的行数。

        - `equals`：精确匹配（可与 `split` 组合，用于逗号分隔的多选列）
        - `not_equals`：不等于该值也算命中（如「缴费情况 != 已缴费」）
        - `exclude_values`：排除一组取值（空值始终不计入命中）
        - `split`：按分隔符拆多值列，逐元素匹配

        三个条件都没配时 = 「只要有值就算」（配合 exclude_values 做减法）。
        """
        target = (m.equals or "").strip()
        not_target = (m.not_equals or "").strip()
        excluded = {v.strip() for v in m.exclude_values}
        if not target and not not_target and not excluded:
            raise ValueError(
                f"指标 {m.key} 使用 count_equals 但未配置 equals / not_equals / exclude_values"
            )
        hits = 0
        for r in ds.rows:
            raw = r.get(col)
            if raw in (None, ""):
                continue
            text = str(raw).strip()
            if m.split:
                parts = {p.strip() for p in text.split(m.split) if p.strip()}
                parts -= excluded
                if not parts:
                    continue
                if target:
                    if target in parts:
                        hits += 1
                elif not_target:
                    if not_target not in parts:
                        hits += 1
                else:
                    hits += 1
                continue
            if text in excluded:
                continue
            if target:
                if text == target:
                    hits += 1
            elif not_target:
                if text != not_target:
                    hits += 1
            else:
                hits += 1
        return float(hits)

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
