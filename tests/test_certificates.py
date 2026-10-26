"""证书台账（小黄卡）测试：有效期解析、数据清洗、派生列、异常规则。

这个类目的核心风险只有一个——**「证书有效期」列写法不统一**，
任何"宽容解析"都会悄悄产出假过期日期。所以这里的测试重点是：
解析器在遇到不合法输入时**必须失败**，而不是退而求其次取一个像样的值。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from bi.anomaly import check_row_anomalies
from bi.cleaning import CleaningRule, clean_rows, clean_value
from bi.config import load_dataset_config
from bi.derive import derive
from bi.excel_reader import Dataset
from bi.expiry import (
    PRECISION_DAY,
    PRECISION_MONTH,
    SHAPE_MONTH,
    SHAPE_NONE,
    SHAPE_RANGE,
    SHAPE_SINGLE,
    parse_expiry,
)

CERT_COLUMNS = [
    "姓名", "身份证号码", "公司名称", "证书编号",
    "证书开始日期", "证书有效期", "日期来源",
]


def _dataset(rows: list[dict]) -> Dataset:
    return Dataset(
        name="证书台账", sheet="小黄卡清单",
        columns=list(CERT_COLUMNS), rows=rows,
        source_file="<test>",
    )


# ---------------------------------------------------------------- 解析器单测

def test_parse_single_date():
    info = parse_expiry("2026/10/01")
    assert info.ok and info.date.isoformat() == "2026-10-01"
    assert info.precision == PRECISION_DAY
    assert info.shape == SHAPE_SINGLE


def test_parse_range_takes_right_end():
    info = parse_expiry("2024/10/01-2026/10/01")
    assert info.ok and info.date.isoformat() == "2026-10-01"
    assert info.shape == SHAPE_RANGE
    assert info.start is not None and info.start.isoformat() == "2024-10-01"


def test_parse_year_month_uses_month_end():
    info = parse_expiry("2027年1月")
    assert info.ok and info.date.isoformat() == "2027-01-31"
    assert info.precision == PRECISION_MONTH
    assert info.shape == SHAPE_MONTH


def test_parse_rejects_invalid_range_end():
    """区间右端是 4 月 31 日（不存在）-> 必须解析失败，**不能拿左端顶替**。

    这是本类目最关键的一条回归：早期版本会跳过非法右端、返回左端（= 开始日期），
    于是 256 张证被算成「有效期 0 天」且过期日期完全错误。
    """
    info = parse_expiry("2026/04/17-2028/04/31")
    assert not info.ok
    assert info.shape == SHAPE_NONE
    assert info.date is None
    assert "不合法" in info.error


def test_parse_rejects_invalid_range_start():
    info = parse_expiry("2026/02/30-2028/04/30")
    assert not info.ok
    assert info.start is None


def test_parse_rejects_inverted_range():
    info = parse_expiry("2028/04/30-2026/04/30")
    assert not info.ok
    assert "倒挂" in info.error


def test_parse_rejects_too_many_tokens():
    info = parse_expiry("2024/10/01-2025/10/01-2026/10/01")
    assert not info.ok
    assert "形态不明" in info.error


@pytest.mark.parametrize("raw", [None, "", "   ", "无", "长期有效"])
def test_parse_unparsable_values(raw):
    info = parse_expiry(raw)
    assert not info.ok
    assert info.shape == SHAPE_NONE


# ------------------------------------------------------------------ 清洗单测

def test_cleaning_removes_tail_noise_only():
    rule = CleaningRule(column="身份证号码", trim=True, full_to_half=True,
                        remove_chars="‘’'’。.　", uppercase_x=True)
    assert clean_value("310229199403034414‘", rule) == ("310229199403034414", True)
    assert clean_value("310229198510301632.", rule) == ("310229198510301632", True)
    assert clean_value(" 310229198510301632 ", rule) == ("310229198510301632", True)
    # 末位小写 x → X
    assert clean_value("11010119900307401x", rule) == ("11010119900307401X", True)


def test_cleaning_never_invents_digits():
    """位数不对时**不许**补位——补出来就是编造。"""
    rule = CleaningRule(column="身份证号码", trim=True, remove_chars="‘’'’。.　")
    value, changed = clean_value("50235199507106139", rule)
    assert value == "50235199507106139" and changed is False
    value, changed = clean_value("4313222200210234414", rule)
    assert value == "4313222200210234414" and changed is False


def test_clean_rows_reports_changes():
    rule = CleaningRule(column="身份证号码", trim=True, remove_chars="‘’'’。.　")
    rows = [{"身份证号码": "310229199403034414‘"}, {"身份证号码": "110101199003074016"}]
    reports = clean_rows(rows, [rule])
    assert rows[0]["身份证号码"] == "310229199403034414"
    assert reports[0].changed == 1
    assert reports[0].total == 2
    assert "清洗列" in reports[0].as_note()


# -------------------------------------------------------- 配置 / 派生 / 异常

@pytest.fixture(scope="module")
def cert_cfg():
    from bi.settings import get_settings

    settings = get_settings()
    return load_dataset_config(settings.config_dir / "datasets" / "certificates.yaml")


def test_cert_config_declares_cleaning_and_expiry(cert_cfg):
    assert cert_cfg.id == "certificates"
    assert cert_cfg.cleaning, "必须声明字段清洗规则"
    assert cert_cfg.derive.expiry.get("column") == "证书有效期"
    # 主键必须带证书编号：一人多证，裸身份证会把多张证覆盖成一条
    assert "证书编号" in cert_cfg.identity.columns
    rule_ids = {r.id for r in cert_cfg.anomaly_rules}
    assert {"expiry_unresolved", "expiry_month_precision",
            "expiry_estimated_source", "id_number_length_invalid"} <= rule_ids


def test_derive_produces_expiry_columns(cert_cfg):
    ds = _dataset([
        {"姓名": "甲", "身份证号码": "110101199003074012", "公司名称": "A公司",
         "证书编号": "CTSE0001", "证书开始日期": "2024/10/01",
         "证书有效期": "2026/10/01", "日期来源": "按2年效期推算"},
        {"姓名": "乙", "身份证号码": "310229199403034414", "公司名称": "B公司",
         "证书编号": "CTSE0002", "证书开始日期": "2024/10/01",
         "证书有效期": "2026/04/17-2028/04/31", "日期来源": "证面原文"},
    ])
    derive(ds, cert_cfg)
    first, second = ds.rows
    assert first["过期日期"] == "2026-10-01"
    assert first["有效期天数"] == 730
    assert first["过期月份"] == "2026-10"
    assert first["有效期形态"] == SHAPE_SINGLE
    # 解析不出来的一律留空，不拿任何东西顶替
    assert second["过期日期"] is None
    assert second["有效期天数"] is None
    assert second["有效期形态"] == SHAPE_NONE


def test_anomalies_detect_certificate_problems(cert_cfg):
    """注入 5 类问题 -> 必须全部检出。"""
    ds = _dataset([
        {"姓名": "甲", "身份证号码": "110101199003074012", "公司名称": "A公司",
         "证书编号": "CTSE0001", "证书开始日期": "2024/10/01",
         "证书有效期": "2026/10/01", "日期来源": "按2年效期推算"},
        # 区间右端非法
        {"姓名": "乙", "身份证号码": "310229199403034414", "公司名称": "B公司",
         "证书编号": "CTSE0002", "证书开始日期": "2024/10/01",
         "证书有效期": "2026/04/17-2028/04/31", "日期来源": "证面原文"},
        # 只有年月 -> 估计值
        {"姓名": "丙", "身份证号码": "320826199512134719", "公司名称": "C公司",
         "证书编号": "CTSE0003", "证书开始日期": "2024/10/31",
         "证书有效期": "2026年10月", "日期来源": "按2年效期推算"},
        # 证件号位数不对
        {"姓名": "丁", "身份证号码": "1101011990030740", "公司名称": "D公司",
         "证书编号": "CTSE0004", "证书开始日期": "2024/10/01",
         "证书有效期": "2026/10/01", "日期来源": "证面原文"},
        # 证件号校验位错
        {"姓名": "戊", "身份证号码": "330106198807265528", "公司名称": "E公司",
         "证书编号": "CTSE0005", "证书开始日期": "2024/10/01",
         "证书有效期": "2026/10/01", "日期来源": "证面原文"},
    ])
    derive(ds, cert_cfg)
    found = check_row_anomalies(ds, cert_cfg)
    by_rule: dict[str, int] = {}
    for a in found:
        by_rule[a.rule_id] = by_rule.get(a.rule_id, 0) + 1
    assert by_rule.get("expiry_unresolved") == 1
    assert by_rule.get("expiry_month_precision") == 1
    assert by_rule.get("expiry_estimated_source") == 2   # 推算来源（含推算行 + 证面原文行不算）
    assert by_rule.get("id_number_length_invalid") == 1
    assert by_rule.get("invalid_id_number") == 1
    # 异常信息要能定位到具体原因
    msg = next(a.message for a in found if a.rule_id == "expiry_unresolved")
    assert "不合法" in msg


def test_certificates_dataset_does_not_touch_denggao(cert_cfg):
    """证书配置的清洗/派生不得影响登高数据集。"""
    from bi.settings import get_settings

    settings = get_settings()
    denggao = load_dataset_config(settings.dataset_config_path)
    assert not denggao.cleaning
    assert not denggao.derive.expiry


# ------------------------------------------------------------------ 真实数据

REAL_CERT_FILE = (Path(__file__).resolve().parents[1] / "data" / "inbound" / "certificates")


def _real_cert_path() -> Path | None:
    if not REAL_CERT_FILE.is_dir():
        return None
    files = [p for p in REAL_CERT_FILE.glob("*.xlsx") if not p.name.startswith("~$")]
    return files[0] if files else None


@pytest.mark.skipif(_real_cert_path() is None, reason="真实证书文件不在库里（data/ 已 gitignore）")
def test_real_certificates_ingest(cert_cfg):
    """真实台账入库：口径数字必须与源表核对一致。"""
    from bi.ingest import ingest

    path = _real_cert_path()
    assert path is not None
    from bi.excel_reader import read_primary

    ds = read_primary(path, sheet=cert_cfg.sheet)
    derive(ds, cert_cfg)
    assert len(ds.rows) == 9085

    shapes: dict[str, int] = {}
    for row in ds.rows:
        shape = row.get("有效期形态") or SHAPE_NONE
        shapes[shape] = shapes.get(shape, 0) + 1
    assert shapes[SHAPE_SINGLE] == 5643
    assert shapes[SHAPE_RANGE] == 2861
    assert shapes[SHAPE_MONTH] == 325
    assert shapes[SHAPE_NONE] == 256          # 区间右端写了不存在的日期

    # 不可解析的 256 条必须留空，不许用左端顶替
    unresolved = [r for r in ds.rows if r.get("有效期形态") == SHAPE_NONE]
    assert all(r.get("过期日期") is None for r in unresolved)
    assert all(r.get("有效期天数") is None for r in unresolved)
