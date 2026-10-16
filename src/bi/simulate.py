"""从真实 D1 派生 D2（次日文件），并记录 **ground truth**。

为什么不用「随便找两份 Excel」
------------------------------
差分引擎要证明「判定正确」，就必须知道正确答案。从真实数据派生下一期，可以在
生成时**精确记录我改了什么**（哪些新增、哪些删除、哪个字段从什么变成什么），
于是测试能够逐条断言引擎输出与事实一致——这比拿两份来历不明的表靠谱得多。

真实 D2 到手后，直接放到 data/inbound/<batch2>/ 即可，本脚本不再需要。

生成的 D2 覆盖所有差分门类 + 5 类已知异常，供阶段三/四/五验证与演示。
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import openpyxl

from .config import DatasetConfig, load_dataset_config
from .derive import DERIVED_COLUMNS, EXAM_STATUS_PASSED, PAYMENT_PAID
from .diff_engine import make_record_key
from .excel_reader import ROW_NUMBER_KEY, read_primary
from .ingest import resolve_input_file
from .settings import get_settings
from .values import canon_value, fmt_date

CHANGE_SEED = 20261013

_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"


def make_id_number(prefix17: str) -> str:
    """由 17 位前缀生成校验位合法的 18 位身份证号。"""
    assert len(prefix17) == 17 and prefix17.isdigit(), prefix17
    total = sum(int(prefix17[i]) * _ID_WEIGHTS[i] for i in range(17))
    return prefix17 + _ID_CHECK[total % 11]


class ChangeLog:
    """变更记录器：边改边记，最后产出 ground truth。"""

    def __init__(self, cfg: DatasetConfig):
        self.cfg = cfg
        self.changes: dict[str, dict[str, dict[str, str]]] = {}
        self.added: list[str] = []
        self.removed: list[str] = []
        self.injections: list[dict[str, Any]] = []
        self.notes: list[str] = []

    def set_field(self, key: str, row: dict[str, Any], field: str, value: Any) -> None:
        d = self.changes.setdefault(key, {})
        if field not in d:
            d[field] = {"from": canon_value(row.get(field), self.cfg.normalization)}
        row[field] = value
        d[field]["to"] = canon_value(value, self.cfg.normalization)

    def inject(self, rule_id: str, key: str, note: str) -> None:
        self.injections.append({"rule_id": rule_id, "record_key": key, "note": note})

    def ground_truth_changed(self) -> list[dict[str, Any]]:
        out = []
        for key, fields in self.changes.items():
            real = {f: v for f, v in fields.items() if v.get("from") != v.get("to")}
            if not real:
                continue
            pieces = key.split(self.cfg.identity.separator, 1)
            out.append({
                "key": key,
                "id_number": pieces[0],
                "name": pieces[1] if len(pieces) > 1 else "",
                "fields": real,
            })
        return sorted(out, key=lambda x: x["key"])


def simulate_next_batch(
    from_batch: str,
    to_batch: str,
    *,
    seed: int = CHANGE_SEED,
    write: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    src = resolve_input_file(settings.inbound_dir, from_batch)
    ds = read_primary(src, sheet=cfg.sheet)
    cols = [c for c in ds.columns if c not in DERIVED_COLUMNS]

    id_col = cfg.id_column
    rows_by_key: dict[str, dict[str, Any]] = {}
    dropped = 0
    for r in ds.rows:
        clean = {k: v for k, v in r.items() if k != ROW_NUMBER_KEY}
        if not str(clean.get(id_col) or "").strip():
            dropped += 1
            continue  # 口径：无证件号者不参与统计（必须与 ingest 的 require_identity 一致）
        rows_by_key[make_record_key(clean, cfg).key] = clean
    count_from = len(rows_by_key)

    rng = random.Random(seed)
    log = ChangeLog(cfg)
    keys = list(rows_by_key.keys())

    def pick(predicate, n: int, used: set[str]) -> list[str]:
        out = []
        for k in keys:
            if k in used:
                continue
            if predicate(rows_by_key[k]):
                out.append(k)
                used.add(k)
                if len(out) >= n:
                    break
        return out

    used: set[str] = set()

    # ---- A. 出分：待实操约考 -> 已通过（40 人）----
    passed_now = pick(lambda r: str(r.get("考试状态") or "").strip() == "待实操约考", 40, used)
    for k in passed_now:
        row = rows_by_key[k]
        log.set_field(k, row, "考试状态", EXAM_STATUS_PASSED)
        log.set_field(k, row, "考试日期", "2026-10-13")
        log.set_field(k, row, "分数", rng.randint(60, 95))

    # ---- B. 状态推进：未申请考试 -> 待实操约考（20 人）----
    for k in pick(lambda r: str(r.get("考试状态") or "").strip() == "未申请考试", 20, used):
        log.set_field(k, rows_by_key[k], "考试状态", "待实操约考")

    # ---- C. 付款：未付款 -> 已付款（15 人）----
    for k in pick(lambda r: str(r.get("是否付款") or "").strip() == "未付款", 15, used):
        row = rows_by_key[k]
        log.set_field(k, row, "是否付款", PAYMENT_PAID)
        log.set_field(k, row, "金额", rng.choice([1200, 1300]))

    # ---- D. 备注更新（25 人）----
    for k in pick(lambda r: True, 25, used):
        log.set_field(k, rows_by_key[k], "备注1", "已归档")

    # ---- E. 分数修正：前 10 个有分者 +5（封顶 100）----
    for k in pick(lambda r: str(r.get("分数") or "").strip() != "", 10, used):
        row = rows_by_key[k]
        try:
            old = float(str(row.get("分数")).strip())
        except ValueError:
            continue
        log.set_field(k, row, "分数", min(100, int(old + 5)))

    # ---- F. 新增报名（12 人，新批次 2026-10-13）----
    area_codes = ["310115", "330106", "320506", "340104"]
    surnames = list("赵钱孙李周吴郑王冯陈褚卫")
    given = list("伟芳娜敏静秀丽强磊洋艳勇军")
    for i in range(12):
        idn = make_id_number(f"{area_codes[i % 4]}1991{i % 12 + 1:02d}{(i + 1) % 28 + 1:02d}{i + 100:03d}"[:17])
        name = surnames[i % len(surnames)] + given[(i * 3) % len(given)]
        new_row = dict.fromkeys(cols, None)
        new_row.update({
            "姓名": name,
            "证件号码": idn,
            "取证类型": "首次取证",
            "开班编号": f"070678261013{i % 3 + 1}",
            "工作单位": "杭州东信网络技术有限公司",
            "科目": "登高",
            "考试状态": "未申请考试",
            "是否付款": "未付款",
            "金额": 1200,
        })
        key = make_record_key(new_row, cfg).key
        rows_by_key[key] = new_row
        log.added.append(key)

    # ---- G. 移除（8 人：退费/转出）----
    removable = [k for k in keys if k not in used][:8]
    for k in removable:
        log.removed.append(k)
        del rows_by_key[k]

    # ---- H. 异常注入（5 类，供阶段三/四/五演示）----
    inj_ok = [k for k in rows_by_key if k in passed_now]
    if len(inj_ok) >= 3:
        k = inj_ok[0]
        log.set_field(k, rows_by_key[k], "分数", 150)
        log.inject("score_out_of_range", k, "分数=150 越界")

        k = inj_ok[1]
        log.set_field(k, rows_by_key[k], "分数", 45)
        log.inject("passed_but_low_score", k, "已通过但分数 45")

        k = inj_ok[2]
        log.set_field(k, rows_by_key[k], "考试日期", None)
        log.inject("passed_but_no_exam_date", k, "已通过但清空考试日期")

    paid_keys = [k for k, r in rows_by_key.items() if str(r.get("是否付款") or "").strip() == PAYMENT_PAID]
    if paid_keys:
        k = paid_keys[-1]
        log.set_field(k, rows_by_key[k], "金额", 0)
        log.inject("paid_but_zero_amount", k, "已付款但金额 0")

    # 状态回退：从 D1 就是「已通过」的人里挑一个退回
    regressed = [k for k in rows_by_key
                 if k not in log.added and str(rows_by_key[k].get("考试状态") or "").strip() == EXAM_STATUS_PASSED
                 and k not in passed_now]
    if regressed:
        k = regressed[0]
        log.set_field(k, rows_by_key[k], "考试状态", "待实操约考")
        log.inject("exam_status_regressed", k, "已通过 -> 待实操约考（状态回退）")

    # ---- 写出 D2 xlsx ----
    out_path = settings.inbound_dir / str(to_batch) / f"{Path(src).name}"
    if write:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = ds.sheet
        ws.append(cols)
        for row in rows_by_key.values():
            ws.append([row.get(c) for c in cols])
        wb.save(out_path)

    truth = {
        "from_batch": str(from_batch),
        "to_batch": str(to_batch),
        "seed": seed,
        "count_from": count_from,
        "count_to": len(rows_by_key),
        "dropped_source_rows": dropped,
        "added": sorted(log.added),
        "removed": sorted(log.removed),
        "changed": log.ground_truth_changed(),
        "anomaly_injections": log.injections,
        "scenario": {
            "passed_now": len(passed_now),
            "status_advanced": 20,
            "paid_now": 15,
            "remark_updated": 25,
            "score_corrected": 10,
            "added": len(log.added),
            "removed": len(log.removed),
        },
    }
    truth["changed_event_count"] = sum(len(c["fields"]) for c in truth["changed"])
    truth["unchanged"] = truth["count_to"] - len(truth["added"]) - len(truth["changed"])

    gt_path = settings.project_root / "data" / "store" / f"ground_truth_{from_batch}_{to_batch}.json"
    if write:
        gt_path.parent.mkdir(parents=True, exist_ok=True)
        gt_path.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已生成 D2: {out_path}")
    print(f"  {truth['count_from']} 人 -> {truth['count_to']} 人")
    print(f"  新增 {len(truth['added'])}  移除 {len(truth['removed'])}  "
          f"变更 {len(truth['changed'])}（{truth['changed_event_count']} 字段级）  未变 {truth['unchanged']}")
    print(f"  注入异常 {len(truth['anomaly_injections'])} 条 -> {gt_path.name}")
    return truth
