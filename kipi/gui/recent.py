"""The board files opened lately, newest first, for the Open button's menu."""

from __future__ import annotations

from pathlib import Path
from typing import List

from qtpy.QtCore import QSettings

KEY = "recent/boards"
LIMIT = 10


def settings() -> QSettings:
    return QSettings("KiPI", "KiPI")


def boards() -> List[str]:
    """The recent boards that still exist."""
    value = settings().value(KEY, [])
    if isinstance(value, str):          # QSettings gives a one-item list back as a string
        value = [value]
    return [p for p in (value or []) if isinstance(p, str) and Path(p).is_file()]


def add(path) -> None:
    path = str(Path(path).resolve())
    keep = [p for p in boards() if Path(p) != Path(path)]
    settings().setValue(KEY, [path] + keep[:LIMIT - 1])


def clear() -> None:
    settings().remove(KEY)
