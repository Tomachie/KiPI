"""A saved KiPI snapshot (``.kipi.json.gz``) as a :class:`~kipi.sources.base.BoardSource`."""

from __future__ import annotations

from pathlib import Path
from typing import List

from kipi.core.model.board import BoardSnapshot
from kipi.core.model.snapshot_io import load
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.base import BoardSource, Capabilities


class SnapshotFileSource(BoardSource):
    caps = Capabilities(reload=True)

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.description = f"the file {self.path}"

    def load(self, progress: ProgressReporter = NULL,
             refill_zones: bool = False) -> BoardSnapshot:
        progress.info(f"Reading {self.path}")
        return load(self.path)

    def files(self) -> List[Path]:
        return [self.path]
