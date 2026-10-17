"""阶段二：事实存储（SQLite）。

存什么、为什么
--------------
五张事实表构成「可追溯的证据链」：

  upload_versions    每次上传的版本（文件指纹、口径、行数）—— 回答「这份数据是哪来的」
  raw_records        每期每个人的原始记录（含 Excel 物理行号）—— 回答「当时这个人是什么状态」
  diff_events        两期之间的每条差分事件（字段级 from -> to）—— 回答「到底变了什么」
  metric_snapshots   每期的核心指标快照 —— 回答「指标是多少、为什么变」
  anomalies          数据质量与业务异常 —— 回答「哪里不对劲」

隐私设计
--------
证件号码默认**不落明文**：只存 sha256 摘要（用于跨期对齐与追溯）+ 掩码（用于展示）。
需要回明文时走 `BI_STORE_PII_PLAINTEXT=true` 显式开启，属于知情选择而非默认行为。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .anomaly import Anomaly
from .diff_engine import EVENT_ADDED, EVENT_CHANGED, EVENT_REMOVED, DiffResult
from .excel_reader import ROW_NUMBER_KEY
from .ingest import Ingested
from .settings import Settings, get_settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 上传版本：一次上传 = 一个版本
CREATE TABLE IF NOT EXISTS upload_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id       TEXT    NOT NULL,
    batch            TEXT    NOT NULL,
    source_file      TEXT    NOT NULL,
    filename         TEXT    NOT NULL,
    sheet            TEXT,
    sha256           TEXT    NOT NULL,
    size_bytes       INTEGER,
    file_modified_at TEXT,
    captured_at      TEXT    NOT NULL,
    row_count        INTEGER NOT NULL,
    record_count     INTEGER NOT NULL,
    dropped_rows     INTEGER NOT NULL DEFAULT 0,
    columns_json     TEXT    NOT NULL,
    notes_json       TEXT,
    status           TEXT    NOT NULL DEFAULT 'active',
    UNIQUE (dataset_id, batch)
);

-- 原始记录：每期每人一行
CREATE TABLE IF NOT EXISTS raw_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES upload_versions(id) ON DELETE CASCADE,
    record_key      TEXT    NOT NULL,
    id_number_hash  TEXT    NOT NULL,
    id_number_masked TEXT,
    id_number_plain TEXT,
    name            TEXT,
    source_row      INTEGER,
    payload_json    TEXT    NOT NULL,
    UNIQUE (version_id, record_key)
);
CREATE INDEX IF NOT EXISTS idx_raw_records_version ON raw_records(version_id);
CREATE INDEX IF NOT EXISTS idx_raw_records_hash ON raw_records(id_number_hash);

-- 差分事件：字段级
CREATE TABLE IF NOT EXISTS diff_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id       TEXT    NOT NULL,
    from_version_id  INTEGER REFERENCES upload_versions(id) ON DELETE CASCADE,
    to_version_id    INTEGER NOT NULL REFERENCES upload_versions(id) ON DELETE CASCADE,
    batch_from       TEXT    NOT NULL,
    batch_to         TEXT    NOT NULL,
    event_type       TEXT    NOT NULL,
    record_key       TEXT    NOT NULL,
    id_number_hash   TEXT,
    name             TEXT,
    field_name       TEXT,
    field_std_name   TEXT,
    value_from       TEXT,
    value_to         TEXT,
    value_from_raw   TEXT,
    value_to_raw     TEXT,
    severity         TEXT,
    source_row_from  INTEGER,
    source_row_to    INTEGER,
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_events_to ON diff_events(to_version_id);
CREATE INDEX IF NOT EXISTS idx_diff_events_type ON diff_events(event_type);
CREATE INDEX IF NOT EXISTS idx_diff_events_key ON diff_events(record_key);

-- 指标快照
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL REFERENCES upload_versions(id) ON DELETE CASCADE,
    batch        TEXT    NOT NULL,
    metric_key   TEXT    NOT NULL,
    metric_name  TEXT,
    value        REAL,
    unit         TEXT,
    decimals     INTEGER,
    descr        TEXT,
    computed_at  TEXT    NOT NULL,
    UNIQUE (version_id, metric_key)
);

-- 异常
CREATE TABLE IF NOT EXISTS anomalies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER REFERENCES upload_versions(id) ON DELETE CASCADE,
    diff_event_id INTEGER REFERENCES diff_events(id) ON DELETE CASCADE,
    scope        TEXT    NOT NULL DEFAULT 'row',
    rule_id      TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    record_key   TEXT,
    field_name   TEXT,
    value        TEXT,
    message      TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_version ON anomalies(version_id);

-- 审计日志（阶段四）
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    actor        TEXT,
    tool         TEXT    NOT NULL,
    params_json  TEXT,
    result_rows  INTEGER,
    duration_ms  INTEGER,
    status       TEXT    NOT NULL,
    message      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


def hash_id_number(value: str) -> str:
    return hashlib.sha256(("yt:" + value).encode("utf-8")).hexdigest()


def mask_id_number(value: str) -> str:
    s = (value or "").strip()
    if len(s) < 10:
        return "*" * len(s)
    return f"{s[:6]}{'*' * (len(s) - 10)}{s[-4:]}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _short_path(p: str | None) -> str | None:
    """把绝对路径缩成「批次目录/文件名」，便于在证据链里区分两期。"""
    if not p:
        return None
    try:
        path = Path(p)
        return f"{path.parent.name}/{path.name}"
    except Exception:
        return p


@dataclass
class VersionRow:
    id: int
    dataset_id: str
    batch: str
    filename: str
    row_count: int
    record_count: int
    captured_at: str
    sha256: str


class FactStore:
    def __init__(self, settings: Settings | None = None, db_path: str | Path | None = None):
        self.settings = settings or get_settings()
        self.db_path = Path(db_path) if db_path else self.settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 连接 ----------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---------------- 写入：一期数据 ----------------
    def save_ingested(self, ingested: Ingested, cfg=None, extra_notes: list[str] | None = None) -> int:
        """落库一期数据（幂等：同 dataset+batch 覆盖重建）。返回 version_id。

        extra_notes: 调用方追加的说明（例：历史期标注 "[示例数据]"）。
        """
        self.init_schema()
        if cfg is None:
            from .config import load_dataset_config

            cfg = load_dataset_config(self.settings.dataset_config_path)
        ds = ingested.dataset
        fp = ingested.fingerprint
        id_col = cfg.id_column
        name_col = cfg.name_column
        pii_plain = self.settings.store_pii_plaintext
        dataset_id = cfg.id

        dropped = 0
        for note in ingested.notes:
            if "已剔除" in note:
                import re
                m = re.search(r"已剔除\s*(\d+)\s*行", note)
                if m:
                    dropped = int(m.group(1))
        all_notes = list(ingested.notes) + list(extra_notes or [])

        with self.connect() as conn:
            # 幂等：先清掉引用这些版本的差分事件，再删版本本身。
            # （diff_events.from_version_id 指向「上一期」，删本期版本时级联不到它，
            #   必须显式清理，否则重跑会撞外键约束。）
            old_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM upload_versions WHERE dataset_id=? AND batch=?",
                (dataset_id, ingested.batch)).fetchall()]
            if old_ids:
                ph = ",".join("?" * len(old_ids))
                conn.execute(
                    f"DELETE FROM diff_events WHERE from_version_id IN ({ph}) OR to_version_id IN ({ph})",
                    [*old_ids, *old_ids],
                )
            conn.execute("DELETE FROM upload_versions WHERE dataset_id=? AND batch=?",
                         (dataset_id, ingested.batch))
            cur = conn.execute(
                """INSERT INTO upload_versions
                   (dataset_id, batch, source_file, filename, sheet, sha256, size_bytes,
                    file_modified_at, captured_at, row_count, record_count, dropped_rows,
                    columns_json, notes_json, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active')""",
                (dataset_id, ingested.batch, ingested.source_file, fp["filename"], ds.sheet,
                 fp["sha256"], fp["size_bytes"], fp["modified_at"], _now(),
                 len(ds.rows) + dropped, len(ds.rows), dropped,
                 json.dumps(ds.columns, ensure_ascii=False),
                 json.dumps(all_notes, ensure_ascii=False)),
            )
            version_id = int(cur.lastrowid)

            from .values import canon_value, display_value

            rows_payload = []
            for row in ds.rows:
                parts = ingested_key(row, cfg)
                id_number = str(row.get(id_col) or "").strip()
                payload = {}
                for k, v in row.items():
                    if k == ROW_NUMBER_KEY:
                        continue
                    if k in (id_col, "联系电话"):
                        payload[k] = mask_id_number(str(v)) if v else ""
                    else:
                        payload[k] = display_value(v)
                rows_payload.append((
                    version_id, parts, hash_id_number(id_number),
                    mask_id_number(id_number),
                    id_number if pii_plain else None,
                    str(row.get(name_col) or ""),
                    int(row.get(ROW_NUMBER_KEY, -1)),
                    json.dumps(payload, ensure_ascii=False),
                ))
            conn.executemany(
                """INSERT OR REPLACE INTO raw_records
                   (version_id, record_key, id_number_hash, id_number_masked,
                    id_number_plain, name, source_row, payload_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows_payload,
            )

            conn.executemany(
                """INSERT INTO metric_snapshots
                   (version_id, batch, metric_key, metric_name, value, unit, decimals, descr, computed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(version_id, ingested.batch, m.key, m.name, m.value, m.unit, m.decimals, m.desc, _now())
                 for m in ingested.metrics],
            )

            conn.executemany(
                """INSERT INTO anomalies
                   (version_id, scope, rule_id, severity, record_key, field_name, value, message, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(version_id, "row", a.rule_id, a.severity, a.record_key, a.field_name, a.value, a.message, _now())
                 for a in ingested.anomalies],
            )
        return version_id

    # ---------------- 写入：差分结果 ----------------
    def save_diff(self, result: DiffResult, cfg, from_version_id: int, to_version_id: int) -> int:
        """落库差分事件（先删旧的同 from->to 事件，保证可重跑）。"""
        dataset_id = cfg.id
        sep = cfg.identity.separator
        added_keys = set(result.added)
        removed_keys = set(result.removed)

        with self.connect() as conn:
            conn.execute(
                "DELETE FROM diff_events WHERE dataset_id=? AND batch_from=? AND batch_to=?",
                (dataset_id, result.batch_from, result.batch_to),
            )
            payload = []
            for k in result.added:
                p = result.key_parts.get(k, {})
                payload.append((dataset_id, from_version_id, to_version_id, result.batch_from,
                                result.batch_to, EVENT_ADDED, k,
                                hash_id_number(p.get("id_number", "")) if p.get("id_number") else None,
                                p.get("name", ""), None, None, None, None, None, None,
                                "info", None, None, _now()))
            for k in result.removed:
                p = result.key_parts.get(k, {})
                payload.append((dataset_id, from_version_id, to_version_id, result.batch_from,
                                result.batch_to, EVENT_REMOVED, k,
                                hash_id_number(p.get("id_number", "")) if p.get("id_number") else None,
                                p.get("name", ""), None, None, None, None, None, None,
                                "high", None, None, _now()))
            for rc in result.changed:
                for ch in rc.changes:
                    payload.append((dataset_id, from_version_id, to_version_id, result.batch_from,
                                    result.batch_to, EVENT_CHANGED, rc.key,
                                    hash_id_number(rc.id_number) if rc.id_number else None,
                                    rc.name, ch.field, cfg.standard_name(ch.field),
                                    ch.from_value, ch.to_value, ch.from_display, ch.to_display,
                                    ch.severity, None, None, _now()))
            conn.executemany(
                """INSERT INTO diff_events
                   (dataset_id, from_version_id, to_version_id, batch_from, batch_to, event_type,
                    record_key, id_number_hash, name, field_name, field_std_name,
                    value_from, value_to, value_from_raw, value_to_raw, severity,
                    source_row_from, source_row_to, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
            n = len(payload)
        return n

    def save_cross_period_anomalies(self, anomalies: list[Anomaly], version_id: int) -> int:
        if not anomalies:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO anomalies
                   (version_id, scope, rule_id, severity, record_key, field_name, value, message, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(version_id, "cross_period", a.rule_id, a.severity, a.record_key,
                  a.field_name, a.value, a.message, _now()) for a in anomalies],
            )
        return len(anomalies)

    # ---------------- 查询 ----------------
    def version_id_of(self, batch: str, dataset_id: str = "denggao") -> int | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT id FROM upload_versions WHERE dataset_id=? AND batch=?", (dataset_id, batch)
            ).fetchone()
        return int(r["id"]) if r else None

    def list_versions(self, dataset_id: str = "denggao") -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT v.*, (SELECT COUNT(*) FROM diff_events e WHERE e.to_version_id=v.id) AS event_count
                   FROM upload_versions v WHERE v.dataset_id=? ORDER BY v.batch""",
                (dataset_id,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # 解析 notes 找 [示例] 标记
            if d.get("notes_json"):
                try:
                    notes = json.loads(d["notes_json"])
                    d["is_demo"] = any("[示例" in n for n in notes)
                    d["notes"] = notes
                except (json.JSONDecodeError, TypeError):
                    d["is_demo"] = False
            else:
                d["is_demo"] = False
            result.append(d)
        return result

    def metric_snapshot(self, batch: str) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT metric_key, metric_name, value, unit, decimals, descr
                   FROM metric_snapshots WHERE batch=?""", (batch,)
            ).fetchall()
        return {r["metric_key"]: dict(r) for r in rows}

    def metric_timeseries(self, metric_key: str,
                          dataset_id: str = "denggao",
                          from_batch: str | None = None,
                          to_batch: str | None = None) -> list[dict[str, Any]]:
        """某指标在多期上的值（按 batch 升序）。指标快照表读取，O(N) 极快。"""
        with self.connect() as conn:
            where = ["v.dataset_id=?", "m.metric_key=?"]
            params: list[Any] = [dataset_id, metric_key]
            if from_batch:
                where.append("m.batch >= ?"); params.append(from_batch)
            if to_batch:
                where.append("m.batch <= ?"); params.append(to_batch)
            rows = conn.execute(
                f"""SELECT m.batch, m.value, m.unit, m.metric_name, m.decimals,
                          v.sha256 AS file_sha256,
                          v.record_count, v.row_count
                   FROM metric_snapshots m
                   JOIN upload_versions v ON v.id = m.version_id
                   WHERE {' AND '.join(where)}
                   ORDER BY m.batch""", params).fetchall()
        return [dict(r) for r in rows]

    def anomaly_counts(self, batch: str) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.rule_id, COUNT(*) c FROM anomalies a
                   JOIN upload_versions v ON v.id=a.version_id
                   WHERE v.batch=? GROUP BY a.rule_id ORDER BY c DESC""", (batch,)
            ).fetchall()
        return {r["rule_id"]: int(r["c"]) for r in rows}

    def list_anomalies(self, batch: str, limit: int = 200,
                       severity: str | None = None) -> list[dict[str, Any]]:
        where = ["v.batch=?"]
        params: list[Any] = [batch]
        if severity:
            where.append("a.severity=?")
            params.append(severity)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT a.*, v.batch FROM anomalies a
                    JOIN upload_versions v ON v.id=a.version_id
                    WHERE {' AND '.join(where)}
                    ORDER BY CASE a.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                             WHEN 'low' THEN 2 ELSE 3 END, a.id
                    LIMIT ?""",
                [*params, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def diff_events(self, batch_from: str, batch_to: str,
                    event_type: str | None = None, field_name: str | None = None,
                    record_key: str | None = None,
                    limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where = ["batch_from=? AND batch_to=?"]
        params: list[Any] = [batch_from, batch_to]
        if event_type:
            where.append("event_type=?")
            params.append(event_type)
        if field_name:
            where.append("field_name=?")
            params.append(field_name)
        if record_key:
            where.append("record_key=?")
            params.append(record_key)
        w = " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) c FROM diff_events WHERE {w}", params).fetchone()["c"]
            rows = conn.execute(
                f"""SELECT * FROM diff_events WHERE {w}
                    ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                           WHEN 'low' THEN 2 ELSE 3 END, id
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def diff_summary(self, batch_from: str, batch_to: str) -> dict[str, Any]:
        with self.connect() as conn:
            by_type = conn.execute(
                """SELECT event_type, COUNT(*) c FROM diff_events
                   WHERE batch_from=? AND batch_to=? GROUP BY event_type""",
                (batch_from, batch_to),
            ).fetchall()
            by_field = conn.execute(
                """SELECT field_name, severity, COUNT(*) c FROM diff_events
                   WHERE batch_from=? AND batch_to=? AND event_type='changed'
                   GROUP BY field_name, severity ORDER BY c DESC""",
                (batch_from, batch_to),
            ).fetchall()
            by_sev = conn.execute(
                """SELECT severity, COUNT(*) c FROM diff_events
                   WHERE batch_from=? AND batch_to=? GROUP BY severity""",
                (batch_from, batch_to),
            ).fetchall()
        return {
            "batch_from": batch_from,
            "batch_to": batch_to,
            "by_type": {r["event_type"]: int(r["c"]) for r in by_type},
            "by_field": [{"field": r["field_name"], "severity": r["severity"], "count": int(r["c"])}
                         for r in by_field],
            "by_severity": {r["severity"]: int(r["c"]) for r in by_sev},
        }

    def evidence(self, event_id: int) -> dict[str, Any] | None:
        """证据追溯：一条差分事件 -> 上一期/本期原始记录 + 源文件 + Excel 行号。"""
        with self.connect() as conn:
            ev = conn.execute("SELECT * FROM diff_events WHERE id=?", (event_id,)).fetchone()
            if not ev:
                return None
            ev = dict(ev)
            chain = []
            for ver_id, row_key, tag in ((ev["from_version_id"], "before", "上期"),
                                         (ev["to_version_id"], "after", "本期")):
                if not ver_id:
                    continue
                rec = conn.execute(
                    "SELECT * FROM raw_records WHERE version_id=? AND record_key=?",
                    (ver_id, ev["record_key"]),
                ).fetchone()
                ver = conn.execute("SELECT * FROM upload_versions WHERE id=?", (ver_id,)).fetchone()
                chain.append({
                    "which": tag,
                    "batch": ver["batch"] if ver else None,
                    "version_id": ver_id,
                    "file": ver["filename"] if ver else None,
                    "path": _short_path(ver["source_file"]) if ver else None,
                    "file_sha256": ver["sha256"] if ver else None,
                    "source_row": rec["source_row"] if rec else None,
                    "id_number_masked": rec["id_number_masked"] if rec else None,
                    "present": rec is not None,
                    "payload": json.loads(rec["payload_json"]) if rec else None,
                })
            return {"event": ev, "chain": chain}

    # ---------------- 审计 ----------------
    def audit(self, tool: str, params: Any, result_rows: int | None,
              status: str = "ok", actor: str = "api", duration_ms: int | None = None,
              message: str | None = None) -> None:
        if not self.settings.audit_enabled:
            return
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO audit_log (ts, actor, tool, params_json, result_rows, duration_ms, status, message)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (_now(), actor, tool, json.dumps(params, ensure_ascii=False, default=str),
                 result_rows, duration_ms, status, message),
            )

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def ingested_key(row: dict[str, Any], cfg) -> str:
    """与 diff_engine.make_record_key 保持一致的键计算（避免循环 import）。"""
    from .values import canon_key

    parts = [canon_key(row.get(c)) for c in cfg.key_columns]
    id_number = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    return cfg.identity.separator.join(p for p in parts if p) if name else id_number
