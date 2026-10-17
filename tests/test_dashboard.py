from __future__ import annotations

from fastapi.testclient import TestClient

from bi.api import app


client = TestClient(app)


def test_dashboard_page_is_available():
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "云学通 · 业务驾驶舱" in response.text
    assert "/dashboard/data" in response.text


def test_dashboard_data_is_aggregated_and_contains_no_person_identifiers(seeded):
    response = client.get("/dashboard/data")
    assert response.status_code == 200
    body = response.json()
    assert set(body["datasets"]) == {
        "denggao",
        "personnel_roster",
        "course_monthly_summary",
    }
    assert body["datasets"]["denggao"]["batch"] == "20261013"
    assert body["datasets"]["denggao"]["record_count"] == 887
    assert {item["key"] for item in body["datasets"]["denggao"]["kpis"]} == {
        "enrolled_total",
        "exam_scheduled",
        "exam_passed",
        "avg_score",
        "paid_count",
    }
    assert body["datasets"]["denggao"]["score_bands"]
    assert body["datasets"]["denggao"]["anomalies"]["trend"]

    def assert_no_person_identifiers(value):
        if isinstance(value, dict):
            assert not set(value).intersection({
                "姓名", "证件号码", "联系电话", "手机", "id_number", "phone",
                "_identity_key", "id_number_hash",
            })
            for nested in value.values():
                assert_no_person_identifiers(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_person_identifiers(nested)

    assert_no_person_identifiers(body)


def test_dashboard_api_records_audit_entry(seeded):
    response = client.get("/dashboard/data")
    assert response.status_code == 200
    audit = client.get("/audit", params={"limit": 20})
    assert any(item["tool"] == "dashboard_data" for item in audit.json()["items"])
