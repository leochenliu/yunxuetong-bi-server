#!/usr/bin/env python
"""把「证书有效期」列里的**区间**折叠成到期日（右端）。

    "2026/09/18-2028/09/30"  ->  "2028/09/30"

为什么只折叠区间、不做别的
--------------------------
解析统一走 `src/bi/expiry.py::parse_expiry`（与证书台账入库用的是**同一份**实现），
所以脚本改出来的值一定和库里算出的 `过期日期` 一致，不会出现「脚本一套、入库一套」。

三条边界（与 AGENTS.md 约定 13/14 一致）：

1. **区间取右端，不取左端**。左端是生效日，不是到期日。
2. **右端不合法 → 整行不动**。实测 256 行是 `2026/04/17-2028/04/31`——4 月 31 日
   根本不存在。此时**绝不拿左端顶替**（那会算出一批"有效期 0 天"的假数据），
   原值保持不动并写进报告，交人工核对。
3. **单日期原样保留**。它本来就是到期日，不需要动。
   只有年月（`2027年1月`）默认也不动——按月末取值是**估计值**，
   需要显式加 `--mode all` 才做，且报告里会标出「估计值」。

不覆盖原文件、也不写回入站目录
------------------------------
原始 Excel 是唯一真实数据源（且含 PII）。默认写到
`data/cleaned/<批次>/<原名>.cleaned.xlsx` + 同目录的明细报告 CSV。

**为什么不写在原文件旁边**：`config/import_sources.yaml` 是按文件名片段路由的
（`小黄卡`）。同一个批次目录里若出现两份都含该片段的 xlsx，入站会把它们当成
**两份台账**互相覆盖。所以清洗结果必须与 `data/inbound/` 物理隔离；
`-o` 指到入站目录内或指到输入文件本身都会被直接拒绝（防手滑）。

用法
----
    # 默认：只折叠区间，输出 data/cleaned/<批次>/<原名>.cleaned.xlsx + 报告 CSV
    python scripts/clean_cert_validity.py data/inbound/certificates/小黄卡简表_....xlsx

    # 先看统计不落盘
    python scripts/clean_cert_validity.py <file.xlsx> --dry-run

    # 连「只有年月」的也按月末折叠（会得到估计值，报告里标注）
    python scripts/clean_cert_validity.py <file.xlsx> --mode all

    # 指定输出与报告路径
    python scripts/clean_cert_validity.py <file.xlsx> -o out.xlsx --report out.report.csv

本脚本**只改目标列**，其他列、其他工作表、原有格式一律不动。
报告里只有 Excel 物理行号与日期文本，**不含姓名/身份证号等个人信息**。
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import openpyxl  # noqa: E402

from bi.expiry import (  # noqa: E402
    SHAPE_MONTH,
    SHAPE_NONE,
    SHAPE_RANGE,
    SHAPE_SINGLE,
    parse_expiry,
)

DEFAULT_COLUMN = "证书有效期"
DEFAULT_SHEET = "小黄卡清单"
DEFAULT_DATE_FORMAT = "%Y/%m/%d"

MODE_RANGE = "range"   # 只折叠区间（默认，最保守）
MODE_ALL = "all"       # 区间 + 年月（年月按月末取值，属估计值）

# 报告里的状态标签
ST_CLEANED = "已清洗"
ST_CLEANED_EST = "已清洗（估计值）"
ST_NORMALIZED = "已规范化"
ST_KEEP_SINGLE = "保持不变（已是单日期）"
ST_KEEP_MONTH = "保持不变（只有年月）"
ST_UNPARSABLE = "不可解析（保持原值）"


@dataclass
class Decision:
    """单行的处理结论。new_value 为 None 表示不修改。"""

    new_value: str | None
    status: str
    detail: str
    shape: str


def decide(raw: Any, mode: str, date_format: str) -> Decision:
    """根据原始值与清洗口径，决定这一行怎么处理。"""
    info = parse_expiry(raw)

    if info.shape == SHAPE_RANGE:
        if info.ok:
            assert info.date is not None
            return Decision(
                info.date.strftime(date_format),
                ST_CLEANED,
                f"区间取右端：{info.start}~{info.date}",
                info.shape,
            )
        # 区间里有不合法的端点 —— 不猜、不顶替，原值不动
        return Decision(None, ST_UNPARSABLE, info.error or "区间不可解析", info.shape)

    if info.shape == SHAPE_MONTH:
        if mode != MODE_ALL:
            return Decision(
                None,
                ST_KEEP_MONTH,
                "只有年月，按月末取值是估计值；需要时加 --mode all",
                info.shape,
            )
        if info.ok:
            assert info.date is not None
            return Decision(
                info.date.strftime(date_format),
                ST_CLEANED_EST,
                "年月按月末取值，精度=月（估计值，续证前需回证面核对）",
                info.shape,
            )
        return Decision(None, ST_UNPARSABLE, info.error or "年月不可解析", info.shape)

    if info.shape == SHAPE_SINGLE:
        if not info.ok:
            return Decision(None, ST_UNPARSABLE, info.error or "日期不合法", info.shape)
        if mode != MODE_ALL:
            return Decision(None, ST_KEEP_SINGLE, "", info.shape)
        assert info.date is not None
        normalized = info.date.strftime(date_format)
        if str(raw).strip() == normalized:
            return Decision(None, ST_KEEP_SINGLE, "", info.shape)
        return Decision(normalized, ST_NORMALIZED, f"统一为 {date_format}", info.shape)

    return Decision(
        None, ST_UNPARSABLE, info.error or "未识别到日期片段", SHAPE_NONE
    )


@dataclass
class RowResult:
    row: int
    before: str
    after: str
    shape: str
    status: str
    detail: str


@dataclass
class Summary:
    total: int = 0
    changed: int = 0
    by_shape: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    samples: list[RowResult] = field(default_factory=list)

    def add(self, result: RowResult, changed: bool) -> None:
        self.total += 1
        self.by_shape[result.shape] = self.by_shape.get(result.shape, 0) + 1
        self.by_status[result.status] = self.by_status.get(result.status, 0) + 1
        if changed:
            self.changed += 1
            if len(self.samples) < 8:
                self.samples.append(result)


def _find_header(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    column: str,
    max_scan: int = 5,
) -> tuple[int | None, int | None]:
    """在前 max_scan 行里找表头，返回 (表头行号, 列号)，都是 1-based。"""
    for r_idx, row in enumerate(
        ws.iter_rows(min_row=1, max_row=max_scan, values_only=True), start=1
    ):
        for c_idx, value in enumerate(row, start=1):
            if isinstance(value, str) and value.strip() == column:
                return r_idx, c_idx
    return None, None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clean_workbook(
    source: Path,
    sheet: str | None,
    column: str,
    mode: str,
    date_format: str,
    apply: bool = True,
) -> tuple[
    openpyxl.workbook.workbook.Workbook | None,
    openpyxl.worksheet.worksheet.Worksheet,
    Summary,
    list[RowResult],
]:
    """扫描（并可选地就地修改）目标列。apply=False 时只统计。

    返回 (workbook, worksheet, summary, rows)。apply=False 时 workbook 为 None，
    rows 仍然完整（供 dry-run 输出报告）。
    """
    wb = openpyxl.load_workbook(source, data_only=False)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
    except KeyError:
        names = "、".join(wb.sheetnames)
        wb.close()
        raise SystemExit(f"[错误] 找不到工作表 {sheet!r}。现有工作表：{names}")

    header_row, col_idx = _find_header(ws, column)
    if header_row is None or col_idx is None:
        head = [
            str(c.value).strip()
            for c in ws[1]
            if c.value is not None
        ]
        wb.close()
        raise SystemExit(
            f"[错误] 在工作表 {ws.title!r} 前 5 行里找不到列 {column!r}。\n"
            f"       现有表头：{'、'.join(head)}"
        )

    summary = Summary()
    rows: list[RowResult] = []
    for r_idx in range(header_row + 1, ws.max_row + 1):
        cell = ws.cell(row=r_idx, column=col_idx)
        before = _cell_text(cell.value)
        if before == "":
            continue  # 空行不参与（也不计入 total，避免分母虚高）
        decision = decide(cell.value, mode, date_format)
        changed = decision.new_value is not None and decision.new_value != before
        after = decision.new_value if changed else before
        if changed and apply:
            cell.value = after
        result = RowResult(
            row=r_idx,
            before=before,
            after=after,
            shape=decision.shape,
            status=decision.status,
            detail=decision.detail,
        )
        rows.append(result)
        summary.add(result, changed=changed)

    return (wb if apply else None), ws, summary, rows


def write_report(path: Path, summary_rows: list[RowResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Excel行号", "原值", "清洗后", "形态", "状态", "说明"])
        for r in summary_rows:
            writer.writerow([r.row, r.before, r.after, r.shape, r.status, r.detail])


INBOUND_ROOT = REPO_ROOT / "data" / "inbound"
CLEANED_ROOT = REPO_ROOT / "data" / "cleaned"


def default_paths(source: Path) -> tuple[Path, Path]:
    """默认输出位置。

    **不能**把清洗结果写在原文件旁边（`data/inbound/<批次>/` 目录内）：
    `config/import_sources.yaml` 是按文件名片段路由的（`小黄卡`），
    同一批次目录里出现两份都含该片段的 xlsx，入站就会把它们当成**两份台账**
    互相覆盖。所以统一输出到 `data/cleaned/<批次>/`，与入站目录物理隔离。
    """
    try:
        relative = source.relative_to(INBOUND_ROOT)
        out_dir = CLEANED_ROOT / relative.parent
    except ValueError:
        out_dir = source.parent
    output = out_dir / f"{source.stem}.cleaned{source.suffix}"
    report = out_dir / f"{source.stem}.cleaned.report.csv"
    return output, report


def assert_output_outside_inbound(path: Path) -> None:
    """硬护栏：清洗结果不许落在 `data/inbound/` 下（否则会被入站路由重复摄入）。"""
    try:
        path.resolve().relative_to(INBOUND_ROOT.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"[错误] 输出路径在 data/inbound/ 下：{path}\n"
        "       入站路由按文件名片段匹配，同目录出现第二份同名片段文件会被当成"
        "第二份数据源互相覆盖。请换到 data/cleaned/ 或其它目录。"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把「证书有效期」列里的区间折叠成到期日（右端）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/clean_cert_validity.py data/inbound/certificates/xxx.xlsx\n"
            "  python scripts/clean_cert_validity.py xxx.xlsx --dry-run\n"
            "  python scripts/clean_cert_validity.py xxx.xlsx --mode all\n"
        ),
    )
    p.add_argument("source", help="输入的 .xlsx 路径")
    p.add_argument(
        "-o", "--output",
        help="输出 .xlsx 路径（默认 data/cleaned/<批次>/<原名>.cleaned.xlsx；输入不在 "
             "data/inbound 下时默认写同目录）",
    )
    p.add_argument("-s", "--sheet", default=None, help=f"工作表名（默认 {DEFAULT_SHEET}，取不到则第一个）")
    p.add_argument("-c", "--column", default=DEFAULT_COLUMN, help=f"目标列名（默认 {DEFAULT_COLUMN}）")
    p.add_argument(
        "-m", "--mode", choices=(MODE_RANGE, MODE_ALL), default=MODE_RANGE,
        help="range=只折叠区间（默认）；all=另外把只有年月的按月末折叠（估计值）",
    )
    p.add_argument("--date-format", default=DEFAULT_DATE_FORMAT, help=f"输出日期格式（默认 {DEFAULT_DATE_FORMAT}）")
    p.add_argument("--report", default=None, help="明细报告 CSV 路径（默认与输出同目录的 <原名>.cleaned.report.csv）")
    p.add_argument("--no-report", action="store_true", help="不写明细报告")
    p.add_argument("--dry-run", action="store_true", help="只统计，不写 xlsx（也不写报告）")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"[错误] 输入文件不存在：{source}")

    out_default, report_default = default_paths(source)
    output = Path(args.output).expanduser().resolve() if args.output else out_default
    report = Path(args.report).expanduser().resolve() if args.report else report_default

    if not args.dry_run and output == source:
        raise SystemExit(
            "[错误] 输出路径与输入文件相同。原始 Excel 是唯一真实数据源，"
            "本脚本拒绝就地覆盖；请用 -o 指定另一个路径。"
        )
    if not args.dry_run:
        assert_output_outside_inbound(output)

    print(f"[读取] {source.name}")
    sheet = args.sheet or DEFAULT_SHEET
    wb, ws, summary, rows = clean_workbook(
        source, sheet, args.column, args.mode, args.date_format, apply=not args.dry_run
    )

    print(f"[表]   {ws.title}｜列「{args.column}」｜口径 mode={args.mode}｜格式 {args.date_format}")
    print(f"[统计] 非空行 {summary.total}｜被修改 {summary.changed} 行")
    print("       形态分布：" + "、".join(f"{k} {v}" for k, v in sorted(summary.by_shape.items())))
    print("       状态分布：" + "、".join(f"{k} {v}" for k, v in sorted(summary.by_status.items())))

    if summary.samples:
        print("[样例] 前几条改动：")
        for s in summary.samples:
            print(f"       第 {s.row} 行  {s.before}  ->  {s.after}   （{s.status}）")

    unparsable = summary.by_status.get(ST_UNPARSABLE, 0)
    month_kept = summary.by_status.get(ST_KEEP_MONTH, 0)
    if unparsable:
        print(
            f"[注意] {unparsable} 行不可解析，**原值未改动**（右端日期不合法时不拿左端顶替）。"
            "明细见报告，需人工核对。"
        )
    if month_kept and args.mode != MODE_ALL:
        print(f"[注意] {month_kept} 行只有年月，默认未改（按月末取值是估计值）；需要时加 --mode all。")

    if args.dry_run:
        print("[dry-run] 未写任何文件。")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    assert wb is not None
    wb.save(output)
    wb.close()
    print(f"[写出] {output}")

    if not args.no_report:
        write_report(report, rows)
        print(f"[报告] {report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
