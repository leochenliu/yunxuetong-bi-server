"""入站编排：一个批次的 Excel -> 数据集 + 快照 + 指标 + 异常。

这是阶段一与阶段二的接缝：阶段一产出这些内存对象，阶段二的 store 负责落库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .anomaly import Anomaly, check_row_anomalies, summarize
from .config import DatasetConfig, MetricDef, load_dataset_config, load_metrics_config
from .derive import derive
from .diff_engine import Snapshot, build_snapshot
from .excel_reader import Dataset, file_fingerprint, read_primary
from .metrics import MetricValue, compute_metrics
from .settings import Settings, get_settings

TEMP_PREFIXES = ("~$", ".~lock")


@dataclass
class Ingested:
    batch: str
    dataset: Dataset
    snapshot: Snapshot
    metrics: list[MetricValue]
    anomalies: list[Anomaly]
    fingerprint: dict[str, Any]
    source_file: str
    notes: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.dataset.rows)

    def anomaly_summary(self) -> dict[str, Any]:
        return summarize(self.anomalies)


def resolve_input_file(inbound_dir: Path, batch: str, glob: str = "*.xlsx") -> Path:
    """在 <inbound>/<batch>/ 下找 Excel。忽略 Excel/LibreOffice 的临时文件。"""
    d = Path(inbound_dir) / str(batch)
    if not d.is_dir():
        raise FileNotFoundError(f"批次目录不存在: {d}")
    candidates = [
        p for p in sorted(d.glob(glob))
        if not p.name.startswith(TEMP_PREFIXES) and not p.name.startswith("~")
    ]
    if not candidates:
        raise FileNotFoundError(f"批次目录内没有 Excel 文件: {d}")
    if len(candidates) > 1:
        # 多个文件时优先名字含「总表」的（本项目约定）
        preferred = [p for p in candidates if "总表" in p.name]
        if preferred:
            return preferred[0]
    return candidates[0]


def ingest(
    batch: str,
    *,
    settings: Settings | None = None,
    cfg: DatasetConfig | None = None,
    metrics_defs: list[MetricDef] | None = None,
    file_path: str | Path | None = None,
) -> Ingested:
    settings = settings or get_settings()
    cfg = cfg or load_dataset_config(settings.dataset_config_path)
    metrics_defs = metrics_defs or load_metrics_config(settings.metrics_config_path)

    path = Path(file_path) if file_path else resolve_input_file(settings.inbound_dir, batch)

    ds = read_primary(path, sheet=cfg.sheet)
    derive(ds, cfg)
    snap = build_snapshot(ds, cfg)
    metric_values = compute_metrics(ds, metrics_defs)
    anomalies = check_row_anomalies(ds, cfg)

    return Ingested(
        batch=str(batch),
        dataset=ds,
        snapshot=snap,
        metrics=metric_values,
        anomalies=anomalies,
        fingerprint=file_fingerprint(path),
        source_file=str(path),
        notes=list(ds.notes),
    )
