"""时间序列 + 历史期测试。

注意：本文件独立管理 4 个历史期（is_demo=True），与
conftest.py 的 `seeded` 夹具**解耦**——`seeded` 只保证 D1+D2 入库，
timeseries 测试自己 ingest + 清理。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from bi.config import load_dataset_config
from bi.ingest import ingest
from bi.settings import get_settings
from bi.store import FactStore

F, T = "20261012", "20261013"
DEMO_BATCHES = ["20260619", "20260719", "20260818", "20260917"]


@pytest.fixture
def demo_loaded():
    """每个测试：清掉 4 个历史期 → 重新生成 → 测试 → 清掉。

    不使用 scope="module" 是因为其他模块测试希望只有 D1+D2。
    """
    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    store = FactStore(settings)

    _cleanup_demo(store)
    from bi.historical import make_historical_batches
    plan = make_historical_batches(write=True)
    for batch, _n in plan["batches"]:
        ds = ingest(batch, settings=settings, cfg=cfg)
        store.save_ingested(ds, cfg, extra_notes=[
            f"[示例数据] 倒推自 D1={cfg.id} 用于趋势图演示"
        ])

    yield store

    _cleanup_demo(store)
    for d in DEMO_BATCHES:
        p = settings.inbound_dir / d
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _cleanup_demo(store: FactStore) -> None:
    """清理掉 4 个示例历史期。"""
    c = sqlite3.connect(store.db_path)
    try:
        v_ids = [r[0] for r in c.execute(
            "SELECT id FROM upload_versions WHERE batch IN (?,?,?,?)",
            DEMO_BATCHES).fetchall()]
        for vid in v_ids:
            c.execute("DELETE FROM metric_snapshots WHERE version_id=?", (vid,))
            c.execute("DELETE FROM raw_records WHERE version_id=?", (vid,))
            c.execute("DELETE FROM anomalies WHERE version_id=?", (vid,))
        if v_ids:
            c.execute(f"DELETE FROM upload_versions WHERE id IN ({','.join('?' * len(v_ids))})", v_ids)
        c.commit()
    finally:
        c.close()


def test_timeseries_metric_returns_points_in_order(demo_loaded):
    """单指标时间序列：按 batch 升序，至少 2 个点（D1 + D2）。"""
    store = demo_loaded
    ts = store.metric_timeseries("enrolled_total")
    assert len(ts) >= 2
    batches = [p["batch"] for p in ts]
    assert batches == sorted(batches), "必须按 batch 升序"
    # 全部是真实 D1+D2（无历史期时也应该有）
    assert F in batches and T in batches


def test_timeseries_returns_full_record(demo_loaded):
    """每条点应包含 value、unit、file_sha256（用于证据追溯）。"""
    store = demo_loaded
    ts = store.metric_timeseries("enrolled_total")
    first = ts[0]
    for field in ("batch", "value", "unit", "metric_name", "decimals", "file_sha256",
                  "record_count", "row_count"):
        assert field in first, f"missing field: {field}"
    assert first["file_sha256"] and len(first["file_sha256"]) == 64


def test_timeseries_filter_by_range(demo_loaded):
    """from_batch/to_batch 过滤：只返回范围内的点。"""
    store = demo_loaded
    ts = store.metric_timeseries("enrolled_total", from_batch=F, to_batch=T)
    batches = [p["batch"] for p in ts]
    assert all(F <= b <= T for b in batches)


def test_timeseries_demo_batches_flagged_in_versions(demo_loaded):
    """4 个历史期应该在 is_demo=True。"""
    store = demo_loaded
    versions = store.list_versions()
    demo = [v for v in versions if v.get("is_demo")]
    real = [v for v in versions if not v.get("is_demo")]
    assert len(demo) == 4, "应该有 4 个 [示例] 期"
    assert len(real) == 2, "应该有 2 个真实期（D1 + D2）"
    # 所有 demo 的 notes 都有 [示例]
    for v in demo:
        notes = v.get("notes", [])
        assert any("[示例" in n for n in notes), f"v{v['id']} 没有 [示例] 标记"


def test_insights_data_includes_timeseries_and_demo_batches(demo_loaded):
    """insights/data 端点必须包含 timeseries + demo_batches 字段。"""
    from fastapi.testclient import TestClient
    from bi.api import app
    c = TestClient(app)
    r = c.get("/insights/data")
    assert r.status_code == 200
    d = r.json()
    assert "timeseries" in d
    assert "demo_batches" in d
    assert "enrolled_total" in d["timeseries"]
    # demo_batches 包含 4 个示例期
    assert len(d["demo_batches"]) == 4


def test_metrics_timeseries_endpoint(demo_loaded):
    """GET /metrics/timeseries?key=xxx 单指标查询。"""
    from fastapi.testclient import TestClient
    from bi.api import app
    c = TestClient(app)
    r = c.get("/metrics/timeseries", params={"key": "exam_passed"})
    assert r.status_code == 200
    body = r.json()
    assert body["metric_key"] == "exam_passed"
    assert len(body["points"]) == 6  # 4 个历史 + D1 + D2


def test_historical_generator_deterministic():
    """make_historical_batches 是 deterministic 的（无 random）。"""
    from pathlib import Path
    from bi.historical import make_historical_batches
    from bi.settings import get_settings

    s = get_settings()
    real_path = Path(s.inbound_dir) / F / f"(登高)总表.xlsx"
    if not real_path.exists():
        pytest.skip("D1 真实数据不存在")

    r1 = make_historical_batches(write=True)
    r2 = make_historical_batches(write=True)
    # plan 一致
    assert [b[0] for b in r1["batches"]] == [b[0] for b in r2["batches"]]
    # batch 日期早于 D1
    for batch, _n in r1["batches"]:
        assert batch < F
    # 必然 4 期
    assert len(r1["batches"]) == 4
    # 4 期是递增趋势（人数爬坡）
    counts = [n for _, n in r1["batches"]]
    assert counts == sorted(counts), f"应有递增趋势，实际 {counts}"

    # 测试完清掉生成的 xlsx（不污染 inbound/）
    for batch, _ in r1["batches"]:
        p = s.inbound_dir / batch
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
