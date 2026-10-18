"""A KiCad board file (``.kicad_pcb``) as a :class:`~kipi.sources.base.BoardSource`."""

from __future__ import annotations

from pathlib import Path
from typing import List

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.base import BoardSource, Capabilities


class KiCadFileSource(BoardSource):
    """The board saved in ``path``. Reload reads the file again, so edits saved in KiCad
    show up; zones are as KiCad last filled them before saving."""
    caps = Capabilities(reload=True)

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.description = f"the file {self.path}"

    def load(self, progress: ProgressReporter = NULL,
             refill_zones: bool = False) -> BoardSnapshot:
        from kipi.sources.kicad_pcb.reader import read_board
        return read_board(self.path, progress)

    def files(self) -> List[Path]:
        return [self.path]
