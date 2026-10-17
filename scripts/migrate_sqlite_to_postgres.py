from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bi.settings import load_settings
from bi.store import FactStore

TABLES = (
    "upload_versions",
    "raw_records",
    "diff_events",
    "metric_snapshots",
    "anomalies",
    "audit_log",
)
BATCH_SIZE = 1000


def migrate() -> dict[str, int]:
    settings = load_settings()
    if not settings.database_url:
        raise RuntimeError("Set BI_DATABASE_URL to the PostgreSQL destination before migrating.")
    if not settings.db_path.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {settings.db_path}")

    target = FactStore(settings)
    target.init_schema()
    copied: dict[str, int] = {}
    with closing(sqlite3.connect(settings.db_path)) as source:
        source.row_factory = sqlite3.Row
        with target.connect() as destination:
            for table in TABLES:
                if destination.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]:
                    raise RuntimeError(
                        f"PostgreSQL table {table} is not empty; migration was not started."
                    )

            for table in TABLES:
                columns = [
                    row["name"]
                    for row in source.execute(f"PRAGMA table_info({table})").fetchall()
                ]
                if not columns:
                    raise RuntimeError(f"SQLite source table is missing: {table}")
                column_list = ", ".join(columns)
                placeholders = ", ".join("?" for _ in columns)
                insert = (
                    f"INSERT INTO {table} ({column_list}) "
                    f"VALUES ({placeholders})"
                )
                cursor = source.execute(f"SELECT {column_list} FROM {table}")
                count = 0
                while rows := cursor.fetchmany(BATCH_SIZE):
                    destination.executemany(insert, [tuple(row) for row in rows])
                    count += len(rows)
                copied[table] = count

            for table in TABLES:
                destination.execute(
                    f"""SELECT setval(
                        pg_get_serial_sequence('{table}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {table}), 1),
                        EXISTS (SELECT 1 FROM {table})
                    )"""
                )
    return copied


if __name__ == "__main__":
    counts = migrate()
    print("SQLite → PostgreSQL migration complete:")
    for table, count in counts.items():
        print(f"  {table}: {count} rows")
