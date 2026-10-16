"""统一 CLI：把五个阶段的操作收敛到一个入口。

用法（设置 PYTHONPATH=src，或 pip install -e . 后直接用 bi）：
  python -m bi.cli profile  [batch]            列画像与数据质量摸底
  python -m bi.cli ingest   <batch>            入站并落库（阶段一 + 阶段二）
  python -m bi.cli seed                        灌入所有批次并建立差分事件
  python -m bi.cli diff     <from> <to>        两个批次的差分明细
  python -m bi.cli metrics  [batch]            指标快照
  python -m bi.cli simulate <from> <to>        从真实数据派生下一期（含 ground truth）
  python -m bi.cli api                         启动 FastAPI 服务
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许直接 `python src/bi/cli.py` 运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bi.config import load_dataset_config, load_metrics_config  # noqa: E402
from bi.ingest import ingest, resolve_input_file  # noqa: E402
from bi.settings import get_settings  # noqa: E402


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_profile(batch: str | None) -> None:
    """阶段一：数据摸底——列类型、空值率、去重、Top 值、质量问题。"""
    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    path = resolve_input_file(settings.inbound_dir, batch) if batch else None

    ds = ingest(batch, settings=settings, cfg=cfg, file_path=path) if batch else None
    if ds is None:
        print("用法: profile [batch]，例如 python -m bi.cli profile 20261012")
        return

    raw_rows = ds.dataset.rows
    print(f"批次 {ds.batch}  文件 {Path(ds.source_file).name}")
    print(f"工作表 {ds.dataset.sheet}  列数 {len(ds.dataset.columns)}  参与统计行数 {len(raw_rows)}")
    print("口径备注:")
    for n in ds.notes:
        print(f"  - {n}")
    print("\n指标快照:")
    for m in ds.metrics:
        print(f"  {m.key:<16} {m.display():>12}   {m.desc}")
    print("\n异常汇总:")
    _dump(ds.anomaly_summary())
    print("\n高频字段样例（前 3 个追踪字段的 Top5）:")
    for col in cfg.tracked_fields[:3]:
        if col not in ds.dataset.columns:
            continue
        counter: dict[str, int] = {}
        for r in raw_rows:
            v = str(r.get(col) or "").strip() or "(空)"
            counter[v] = counter.get(v, 0) + 1
        top = sorted(counter.items(), key=lambda kv: -kv[1])[:5]
        print(f"  {col}: " + ", ".join(f"{k}×{v}" for k, v in top))


def cmd_metrics(batch: str | None) -> None:
    if not batch:
        print("用法: metrics <batch>")
        return
    ds = ingest(batch)
    print(f"批次 {ds.batch}（{len(ds.dataset.rows)} 人）")
    for m in ds.metrics:
        print(f"  {m.name:<12} {m.display():>10}")


def cmd_diff(batch_from: str, batch_to: str) -> None:
    from bi.diff_engine import diff_dataset_against_snapshot

    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    prev = ingest(batch_from, settings=settings, cfg=cfg)
    cur = ingest(batch_to, settings=settings, cfg=cfg)
    result = diff_dataset_against_snapshot(prev.snapshot, cur.dataset, cfg, batch_from, batch_to)

    print(f"差分 {batch_from} -> {batch_to}")
    print(f"  上期 {result.count_from} 人 / 本期 {result.count_to} 人")
    print(f"  新增 {len(result.added)}  移除 {len(result.removed)}  "
          f"变更 {len(result.changed)}（{result.event_count} 个字段级事件）  未变 {result.unchanged}")
    print("  字段维度:")
    for f in result.field_summary:
        print(f"    {f['field']:<10} {f['count']:>4}  [{f['severity']}]")
    if result.changed:
        print("  前 5 条变更:")
        for rc in result.changed[:5]:
            detail = "; ".join(f"{c.field}: {c.from_display or '(空)'}→{c.to_display or '(空)'}" for c in rc.changes)
            print(f"    {rc.name or rc.id_number}: {detail}")


def cmd_ingest(batch: str) -> None:
    from bi.store import FactStore

    settings = get_settings()
    store = FactStore(settings)
    ds = ingest(batch, settings=settings)
    vid = store.save_ingested(ds)
    print(f"已入站批次 {batch} -> version_id={vid}")
    print(f"  记录数 {len(ds.snapshot.records)}  指标 {len(ds.metrics)}  异常 {len(ds.anomalies)}")


def cmd_seed() -> None:
    from bi.seed import seed_all

    seed_all()


def cmd_simulate(batch_from: str, batch_to: str) -> None:
    from bi.simulate import simulate_next_batch

    simulate_next_batch(batch_from, batch_to)


def cmd_api() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("bi.api:app", host=settings.api_host, port=settings.api_port, reload=False)


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    cmd, *rest = argv
    try:
        if cmd == "profile":
            cmd_profile(rest[0] if rest else None)
        elif cmd == "metrics":
            cmd_metrics(rest[0] if rest else None)
        elif cmd == "diff":
            cmd_diff(rest[0], rest[1])
        elif cmd == "ingest":
            cmd_ingest(rest[0])
        elif cmd == "seed":
            cmd_seed()
        elif cmd == "simulate":
            cmd_simulate(rest[0], rest[1])
        elif cmd == "api":
            cmd_api()
        else:
            print(__doc__)
            return 1
    except (IndexError, KeyError) as e:
        print(f"参数缺失: {e}\n{__doc__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
