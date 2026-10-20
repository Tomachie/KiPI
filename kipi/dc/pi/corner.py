# Copyright (c) 2026 Tomachie LLC.
"""The DC solve at its typical or its worst case.

A setup can hold, besides each value KiPI solves with, a maximum: each load's current
(and each pin group's) and each series link's resistance. ``corner`` picks one:

* ``typ``: the values as entered.
* ``max``: the maximum where one is given, else the value as entered; the items solved
  without a maximum are listed, so the result says which loads were not at their worst.
  A VRM with a DC accuracy is solved at the end of it nearer 0 V (its lowest output),
  the loads' tolerances and the VRMs' input ranges first fixed in volts at the set
  voltages, so they do not move with it.

:func:`resolve` turns a setup into a copy holding only the corner's numbers, before
anything else in the solve looks at it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kipi.dc.pi.model import net_groups, rail_nominal, range_unit, range_volts, vin_range

CORNERS = ("typ", "max")
LABELS = {"typ": "Typical", "max": "Worst case (max)"}


@dataclass
class CornerInfo:
    corner: str = "typ"
    typical_used: List[str] = field(default_factory=list)   # worst case, no maximum given
    basis: Dict[str, str] = field(default_factory=dict)     # load name -> typ / max / ...
    set_points: Dict[str, Tuple[float, float]] = field(default_factory=dict)  # VRM -> (set, solved)
    no_accuracy: List[str] = field(default_factory=list)    # VRMs solved at their set voltage

    def warnings(self) -> List[str]:
        if self.corner != "max":
            return []
        out = []
        if self.set_points:
            out.append("Worst case: these VRMs are solved at the low end of their DC "
                       "accuracy: " + ", ".join(f"{n} {lo:.4g} V (set {v:.4g} V)"
                                                for n, (v, lo) in self.set_points.items()))
        if self.no_accuracy:
            out.append("Worst case: no DC accuracy is given for these VRMs, so they are "
                       "solved at their set voltage: " + ", ".join(self.no_accuracy))
        if self.typical_used:
            out.append("Worst case: no maximum is given for these, so the typical value is "
                       "solved: " + ", ".join(self.typical_used))
        return out


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
        if (k.forward_v is not None and k.forward_v_max is not None
                and k.forward_v_max < k.forward_v):
            errors.append(f"DC connection {k.name}: the maximum forward voltage is below "
                          "its typical forward voltage")
    return errors


def _pick(worst: bool, name: str, typ: float, mx: Optional[float],
          info: CornerInfo) -> Tuple[float, str]:
    if not worst:
        return typ, "typ"
    if mx is not None:
        return mx, "max"
    info.typical_used.append(name)
    return typ, "typ (no max)"


def _fix_in_volts(setup):
    """Every load tolerance and VRM input range given in % or volts from a nominal, as
    volts at the set voltages now, so lowering a set voltage does not move them."""
    groups = net_groups(setup)
    ranges = {v.name: vin_range(setup, v, groups) for v in setup.vrms
              if v.vin_pct or v.vin_dv}
    for l in setup.loads:
        if l.has_tolerance() and (l.tol_pct or l.tol_dv):
            nominal = rail_nominal(setup, l.supply.net, groups=groups)
            if nominal:
                l.tol_min, l.tol_max = l.limits(nominal)
                l.tol_pct = l.tol_dv = False
    for v in setup.vrms:
        if v.name in ranges:
            v.vin_min, v.vin_max = ranges[v.name]
            v.vin_pct = v.vin_dv = False


def _low_end(v) -> Optional[float]:
    """The VRM's lowest output in its DC accuracy (nearest 0 V on a negative rail), None
    without an accuracy."""
    if not v.has_accuracy() or not v.voltage:
        return None
    ends = [x for x in range_volts(v.voltage, v.acc_min, v.acc_max,
                                   range_unit(v.acc_pct, v.acc_dv)) if x is not None]
    lowest = min(ends + [v.voltage], key=abs)
    return lowest if lowest != v.voltage else None


def _set_points(out, info: CornerInfo):
    lows = {v.name: _low_end(v) for v in out.vrms}
    info.no_accuracy = [v.name for v in out.vrms if not v.has_accuracy()]
    if not any(x is not None for x in lows.values()):
        return
    _fix_in_volts(out)
    for v in out.vrms:
        if lows[v.name] is not None:
            info.set_points[v.name] = (v.voltage, lows[v.name])
            v.voltage = lows[v.name]


def resolve(setup):
    """A copy of ``setup`` with every current, resistance and VRM set voltage the
    corner's, and what was picked (:class:`CornerInfo`)."""
    out = copy.deepcopy(setup)
    info = CornerInfo(getattr(setup, "corner", "typ"))
    worst = info.corner == "max"
    if worst:
        _set_points(out, info)
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
        if k.forward_v is not None:
            k.forward_v, _ = _pick(worst, f"DC connection {k.name} forward voltage",
                                   k.forward_v, k.forward_v_max, info)
            k.forward_v_max = None
        if worst and k.resistance_max is None and k.resistance == 0:
            continue                    # a short stays a short: nothing to warn about
        k.resistance, _ = _pick(worst, f"DC connection {k.name}", k.resistance,
                                k.resistance_max, info)
        k.resistance_max = None
    return out, info
