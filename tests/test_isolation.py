"""事实库护栏的**机器校验**。

本地事实库**绝不能**是线上生产库（Neon PG 托管域），也不能与生产库共用同一个连接串：
`_assert_not_source_db()` 在 `FactStore` 构造时强制拒绝。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from bi.settings import get_settings
from bi.store import FactStore, _assert_not_source_db

NEON_DSN = (
    "postgresql://owner:secret@ep-cool-name-123456.us-east-2.aws.neon.tech"
    "/neondb?sslmode=require"
)
OWN_DSN = "postgresql://bi:secret@db.internal:5432/yunxuetong_bi"


# ------------------------------------------------------------------ 事实库护栏
def test_rejects_neon_host():
    with pytest.raises(RuntimeError, match="Neon"):
        _assert_not_source_db(NEON_DSN)


def test_rejects_same_db_as_source_env(monkeypatch):
    dsn = "postgresql://u:p@db.internal:5432/neondb"
    monkeypatch.setenv("TRAININGOS_DATABASE_URL", dsn)
    with pytest.raises(RuntimeError, match="同一个库"):
        _assert_not_source_db(dsn)


def test_allows_own_postgres_and_sqlite(monkeypatch):
    monkeypatch.delenv("TRAININGOS_DATABASE_URL", raising=False)
    _assert_not_source_db(None)
    _assert_not_source_db(OWN_DSN)


def test_factstore_refuses_neon_database_url():
    settings = replace(get_settings(), database_url=NEON_DSN)
    with pytest.raises(RuntimeError, match="Neon"):
        FactStore(settings)
