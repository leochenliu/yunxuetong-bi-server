#!/usr/bin/env python
"""从 `raw_records` 投影出三张**本地实体表**：certificates / students / companies。

    9085 行 raw_records  ->  certificates 9085 行（一行一张证）
                          ->  students     8634 行（一行一个人）
                          ->  companies    1319 行（一行一家公司）

为什么要有这三张表
------------------
`raw_records` 是**事实快照层**：它按版本存，字段塞在 `payload_json` 里，只适合差分与
证据链，不适合直接查（每次都要 `json_extract`，也没有索引）。

业务上真正想问的是「这个人有几张证」「这家公司多少人」「谁最快到期」。那需要**实体表**：
有主键、有类型、有索引，`SELECT * FROM certificates` 就能看。

两层别混（这是最容易搞错的地方）：

    ① raw_records                        事实快照层，版本化，**不可改**
    ② certificates/students/companies    本地实体表，当前状态，**本脚本产物**（可重建）

②**不是**①：实体表可随时整体重建，事实层不能改。库里也**不再镜像任何外部系统的表**——
外部快照会让同一个问题有两套口径（源库 UUID vs 台账公司名），对账请在库外按证书编号比对。

口径（每一列怎么来的）
----------------------
certificates（主键 = 证书编号，实测 9085 个无重复）
  * `expiry_date` 可空 —— 256 张写的是 `2026/04/17-2028/04/31`（4 月 31 日不存在），
    **不猜、不兜底**，留空并由 `validity_shape='无法解析'` 标出来；
  * `expiry_precision='月'` 表示按月末取值的**估计值**（325 张）；
  * `expiry_source` 区分 **证面原文**（3117 张）与 **按2年效期推算**（5968 张）。

students（主键 = 身份证号码）
  * 跨公司的人**只占一行**，`company_names` 里列出全部公司；
  * `cert_count` 相加 = 证书总数（一张证只属于一个人），**可以加**。

companies（主键 = 公司名称）
  * `person_count` 相加 **不等于** 全量持证人数（223 人跨公司，相加 8868 vs 实际 8634）；
  * `expiry_date_min` 是**最早**到期日（续证提醒看这个），`expiry_date_max` 是整体覆盖结束。

幂等与重建
----------
每次运行先 `DELETE` 三张表再 `INSERT`（一个事务），所以可以反复跑。
表结构由 `sql/schema.sql` 定义（单一事实源），本脚本**不写 DDL**。

用法
----
    python scripts/build_entity_tables.py              # 从库里最新一期 certificates 重建
    python scripts/build_entity_tables.py --dry-run    # 只统计不落库
    python scripts/build_entity_tables.py --batch 20261026
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from bi.config import DatasetConfig, load_dataset_config  # noqa: E402
from bi.derive import COL_CERT_EXPIRY_SHAPE  # noqa: E402
from bi.expiry import PRECISION_DAY  # noqa: E402
from bi.jobs import get_job, run_clock  # noqa: E402
from bi.settings import get_settings  # noqa: E402
from bi.store import FactStore  # noqa: E402

DATASET_CONFIG = REPO_ROOT / "config" / "datasets" / "certificates.yaml"
DATASET_ID = "certificates"
JOB_ID = "refresh_entity_tables"

# 表名常量（DDL 在 sql/schema.sql，这里只用名字）
CERT_TABLE = "certificates"
STUDENT_TABLE = "students"
COMPANY_TABLE = "companies"

# 幂等自证：只对**业务列**做指纹。
# 刻意排除两步：
#   * updated_at —— 它本来就是「最后一次刷新时间」；
#   * version_id  —— 它是版本标识，同一份台账重新入站后 id 必然变（先删后插），
#                    算进指纹会让「内容没变但指纹变了」，把幂等自证变成噪音。
BUSINESS_COLUMNS: dict[str, tuple[str, str]] = {
    CERT_TABLE: (
        "cert_number",
        "cert_number, person_name, id_number, company_name, start_date, expiry_date,"
        " expiry_precision, validity_shape, validity_raw, expiry_source,"
        " source_row, expiry_days, days_as_of",
    ),
    STUDENT_TABLE: (
        "id_number",
        "id_number, person_name, cert_count, company_count, company_names,"
        " first_start_date, last_expiry_date, min_expiry_days, days_as_of",
    ),
    COMPANY_TABLE: (
        "company_name",
        "company_name, cert_count, person_count, cross_company_person_count,"
        " start_date_min, start_date_max, expiry_date_min, expiry_date_max,"
        " expiry_unresolved_count, expiry_month_precision_count,"
        " expiry_from_cert_count, expiry_estimated_count, min_expiry_days, days_as_of",
    ),
}


class JobBusy(RuntimeError):
    """另一个实例正在跑（或锁文件未能回收）。"""


@contextmanager
def exclusive_lock(path: Path, stale_after_hours: float = 2.0):
    """跨进程互斥锁。

    不用 fcntl（Windows 没有）。用 O_CREAT|O_EXCL 建锁文件，并处理
    「上次进程崩溃留下锁文件」——否则第二天起会永远跳过硬锁。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age_h = (time.time() - path.stat().st_mtime) / 3600
        if age_h < stale_after_hours:
            raise JobBusy(f"另一个实例正在运行（锁 {path}，已持有 {age_h:.1f}h）") from None
        path.unlink(missing_ok=True)  # 陈旧锁：超时即回收
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def content_hash(conn: sqlite3.Connection) -> dict[str, str]:
    """业务内容的稳定指纹：同一 as_of、同一输入版本，跑多少次都必须相同。"""
    out: dict[str, str] = {}
    for table, (key, cols) in BUSINESS_COLUMNS.items():
        digest = hashlib.sha256()
        for row in conn.execute(f"SELECT {cols} FROM {table} ORDER BY {key}"):
            digest.update("\x1f".join("" if v is None else str(v) for v in row).encode("utf-8"))
            digest.update(b"\x1e")
        out[table] = digest.hexdigest()[:16]
    return out


