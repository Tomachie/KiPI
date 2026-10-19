# Copyright (c) 2026 Tomachie LLC.
"""The DC solve at its typical or its worst case.

A setup can hold, besides each value KiPI solves with, a maximum: each load's current
(and each pin group's) and each series link's resistance. ``corner`` picks one:

* ``typ``: the values as entered.
* ``max``: the maximum where one is given, else the value as entered; the items solved
  without a maximum are listed, so the result says which loads were not at their worst.

:func:`resolve` turns a setup into a copy holding only the corner's numbers, before
anything else in the solve looks at it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CORNERS = ("typ", "max")
LABELS = {"typ": "Typical", "max": "Worst case (max)"}


@dataclass
class CornerInfo:
    corner: str = "typ"
    typical_used: List[str] = field(default_factory=list)   # worst case, no maximum given
    basis: Dict[str, str] = field(default_factory=dict)     # load name -> typ / max / ...

    def warnings(self) -> List[str]:
        if self.corner != "max" or not self.typical_used:
            return []
        return ["Worst case: no maximum is given for these, so the typical value is "
                "solved: " + ", ".join(self.typical_used)]


def corner_errors(setup) -> List[str]:
    """What makes the corners unsolvable: an unknown corner, or a maximum below its
    typical value."""
    errors = []
    if getattr(setup, "corner", "typ") not in CORNERS:
        errors.append(f"Unknown solve corner {setup.corner!r}")
    for l in setup.loads:
        pairs = [(l.current, l.current_max)] + [(g.current, g.current_max) for g in l.groups]
        if any(mx is not None and mx < typ for typ, mx in pairs):
            errors.append(f"Load {l.name}: a maximum current is below its typical current")
    for k in setup.links:
        if k.resistance_max is not None and k.resistance_max < k.resistance:
            errors.append(f"DC connection {k.name}: the maximum resistance is below its "
                          "typical resistance")
    return errors


def _pick(worst: bool, name: str, typ: float, mx: Optional[float],
          info: CornerInfo) -> Tuple[float, str]:
    if not worst:
        return typ, "typ"
    if mx is not None:
        return mx, "max"
    info.typical_used.append(name)
    return typ, "typ (no max)"


def resolve(setup):
    """A copy of ``setup`` with every current and resistance the corner's, and what was
    picked (:class:`CornerInfo`)."""
    out = copy.deepcopy(setup)
    info = CornerInfo(getattr(setup, "corner", "typ"))
    worst = info.corner == "max"
    for l in out.loads:
        if l.groups:
            bases = set()
            for g in l.groups:
                g.current, b = _pick(worst, f"{l.name} [{g.name or ','.join(g.pins)}]",
                                     g.current, g.current_max, info)
                g.current_max = None
                bases.add(b)
            info.basis[l.name] = bases.pop() if len(bases) == 1 else "mixed"
        else:
            l.current, info.basis[l.name] = _pick(worst, l.name, l.current, l.current_max,
                                                  info)
        l.current_max = None
    for k in out.links:
        if worst and k.resistance_max is None and k.resistance == 0:
            continue                    # a short stays a short: nothing to warn about
        k.resistance, _ = _pick(worst, f"DC connection {k.name}", k.resistance,
                                k.resistance_max, info)
        k.resistance_max = None
    return out, info
