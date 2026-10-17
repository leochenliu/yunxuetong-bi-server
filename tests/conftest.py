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


@pytest.fixture(scope="session", autouse=True)
def _ensure_only_d1_d2(settings: Settings):
    """任何测试开始前，确保库里没有 4 个示例历史期（20260619/0719/0818/0917）。

    test_timeseries.py 自己也管理这 4 个 demo 期，但它的 demo_loaded 是函数级
    夹具，测试完会清。如果它跑完后其他测试再跑，库是干净的。但如果手动
    跑了入库脚本或在演示环境里 demo 期被持久化了，这条会自动清理。
    """
    from bi.store import FactStore

    DEMO = ("20260619", "20260719", "20260818", "20260917")
    store = FactStore(settings)
    with store.connect() as c:
        v_ids = [r["id"] for r in c.execute(
            "SELECT id FROM upload_versions WHERE batch IN (?,?,?,?)", DEMO
        ).fetchall()]
        for vid in v_ids:
            c.execute("DELETE FROM metric_snapshots WHERE version_id=?", (vid,))
            c.execute("DELETE FROM raw_records WHERE version_id=?", (vid,))
            c.execute("DELETE FROM anomalies WHERE version_id=?", (vid,))
        if v_ids:
            c.execute(f"DELETE FROM upload_versions WHERE id IN ({','.join('?' * len(v_ids))})", v_ids)
    yield


@pytest.fixture(scope="session", autouse=True)
def _disable_llm_in_tests():
    """测试套件：禁用 LLM 通道。LLM 走网络，测试应该快速且确定性。

    agent 测试断言基于本地模板的固定段落（【结论】等），不验证 LLM 输出。
    如需手动验证 LLM 路径，跑 `scripts/acceptance.py`（不强制走 LLM）。
    """
    import os
    saved = {k: os.environ.get(k) for k in ("BI_LLM_BASE_URL", "BI_LLM_API_KEY", "BI_LLM_MODEL")}
    for k in saved:
        os.environ[k] = ""  # 强制 get_settings() 视为"未配置"
    # 重新创建 settings 单例
    from bi import settings as _s
    _s._settings = None
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
    _s._settings = None


@pytest.fixture(scope="session")
def seeded(settings: Settings, batches) -> dict:
    """库中已灌入两期 + 差分事件。"""
    from bi.seed import seed_all

    return seed_all(BATCH_1, BATCH_2)
