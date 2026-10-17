from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from bi.cli import load_import_sources
from bi.settings import get_settings
from bi.store import FactStore
from bi.watcher import InboundWatcher


ROOT = Path(__file__).resolve().parents[1]


def test_watcher_waits_for_stable_configured_files_and_processes_once(tmp_path):
    batch = "20261015"
    batch_dir = tmp_path / "inbound" / batch
    batch_dir.mkdir(parents=True)
    settings = replace(
        get_settings(),
        db_path=tmp_path / "store" / "bi.sqlite3",
        database_url=None,
        inbound_dir=batch_dir.parent,
        config_dir=ROOT / "config",
    )
    for source in load_import_sources(settings):
        (batch_dir / source["filename"]).write_bytes(b"test workbook")
    FactStore(settings).init_schema()

    clock = [0.0]
    processed: list[str] = []
    watcher = InboundWatcher(
        settings,
        stable_seconds=10,
        process_batch=lambda name, _: processed.append(name),
        clock=lambda: clock[0],
    )

    watcher.scan_once()
    assert processed == []
    clock[0] = 9.0
    watcher.scan_once()
    assert processed == []
    clock[0] = 10.0
    watcher.scan_once()
    assert processed == [batch]
    watcher.scan_once()
    assert processed == [batch]
