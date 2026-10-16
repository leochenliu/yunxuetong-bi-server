"""pytest 共享夹具。

设计要点：夹具按需求生成数据（D2 不存在时自动从真实 D1 派生），
因此 `pytest` 在全新克隆的仓库里也能直接跑通。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bi.config import DatasetConfig, MetricDef, load_dataset_config, load_metrics_config  # noqa: E402
from bi.settings import Settings, get_settings  # noqa: E402

BATCH_1 = "20261012"
BATCH_2 = "20261013"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def cfg(settings: Settings) -> DatasetConfig:
    return load_dataset_config(settings.dataset_config_path)


@pytest.fixture(scope="session")
def metrics_defs(settings: Settings) -> list[MetricDef]:
    return load_metrics_config(settings.metrics_config_path)


@pytest.fixture(scope="session")
def batches(settings: Settings, cfg: DatasetConfig):
    """确保 D1/D2 就绪，返回两期的 ingest 结果。"""
    from bi.ingest import ingest
    from bi.simulate import simulate_next_batch

    d2_dir = settings.inbound_dir / BATCH_2
    if not d2_dir.is_dir() or not any(d2_dir.glob("*.xlsx")):
        simulate_next_batch(BATCH_1, BATCH_2, write=True)

    d1 = ingest(BATCH_1, settings=settings, cfg=cfg)
    d2 = ingest(BATCH_2, settings=settings, cfg=cfg)
    return d1, d2


@pytest.fixture(scope="session")
def ground_truth(settings: Settings, batches) -> dict:
    """D2 生成时记录的 ground truth（依赖 batches 以确保已生成）。"""
    p = settings.project_root / "data" / "store" / f"ground_truth_{BATCH_1}_{BATCH_2}.json"
    assert p.exists(), f"ground truth 不存在: {p}"
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def seeded(settings: Settings, batches) -> dict:
    """库中已灌入两期 + 差分事件。"""
    from bi.seed import seed_all

    return seed_all(BATCH_1, BATCH_2)
