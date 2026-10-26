"""按「自然时间周期」对记录分组（口径来自 config/period.yaml，单位：天）。

为什么不是一个写死的「自然月」
----------------------------
业务问的统计周期是可变的：一周（7 天）看招生节奏，一个自然月（30 天）看经营盘面。
所以周期长度做成配置项，引擎只负责「按配置把日期切进窗口」：

    周期 = 连续 N 天（N = config/period.yaml 的 days）
    窗口 = [anchor + k*N, anchor + (k+1)*N)

分组是在**查询时**算的（不落库、不改派生列），因此改配置只需刷新页面，
不会影响差分事件、指标快照与 ground truth。
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from typing import Any

from .config import PeriodConfig
from .values import to_date


def group_by_period(
    rows: list[dict[str, Any]],
    date_field: str,
    period: PeriodConfig,
    *,
    status_field: str | None = None,
    empty_status: str = "未填写",
) -> tuple[list[dict[str, Any]], int]:
    """按周期把行分组。

    返回 ``(buckets, unassigned)``：

    - ``buckets[i]`` = ``{"period": 窗口起始日, "period_end": 窗口结束日,
      "count": 行数, "statuses": [...]}``（``status_field`` 给了才有 statuses）
    - ``unassigned`` = 该字段取不到日期、无法归组的行数（调用方应把它显示出来，
      不允许让分母悄悄变小）
    """
    counts: Counter[str] = Counter()
    status_counts: Counter[tuple[str, str]] = Counter()
    unassigned = 0

    for row in rows:
        d = to_date(row.get(date_field))
        if d is None:
            unassigned += 1
            continue
        key = period.bucket_of(d)
        counts[key] += 1
        if status_field:
            status = str(row.get(status_field) or "").strip() or empty_status
            status_counts[(key, status)] += 1

    buckets: list[dict[str, Any]] = []
    for key in sorted(counts):
        start = _dt.date.fromisoformat(key)
        item: dict[str, Any] = {
            "period": key,
            "period_end": period.bucket_end_for(start).isoformat(),
            "count": counts[key],
        }
        if status_field:
            item["statuses"] = [
                {"label": status, "count": count}
                for (bucket, status), count in sorted(status_counts.items())
                if bucket == key
            ]
        buckets.append(item)
    return buckets, unassigned
