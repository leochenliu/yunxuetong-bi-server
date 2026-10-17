"""统一 CLI：把五个阶段的操作收敛到一个入口。

用法（设置 PYTHONPATH=src，或 pip install -e . 后直接用 bi）：
  python -m bi.cli profile  [batch]            列画像与数据质量摸底
  python -m bi.cli ingest   <batch>            入站并落库（阶段一 + 阶段二）
  python -m bi.cli seed                        灌入所有批次并建立差分事件
  python -m bi.cli diff     <from> <to>        两个批次的差分明细
  python -m bi.cli metrics  [batch]            指标快照
  python -m bi.cli simulate <from> <to>        从真实数据派生下一期（含 ground truth）
  python -m bi.cli api                         启动 FastAPI 服务
  python -m bi.cli watch                       监控新批次并自动入库、分析
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# 允许直接 `python src/bi/cli.py` 运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bi.config import DatasetConfig, MetricDef, load_dataset_config, load_metrics_config  # noqa: E402
from bi.excel_reader import file_fingerprint  # noqa: E402
from bi.ingest import Ingested, ingest, resolve_input_file  # noqa: E402
from bi.settings import Settings, get_settings  # noqa: E402


@dataclass
class BatchIngestResult:
    dataset_id: str
    version_id: int
    filename: str
    records: int
    metrics: int
    anomalies: int
    cfg: DatasetConfig
    metrics_defs: list[MetricDef]
    ingested: Ingested


def load_import_sources(settings: Settings) -> list[dict[str, str]]:
    manifest_path = settings.config_dir / "import_sources.yaml"
    if not manifest_path.exists():
        return []
    with manifest_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sources = data.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError(f"sources 必须是列表: {manifest_path}")
    return sources


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


def ingest_batch(batch: str, settings: Settings) -> list[BatchIngestResult]:
    from bi.store import FactStore

    store = FactStore(settings)
    source_specs = load_import_sources(settings)

    batch_dir = settings.inbound_dir / batch
    configured_files = [
        (spec, batch_dir / spec["filename"])
        for spec in source_specs
        if (batch_dir / spec["filename"]).is_file()
    ]
    if configured_files:
        missing = [
            spec["filename"] for spec in source_specs
            if not (batch_dir / spec["filename"]).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"批次 {batch} 缺少配置的数据文件: {', '.join(missing)}"
            )
        results: list[BatchIngestResult] = []
        for spec, file_path in configured_files:
            cfg = load_dataset_config(settings.config_dir / spec["dataset_config"])
            metrics_defs = load_metrics_config(settings.config_dir / spec["metrics_config"])
            ds = ingest(batch, settings=settings, cfg=cfg, metrics_defs=metrics_defs,
                        file_path=file_path)
            vid = store.save_ingested(ds, cfg)
            results.append(BatchIngestResult(
                cfg.id, vid, file_path.name, len(ds.snapshot.records),
                len(ds.metrics), len(ds.anomalies), cfg, metrics_defs, ds,
            ))
        return results

    cfg = load_dataset_config(settings.dataset_config_path)
    metrics_defs = load_metrics_config(settings.metrics_config_path)
    ds = ingest(batch, settings=settings, cfg=cfg, metrics_defs=metrics_defs)
    vid = store.save_ingested(ds, cfg)
    return [BatchIngestResult(
        cfg.id, vid, Path(ds.source_file).name, len(ds.snapshot.records),
        len(ds.metrics), len(ds.anomalies), cfg, metrics_defs, ds,
    )]


def analyze_batch(batch: str, settings: Settings) -> list[BatchIngestResult]:
    from bi.anomaly import check_cross_period_anomalies
    from bi.diff_engine import diff_dataset_against_snapshot
    from bi.store import FactStore

    store = FactStore(settings)
    results = ingest_batch(batch, settings)
    for current in results:
        print(
            f"已入站数据集 {current.dataset_id}（批次 {batch}）"
            f"-> version_id={current.version_id}"
        )
        print(
            f"  文件 {current.filename}  记录数 {current.records}"
            f"  指标 {current.metrics}  异常 {current.anomalies}"
        )
        prior_versions = [
            version for version in store.list_versions(current.dataset_id)
            if version["batch"] < batch
        ]
        if not prior_versions:
            print(f"无同数据集的上一批次：{current.dataset_id}（{batch}），跳过差分")
            continue

        previous = max(prior_versions, key=lambda version: version["batch"])
        previous_source = Path(previous["source_file"])
        if file_fingerprint(previous_source)["sha256"] != previous["sha256"]:
            raise RuntimeError(
                f"上一批次源文件指纹已变化，拒绝生成不可靠差分：{previous_source}"
            )
        previous_data = ingest(
            previous["batch"],
            settings=settings,
            cfg=current.cfg,
            metrics_defs=current.metrics_defs,
            file_path=previous_source,
        )
        diff = diff_dataset_against_snapshot(
            previous_data.snapshot, current.ingested.dataset, current.cfg,
            previous["batch"], batch,
        )
        previous_version_id = store.version_id_of(
            previous["batch"], current.dataset_id
        )
        if previous_version_id is None:
            raise RuntimeError(
                f"找不到上一批次版本：{current.dataset_id}/{previous['batch']}"
            )
        event_count = store.save_diff(
            diff,
            current.cfg,
            previous_version_id,
            current.version_id,
        )
        cross_period = check_cross_period_anomalies(diff, current.cfg)
        if cross_period:
            store.save_cross_period_anomalies(cross_period, current.version_id)
        print(
            f"分析完成 {current.dataset_id}：{previous['batch']} -> {batch}，"
            f"差分事件 {event_count} 条，跨期异常 {len(cross_period)} 条"
        )
    return results


def cmd_ingest(batch: str) -> None:
    analyze_batch(batch, get_settings())


def cmd_watch() -> None:
    from bi.watcher import watch_inbound

    watch_inbound(get_settings())


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
        elif cmd == "watch":
            cmd_watch()
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
