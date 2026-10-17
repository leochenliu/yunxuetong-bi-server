"""生成「测试用」历史批次（D-N / D-N+1 ...），仅用于趋势图演示。

设计原则
- 不复用 simulate.py：D2 是已知规则的"真实派生"，不能从中再倒推历史点（会自循环）
- 指标曲线：让 5 个核心指标沿"业务增长前"的轨迹，呈现 6 期的连续趋势
- 每个历史点基于"前一期"+确定的微变化量，**无随机**，能复现
- 学员子集：从 D1 的 883 人里挑出"应该上个月就在的"作为前期人员
- 标注在 notes 里告诉 UI：这是测试数据，**不能当真实业务依据**

状态迁移（让趋势平滑，而不是悬崖式跳变）
- D1=已通过 → 历史期"待实操约考"（向前回退一步）
- D1=可结算 → 历史期"已通过"（结算 = 已考过）
- D1=待实操约考 → 历史期"未申请考试"
- D1=理论/实操考试阶段 → 历史期"未申请考试"
- D1=未申请考试 / 取消考试 → 保留
- 付款：D1 已付 → 保留（业务连续）；D1 未付 → 30% 概率改为已付（按名字 hash 稳定决定）
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import openpyxl

from .config import load_dataset_config
from .derive import COL_CLASS_DATE, derive
from .excel_reader import ROW_NUMBER_KEY, read_primary
from .ingest import resolve_input_file
from .settings import get_settings


# 4 个历史批次（D-3 / D-2 / D-1 / D0），每个比 D1 早 N 个月
HISTORICAL_OFFSETS_DAYS = [120, 90, 60, 30]

# 每期相对 D1 的人数（业务爬坡曲线）
ENROLLMENT_RATIO = [0.78, 0.83, 0.88, 0.94]


def make_historical_batches(
    real_batch: str = "20261012",
    *,
    write: bool = True,
) -> dict[str, Any]:
    """从 D1 出发，倒推 4 个历史批次（仅用于趋势图演示）。"""
    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    real_path = resolve_input_file(settings.inbound_dir, real_batch)
    real_ds = read_primary(real_path, sheet=cfg.sheet)
    real_rows = list(real_ds.rows)
    real_total = len(real_rows)

    plan = []
    today = _dt.date(2026, 10, 17)
    for offset, ratio in zip(HISTORICAL_OFFSETS_DAYS, ENROLLMENT_RATIO):
        target_count = int(real_total * ratio)
        sub = real_rows[:target_count]
        batch_date = today - _dt.timedelta(days=offset)
        batch = batch_date.strftime("%Y%m%d")
        plan.append((batch, sub, batch_date))

    result = {"batches": [], "note": "[示例数据] 4 个历史批次由 D1 倒推生成，仅用于趋势图演示"}
    if not write:
        return result

    for batch, sub_rows, batch_date in plan:
        out_dir = settings.inbound_dir / batch
        # 文件名前缀 [示例] 让"看 inbound 目录"的人立刻知道这是测试数据
        out_path = out_dir / f"[示例]{Path(real_path).name}"
        out_dir.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = real_ds.sheet
        cols = [c for c in real_ds.columns if c != ROW_NUMBER_KEY]
        ws.append(cols)
        for r in sub_rows:
            adjusted = adjust_row_for_period(r, batch_date, today)
            ws.append([adjusted.get(c) for c in cols])
        wb.save(out_path)
        result["batches"].append((batch, len(sub_rows)))
        print(f"  生成 {batch}（{batch_date.isoformat()}）：{len(sub_rows)} 人 → {out_path.name}")

    return result


def make_key(row: dict[str, Any], cfg) -> str:
    return f"{row.get(cfg.id_column) or ''}|{row.get('姓名') or ''}"


def adjust_row_for_period(r: dict[str, Any], batch_date: _dt.date, today: _dt.date) -> dict[str, Any]:
    """把 D1 的"当前状态"调整为该历史期的状态（状态迁移而非随机生成）。"""
    out = dict(r)
    cur_status = str(r.get("考试状态") or "").strip()
    if cur_status == "已通过":
        out["考试状态"] = "待实操约考"
    elif cur_status in ("待实操约考",):
        out["考试状态"] = "未申请考试"
        out["考试日期"] = ""
        out["分数"] = ""
    elif cur_status in ("理论考试阶段", "实操考试阶段"):
        out["考试状态"] = "未申请考试"
        out["考试日期"] = ""
        out["分数"] = ""
    elif cur_status == "可结算":
        out["考试状态"] = "已通过"
    # D1=未申请考试 / 取消考试：保留

    # 付款：D1 已付 → 保留；D1 未付 → 30% 概率改为已付
    name = str(r.get("姓名") or "")
    if str(r.get("是否付款") or "").strip() == "已付款":
        pass
    else:
        paid = sum(ord(c) for c in name) % 10 < 3
        if paid:
            out["是否付款"] = "已付款"
            out["金额"] = 1200
    return out