def _days_left(expiry: str | None, today: date) -> int | None:
    """距到期天数：负数=已过期，0=今天到期，None=到期日算不出（不猜、不兜底）。"""
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry[:10]) - today).days
    except ValueError:
        return None


def _columns(cfg: DatasetConfig) -> dict[str, str]:
    """从业务契约反查中文列名，避免在脚本里硬编码。"""
    reverse = {v: k for k, v in cfg.field_mapping.items()}
    return {
        "name": reverse.get("name", "姓名"),
        "id_number": cfg.identity.id_column,
        "company": reverse.get("company", "公司名称"),
        "cert_no": reverse.get("cert_no", "证书编号"),
        "validity_raw": reverse.get("validity_raw", "证书有效期"),
        "date_source": reverse.get("date_source", "日期来源"),
        "start_date": reverse.get("start_date_iso", "开始日期"),
        "expiry_date": reverse.get("expiry_date", "过期日期"),
        "precision": reverse.get("expiry_precision", "过期日期精度"),
        "shape": reverse.get(COL_CERT_EXPIRY_SHAPE, COL_CERT_EXPIRY_SHAPE),
    }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass
class CertRow:
    cert_number: str
    person_name: str
    id_number: str
    company_name: str
    start_date: str | None
    expiry_date: str | None
    expiry_precision: str | None
    validity_shape: str | None
    validity_raw: str
    expiry_source: str | None
    version_id: int
    source_row: int | None
    expiry_days: int | None = None


