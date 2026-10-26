"""`scripts/build_entity_tables.py`（raw_records → 三张实体表）的验收测试。

用**注入的小数据**验证投影口径，不依赖真实台账：

1. `certificates` 一行一张证，主键是证书编号；到期日解析不出就留空，不兜底；
2. `students` 一人一行（跨公司的人只占一行），`cert_count` 相加等于证书总数；
3. `companies` 一家一行，`person_count` 相加**会重复计人**（222 那个人跨两家）；
4. 三张表的行数/计数必须彼此对得上（`--dry-run` 之后再写库，结果一致）；
5. 幂等：重复构建不会翻倍。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from bi.config import load_dataset_config  # noqa: E402
from bi.store import FactStore  # noqa: E402
from build_entity_tables import (  # noqa: E402
    COMPANY_TABLE,
    DATASET_CONFIG,
    DATASET_ID,
    CERT_TABLE,
    STUDENT_TABLE,
    collect,
    content_hash,
    verify,
    write_tables,
)

BATCH = "certificates"

# (证书编号, 姓名, 证件号, 公司, 开始日期, 过期日期, 精度, 形态, 原始写法, 来源)
ROWS = [
    ("C1", "甲", "111111199001010001", "公司A", "2024-10-01", "2026-10-01", "日", "单日期", "2026/10/01", "证面原文"),
    ("C2", "甲", "111111199001010001", "公司A", "2025-01-31", "2028-01-31", "日", "区间", "2026/01/01-2028/01/31", "证面原文"),
    ("C3", "乙", "222222199001010002", "公司A", "2024-10-02", None, None, "无法解析", "2026/04/17-2028/04/31", "证面原文"),
    # 同一个人（甲）出现在第二家公司 —— 跨公司
    ("C4", "甲", "111111199001010001", "公司B", "2026-09-18", "2027-01-31", "月", "年月", "2027年1月", "按2年效期推算"),
    ("C5", "丙", "333333199001010003", "公司C", "2026-09-18", "2028-09-30", "日", "区间", "2026/09/18-2028/09/30", "证面原文"),
]


def _payload(row) -> str:
    cert_no, name, id_number, company, start, expiry, precision, shape, raw, source = row
    return json.dumps({
        "姓名": name,
        "身份证号码": "***",
        "公司名称": company,
        "证书编号": cert_no,
        "证书开始日期": raw if shape == "单日期" else "2024/10/01",
        "证书有效期": raw,
        "日期来源": source,
        "开始日期": start,
        "过期日期": expiry or "",
        "过期日期精度": precision or "",
        "有效期天数": "",
        "过期月份": (expiry or "")[:7],
        "有效期形态": shape,
    }, ensure_ascii=False)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """一个只含一期 certificates 的最小事实库。"""
    path = tmp_path / "bi.sqlite3"
    FactStore(db_path=path).init_schema()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO upload_versions (dataset_id, batch, source_file, filename, sha256,"
                " captured_at, row_count, record_count, columns_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (DATASET_ID, BATCH, "x.xlsx", "x.xlsx", "0" * 64,
                 "2026-10-26T00:00:00", len(ROWS), len(ROWS), "[]"),
            )
            version_id = conn.execute("SELECT id FROM upload_versions").fetchone()[0]
            for row in ROWS:
                cert_no, name, id_number = row[0], row[1], row[2]
                conn.execute(
                    "INSERT INTO raw_records (version_id, record_key, id_number, name,"
                    " source_row, payload_json) VALUES (?,?,?,?,?,?)",
                    (version_id, f"{id_number}|{name}|{cert_no}", id_number, name,
                     2 + ROWS.index(row), _payload(row)),
                )
    finally:
        conn.close()
    return path


@pytest.fixture(scope="module")
def cfg():
    return load_dataset_config(DATASET_CONFIG)


def test_certificates_one_row_per_certificate(db: Path, cfg):
    result = collect(db, cfg)
    assert result.raw_rows == 5
    assert [c.cert_number for c in result.certs] == ["C1", "C2", "C3", "C4", "C5"]

    by_no = {c.cert_number: c for c in result.certs}
    # 解析不出的那张：到期日必须留空，不许拿别的顶替
    assert by_no["C3"].expiry_date is None
    assert by_no["C3"].validity_shape == "无法解析"
    assert by_no["C3"].validity_raw == "2026/04/17-2028/04/31"
    # 年月精度要标出来（估计值）
    assert by_no["C4"].expiry_precision == "月"
    assert by_no["C4"].expiry_source == "按2年效期推算"


def test_students_one_row_per_person(db: Path, cfg):
    result = collect(db, cfg)
    by_id = {s.id_number: s for s in result.students}

    assert len(result.students) == 3          # 8634 的思路：一人一行
    jia = by_id["111111199001010001"]
    assert len(jia.cert_numbers) == 3          # 甲有 3 张证
    assert len(jia.companies) == 2             # 但分布在 2 家公司
    assert sorted(jia.companies) == ["公司A", "公司B"]
    # 最早开证日 / 最晚到期日
    assert min(jia.start_dates) == "2024-10-01"
    assert max(jia.expiry_dates) == "2028-01-31"


def test_companies_aggregation(db: Path, cfg):
    result = collect(db, cfg)
    by_name = {g.company_name: g for g in result.companies}

    a = by_name["公司A"]
    assert a.cert_count == 3
    assert len(a.persons) == 2                 # 甲 + 乙
    assert a.cross_company_person_count == 1   # 甲也在公司B
    assert a.unresolved == 1                   # C3
    assert a.month_precision == 0
    assert a.from_cert == 2                    # C1 C2 都是证面原文
    assert a.estimated == 0

    b = by_name["公司B"]
    assert b.cert_count == 1
    assert b.month_precision == 1
    assert b.estimated == 1
    assert b.from_cert == 0

    c = by_name["公司C"]
    assert c.cross_company_person_count == 0


def test_person_count_sum_is_not_total_headcount(db: Path, cfg):
    """口径陷阱：各公司 person_count 相加会重复计人（甲被算两次）。"""
    result = collect(db, cfg)
    total = sum(len(g.persons) for g in result.companies)
    assert total == 4                              # 2 + 1 + 1
    assert len(result.students) == 3               # 实际只有 3 个人


def test_write_then_verify_counts_match(db: Path, cfg):
    result = collect(db, cfg)
    write_tables(db, result)
    stats = verify(db)

    assert stats["certificates"] == 5
    assert stats["students"] == 3
    assert stats["companies"] == 3
    # 三张表的口径必须对得上
    assert stats["sum_company_cert_count"] == stats["certificates"]
    assert stats["sum_student_cert_count"] == stats["certificates"]

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT * FROM {CERT_TABLE} WHERE cert_number='C3'").fetchone()
        assert row["expiry_date"] is None
        assert row["source_row"] == 4            # Excel 物理行号留痕
        stu = conn.execute(
            f"SELECT * FROM {STUDENT_TABLE} WHERE id_number='111111199001010001'").fetchone()
        assert stu["cert_count"] == 3
        assert stu["company_count"] == 2
        assert "公司A" in stu["company_names"]
        comp = conn.execute(
            f"SELECT * FROM {COMPANY_TABLE} WHERE company_name='公司B'").fetchone()
        assert comp["expiry_date_min"] == "2027-01-31"
    finally:
        conn.close()


def test_rebuild_is_idempotent(db: Path, cfg):
    result = collect(db, cfg)
    write_tables(db, result)
    write_tables(db, result)
    stats = verify(db)
    assert (stats["certificates"], stats["students"], stats["companies"]) == (5, 3, 3)


def test_missing_cert_number_falls_back_to_record_key(db: Path, cfg):
    conn = sqlite3.connect(db)
    try:
        with conn:
            payload = json.loads(conn.execute(
                "SELECT payload_json FROM raw_records LIMIT 1").fetchone()[0])
            payload["证书编号"] = ""
            version_id = conn.execute("SELECT id FROM upload_versions").fetchone()[0]
            conn.execute(
                "INSERT INTO raw_records (version_id, record_key, id_number, name,"
                " source_row, payload_json) VALUES (?,?,?,?,?,?)",
                (version_id, "999999199001010009|丁|", "999999199001010009", "丁",
                 99, json.dumps(payload, ensure_ascii=False)),
            )
    finally:
        conn.close()

    result = collect(db, cfg)
    assert result.missing_cert_number == 1
    # 主键回退为 record_key，不会出现空主键
    assert any(c.cert_number == "999999199001010009|丁|" for c in result.certs)


def test_explicit_batch_must_exist(db: Path, cfg):
    with pytest.raises(SystemExit, match="找不到批次"):
        collect(db, cfg, batch="19000101")


def test_expiry_days_are_computed_against_as_of(db: Path, cfg):
    """天数按传入的 as_of 算：负数=已过期，NULL=到期日算不出（绝不当 0）。"""
    as_of = date(2026, 10, 26)
    result = collect(db, cfg, as_of=as_of)
    by_no = {c.cert_number: c for c in result.certs}

    assert result.as_of == "2026-10-26"
    assert by_no["C1"].expiry_days == (date(2026, 10, 1) - as_of).days
    assert by_no["C1"].expiry_days < 0
    assert by_no["C4"].expiry_days == (date(2027, 1, 31) - as_of).days
    assert by_no["C3"].expiry_days is None

    # 换一天算，结果整体位移（可复现、可回填）
    later = collect(db, cfg, as_of=date(2026, 10, 28))
    assert {c.cert_number: c.expiry_days for c in later.certs}["C4"] == (
        date(2027, 1, 31) - date(2026, 10, 28)).days


def test_expiry_days_written_for_all_three_tables(db: Path, cfg):
    as_of = date(2026, 10, 26)
    write_tables(db, collect(db, cfg, as_of=as_of))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        cert = conn.execute(f"SELECT * FROM {CERT_TABLE} WHERE cert_number='C1'").fetchone()
        assert cert["expiry_days"] == (date(2026, 10, 1) - as_of).days
        assert cert["days_as_of"] == "2026-10-26"

        stu = conn.execute(
            f"SELECT * FROM {STUDENT_TABLE} WHERE id_number='111111199001010001'").fetchone()
        assert stu["min_expiry_days"] == (date(2026, 10, 1) - as_of).days
        assert stu["days_as_of"] == "2026-10-26"

        comp = conn.execute(
            f"SELECT * FROM {COMPANY_TABLE} WHERE company_name='公司B'").fetchone()
        assert comp["min_expiry_days"] == (date(2027, 1, 31) - as_of).days

        # 三张表的 as_of 必须一致（不能一处今天、一处昨天）
        assert conn.execute(
            f"SELECT COUNT(DISTINCT days_as_of) FROM {CERT_TABLE}").fetchone()[0] == 1
    finally:
        conn.close()


def test_content_hash_is_stable_across_rebuilds(db: Path, cfg):
    """幂等自证：同一 as_of 连写两次，业务内容指纹必须完全相同。"""
    as_of = date(2026, 10, 26)

    write_tables(db, collect(db, cfg, as_of=as_of))
    conn = sqlite3.connect(db)
    try:
        first = content_hash(conn)
    finally:
        conn.close()

    write_tables(db, collect(db, cfg, as_of=as_of))
    conn = sqlite3.connect(db)
    try:
        second = content_hash(conn)
    finally:
        conn.close()

    assert first == second
