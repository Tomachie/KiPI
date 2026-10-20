# Copyright (c) 2026 Tomachie LLC.
"""A series diode solved on its datasheet's forward-voltage curve.

The solver takes a diode as a forward drop plus a slope resistance, V = forward_v +
I x R, a straight line. A Schottky's curve is far from straight over its range: a line
fitted from 2 A to 20 A reads about 0.16 V high at 0.5 A. A diode that carries its
curve (:attr:`kipi.dc.pi.model.DcLink.vf_curve`) keeps its slope R, so the system is
factorized once, and has its forward_v moved each pass so the line meets the curve at
the current it carries: forward_v = curve(I) - R x I. The loads are fixed currents, so
the diode's current hardly depends on its own drop and this settles in a pass or two.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from kipi.dc.solver.setup import DIODE_MIN_OHMS


def curve_volts(points: Sequence[Sequence[float]], amps: float) -> float:
    """The curve's forward voltage at ``amps``: straight lines in log current between
    its points (a diode's voltage grows with the log of its current); beyond its ends,
    the line through its last two points the same way."""
    pts = sorted((float(i), float(v)) for i, v in points if float(i) > 0)
    if len(pts) == 1:
        return pts[0][1]
    x = math.log(max(amps, 1e-12))
    k = next((n for n in range(1, len(pts)) if pts[n][0] >= amps), len(pts) - 1)
    (i1, v1), (i2, v2) = pts[k - 1], pts[k]
    x1, x2 = math.log(i1), math.log(i2)
    return max(v1 + (v2 - v1) * (x - x1) / (x2 - x1), 0.0)


class DiodeCurves:
    """The forward voltages of the diodes with a curve in one net group, per pass."""

    def __init__(self, links: List):
        self.links = [k for k in links if k.diode_vf and k.vf_curve]
        self.forward_v: Dict[str, float] = {k.name: k.diode_vf for k in self.links}

    def __bool__(self) -> bool:
        return bool(self.links)

    def update(self, series_current: Dict[str, float]) -> float:
        """Move each diode's forward_v to meet its curve at its solved current; the
        largest move, volts. A diode carrying no forward current keeps its own."""
        change = 0.0
        for k in self.links:
            amps: Optional[float] = series_current.get(k.name)
            if amps is None or not amps > 0:
                continue
            r = max(k.resistance, DIODE_MIN_OHMS)
            new = curve_volts(k.vf_curve, amps) - r * amps
            change = max(change, abs(new - self.forward_v[k.name]))
            self.forward_v[k.name] = new
        return change