@dataclass
class StudentAgg:
    id_number: str
    person_name: str = ""
    cert_numbers: list[str] = field(default_factory=list)
    companies: set[str] = field(default_factory=set)
    start_dates: list[str] = field(default_factory=list)
    expiry_dates: list[str] = field(default_factory=list)
    expiry_day_counts: list[int] = field(default_factory=list)

    @property
    def min_expiry_days(self) -> int | None:
        return min(self.expiry_day_counts) if self.expiry_day_counts else None

    def as_row(self, now: str, as_of: str) -> tuple:
        return (
            self.id_number,
            self.person_name,
            len(self.cert_numbers),
            len(self.companies),
            "、".join(sorted(self.companies)),
            min(self.start_dates) if self.start_dates else None,
            max(self.expiry_dates) if self.expiry_dates else None,
            self.min_expiry_days,
            as_of,
            now,
        )


@dataclass
class CompanyAgg:
    company_name: str
    cert_count: int = 0
    persons: set[str] = field(default_factory=set)
    start_dates: list[str] = field(default_factory=list)
    expiry_dates: list[str] = field(default_factory=list)
    expiry_day_counts: list[int] = field(default_factory=list)
    unresolved: int = 0
    month_precision: int = 0
    from_cert: int = 0
    estimated: int = 0
    cross_company_person_count: int = 0

    @property
    def min_expiry_days(self) -> int | None:
        return min(self.expiry_day_counts) if self.expiry_day_counts else None


@dataclass
class BuildResult:
    version_id: int
    batch: str
    raw_rows: int
    certs: list[CertRow]
    students: list[StudentAgg]
    companies: list[CompanyAgg]
    estimated_values: set[str]
    missing_cert_number: int
    as_of: str = ""


def _pick_version(conn: sqlite3.Connection, batch: str | None) -> sqlite3.Row:
    if batch:
        row = conn.execute(
            "SELECT * FROM upload_versions WHERE dataset_id=? AND batch=?",
            (DATASET_ID, batch),
        ).fetchone()
        if row is None:
            raise SystemExit(f"[错误] 找不到批次 {batch!r} 的 {DATASET_ID} 版本")
        return row
    row = conn.execute(
        "SELECT * FROM upload_versions WHERE dataset_id=?"
        " ORDER BY captured_at DESC, id DESC LIMIT 1",
        (DATASET_ID,),
    ).fetchone()
    if row is None:
        raise SystemExit(
            f"[错误] 库里没有 {DATASET_ID} 数据。先入站：\n"
            "       python -m bi.cli ingest certificates --file <台账>.xlsx"
        )
    return row


