#!/usr/bin/env python
"""把 SQLite 事实库整体复制到 PostgreSQL（同构复制，可重复执行）。

为什么需要它
------------
默认后端是 SQLite（data/store/bi.sqlite3）。当事实库要交给外部只读工具
（ChatBI / Text2SQL）时，不能直接暴露这个文件：

  * SQLite 没有账号级只读权限，给了文件就等于给了写权限；
  * 事实层 raw_records.id_number / certificates.id_number 是**明文**证件号，
    直连会让明文进入对方的查询结果与模型 prompt（AGENTS.md 约定 10）。

正确做法是在自有 PG 上建一个**独立 database**（不要塞进别的应用的库，
否则只读账号会连带看见那个应用的表），复制过去后只授 SELECT。

设计约束
--------
* 建表**不写第二份 DDL**：调 FactStore.init_schema()，与 sql/schema.sql 同源；
* 幂等：先 TRUNCATE ... RESTART IDENTITY CASCADE 再全量插入，跑几次结果一致；
* 显式插入了自增 id，**必须 setval 重置序列**，否则目标库下一次 INSERT 撞主键；
* 按外键依赖顺序插入，不依赖 session_replication_role（那是 superuser 才有的特权）。

用法
----
    python scripts/migrate_to_postgres.py \
        --target postgresql://postgres:<pw>@localhost:5432/bi_projection

    # 只校验不写入
    python scripts/migrate_to_postgres.py --target ... --check
"""

from __future__ import annotations

import argparse
import dataclasses
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bi.settings import get_settings  # noqa: E402
from bi.store import FactStore  # noqa: E402

# 外键决定插入顺序：父表必须先于子表。
# upload_versions 是 raw_records / diff_events / metric_* / anomalies / certificates 的父表。
TABLE_ORDER: tuple[str, ...] = (
    "upload_versions",
    "raw_records",
    "diff_events",
    "metric_snapshots",
    "metric_buckets",
    "anomalies",
    "job_runs",
    "audit_log",
    "certificates",
    "students",
    "companies",
)

# 带 INTEGER ... AS IDENTITY 主键的表：显式插入 id 后必须重置序列。
# certificates / students / companies 的主键是业务自然键（证书编号 / 证件号 / 公司名），
# 没有 id 列，因此不在这个列表里。
IDENTITY_TABLES: tuple[str, ...] = (
    "upload_versions",
    "raw_records",
    "diff_events",
    "metric_snapshots",
    "metric_buckets",
    "anomalies",
    "job_runs",
    "audit_log",
)


def _sqlite_rows(db_path: Path, table: str) -> tuple[list[str], list[tuple]]:
    """读一张表的全部行，返回 (列名, 行元组)。列名从游标描述里取，不硬编码。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        columns = [d[0] for d in cur.description]
        rows = [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return columns, rows


def _pg_counts(store: FactStore) -> dict[str, int]:
    with store.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in TABLE_ORDER
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 事实库 -> PostgreSQL")
    parser.add_argument("--source", default=None,
                        help="源 SQLite 路径（默认取 BI_DB_PATH）")
    parser.add_argument("--target", default=None,
                        help="目标 PG 连接串（默认取 BI_DATABASE_URL）")
    parser.add_argument("--check", action="store_true",
                        help="只比对两侧行数，不写入")
    args = parser.parse_args()

    settings = get_settings()
    source = Path(args.source) if args.source else settings.db_path
    target = (args.target or settings.database_url or "").strip()

    if not source.is_file():
        print(f"找不到源库：{source}")
        return 1
    if not target:
        print("必须给 --target，或设置 BI_DATABASE_URL")
        return 1

    # 建表走单一事实源（sql/schema.sql -> POSTGRES_SCHEMA）。
    # _assert_not_source_db 护栏在这里同样生效：指向 Neon / 生产库会直接抛错。
    store = FactStore(settings=dataclasses.replace(settings, database_url=target))

    if args.check:
        target_counts = _pg_counts(store)
        bad = 0
        for table in TABLE_ORDER:
            _, rows = _sqlite_rows(source, table)
            same = len(rows) == target_counts.get(table, -1)
            bad += 0 if same else 1
            print(f"  {'OK ' if same else '差异'} {table}: "
                  f"sqlite={len(rows)} pg={target_counts.get(table, -1)}")
        print("\n一致" if not bad else f"\n{bad} 张表行数不一致")
        return 0 if not bad else 2

    print(f"源：{source}")
    print(f"目标：{target.split('@')[-1]}")
    print(f"后端：{store.backend}")

    store.init_schema()
    print("建表完成（结构与 sql/schema.sql 一致）")

    with store.connect() as conn:
        # 一次性清空 + 重置自增，避免按表 TRUNCATE 时的 CASCADE 顺序干扰
        conn.execute(
            "TRUNCATE TABLE " + ", ".join(TABLE_ORDER) + " RESTART IDENTITY CASCADE"
        )

        for table in TABLE_ORDER:
            columns, rows = _sqlite_rows(source, table)
            if rows:
                placeholders = ", ".join(["%s"] * len(columns))
                sql = (f"INSERT INTO {table} ({', '.join(columns)}) "
                       f"VALUES ({placeholders})")
                conn.executemany(sql, rows)
            print(f"  {table}: {len(rows)} 行")

        # 关键：显式插入了 id，序列仍停在 1，不重置的话目标库下一次 INSERT 会撞主键
        for table in IDENTITY_TABLES:
            conn.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"(SELECT COUNT(*) > 0 FROM {table}))"
            )
        print("自增序列已重置")

    # ---- 校验：行数必须逐表一致 ----
    target_counts = _pg_counts(store)
    print("\n=== 校验 ===")
    bad = 0
    for table in TABLE_ORDER:
        _, rows = _sqlite_rows(source, table)
        same = len(rows) == target_counts.get(table, -1)
        bad += 0 if same else 1
        print(f"  {'OK ' if same else '差异'} {table}: "
              f"sqlite={len(rows)} pg={target_counts.get(table, -1)}")

    # ---- 序列自证：用一次回滚的 INSERT 试内存？不做。改为核对序列值 >= MAX(id) ----
    with store.connect() as conn:
        for table in IDENTITY_TABLES:
            seq = conn.execute(
                f"SELECT last_value FROM {table}_id_seq"
            ).fetchone()["last_value"]
            max_id = conn.execute(
                f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}"
            ).fetchone()["m"]
            flag = "OK " if seq >= max_id else "风险"
            bad += 0 if seq >= max_id else 1
            print(f"  {flag} {table}_id_seq last_value={seq} max(id)={max_id}")

    print("\n迁移完成，两侧行数与序列一致" if not bad
          else f"\n完成，但有 {bad} 处不一致，请核查")
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    raise SystemExit(main())
