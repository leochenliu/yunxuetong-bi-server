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

from .anomaly import summarize, check_cross_period_anomalies
from .config import load_dataset_config
from .diff_engine import diff_dataset_against_snapshot
from .ingest import ingest
from .settings import get_settings
from .simulate import simulate_next_batch
from .store import FactStore

BATCH_1 = "20261012"
BATCH_2 = "20261013"


def _force_utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def seed_all(from_batch: str = BATCH_1, to_batch: str = BATCH_2) -> dict:
    _force_utf8()
    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    store = FactStore(settings)

    d2_dir = settings.inbound_dir / to_batch
    if not d2_dir.is_dir() or not any(d2_dir.glob("*.xlsx")):
        print(f"[1/4] D2（{to_batch}）不存在，从真实 D1 派生…")
        simulate_next_batch(from_batch, to_batch)
    else:
        print(f"[1/4] D2（{to_batch}）已存在，跳过派生")

    print(f"[2/4] 入站 {from_batch} …")
    d1 = ingest(from_batch, settings=settings, cfg=cfg)
    v1 = store.save_ingested(d1, cfg)
    print(f"      version_id={v1}  记录 {len(d1.snapshot.records)}  指标 {len(d1.metrics)}  异常 {len(d1.anomalies)}")

    print(f"[3/4] 入站 {to_batch} …")
    d2 = ingest(to_batch, settings=settings, cfg=cfg)
    v2 = store.save_ingested(d2, cfg)
    print(f"      version_id={v2}  记录 {len(d2.snapshot.records)}  指标 {len(d2.metrics)}  异常 {len(d2.anomalies)}")

    print(f"[4/4] 建立差分事件 {from_batch} -> {to_batch} …")
    result = diff_dataset_against_snapshot(d1.snapshot, d2.dataset, cfg, from_batch, to_batch)
    n_events = store.save_diff(result, cfg, v1, v2)
    cross = check_cross_period_anomalies(result, cfg)
    if cross:
        store.save_cross_period_anomalies(cross, v2)
    print(f"      事件 {n_events} 条（新增 {len(result.added)} / 移除 {len(result.removed)} / "
          f"变更 {result.event_count}）  跨期异常 {len(cross)}")

    summary = store.diff_summary(from_batch, to_batch)
    print("\n差分汇总:")
    print(f"  按类型 {summary['by_type']}")
    print(f"  按严重度 {summary['by_severity']}")
    print("  按字段:")
    for f in summary["by_field"]:
        print(f"    {f['field']:<10} {f['count']:>4}  [{f['severity']}]")

    print(f"\n库文件: {settings.db_path}")
    return {"v1": v1, "v2": v2, "events": n_events, "summary": summary}
