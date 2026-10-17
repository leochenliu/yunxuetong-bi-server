from __future__ import annotations

from dataclasses import replace

from bi.settings import get_settings, load_settings
from bi.store import FactStore


def test_database_url_selects_postgresql_backend(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "BI_DATABASE_URL",
        "postgresql://bi_user:secret@localhost:5432/bi_test",
    )
    settings = load_settings(tmp_path / "missing.env")
    assert settings.database_url == "postgresql://bi_user:secret@localhost:5432/bi_test"
    assert FactStore(settings).backend == "postgresql"


def test_sqlite_path_override_keeps_sqlite_backend(tmp_path):
    settings = replace(
        get_settings(),
        db_path=tmp_path / "store.sqlite3",
        database_url=None,
    )
    assert FactStore(settings).backend == "sqlite"
