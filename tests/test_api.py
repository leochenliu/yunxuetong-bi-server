"""阶段三验收：四类固定查询的正确性。

  指标对比  -> 数值、变化量、归因是否自洽（残差为 0）
  差分汇总  -> 与 ground truth 一致
  明细下钻  -> 分页、过滤、严重度排序正确
  证据追溯  -> 能回到源文件与 Excel 行号
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bi.api import MAX_LIMIT, app

client = TestClient(app)

F, T = "20261012", "20261013"


@pytest.fixture(scope="module", autouse=True)
def _need_data(seeded):
    """确保库里有数据（seeded 是 session 级夹具）。"""
    yield


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_backend"] in {"sqlite", "postgresql"}
    assert F in body["batches"] and T in body["batches"]
    assert body["records"][F] == 883
    assert body["records"][T] == 887


def test_api_startup_initializes_database(settings):
    with TestClient(app) as startup_client:
        response = startup_client.get("/health")
    assert response.status_code == 200
    assert response.json()["db_backend"] in {"sqlite", "postgresql"}


def test_metric_compare_values_and_direction():
    r = client.get("/metrics/compare", params={"from": F, "to": T})
    assert r.status_code == 200
    items = {i["metric_key"]: i for i in r.json()["items"]}
    assert set(items) == {"enrolled_total", "exam_scheduled", "exam_passed", "avg_score", "paid_count"}

    enrolled = items["enrolled_total"]
    assert enrolled["value_from"] == 883
    assert enrolled["value_to"] == 887
    assert enrolled["delta"] == 4
    assert enrolled["direction"] == "up"

    paid = items["paid_count"]
    assert paid["delta"] == 15  # 15 人从「未付款」变「已付款」

    passed = items["exam_passed"]
    assert passed["delta"] == 39  # 40 人出分通过 - 1 人状态回退


def test_metric_attribution_fully_explains_enrolled():
    """报名总人数的变化必须被「新增 - 移除」完全解释，残差为 0。"""
    r = client.get("/metrics/compare", params={"from": F, "to": T})
    enrolled = next(i for i in r.json()["items"] if i["metric_key"] == "enrolled_total")
    attr = enrolled["attribution"]
    assert attr["residual"] == 0
    assert attr["fully_explained"] is True
    drivers = {d["type"]: d for d in attr["drivers"]}
    assert drivers["added"]["count"] == 12
    assert drivers["removed"]["count"] == 8


def test_metric_attribution_explains_passed():
    r = client.get("/metrics/compare", params={"from": F, "to": T})
    passed = next(i for i in r.json()["items"] if i["metric_key"] == "exam_passed")
    attr = passed["attribution"]
    drivers = {d["type"]: d for d in attr["drivers"]}
    # 40 人出分 + 1 人回退 = 净 +39
    assert drivers["changed_gain"]["count"] == 40
    assert drivers["changed_loss"]["count"] == 1
    assert attr["residual"] == 0
    assert attr["fully_explained"] is True


def test_every_countable_metric_is_fully_explained():
    """所有可计数归因的指标，残差都必须为 0 —— 即指标变化被差分事件完全解释。

    这是本项目最重要的一致性保证：如果这里失败，说明有未被追踪的变化，
    结论就不能说成「已解释」。
    """
    r = client.get("/metrics/compare", params={"from": F, "to": T})
    checked = 0
    for i in r.json()["items"]:
        attr = i.get("attribution") or {}
        if attr.get("residual") is None:
            continue  # 均值型指标（平均分）不做计数归因
        assert attr["residual"] == 0, (
            f"{i['metric_key']} 变化 {i['delta']} 未被完全解释，残差 {attr['residual']}")
        assert attr["fully_explained"] is True
        checked += 1
    assert checked >= 4, "至少应有 4 个可计数归因的指标"


def test_diff_summary_matches_ground_truth(ground_truth):
    r = client.get("/diffs/summary", params={"from": F, "to": T})
    assert r.status_code == 200
    body = r.json()
    assert body["by_type"]["added"] == 12
    assert body["by_type"]["removed"] == 8
    assert body["by_type"]["changed"] == ground_truth["changed_event_count"]
    assert body["total_events"] == 221
    fields = {f["field"]: f["count"] for f in body["by_field"]}
    assert fields["考试状态"] == 61
    assert fields["分数"] == 50


def test_diff_records_pagination():
    r1 = client.get("/diffs/records", params={"from": F, "to": T, "limit": 10, "offset": 0})
    r2 = client.get("/diffs/records", params={"from": F, "to": T, "limit": 10, "offset": 10})
    assert r1.status_code == r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b1["total"] == 221 and b1["returned"] == 10
    ids1 = {x["id"] for x in b1["items"]}
    ids2 = {x["id"] for x in b2["items"]}
    assert not (ids1 & ids2), "分页结果不应重叠"


def test_diff_records_filter_and_severity_order():
    r = client.get("/diffs/records", params={"from": F, "to": T, "field_name": "分数", "limit": 100})
    body = r.json()
    assert body["total"] == 50
    assert all(x["field_name"] == "分数" for x in body["items"])
    assert body["items"][0]["severity"] == "high"
    # 第二个事件若同为 high 或之后才降到 medium
    sev = [x["severity"] for x in body["items"]]
    assert sev == sorted(sev, key=lambda s: {"high": 0, "medium": 1, "low": 2, "info": 3}[s])


def test_diff_records_rejects_bad_event_type():
    r = client.get("/diffs/records", params={"from": F, "to": T, "event_type": "deleted"})
    assert r.status_code == 400


def test_limit_is_clamped_by_schema():
    r = client.get("/diffs/records", params={"from": F, "to": T, "limit": MAX_LIMIT + 1})
    assert r.status_code == 422, "超过上限的 limit 必须被拒绝"


def test_evidence_traces_full_chain():
    r = client.get("/diffs/records", params={"from": F, "to": T, "event_type": "changed",
                                             "field_name": "分数", "limit": 1})
    ev = r.json()["items"][0]
    e = client.get(f"/evidence/{ev['id']}").json()
    assert e["event"]["id"] == ev["id"]
    assert len(e["chain"]) == 2
    before, after = e["chain"]
    assert before["batch"] == F and after["batch"] == T
    assert before["source_row"] > 1 and after["source_row"] > 1
    assert before["file_sha256"] and len(before["file_sha256"]) == 64
    assert before["id_number_masked"] and "*" in before["id_number_masked"]
    assert "分数" in before["payload"]


def test_evidence_404():
    assert client.get("/evidence/99999999").status_code == 404


def test_anomalies_endpoint():
    r = client.get("/anomalies", params={"batch": T})
    body = r.json()
    assert body["counts"].get("score_out_of_range", 0) >= 1
    assert body["counts"].get("exam_status_regressed", 0) >= 1
    assert body["total"] >= 7


def test_audit_endpoint_records_queries():
    client.get("/health")
    r = client.get("/audit", params={"limit": 20})
    items = r.json()["items"]
    tools = {i["tool"] for i in items}
    assert "metrics_compare" in tools or "diffs_records" in tools
    assert all("ts" in i and "status" in i for i in items)
