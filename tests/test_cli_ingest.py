from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import Workbook

from bi.cli import ingest_batch
from bi.store import FactStore
from bi.settings import get_settings


ROOT = Path(__file__).resolve().parents[1]
BATCH = "20261015"


def _write_workbook(path: Path, sheet: str, headers: list[str], rows: list[list[str]]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def test_configured_files_ingest_as_separate_datasets_and_mask_pii(tmp_path):
    inbound = tmp_path / "inbound" / BATCH
    inbound.mkdir(parents=True)
    _write_workbook(
        inbound / "(培训人员)表格视图.xlsx",
        "表格视图",
        ["姓名", "证件号码", "最高学历", "参加工作时间", "工作单位",
         "职称等级", "手机", "课程", "年份", "缴费情况"],
        [["测试人员", "111111199001010001", "本科", "2010", "单位甲",
          "中级", "13800000001", "课程甲", "2026", "已缴费"]],
    )
    _write_workbook(
        inbound / "合并汇总表（带课程月份）.xlsx",
        "汇总",
        ["姓名", "证件号码", "最高学历", "参加工作时间", "工作单位",
         "职称等级", "手机", "课程", "年份", "月份", "缴费情况"],
        [
            ["测试人员", "111111199001010001", "本科", "2010", "单位甲",
             "中级", "13800000001", "课程甲", "2026", "1", "已缴费"],
            ["测试人员", "111111199001010001", "本科", "2010", "单位甲",
             "中级", "13800000001", "课程乙", "2026", "2", "已缴费"],
        ],
    )
    settings = replace(
        get_settings(),
        db_path=tmp_path / "store" / "bi.sqlite3",
        database_url=None,
        inbound_dir=inbound.parent,
        config_dir=ROOT / "config",
    )

    results = ingest_batch(BATCH, settings)
    assert {result.dataset_id for result in results} == {
        "personnel_roster",
        "course_monthly_summary",
    }

    store = FactStore(settings)
    roster = store.list_versions("personnel_roster")
    summary = store.list_versions("course_monthly_summary")
    assert roster[0]["record_count"] == 1
    assert summary[0]["record_count"] == 2

    with store.connect() as conn:
        roster_row = conn.execute(
            """SELECT r.id_number, r.payload_json
               FROM raw_records r
               JOIN upload_versions v ON v.id=r.version_id
               WHERE v.dataset_id='personnel_roster'"""
        ).fetchone()
        summary_rows = conn.execute(
            """SELECT record_key FROM raw_records r
               JOIN upload_versions v ON v.id=r.version_id
               WHERE v.dataset_id='course_monthly_summary'"""
        ).fetchall()

    # 事实层落明文（用于按证件号精确去重 / 跨源关联）
    assert roster_row["id_number"] == "111111199001010001"
    # 展示层一律掩码
    assert "*" in json.loads(roster_row["payload_json"])["证件号码"]
    assert "*" in json.loads(roster_row["payload_json"])["手机"]
    assert len({row["record_key"] for row in summary_rows}) == 2


def test_analyze_batch_persists_diff_against_previous_batch(tmp_path):
    inbound = tmp_path / "inbound"
    for batch, status in (("20261014", "待考试"), ("20261015", "已通过")):
        batch_dir = inbound / batch
        batch_dir.mkdir(parents=True)
        _write_workbook(
            batch_dir / "report.xlsx",
            "记录",
            ["证件号码", "姓名", "状态"],
            [["320826197306181638", "测试人员", status]],
        )

    config_dir = tmp_path / "config"
    (config_dir / "datasets").mkdir(parents=True)
    (config_dir / "metrics").mkdir()
    (config_dir / "datasets" / "records.yaml").write_text(
        """dataset:
  id: test_records
  name: 测试记录
  sheet: 记录
identity:
  columns: [证件号码, 姓名]
  id_column: 证件号码
  name_column: 姓名
field_mapping:
  证件号码: id_number
  姓名: name
  状态: status
tracked_fields: [状态]
severity:
  状态: high
""",
        encoding="utf-8",
    )
    (config_dir / "metrics" / "records.yaml").write_text(
        """metrics:
  - key: rows
    name: 行数
    agg: count_rows
""",
        encoding="utf-8",
    )
    (config_dir / "import_sources.yaml").write_text(
        """sources:
  - filename: report.xlsx
    dataset_config: datasets/records.yaml
    metrics_config: metrics/records.yaml
""",
        encoding="utf-8",
    )
    settings = replace(
        get_settings(),
        db_path=tmp_path / "store" / "bi.sqlite3",
        database_url=None,
        inbound_dir=inbound,
        config_dir=config_dir,
    )

    from bi.cli import analyze_batch

    analyze_batch("20261014", settings)
    analyze_batch("20261015", settings)

    with FactStore(settings).connect() as conn:
        events = conn.execute(
            """SELECT event_type, field_name, value_from_raw, value_to_raw
               FROM diff_events WHERE dataset_id='test_records'
               AND batch_from='20261014' AND batch_to='20261015'"""
        ).fetchall()
    assert [(row["event_type"], row["field_name"]) for row in events] == [
        ("changed", "状态")
    ]
    assert events[0]["value_from_raw"] == "待考试"
    assert events[0]["value_to_raw"] == "已通过"


def _write_minimal_config(config_dir: Path, filename: str = "report.xlsx") -> None:
    """一份最小可用的数据集契约 + 指标 + 入站路由。"""
    (config_dir / "datasets").mkdir(parents=True)
    (config_dir / "metrics").mkdir()
    (config_dir / "datasets" / "records.yaml").write_text(
        """dataset:
  id: test_records
  name: 测试记录
  sheet: 记录
identity:
  columns: [证件号码, 姓名]
  id_column: 证件号码
  name_column: 姓名
field_mapping:
  证件号码: id_number
  姓名: name
  状态: status
tracked_fields: [状态]
severity:
  状态: high
""",
        encoding="utf-8",
    )
    (config_dir / "metrics" / "records.yaml").write_text(
        """metrics:
  - key: rows
    name: 行数
    agg: count_rows
""",
        encoding="utf-8",
    )
    (config_dir / "import_sources.yaml").write_text(
        f"""sources:
  - filename: {filename}
    dataset_config: datasets/records.yaml
    metrics_config: metrics/records.yaml
""",
        encoding="utf-8",
    )


def test_ingest_accepts_explicit_file_outside_inbound_dir(tmp_path):
    """`--file` 入站：文件不在 data/inbound 下也能入库（清洗产物在 data/cleaned 下）。

    这是 `scripts/clean_cert_validity.py` 的产物进入事实库的唯一通道——
    把文件拷进 inbound 目录会让同目录出现两份同名片段文件，路由会把它们
    当成两份数据互相覆盖。
    """
    config_dir = tmp_path / "config"
    _write_minimal_config(config_dir)

    inbound = tmp_path / "inbound"
    inbound.mkdir()
    cleaned = tmp_path / "cleaned"
    cleaned.mkdir()
    explicit = cleaned / "report.xlsx"
    _write_workbook(
        explicit, "记录", ["证件号码", "姓名", "状态"],
        [["320826197306181638", "测试人员", "已通过"]],
    )

    settings = replace(
        get_settings(),
        db_path=tmp_path / "store" / "bi.sqlite3",
        database_url=None,
        inbound_dir=inbound,
        config_dir=config_dir,
    )

    results = ingest_batch("20261015", settings, file_path=explicit)
    assert len(results) == 1
    assert results[0].dataset_id == "test_records"
    assert results[0].records == 1

    version = FactStore(settings).list_versions("test_records")[0]
    assert Path(version["source_file"]) == explicit
    assert version["filename"] == "report.xlsx"
    assert version["record_count"] == 1
    # 留痕：说明这一期是从显式路径入站的
    assert "显式指定文件" in (version["notes_json"] or "")


def test_ingest_explicit_file_rejects_unroutable_name(tmp_path):
    config_dir = tmp_path / "config"
    _write_minimal_config(config_dir)  # 只认 report.xlsx

    inbound = tmp_path / "inbound"
    inbound.mkdir()
    other = tmp_path / "cleaned" / "别的名字.xlsx"
    other.parent.mkdir()
    _write_workbook(other, "记录", ["证件号码", "姓名", "状态"],
                    [["320826197306181638", "测试人员", "已通过"]])

    settings = replace(
        get_settings(),
        db_path=tmp_path / "store" / "bi.sqlite3",
        database_url=None,
        inbound_dir=inbound,
        config_dir=config_dir,
    )

    with pytest.raises(RuntimeError, match="无法按文件名路由"):
        ingest_batch("20261015", settings, file_path=other)


def test_ingest_explicit_file_must_exist(tmp_path):
    settings = replace(
        get_settings(),
        db_path=tmp_path / "store" / "bi.sqlite3",
        database_url=None,
        inbound_dir=tmp_path / "inbound",
        config_dir=ROOT / "config",
    )

    with pytest.raises(FileNotFoundError):
        ingest_batch(
            "20261015", settings, file_path=tmp_path / "nope" / "report.xlsx"
        )


# ---------------------------------------------------------------------------
# 入站幂等护栏
# ---------------------------------------------------------------------------
ROSTER_HEADERS = ["姓名", "证件号码", "最高学历", "参加工作时间", "工作单位",
                  "职称等级", "手机", "课程", "年份", "缴费情况"]


def _roster_rows(count: int) -> list[list[str]]:
    return [
        [f"人员{i}", f"1111111990010100{i:02d}", "本科", "2010", "单位甲",
         "中级", f"138000000{i:02d}", "课程甲", "2026", "已缴费"]
        for i in range(count)
    ]


def _inbound_settings(tmp_path: Path, inbound: Path):
    return replace(
        get_settings(),
        db_path=tmp_path / "store" / "bi.sqlite3",
        database_url=None,
        inbound_dir=inbound,
        config_dir=ROOT / "config",
    )


def test_same_file_twice_is_skipped_without_writing(tmp_path):
    """同一份文件重复导入：指纹未变 -> 直接跳过，不新增版本、不重复插行。"""
    inbound = tmp_path / "inbound"
    batch_dir = inbound / BATCH
    batch_dir.mkdir(parents=True)
    _write_workbook(batch_dir / "(培训人员)表格视图.xlsx", "表格视图",
                    ROSTER_HEADERS, _roster_rows(2))
    settings = _inbound_settings(tmp_path, inbound)

    first = ingest_batch(BATCH, settings)
    assert [r.dataset_id for r in first] == ["personnel_roster"]

    second = ingest_batch(BATCH, settings)
    assert second == [], "指纹未变应当跳过（返回空结果）"

    store = FactStore(settings)
    assert len(store.list_versions("personnel_roster")) == 1
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 2


def test_same_file_in_another_batch_is_rejected(tmp_path):
    """同一份文件占两个批次快照：必须显式允许，否则拒绝。"""
    inbound = tmp_path / "inbound"
    for batch in (BATCH, "20261016"):
        directory = inbound / batch
        directory.mkdir(parents=True)
        _write_workbook(directory / "(培训人员)表格视图.xlsx", "表格视图",
                        ROSTER_HEADERS, _roster_rows(2))
    settings = _inbound_settings(tmp_path, inbound)

    ingest_batch(BATCH, settings)
    with pytest.raises(RuntimeError, match="已作为批次"):
        ingest_batch("20261016", settings)

    assert ingest_batch("20261016", settings, allow_duplicate_file=True)


def test_suspicious_drop_is_rejected(tmp_path):
    """记录数不足上一期 50%：疑似增量当全量，拒绝算差分。"""
    inbound = tmp_path / "inbound"
    first_dir = inbound / "20261015"
    first_dir.mkdir(parents=True)
    _write_workbook(first_dir / "(培训人员)表格视图.xlsx", "表格视图",
                    ROSTER_HEADERS, _roster_rows(4))
    settings = _inbound_settings(tmp_path, inbound)
    ingest_batch("20261015", settings)

    second_dir = inbound / "20261016"
    second_dir.mkdir(parents=True)
    _write_workbook(second_dir / "(培训人员)表格视图.xlsx", "表格视图",
                    ROSTER_HEADERS, _roster_rows(1))

    with pytest.raises(RuntimeError, match="疑似把"):
        ingest_batch("20261016", settings)
    assert ingest_batch("20261016", settings, force_large_drop=True)
