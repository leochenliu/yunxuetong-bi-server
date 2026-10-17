"""差分引擎：把「上期快照」与「本期数据集」逐人逐字段比对。

为什么不直接比对两份 Excel
---------------------------
每期拿到的 xlsx 是**当前全量状态**，只有每个人「今天」的字段值。上一期那些
「当时的状态」在下一期文件里已不存在——不落盘就算不出差异。所以必须先取快照。

四类判定
--------
  added      本期有、上期无（新录入 / 新报名）
  removed    上期有、本期无（退费 / 剔除 / 漏报）
  changed    主键相同但追踪字段值不同（逐字段记录 from -> to）
  unchanged  两期所有追踪字段完全一致
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import DatasetConfig
from .excel_reader import ROW_NUMBER_KEY, Dataset
from .values import canon_key, canon_value, display_value

# 事件类型
EVENT_ADDED = "added"
EVENT_REMOVED = "removed"
EVENT_CHANGED = "changed"


@dataclass
class RecordKeyParts:
    """复合主键的组成部分，便于展示与下钻。"""
    id_number: str
    name: str
    key: str


def make_record_key(row: dict[str, Any], cfg: DatasetConfig) -> RecordKeyParts:
    cols = cfg.key_columns
    parts = [canon_key(row.get(c)) for c in cols]
    id_number = canon_key(row.get(cfg.id_column))
    name = canon_key(row.get(cfg.name_column))
    key = cfg.identity.separator.join(p for p in parts if p != "") if name else id_number
    return RecordKeyParts(id_number=id_number, name=name, key=key)


@dataclass
class Snapshot:
    """一期数据的状态快照。"""
    records: dict[str, dict[str, str]] = field(default_factory=dict)
    # 键 -> Excel 物理行号（证据追溯用）
    source_rows: dict[str, int] = field(default_factory=dict)
    # 键 -> 原始行（明文，仅内存内使用；落库时按隐私开关处理）
    raw_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    duplicate_keys: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)


def build_snapshot(ds: Dataset, cfg: DatasetConfig) -> Snapshot:
    """从数据集生成快照。按**物理行**建键（不去重）——重复证件号属于不同的人，
    静默去重会让报表人数与基线人数对不上（实测 905 行 / 883 个唯一键）。"""
    fields = [f for f in cfg.all_tracked if f in ds.columns]
    snap = Snapshot(fields=fields)
    hits: dict[str, int] = {}

    for row in ds.rows:
        parts = make_record_key(row, cfg)
        if not parts.id_number:
            continue
        key = parts.key
        hits[key] = hits.get(key, 0) + 1
        snap.records[key] = {f: canon_value(row.get(f), cfg.normalization) for f in fields}
        snap.source_rows[key] = int(row.get(ROW_NUMBER_KEY, -1))
        snap.raw_rows[key] = dict(row)

    snap.duplicate_keys = sorted([k for k, n in hits.items() if n > 1])
    return snap


@dataclass
class FieldChange:
    field: str
    from_value: str
    to_value: str
    severity: str = "info"
    # 原始（未归一化）值，证据追溯时回显
    from_display: str = ""
    to_display: str = ""


@dataclass
class RecordChange:
    key: str
    id_number: str
    name: str
    changes: list[FieldChange] = field(default_factory=list)


@dataclass
class DiffResult:
    batch_from: str
    batch_to: str
    count_from: int
    count_to: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[RecordChange] = field(default_factory=list)
    unchanged: int = 0
    field_summary: list[dict[str, Any]] = field(default_factory=list)
    # 主键构成映射：key -> {id_number, name}
    key_parts: dict[str, dict[str, str]] = field(default_factory=dict)
    event_count: int = 0

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    @property
    def total_events(self) -> int:
        """事件数 = 新增 + 移除 + 字段级变更条数（与 diff_events 表行数一致）。"""
        return len(self.added) + len(self.removed) + self.event_count


def diff_snapshots(baseline: Snapshot, current: Snapshot, cfg: DatasetConfig,
                   batch_from: str, batch_to: str) -> DiffResult:
    """比较两期快照。只比两期都存在的字段（本期表少列时不误报）。"""
    fields = [f for f in baseline.fields if f in current.fields]
    sep = cfg.identity.separator

    result = DiffResult(
        batch_from=batch_from,
        batch_to=batch_to,
        count_from=len(baseline.records),
        count_to=len(current.records),
    )

    def parts_of(key: str) -> dict[str, str]:
        pieces = key.split(sep, 1)
        return {
            "id_number": pieces[0] if pieces else "",
            "name": pieces[1] if len(pieces) > 1 else "",
        }

    for key, now in current.records.items():
        before = baseline.records.get(key)
        if before is None:
            result.added.append(key)
            result.key_parts[key] = parts_of(key)
            continue

        changes: list[FieldChange] = []
        for f in fields:
            fv = before.get(f, "")
            tv = now.get(f, "")
            if fv != tv:
                br = baseline.raw_rows.get(key, {})
                cr = current.raw_rows.get(key, {})
                changes.append(
                    FieldChange(
                        field=f,
                        from_value=fv,
                        to_value=tv,
                        severity=cfg.severity_of(f),
                        from_display=display_value(br.get(f)),
                        to_display=display_value(cr.get(f)),
                    )
                )
        if changes:
            p = parts_of(key)
            result.changed.append(RecordChange(key=key, id_number=p["id_number"], name=p["name"], changes=changes))
            result.key_parts[key] = p
            result.event_count += len(changes)
        else:
            result.unchanged += 1

    for key in baseline.records:
        if key not in current.records:
            result.removed.append(key)
            result.key_parts[key] = parts_of(key)

    # 字段维度汇总
    counter: dict[str, int] = {}
    for rc in result.changed:
        for ch in rc.changes:
            counter[ch.field] = counter.get(ch.field, 0) + 1
    result.field_summary = [
        {"field": f, "count": c, "severity": cfg.severity_of(f)}
        for f, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return result


def diff_dataset_against_snapshot(baseline: Snapshot, ds: Dataset, cfg: DatasetConfig,
                                  batch_from: str, batch_to: str) -> DiffResult:
    return diff_snapshots(baseline, build_snapshot(ds, cfg), cfg, batch_from, batch_to)