def collect(db_path: Path, cfg: DatasetConfig, batch: str | None = None,
            as_of: date | None = None) -> BuildResult:
    """读 raw_records，聚成三张表的行（不落库）。

    ``as_of`` 决定「距到期天数」按哪一天算：默认今天；显式传入即可复现历史某天的结果。
    """
    today = as_of or date.today()
    cols = _columns(cfg)
    estimated_values = set((cfg.derive.expiry or {}).get("estimated_source_values") or [])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        version = _pick_version(conn, batch)
        version_id = int(version["id"])
        rows = conn.execute(
            "SELECT source_row, record_key, id_number, name, payload_json"
            " FROM raw_records WHERE version_id=? ORDER BY source_row, id",
            (version_id,),
        ).fetchall()
    finally:
        conn.close()

    certs: list[CertRow] = []
    students: dict[str, StudentAgg] = {}
    companies: dict[str, CompanyAgg] = {}
    missing_cert_number = 0

    for row in rows:
        payload = json.loads(row["payload_json"])
        cert_no = _text(payload.get(cols["cert_no"]))
        if not cert_no:
            cert_no = _text(row["record_key"])  # 兜底保证主键唯一，并在报告里计数
            missing_cert_number += 1
        id_number = _text(row["id_number"]) or _text(payload.get(cols["id_number"]))
        person_name = _text(row["name"]) or _text(payload.get(cols["name"]))
        company = _text(payload.get(cols["company"]))
        start_date = _text(payload.get(cols["start_date"])) or None
        expiry_date = _text(payload.get(cols["expiry_date"])) or None
        precision = _text(payload.get(cols["precision"])) or None
        shape = _text(payload.get(cols["shape"])) or None
        source = _text(payload.get(cols["date_source"])) or None
        days = _days_left(expiry_date, today)

        certs.append(CertRow(
            cert_number=cert_no,
            person_name=person_name,
            id_number=id_number,
            company_name=company,
            start_date=start_date,
            expiry_date=expiry_date,
            expiry_precision=precision,
            validity_shape=shape,
            validity_raw=_text(payload.get(cols["validity_raw"])),
            expiry_source=source,
            version_id=version_id,
            source_row=row["source_row"],
            expiry_days=days,
        ))

        if id_number:
            stu = students.setdefault(id_number, StudentAgg(id_number=id_number))
            stu.cert_numbers.append(cert_no)
            if person_name and not stu.person_name:
                stu.person_name = person_name
            if company:
                stu.companies.add(company)
            if start_date:
                stu.start_dates.append(start_date)
            if expiry_date:
                stu.expiry_dates.append(expiry_date)
            if days is not None:
                stu.expiry_day_counts.append(days)

        if company:
            agg = companies.setdefault(company, CompanyAgg(company_name=company))
            agg.cert_count += 1
            if id_number:
                agg.persons.add(id_number)
            if start_date:
                agg.start_dates.append(start_date)
            if expiry_date:
                agg.expiry_dates.append(expiry_date)
                if days is not None:
                    agg.expiry_day_counts.append(days)
                if precision and precision != PRECISION_DAY:
                    agg.month_precision += 1
                if source and source in estimated_values:
                    agg.estimated += 1
                else:
                    agg.from_cert += 1
            else:
                agg.unresolved += 1

    # 跨公司的人：出现在 >1 家公司的证件号
    companies_of: dict[str, set[str]] = defaultdict(set)
    for agg in companies.values():
        for person in agg.persons:
            companies_of[person].add(agg.company_name)
    cross = {p for p, names in companies_of.items() if len(names) > 1}
    for agg in companies.values():
        agg.cross_company_person_count = sum(1 for p in agg.persons if p in cross)

    return BuildResult(
        version_id=version_id,
        batch=str(version["batch"]),
        raw_rows=len(rows),
        certs=certs,
        students=sorted(students.values(), key=lambda s: s.id_number),
        companies=sorted(companies.values(), key=lambda c: (-c.cert_count, c.company_name)),
        estimated_values=estimated_values,
        missing_cert_number=missing_cert_number,
        as_of=today.isoformat(),
    )


def write_tables(db_path: Path, result: BuildResult) -> None:
    """幂等：三张表整体重写（一个事务）。表结构来自 sql/schema.sql。"""
    FactStore(db_path=db_path).init_schema()  # 保证表存在（DDL 不在这里重复写）
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(f"DELETE FROM {CERT_TABLE}")
            conn.execute(f"DELETE FROM {STUDENT_TABLE}")
            conn.execute(f"DELETE FROM {COMPANY_TABLE}")

            conn.executemany(
                f"INSERT INTO {CERT_TABLE} (cert_number, person_name, id_number, company_name,"
                " start_date, expiry_date, expiry_precision, validity_shape, validity_raw,"
                " expiry_source, version_id, source_row, expiry_days, days_as_of, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (c.cert_number, c.person_name, c.id_number, c.company_name,
                     c.start_date, c.expiry_date, c.expiry_precision, c.validity_shape,
                     c.validity_raw, c.expiry_source, c.version_id, c.source_row,
                     c.expiry_days, result.as_of, now)
                    for c in result.certs
                ],
            )
            conn.executemany(
                f"INSERT INTO {STUDENT_TABLE} (id_number, person_name, cert_count,"
                " company_count, company_names, first_start_date, last_expiry_date,"
                " min_expiry_days, days_as_of, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [s.as_row(now, result.as_of) for s in result.students],
            )
            conn.executemany(
                f"INSERT INTO {COMPANY_TABLE} (company_name, cert_count, person_count,"
                " cross_company_person_count, start_date_min, start_date_max,"
                " expiry_date_min, expiry_date_max, expiry_unresolved_count,"
                " expiry_month_precision_count, expiry_from_cert_count, expiry_estimated_count,"
                " min_expiry_days, days_as_of, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        g.company_name, g.cert_count, len(g.persons),
                        g.cross_company_person_count,
                        min(g.start_dates) if g.start_dates else None,
                        max(g.start_dates) if g.start_dates else None,
                        min(g.expiry_dates) if g.expiry_dates else None,
                        max(g.expiry_dates) if g.expiry_dates else None,
                        g.unresolved, g.month_precision, g.from_cert, g.estimated,
                        g.min_expiry_days, result.as_of, now,
                    )
                    for g in result.companies
                ],
            )
    finally:
        conn.close()


