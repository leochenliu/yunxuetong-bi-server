"""阶段一验收：差分引擎的输出必须与 ground truth 逐条一致。

这是「第一阶段：Excel 差分原型」正确性的核心证明——不是「看着差不多」，
而是每个新增/移除/变更的**人和字段**都能被断言。
"""

from __future__ import annotations

from bi.diff_engine import diff_dataset_against_snapshot
from bi.derive import require_identity


def _diff(batches, cfg):
    d1, d2 = batches
    return diff_dataset_against_snapshot(d1.snapshot, d2.dataset, cfg, "20261012", "20261013")


def test_counts_match_ground_truth(batches, cfg, ground_truth):
    r = _diff(batches, cfg)
    assert r.count_from == ground_truth["count_from"], "上期人数不符"
    assert r.count_to == ground_truth["count_to"], "本期人数不符"
    assert r.unchanged == ground_truth["unchanged"], "未变人数不符"
    assert r.event_count == ground_truth["changed_event_count"], "字段级事件数不符"


def test_added_set_exact(batches, cfg, ground_truth):
    r = _diff(batches, cfg)
    assert sorted(r.added) == sorted(ground_truth["added"])


def test_removed_set_exact(batches, cfg, ground_truth):
    r = _diff(batches, cfg)
    assert sorted(r.removed) == sorted(ground_truth["removed"])


def test_changed_people_and_fields_exact(batches, cfg, ground_truth):
    r = _diff(batches, cfg)
    expected = {c["key"]: c["fields"] for c in ground_truth["changed"]}
    actual = {
        rc.key: {ch.field: {"from": ch.from_value, "to": ch.to_value} for ch in rc.changes}
        for rc in r.changed
    }
    assert set(actual) == set(expected), (
        f"变更人员集合不符\n仅引擎有: {sorted(set(actual) - set(expected))[:5]}"
        f"\n仅事实有: {sorted(set(expected) - set(actual))[:5]}"
    )
    for key in expected:
        assert set(actual[key]) == set(expected[key]), f"{key} 变更字段不符: {actual[key]} vs {expected[key]}"
        for field, pair in expected[key].items():
            assert actual[key][field] == pair, f"{key}.{field}: 引擎 {actual[key][field]} vs 事实 {pair}"


def test_field_summary(batches, cfg, ground_truth):
    r = _diff(batches, cfg)
    by_field = {f["field"]: f["count"] for f in r.field_summary}
    # 从 ground truth 独立统计字段维度
    want: dict[str, int] = {}
    for c in ground_truth["changed"]:
        for f in c["fields"]:
            want[f] = want.get(f, 0) + 1
    assert by_field == want


def test_injected_anomalies_are_diff_events(batches, cfg, ground_truth):
    """注入的异常（分数越界/低分通过/回退等）必须能在差分事件里被指认。"""
    r = _diff(batches, cfg)
    changed_keys = {rc.key for rc in r.changed}
    for inj in ground_truth["anomaly_injections"]:
        if inj["rule_id"] == "exam_status_regressed":
            assert inj["record_key"] in changed_keys, f"状态回退未被识别: {inj}"
        else:
            assert inj["record_key"] in changed_keys, f"注入异常未被识别: {inj}"


def test_require_identity_idempotent(batches, cfg):
    """口径过滤必须幂等：重复调用不改变行数。"""
    d1, _ = batches
    # 复制一份避免污染共享夹具
    import copy

    ds = copy.deepcopy(d1.dataset)
    before = len(ds.rows)
    require_identity(ds, cfg)
    after1 = len(ds.rows)
    require_identity(ds, cfg)
    after2 = len(ds.rows)
    # ingest 已经过滤过，因此这里应无变化
    assert after1 == before == after2 == 883
