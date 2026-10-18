"""Arcs given as start / mid / end points, as KiCad and most board formats store them."""

from __future__ import annotations

import math
from typing import List

ARC_SEGMENTS_PER_CIRCLE = 64


def arc_points(start, mid, end, include_start: bool = False) -> List[tuple]:
    """Discretize a start/mid/end arc (mm tuples) into points, excluding start."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return ([start] if include_start else []) + [end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1)
          + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3)
          + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def ccw_delta(a, b):
        return (b - a) % (2 * math.pi)

    # Sweep in whichever direction passes through the mid point.
    sweep = ccw_delta(a1, a3)
    if ccw_delta(a1, a2) > sweep:
        sweep -= 2 * math.pi
    n = max(2, int(math.ceil(abs(sweep) / (2 * math.pi) * ARC_SEGMENTS_PER_CIRCLE)))
    pts = [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n))
           for i in range(0 if include_start else 1, n)]
    pts.append(end)
    return pts
