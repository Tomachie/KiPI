"""What a board takes from its KiCad project file (``.kicad_pro``, JSON beside it): the
net classes, and the maximum error KiCad cuts curves into segments with.

The project file is optional. Without it nets keep KiCad's ``Default`` class and curves
use KiCad's default error.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kipi.core.geometry.pad_shapes import DEFAULT_MAX_ERROR_MM

DEFAULT_CLASS = "Default"


@dataclass
class Project:
    path: Optional[Path] = None
    max_error_mm: float = DEFAULT_MAX_ERROR_MM
    assignments: Dict[str, str] = field(default_factory=dict)       # net -> class
    patterns: List[Tuple[str, str]] = field(default_factory=list)    # (pattern, class)

    def netclass(self, net: str) -> str:
        """The net's class: assigned by name, else the first pattern that matches it."""
        if not net:
            return DEFAULT_CLASS
        if net in self.assignments:
            return self.assignments[net]
        for pattern, cls in self.patterns:
            if _matches(pattern, net):
                return cls
        return DEFAULT_CLASS


def _matches(pattern: str, net: str) -> bool:
    """KiCad's net class patterns: wildcards, or a regular expression between slashes."""
    if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
        try:
            return re.search(pattern[1:-1], net) is not None
        except re.error:
            return False
    return fnmatch.fnmatchcase(net, pattern)


def project_file(board: Path) -> Path:
    return board.with_suffix(".kicad_pro")


def read(board: Path) -> Project:
    """The project of the board file ``board``; a default one if it has none, or one KiPI
    cannot read."""
    path = project_file(board)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Project()
    proj = Project(path=path)
    try:
        rules = data["board"]["design_settings"]["rules"]
        err = float(rules.get("max_error", DEFAULT_MAX_ERROR_MM))
        if err > 0:
            proj.max_error_mm = err
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    settings = data.get("net_settings") if isinstance(data, dict) else None
    if isinstance(settings, dict):
        for net, cls in (settings.get("netclass_assignments") or {}).items():
            if isinstance(cls, list):        # KiCad 9+: a net can have several classes
                cls = cls[0] if cls else ""
            if isinstance(cls, str) and cls:
                proj.assignments[net] = cls
        for entry in settings.get("netclass_patterns") or []:
            if isinstance(entry, dict) and entry.get("pattern") and entry.get("netclass"):
                proj.patterns.append((str(entry["pattern"]), str(entry["netclass"])))
    return proj