def verify(db_path: Path) -> dict[str, Any]:
    """落库后的一致性自检：三张表的行数必须彼此对得上。"""
    conn = sqlite3.connect(db_path)
    try:
        return {
            "certificates": conn.execute(f"SELECT COUNT(*) FROM {CERT_TABLE}").fetchone()[0],
            "students": conn.execute(f"SELECT COUNT(*) FROM {STUDENT_TABLE}").fetchone()[0],
            "companies": conn.execute(f"SELECT COUNT(*) FROM {COMPANY_TABLE}").fetchone()[0],
            "sum_company_cert_count": conn.execute(
                f"SELECT COALESCE(SUM(cert_count),0) FROM {COMPANY_TABLE}").fetchone()[0],
            "sum_student_cert_count": conn.execute(
                f"SELECT COALESCE(SUM(cert_count),0) FROM {STUDENT_TABLE}").fetchone()[0],
            "expired_certificates": conn.execute(
                f"SELECT COUNT(*) FROM {CERT_TABLE}"
                " WHERE expiry_days IS NOT NULL AND expiry_days < 0").fetchone()[0],
            "expiry_unknown": conn.execute(
                f"SELECT COUNT(*) FROM {CERT_TABLE} WHERE expiry_days IS NULL").fetchone()[0],
            "as_of": conn.execute(
                f"SELECT MAX(days_as_of) FROM {CERT_TABLE}").fetchone()[0],
        }
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从 raw_records 投影出 certificates / students / companies 三张实体表。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/build_entity_tables.py\n"
            "  python scripts/build_entity_tables.py --dry-run\n"
            "  python scripts/build_entity_tables.py --batch 20261026\n"
        ),
    )
    p.add_argument("-d", "--db", default=None, help="目标库（默认与事实库同一个文件）")
    p.add_argument("--batch", default=None, help="指定批次（默认取最新一期 certificates）")
    p.add_argument("--top", type=int, default=5, help="打印前 N 家公司（默认 5）")
    p.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    p.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                   help="按指定日期计算「距到期天数」（默认今天；回填/测试用，结果可复现）")
    p.add_argument("--trigger", default="manual",
                   choices=("manual", "schedule", "startup", "ingest"),
                   help="触发方式，写入 job_runs.trigger（默认 manual）")
    p.add_argument("--no-record", action="store_true", help="不写 job_runs（调试用）")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    settings = get_settings()
    db_path = Path(args.db).expanduser() if args.db else Path(settings.db_path)
    if not db_path.is_file():
        raise SystemExit(f"[错误] 库不存在：{db_path}")

    spec = get_job(JOB_ID)
    clock = run_clock(spec)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.fromisoformat(clock["as_of"])
    run_date = args.as_of or clock["run_date"]  # 回填时写回那一天的记录

    try:
        with exclusive_lock(db_path.parent / ".refresh_entity_tables.lock"):
            return _run(args, db_path, clock, as_of, run_date)
    except JobBusy as exc:
        print(f"[跳过] {exc}（未写库、未改动任何表）")
        return 2


