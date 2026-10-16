"""阶段二验收：事实存储的写入与查询、证据链完整性。"""

from __future__ import annotations

from bi.store import FactStore, hash_id_number, mask_id_number


def test_versions_persisted(seeded, settings):
    store = FactStore(settings)
    versions = store.list_versions()
    batches = [v["batch"] for v in versions]
    assert batches == ["20261012", "20261013"]
    v1, v2 = versions
    assert v1["record_count"] == 883
    assert v2["record_count"] == 887
    assert v1["dropped_rows"] == 22
    assert v1["sha256"] and len(v1["sha256"]) == 64
    assert v2["event_count"] == 221


def test_metric_snapshots(seeded, settings):
    store = FactStore(settings)
    m1 = store.metric_snapshot("20261012")
    m2 = store.metric_snapshot("20261013")
    assert set(m1) == {"enrolled_total", "exam_scheduled", "exam_passed", "avg_score", "paid_count"}
    assert m1["enrolled_total"]["value"] == 883
    assert m2["enrolled_total"]["value"] == 887
    # 出分 40 人 -> 已通过人数应显著增加
    assert m2["exam_passed"]["value"] > m1["exam_passed"]["value"]
    assert m2["paid_count"]["value"] == m1["paid_count"]["value"] + 15


def test_diff_summary(seeded, settings, ground_truth):
    store = FactStore(settings)
    s = store.diff_summary("20261012", "20261013")
    assert s["by_type"]["added"] == len(ground_truth["added"]) == 12
    assert s["by_type"]["removed"] == len(ground_truth["removed"]) == 8
    assert s["by_type"]["changed"] == ground_truth["changed_event_count"]


def test_diff_events_pagination_and_filter(seeded, settings):
    store = FactStore(settings)
    rows, total = store.diff_events("20261012", "20261013", limit=10, offset=0)
    assert len(rows) == 10
    assert total == 221
    # 过滤：只看分数变更
    rows, total = store.diff_events("20261012", "20261013", field_name="分数", limit=100)
    assert total == 50
    assert all(r["field_name"] == "分数" for r in rows)
    # 高严重度事件应排在最前
    assert rows[0]["severity"] == "high"


def test_evidence_chain_traces_to_source_row(seeded, settings):
    """证据追溯：一条差分事件 -> 上下两期的原始记录 + 源文件 + Excel 行号。"""
    store = FactStore(settings)
    rows, _ = store.diff_events("20261012", "20261013", event_type="changed", field_name="分数", limit=1)
    assert rows, "没有找到分数变更事件"
    ev = rows[0]
    chain = store.evidence(ev["id"])
    assert chain is not None
    assert len(chain["chain"]) == 2
    before, after = chain["chain"]
    # 上期与本期都要能指回具体行号与源文件
    assert before["present"] and after["present"]
    assert isinstance(before["source_row"], int) and before["source_row"] > 1
    assert isinstance(after["source_row"], int) and after["source_row"] > 1
    assert before["batch"] == "20261012" and after["batch"] == "20261013"
    assert before["file"].endswith(".xlsx")
    # 值必须与事件记录一致
    assert before["payload"]["分数"] == ev["value_from_raw"] or str(before["payload"]["分数"]) == str(ev["value_from_raw"])
    assert after["payload"]["分数"] == ev["value_to_raw"]


def test_pii_not_stored_in_plaintext(seeded, settings):
    """默认不落明文证件号：独立列与 payload 都应是掩码。"""
    import json

    store = FactStore(settings)
    with store.connect() as conn:
        rec = conn.execute("SELECT * FROM raw_records LIMIT 1").fetchone()
    assert rec["id_number_plain"] is None
    masked = rec["id_number_masked"]
    assert "*" in masked and len(masked) == 18
    assert len(rec["id_number_hash"]) == 64
    payload = json.loads(rec["payload_json"])
    assert "*" in str(payload.get("证件号码", "")), "payload 中的证件号必须已掩码"
    assert "*" in str(payload.get("联系电话", "")), "payload 中的手机号必须已掩码"


def test_mask_and_hash_helpers():
    masked = mask_id_number("320826197306181638")
    assert masked.startswith("320826") and masked.endswith("1638") and "*" in masked
    assert len(hash_id_number("320826197306181638")) == 64
    assert hash_id_number("320826197306181638") != hash_id_number("320826197306181639")


def test_anomalies_persisted(seeded, settings):
    store = FactStore(settings)
    a1 = store.anomaly_counts("20261012")
    a2 = store.anomaly_counts("20261013")
    assert a1.get("passed_but_no_score") == 3
    # D2 应有注入的 4 类行级异常
    for rule in ["score_out_of_range", "passed_but_low_score", "passed_but_no_exam_date", "paid_but_zero_amount"]:
        assert a2.get(rule, 0) >= 1, rule
    assert a2.get("exam_status_regressed", 0) >= 1


def test_audit_log(seeded, settings):
    store = FactStore(settings)
    store.audit("test_tool", {"foo": "bar"}, result_rows=3, actor="pytest")
    logs = store.recent_audit(5)
    assert logs[0]["tool"] == "test_tool"
    assert logs[0]["result_rows"] == 3
