"""Poll inbound batch folders and analyze stable Excel files."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .cli import analyze_batch, load_import_sources
from .config import load_dataset_config
from .excel_reader import file_fingerprint
from .ingest import TEMP_PREFIXES, resolve_input_file
from .settings import Settings
from .store import FactStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchFiles:
    dataset_files: tuple[tuple[str, Path], ...]
    signature: tuple[tuple[str, int, int], ...]


class InboundWatcher:
    def __init__(
        self,
        settings: Settings,
        *,
        stable_seconds: float = 10,
        retry_seconds: float = 30,
        process_batch=analyze_batch,
        clock=time.monotonic,
    ) -> None:
        self.settings = settings
        self.stable_seconds = stable_seconds
        self.retry_seconds = retry_seconds
        self.process_batch = process_batch
        self.clock = clock
        self.observed: dict[str, tuple[tuple[tuple[str, int, int], ...], float]] = {}
        self.processed: dict[str, tuple[tuple[str, int, int], ...]] = {}
        self.retry_after: dict[str, float] = {}
        self.store = FactStore(settings)

    def _batch_files(self, batch_dir: Path) -> BatchFiles | None:
        sources = load_import_sources(self.settings)
        source_files = {
            spec["filename"]: spec["dataset_config"]
            for spec in sources
        }
        present_sources = [
            (batch_dir / filename, config_path)
            for filename, config_path in source_files.items()
            if (batch_dir / filename).is_file()
        ]

        if present_sources:
            missing = [
                filename for filename in source_files
                if not (batch_dir / filename).is_file()
            ]
            if missing:
                return None
            pairs = [
                (
                    load_dataset_config(self.settings.config_dir / config_path).id,
                    path,
                )
                for path, config_path in present_sources
            ]
        else:
            candidates = [
                path for path in batch_dir.glob("*.xlsx")
                if not path.name.startswith(TEMP_PREFIXES)
                and not path.name.startswith("~")
            ]
            if not candidates:
                return None
            cfg = load_dataset_config(self.settings.dataset_config_path)
            pairs = [(cfg.id, resolve_input_file(self.settings.inbound_dir, batch_dir.name))]

        signature = tuple(
            sorted(
                (str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns)
                for _, path in pairs
            )
        )
        return BatchFiles(tuple(pairs), signature)

    def _already_current(self, batch: str, files: BatchFiles) -> bool:
        for dataset_id, path in files.dataset_files:
            version = next(
                (
                    item for item in self.store.list_versions(dataset_id)
                    if item["batch"] == batch
                ),
                None,
            )
            if version is None or version["sha256"] != file_fingerprint(path)["sha256"]:
                return False
        return True

    def scan_once(self) -> None:
        inbound = self.settings.inbound_dir
        if not inbound.is_dir():
            return

        now = self.clock()
        for batch_dir in sorted(path for path in inbound.iterdir() if path.is_dir()):
            try:
                files = self._batch_files(batch_dir)
            except Exception:
                logger.exception("扫描批次目录 %s 失败", batch_dir)
                continue
            if files is None:
                continue
            batch = batch_dir.name

            if self.processed.get(batch) == files.signature:
                continue
            previous_observation = self.observed.get(batch)
            if previous_observation is None or previous_observation[0] != files.signature:
                self.observed[batch] = (files.signature, now)
                continue
            if now - previous_observation[1] < self.stable_seconds:
                continue
            if now < self.retry_after.get(batch, 0):
                continue

            try:
                if self._already_current(batch, files):
                    logger.info("批次 %s 文件未变化，跳过", batch)
                else:
                    logger.info("发现稳定批次 %s，开始入库和分析", batch)
                    self.process_batch(batch, self.settings)
                self.processed[batch] = files.signature
                self.retry_after.pop(batch, None)
            except Exception:
                logger.exception("处理批次 %s 失败，将在稍后重试", batch)
                self.retry_after[batch] = now + self.retry_seconds


def watch_inbound(
    settings: Settings,
    *,
    poll_seconds: float = 5,
    stable_seconds: float = 10,
) -> None:
    if poll_seconds <= 0 or stable_seconds < 0:
        raise ValueError("poll_seconds 必须大于 0，stable_seconds 不能小于 0")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    watcher = InboundWatcher(settings, stable_seconds=stable_seconds)
    logger.info(
        "开始监控 %s（扫描间隔 %.1f 秒，文件稳定等待 %.1f 秒）",
        settings.inbound_dir, poll_seconds, stable_seconds,
    )
    try:
        while True:
            watcher.scan_once()
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        logger.info("目录监控已停止")
