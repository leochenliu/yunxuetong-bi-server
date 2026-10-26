"""灌数据：把批次落库、建立差分事件、跑跨期异常。

    python -m bi.cli seed

流程：
  1. 若 D2 不存在 -> 从真实 D1 派生（simulate_next_batch）
  2. D1 入站落库（upload_versions / raw_records / metric_snapshots / anomalies）
  3. D2 入站落库
  4. D1 -> D2 差分落库（diff_events）+ 跨期异常
"""

from __future__ import annotations

import sys

from .cli import analyze_batch
from .settings import get_settings
from .simulate import simulate_next_batch

BATCH_1 = "20261012"
BATCH_2 = "20261013"


def _force_utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def seed_all(from_batch: str | None = None, to_batch: str | None = None) -> dict:
    _force_utf8()
    settings = get_settings()

    inbound = settings.inbound_dir
    batch_dirs = sorted(
        d.name for d in inbound.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if from_batch and to_batch:
        # 区间模式（测试 / 指定两期）：只处理闭区间内的批次
        batch_dirs = [b for b in batch_dirs if from_batch <= b <= to_batch]
        # 兼容旧行为：若 to_batch 缺失则先从 from_batch 派生
        to_dir = inbound / to_batch
        if not to_dir.is_dir() or not any(to_dir.glob("*.xlsx")):
            print(f"[seed] {to_batch} 不存在，从 {from_batch} 派生…")
            simulate_next_batch(from_batch, to_batch)
    elif from_batch:
        batch_dirs = [b for b in batch_dirs if b >= from_batch]

    print(f"[seed] 待处理批次：{', '.join(batch_dirs) or '(无)'}")
    summary: dict[str, list[str]] = {}
    for b in batch_dirs:
        try:
            results = analyze_batch(b, settings)
            summary[b] = [r.dataset_id for r in results]
            print(f"  ✓ {b}: {', '.join(r.dataset_id for r in results)}")
        except Exception as e:
            print(f"  ✗ {b} 处理失败: {e}")
    return summary
