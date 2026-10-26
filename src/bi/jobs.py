"""定时任务的注册表与「漏跑」判定。

分工：
  * 期望在这里（``JOBS``：几点该跑、按哪个时区、宽限多久）；
  * 结果在 ``job_runs`` 表（每次跑完写一行，成功失败都写）；
  * 「漏跑」= 该跑的日期在表里**没有行** —— 不补写 miss 行，避免两套互相打架的真相。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class JobSpec:
    """一个定时任务的「期望」：几点该跑、按哪个时区、宽限多久。"""

    job_id: str
    title: str
    daily_at: str = "07:00"
    timezone: str = DEFAULT_TIMEZONE
    grace_minutes: int = 120
    owner: str = "data-platform"


JOBS: dict[str, JobSpec] = {
    "refresh_entity_tables": JobSpec(
        job_id="refresh_entity_tables",
        title="刷新实体表快照（certificates / students / companies）",
    ),
}


def get_job(job_id: str) -> JobSpec:
    if job_id not in JOBS:
        raise KeyError(f"未注册的任务：{job_id}（已注册：{', '.join(JOBS)}）")
    return JOBS[job_id]


def local_now(spec: JobSpec, now: datetime | None = None) -> datetime:
    """按任务声明的时区取当前时间（容器里默认 UTC，不能直接用 date.today()）。"""
    tz = ZoneInfo(spec.timezone)
    return (now or datetime.now(tz)).astimezone(tz)


def run_clock(spec: JobSpec, now: datetime | None = None) -> dict[str, str]:
    """一次运行只取一次时间：run_date / as_of / scheduled_at 全部由它派生。

    三者同源，避免跨零点时出现「10-26 的记录里 as_of=10-25」这种自相矛盾的行。
    """
    cur = local_now(spec, now)
    day = cur.date()
    hh, mm = (int(x) for x in spec.daily_at.split(":"))
    scheduled = datetime.combine(day, time(hh, mm), tzinfo=ZoneInfo(spec.timezone))
    return {
        "run_date": day.isoformat(),
        "as_of": day.isoformat(),
        "scheduled_at": scheduled.isoformat(timespec="seconds"),
        "started_at": cur.isoformat(timespec="seconds"),
    }


def due_dates(spec: JobSpec, days: int = 14, now: datetime | None = None) -> list[str]:
    """最近 days 天里「按计划已经该跑完」的日期（新→旧）。

    今天若还没到 daily_at + grace，**不计入** —— 否则每天早上查状态都会误报「今天漏跑」。
    """
    cur = local_now(spec, now)
    tz = ZoneInfo(spec.timezone)
    hh, mm = (int(x) for x in spec.daily_at.split(":"))
    out: list[str] = []
    for i in range(days):
        day = cur.date() - timedelta(days=i)
        due_at = datetime.combine(day, time(hh, mm), tzinfo=tz)
        if cur < due_at + timedelta(minutes=spec.grace_minutes):
            continue
        out.append(day.isoformat())
    return out


def job_health(store, job_id: str, days: int = 14, now: datetime | None = None) -> dict:
    """最近 days 天的执行日历：``status == "missed"`` 的日期就是漏跑。"""
    spec = get_job(job_id)
    cur = local_now(spec, now)
    due = due_dates(spec, days, cur)
    rows = {r["run_date"]: r for r in store.job_runs(job_id, days=days)}

    missing = [d for d in due if d not in rows]
    unhealthy = [d for d in due if d in rows and rows[d]["status"] != "success"]
    last = max(rows) if rows else None
    return {
        "job_id": job_id,
        "title": spec.title,
        "timezone": spec.timezone,
        "daily_at": spec.daily_at,
        "checked_at": cur.isoformat(timespec="seconds"),
        "days": days,
        # 最近一个「按计划该跑完」的日期。/health 只看它有没有记录：
        # 历史缺口不等于当前落后，否则首次上线后 7 天内会一直告警。
        "latest_due": due[0] if due else None,
        "last_run_date": last,
        "gap_days": (cur.date() - date.fromisoformat(last)).days if last else None,
        "missing": missing,
        "unhealthy": unhealthy,
        "healthy": not missing and not unhealthy,
        "calendar": [
            {
                "date": d,
                "status": rows[d]["status"] if d in rows else "missed",
                "content_hash": rows[d].get("content_hash") if d in rows else None,
                "result_rows": rows[d].get("result_rows") if d in rows else None,
            }
            for d in due
        ],
    }