def _run(args: argparse.Namespace, db_path: Path, clock: dict[str, str],
         as_of: date, run_date: str) -> int:
    """真正的执行体：重建 + 自检 + 记录 job_runs（失败也记录）。"""
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    t0 = time.perf_counter()
    status = "failed"
    detail: dict[str, Any] = {"as_of": as_of.isoformat()}
    stats: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    try:
        cfg = load_dataset_config(DATASET_CONFIG)
        result = collect(db_path, cfg, args.batch, as_of=as_of)

        print(f"[读取] {db_path}")
        print(f"       {DATASET_ID}/{result.batch}（version_id={result.version_id}）"
              f"｜raw_records {result.raw_rows} 行｜as_of {result.as_of}")
        print(f"[投影] certificates {len(result.certs)} 行")
        print(f"       students     {len(result.students)} 人（跨公司的人只占一行）")
        print(f"       companies    {len(result.companies)} 家")

        multi = sum(1 for s in result.students if len(s.companies) > 1)
        print(f"       跨公司持证的人 {multi} 个证件号")
        unresolved = sum(1 for c in result.certs if not c.expiry_date)
        month = sum(1 for c in result.certs
                    if c.expiry_precision and c.expiry_precision != PRECISION_DAY)
        estimated = sum(
            1 for c in result.certs if c.expiry_source and c.expiry_source in result.estimated_values
        )
        print(f"       到期日解析不出 {unresolved} 张｜年月精度（估计值）{month} 张"
              f"｜按2年推算 {estimated} 张")
        expired = sum(1 for c in result.certs if c.expiry_days is not None and c.expiry_days < 0)
        print(f"       已过期 {expired} 张（按 as_of {result.as_of} 算）")
        if result.missing_cert_number:
            print(f"[注意] {result.missing_cert_number} 行没有证书编号，主键回退为 record_key")

        print(f"[排行] 证书张数 Top {min(args.top, len(result.companies))}：")
        for g in result.companies[: args.top]:
            print(f"       {g.company_name:<30} {g.cert_count:>5} 张 / {len(g.persons):>4} 人"
                  f" / 到期 {min(g.expiry_dates) if g.expiry_dates else '-'}"
                  f" ~ {max(g.expiry_dates) if g.expiry_dates else '-'}")

        if args.dry_run:
            print("[dry-run] 未写库（也不写 job_runs）。")
            return 0

        write_tables(db_path, result)
        stats = verify(db_path)
        print(f"[写库] {CERT_TABLE} / {STUDENT_TABLE} / {COMPANY_TABLE}（整体重写，幂等）")
        print(f"       certificates {stats['certificates']}｜students {stats['students']}"
              f"｜companies {stats['companies']}")
        print(f"       校验：companies 证书数合计 {stats['sum_company_cert_count']}"
              f"／students 证书数合计 {stats['sum_student_cert_count']}"
              f"／明细 {stats['certificates']}")
        ok = (stats["sum_company_cert_count"] == stats["certificates"]
              == stats["sum_student_cert_count"])
        print(f"       -> {'一致 ✓' if ok else '**不一致，请检查**'}")
        print(f"       已过期 {stats['expired_certificates']} 张"
              f"｜到期日算不出 {stats['expiry_unknown']} 张（as_of {stats['as_of']}）")

        hash_conn = sqlite3.connect(db_path)
        try:
            hashes = content_hash(hash_conn)
        finally:
            hash_conn.close()
        detail.update(stats)
        detail["content_hash"] = hashes
        print(f"       内容指纹 {hashes}（同 as_of 重跑应完全相同）")
        status = "success" if ok else "partial"
        return 0 if ok else 1
    except Exception as exc:
        detail["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if not args.no_record and not args.dry_run:
            FactStore(db_path=db_path).record_job_run(
                JOB_ID, run_date, status,
                trigger=args.trigger,
                scheduled_at=clock["scheduled_at"],
                started_at=started_at,
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                duration_ms=int((time.perf_counter() - t0) * 1000),
                result_rows=stats.get("certificates"),
                content_hash=hashes.get(CERT_TABLE),
                detail=detail,
            )


if __name__ == "__main__":
    raise SystemExit(main())
