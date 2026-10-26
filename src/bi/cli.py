"""统一 CLI：把五个阶段的操作收敛到一个入口。

用法（设置 PYTHONPATH=src，或 pip install -e . 后直接用 bi）：
  python -m bi.cli profile  [batch]            列画像与数据质量摸底
  python -m bi.cli ingest   <batch> [--file]   入站并落库（阶段一 + 阶段二）
                                               --file 指定任意路径的 xlsx（如 data/cleaned 下的清洗产物）
                                               --allow-duplicate-file  允许同一份文件占两个批次
                                               --force-large-drop      允许本期记录数骤降
  python -m bi.cli seed                        灌入所有批次并建立差分事件
  python -m bi.cli diff     <from> <to>        两个批次的差分明细
  python -m bi.cli metrics  [batch]            指标快照
  python -m bi.cli simulate <from> <to>        从真实数据派生下一期（含 ground truth）
  python -m bi.cli job-status [job_id] [days]  定时任务执行日历（缺口 = 漏跑）
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
from bi.ingest import Ingested, TEMP_PREFIXES, ingest, resolve_input_file  # noqa: E402
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


# 入站后需要重建实体表的类目（其 version_id 外键是 ON DELETE CASCADE）
DATASET_CERTIFICATES = "certificates"


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


def _match_spec(file_name: str, specs: list[dict]) -> dict | None:
    """按「文件名包含的片段」匹配一条 source 规则。

    匹配键 = spec 里的 ``match``（若存在）或 ``filename`` 字段。
    这是「按文件名头 N 字区分项目」的通用实现：命中即路由。
    """
    for spec in specs:
        token = (spec.get("match") or spec.get("filename") or "").strip()
        if token and token in file_name:
            return spec
    return None


def match_sources_for_batch(
    batch_dir: Path,
    specs: list[dict],
    require_xlsx: bool = True,
) -> list[tuple[dict, Path]]:
    """把批次目录里的 Excel 按 import_sources.yaml 路由到对应数据集配置。

    返回 [(spec, file_path), ...]：每份命中的文件独立成项（不再要求所有配置齐备）。
    匹配失败的文件不会进入结果（回退逻辑由调用方处理）。
    """
    out: list[tuple[dict, Path]] = []
    if not batch_dir.is_dir():
        return out
    for p in sorted(batch_dir.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith(TEMP_PREFIXES) or p.name.startswith("~"):
            continue
        if require_xlsx and p.suffix.lower() != ".xlsx":
            continue
        spec = _match_spec(p.name, specs)
        if spec:
            out.append((spec, p))
    return out


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


def _guard_before_save(store, ds, cfg, batch: str, *,
                       allow_duplicate_file: bool = False,
                       force_large_drop: bool = False) -> int | None:
    """入站前的三道护栏（防重复、防误判）。返回已有 version_id 表示「无需写入」。

    1. 同批次 + 指纹相同 -> 幂等短路：不写库、version_id 不变（严格幂等）；
    2. 同一份文件（sha256 相同）出现在**另一个批次** -> 拒绝，避免同一份台账占两期快照；
    3. 本批记录数不足上一期 50% -> 拒绝，避免把「增量台账」当「全量台账」导入。
    """
    store.init_schema()  # 空库时表还不存在（原来靠 save_ingested 顺手建）
    sha = (ds.fingerprint or {}).get("sha256")
    if not sha:
        return None
    versions = store.list_versions(cfg.id)

    same_batch = [v for v in versions if v["batch"] == batch]
    if same_batch and same_batch[0]["sha256"] == sha:
        return int(same_batch[0]["id"])

    others = [v for v in versions if v["batch"] != batch and v["sha256"] == sha]
    if others and not allow_duplicate_file:
        raise RuntimeError(
            f"这份文件已作为批次 {others[0]['batch']} 入站过（sha256 相同）。"
            "同一份文件不应占两个批次快照；确实需要请加 --allow-duplicate-file"
        )

    prior = [v for v in versions if v["batch"] < batch]
    if prior and not force_large_drop:
        previous = max(prior, key=lambda v: v["batch"])
        current_rows = len(ds.snapshot.records)
        previous_rows = int(previous["record_count"] or 0)
        if previous_rows and current_rows < previous_rows * 0.5:
            raise RuntimeError(
                f"本期只有 {current_rows} 条，上一期 {previous['batch']} 有 {previous_rows} 条"
                "（不足 50%）。疑似把「增量台账」当「全量台账」导入；"
                "确认无误请加 --force-large-drop"
            )
    return None


def ingest_batch(
    batch: str,
    settings: Settings,
    file_path: Path | None = None,
    *,
    allow_duplicate_file: bool = False,
    force_large_drop: bool = False,
) -> list[BatchIngestResult]:
    from bi.store import FactStore

    store = FactStore(settings)
    source_specs = load_import_sources(settings)

    if file_path is not None:
        # 显式指定文件：仍按文件名片段路由到数据集配置，但不扫批次目录。
        # 用于把「不在 data/inbound 下」的文件（如 scripts/clean_cert_validity.py 的
        # 清洗产物 data/cleaned/xxx.cleaned.xlsx）入站到事实库。
        explicit = Path(file_path)
        if not explicit.is_file():
            raise FileNotFoundError(f"指定的入站文件不存在：{explicit}")
        spec = _match_spec(explicit.name, source_specs)
        if spec is None:
            raise RuntimeError(
                f"无法按文件名路由到数据集：{explicit.name}"
                f"（检查 config/import_sources.yaml 的 match 片段）"
            )
        cfg = load_dataset_config(settings.config_dir / spec["dataset_config"])
        metrics_defs = load_metrics_config(settings.config_dir / spec["metrics_config"])
        ds = ingest(
            batch, settings=settings, cfg=cfg, metrics_defs=metrics_defs,
            file_path=explicit,
        )
        skipped = _guard_before_save(
            store, ds, cfg, batch,
            allow_duplicate_file=allow_duplicate_file,
            force_large_drop=force_large_drop,
        )
        if skipped is not None:
            print(f"[跳过] {cfg.id}/{batch} 文件指纹未变化（version_id={skipped}），未写库")
            return []
        vid = store.save_ingested(
            ds, cfg, extra_notes=[f"入站来源：显式指定文件 {explicit.name}"]
        )
        return [BatchIngestResult(
            cfg.id, vid, explicit.name, len(ds.snapshot.records),
            len(ds.metrics), len(ds.anomalies), cfg, metrics_defs, ds,
        )]

    batch_dir = settings.inbound_dir / batch
    matched = match_sources_for_batch(batch_dir, source_specs)
    if matched:
        results: list[BatchIngestResult] = []
        for spec, file_path in matched:
            cfg = load_dataset_config(settings.config_dir / spec["dataset_config"])
            metrics_defs = load_metrics_config(settings.config_dir / spec["metrics_config"])
            ds = ingest(batch, settings=settings, cfg=cfg, metrics_defs=metrics_defs,
                        file_path=file_path)
            skipped = _guard_before_save(
                store, ds, cfg, batch,
                allow_duplicate_file=allow_duplicate_file,
                force_large_drop=force_large_drop,
            )
            if skipped is not None:
                print(f"[跳过] {cfg.id}/{batch} 文件指纹未变化（version_id={skipped}），未写库")
                continue
            vid = store.save_ingested(ds, cfg)
            results.append(BatchIngestResult(
                cfg.id, vid, file_path.name, len(ds.snapshot.records),
                len(ds.metrics), len(ds.anomalies), cfg, metrics_defs, ds,
            ))
        return results

    # 回退：没有任何文件命中 import_sources，使用默认单数据集配置（兼容旧批次 / 单项目）
    cfg = load_dataset_config(settings.dataset_config_path)
    metrics_defs = load_metrics_config(settings.metrics_config_path)
    ds = ingest(batch, settings=settings, cfg=cfg, metrics_defs=metrics_defs)
    skipped = _guard_before_save(
        store, ds, cfg, batch,
        allow_duplicate_file=allow_duplicate_file,
        force_large_drop=force_large_drop,
    )
    if skipped is not None:
        print(f"[跳过] {cfg.id}/{batch} 文件指纹未变化（version_id={skipped}），未写库")
        return []
    vid = store.save_ingested(ds, cfg)
    return [BatchIngestResult(
        cfg.id, vid, Path(ds.source_file).name, len(ds.snapshot.records),
        len(ds.metrics), len(ds.anomalies), cfg, metrics_defs, ds,
    )]


def analyze_batch(
    batch: str,
    settings: Settings,
    file_path: Path | None = None,
    *,
    allow_duplicate_file: bool = False,
    force_large_drop: bool = False,
) -> list[BatchIngestResult]:
    from bi.anomaly import check_cross_period_anomalies
    from bi.diff_engine import diff_dataset_against_snapshot
    from bi.store import FactStore

    store = FactStore(settings)
    results = ingest_batch(
        batch, settings, file_path=file_path,
        allow_duplicate_file=allow_duplicate_file, force_large_drop=force_large_drop,
    )
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

    # 重新入站会先删旧版本，而 certificates 的 version_id 外键是 ON DELETE CASCADE
    # —— 三张实体表里的证书行会被一起删掉。必须在入站后立刻重建，否则台账明明导完了，
    # 实体表却是空的（students / companies 还留着旧数据，看着像「三表不一致」）。
    if any(current.dataset_id == DATASET_CERTIFICATES for current in results):
        print("[实体表] 台账已入站 -> 重建实体表快照（否则会被级联删除后留空）")
        try:
            code = cmd_refresh_entities(["--trigger", "ingest"])
        except SystemExit as exc:  # 重建失败不能把「入站成功」这件事一起吞掉
            code = int(exc.code or 1)
        if code != 0:
            print(f"[警告] 实体表重建返回 {code}，请手动跑 python -m bi.cli refresh-entities")
    return results


def cmd_ingest(batch: str, file_path: Path | None = None,
               allow_duplicate_file: bool = False, force_large_drop: bool = False) -> None:
    analyze_batch(
        batch, get_settings(), file_path=file_path,
        allow_duplicate_file=allow_duplicate_file, force_large_drop=force_large_drop,
    )


def cmd_refresh_entities(extra: list[str] | None = None) -> int:
    """重建三张实体表并写 job_runs（供任务计划 / cron 调用的统一入口）。

    直接复用 scripts/build_entity_tables.py 的 main，不 subprocess —— 否则
    退出码、锁、审计日志全都对不上。
    """
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from build_entity_tables import main as build_main

    return build_main(list(extra or []))


def cmd_job_status(job_id: str | None = None, days: int = 14) -> None:
    """定时任务执行日历：缺口（missed）一眼可见。"""
    from bi.jobs import JOBS, job_health
    from bi.store import FactStore

    store = FactStore()
    marks = {"success": "OK  ", "failed": "FAIL", "partial": "PART", "missed": "----"}
    for jid in ([job_id] if job_id else list(JOBS)):
        health = job_health(store, jid, days)
        flag = "正常" if health["healthy"] else "**有缺口**"
        print(f"[{jid}] {health['title']} -> {flag}")
        print(f"  计划 {health['daily_at']} {health['timezone']}"
              f"｜最近一次 {health['last_run_date'] or '从未'}"
              f"｜缺口 {len(health['missing'])} 天"
              f"｜未成功 {len(health['unhealthy'])} 天")
        for item in health["calendar"]:
            print(f"  {item['date']}  {marks[item['status']]:<5}{item['status']}")


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


def _split_file_arg(args: list[str]) -> tuple[str, Path | None, set[str]]:
    """从 ingest 的参数里拆出 `<batch>`、可选的 `--file <path>` 与其余开关。

    例：`ingest certificates --file data/cleaned/xxx.cleaned.xlsx --force-large-drop`
    """
    if not args:
        raise SystemExit("用法：python -m bi.cli ingest <batch> [--file <xlsx>]")
    rest = list(args)
    file_path: Path | None = None
    if "--file" in rest:
        index = rest.index("--file")
        if index + 1 >= len(rest):
            raise SystemExit("--file 后面缺少文件路径")
        file_path = Path(rest[index + 1])
        rest = rest[:index] + rest[index + 2:]
    flags = {item for item in rest[1:] if item.startswith("--")}
    if not rest:
        raise SystemExit("用法：python -m bi.cli ingest <batch> [--file <xlsx>]")
    return rest[0], file_path, flags


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
            batch_arg, explicit_file, flags = _split_file_arg(rest)
            cmd_ingest(
                batch_arg, explicit_file,
                allow_duplicate_file="--allow-duplicate-file" in flags,
                force_large_drop="--force-large-drop" in flags,
            )
        elif cmd == "job-status":
            cmd_job_status(rest[0] if rest else None,
                           int(rest[1]) if len(rest) > 1 else 14)
        elif cmd == "refresh-entities":
            return cmd_refresh_entities(rest)
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
