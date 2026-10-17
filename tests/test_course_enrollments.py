from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from bi import api, mcp_server
from bi.agent import BIAgent, _course_query_minimum
from bi.settings import get_settings
from bi.store import FactStore

BATCH = "20261015"


@pytest.fixture
def course_client(tmp_path, monkeypatch):
    settings = replace(
        get_settings(),
        db_path=tmp_path / "course.sqlite3",
        database_url=None,
    )
    store = FactStore(settings)
    store.init_schema()
    with store.connect() as conn:
        version_id = conn.execute(
            """INSERT INTO upload_versions
               (dataset_id, batch, source_file, filename, sha256, captured_at,
                row_count, record_count, columns_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "course_monthly_summary", BATCH, "source.xlsx", "source.xlsx",
                "f" * 64, "2026-10-15T00:00:00", 7, 7, "[]", "active",
            ),
        ).lastrowid
        rows = [
            ("hash-a", "学员甲", "课程甲", "1"),
            ("hash-a", "学员甲", "课程甲", "2"),
            ("hash-a", "学员甲", "课程乙", "2"),
            ("hash-b", "学员乙", "课程甲", "1"),
            ("hash-c", "学员丙", "课程甲", "1"),
            ("hash-c", "学员丙", "课程乙", "1"),
            ("hash-c", "学员丙", "课程丙", "1"),
        ]
        conn.executemany(
            """INSERT INTO raw_records
               (version_id, record_key, id_number_hash, name, payload_json)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    version_id,
                    f"{identity}|{name}|{course}|2026|{month}",
                    identity,
                    name,
                    json.dumps({"课程": course, "月份": month}, ensure_ascii=False),
                )
                for identity, name, course, month in rows
            ],
        )

    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(mcp_server, "_audit", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    mcp_server.set_query_client(mcp_server.BIQueryClient(sdk_client=client))
    yield client, store
    mcp_server.set_query_client(None)
    client.close()


def test_course_enrollment_api_counts_distinct_courses_and_paginates(course_client):
    client, store = course_client
    response = client.get(
        "/courses/enrollments",
        params={"min_courses": 2, "limit": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["batch"] == BATCH
    assert body["learner_count"] == 3
    assert body["qualifying_learners"] == 2
    assert body["returned"] == 1
    assert body["learners"] == [{
        "name": "学员丙",
        "course_count": 3,
        "courses": ["课程丙", "课程乙", "课程甲"],
    }]
    assert not {"id_number", "id_number_hash", "record_key"} & set(body["learners"][0])
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE tool='course_enrollments' AND status='ok'"
        ).fetchone()["c"] == 1

    second_page = client.get(
        "/courses/enrollments",
        params={"min_courses": 2, "limit": 1, "offset": 1},
    ).json()
    assert second_page["learners"][0]["name"] == "学员甲"
    assert second_page["learners"][0]["course_count"] == 2


def test_course_enrollment_api_rejects_missing_batch_and_limits(course_client):
    client, store = course_client
    assert client.get("/courses/enrollments", params={"batch": "20261016"}).status_code == 404
    assert client.get("/courses/enrollments", params={"min_courses": 0}).status_code == 422
    assert client.get("/courses/enrollments", params={"limit": 501}).status_code == 422
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status FROM audit_log WHERE tool='course_enrollments' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] == "error"


def test_mcp_and_agent_query_course_enrollment_source(course_client):
    result = mcp_server.bi_list_course_enrollments(min_courses=2, limit=10)
    assert result["qualifying_learners"] == 2
    assert result["learners"][0]["name"] == "学员丙"

    answer = BIAgent().ask("报名了两门以上课程的学员有哪些？")
    assert answer["error"] is None
    assert answer["course_enrollments"]["qualifying_learners"] == 2
    assert answer["to"] == BATCH
    assert "报名至少 2 门不同课程的学员有 2 人" in answer["answer"]
    assert "学员甲" in answer["answer"] and "学员丙" in answer["answer"]
    assert answer["steps"][0]["tool"] == "bi_list_course_enrollments"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("报名了两门以上课程的学员", 2),
        ("报名至少3门不同课程的人", 3),
        ("超过两门课程的报名者", 3),
        ("报了 4 门课程的名单", 4),
        ("有哪些人报名多门课程", 2),
        ("为什么报名人数增加", None),
        ("两门以上课程有哪些", None),
    ],
)
def test_course_enrollment_question_threshold_detection(question, expected):
    assert _course_query_minimum(question) == expected
