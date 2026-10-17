from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

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
            """SELECT r.id_number_plain, r.payload_json
               FROM raw_records r
               JOIN upload_versions v ON v.id=r.version_id
               WHERE v.dataset_id='personnel_roster'"""
        ).fetchone()
        summary_rows = conn.execute(
            """SELECT record_key FROM raw_records r
               JOIN upload_versions v ON v.id=r.version_id
               WHERE v.dataset_id='course_monthly_summary'"""
        ).fetchall()

    assert roster_row["id_number_plain"] is None
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
