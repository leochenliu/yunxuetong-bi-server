"""定时任务执行记录与「漏跑」判定的验收测试。

刻意注入固定时间（``now=...``），不依赖真实时钟 —— 否则这些断言在每天 07:00
前后会随机红/绿，变成最烦人的 flaky 测试。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bi.jobs import due_dates, get_job, job_health, run_clock
from bi.store import FactStore

TZ = ZoneInfo("Asia/Shanghai")
JOB = "refresh_entity_tables"


@pytest.fixture()
def store(tmp_path):
    s = FactStore(db_path=tmp_path / "bi.sqlite3")
    s.init_schema()
    return s


def _record(store, run_date: str, status: str = "success", content_hash: str = "abc") -> None:
    store.record_job_run(
        JOB, run_date, status,
        scheduled_at=f"{run_date}T07:00:00+08:00",
        started_at=f"{run_date}T07:00:05+08:00",
        finished_at=f"{run_date}T07:00:09+08:00",
        duration_ms=4000,
        result_rows=9085,
        content_hash=content_hash,
        detail={"as_of": run_date},
    )


def test_missing_day_is_detected(store):
    """日历里缺的那一天就是漏跑。"""
    _record(store, "2026-10-24")
    _record(store, "2026-10-26")

    now = datetime(2026, 10, 26, 9, 0, tzinfo=TZ)  # 已过 07:00 + 120 分钟
    health = job_health(store, JOB, days=3, now=now)

    assert health["missing"] == ["2026-10-25"]
    assert health["healthy"] is False
    assert [c["status"] for c in health["calendar"]] == ["success", "missed", "success"]
    assert health["last_run_date"] == "2026-10-26"


def test_failed_run_is_not_missing(store):
    """「跑了但失败」必须与「没跑」区分：前者在 unhealthy，不在 missing。"""
    _record(store, "2026-10-25", status="failed")
    _record(store, "2026-10-26")

    now = datetime(2026, 10, 26, 9, 0, tzinfo=TZ)
    health = job_health(store, JOB, days=2, now=now)

    assert health["missing"] == []
    assert health["unhealthy"] == ["2026-10-25"]
    assert health["healthy"] is False


def test_today_not_due_yet_is_not_missing(store):
    """今天还没到「07:00 + 宽限」，不能算漏跑（否则每天早上都误报）。"""
    _record(store, "2026-10-25")

    early = datetime(2026, 10, 26, 7, 30, tzinfo=TZ)
    health = job_health(store, JOB, days=2, now=early)

    assert health["missing"] == []
    assert [c["date"] for c in health["calendar"]] == ["2026-10-25"]

    late = datetime(2026, 10, 26, 9, 30, tzinfo=TZ)
    assert job_health(store, JOB, days=2, now=late)["missing"] == ["2026-10-26"]


def test_record_job_run_is_idempotent(store):
    """同一天重跑只留一行，覆盖为最后一次运行（跑 1 次与跑 10 次的库内容一致）。"""
    _record(store, "2026-10-26", content_hash="first")
    _record(store, "2026-10-26", content_hash="first")
    _record(store, "2026-10-26", content_hash="first", status="failed")

    rows = store.job_runs(JOB)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["content_hash"] == "first"


def test_run_clock_keeps_run_date_and_as_of_in_sync():
    """run_date / as_of / scheduled_at 必须同源，跨零点也不能错位。"""
    late_night = datetime(2026, 10, 26, 23, 55, tzinfo=TZ)
    clock = run_clock(get_job(JOB), now=late_night)

    assert clock["run_date"] == "2026-10-26"
    assert clock["as_of"] == "2026-10-26"
    assert clock["scheduled_at"].startswith("2026-10-26T07:00")


def test_due_dates_respects_grace_window():
    spec = get_job(JOB)
    early = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)   # 07:00 + 120min 还没到
    assert due_dates(spec, days=2, now=early) == ["2026-10-25"]

    late = datetime(2026, 10, 26, 9, 1, tzinfo=TZ)
    assert due_dates(spec, days=2, now=late) == ["2026-10-26", "2026-10-25"]
