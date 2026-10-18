"""Pad shapes cut into polygons the way KiCad cuts them.

KiCad turns a pad into a polygon (``PAD::TransformShapeToPolygon``) with its corners and
ends cut into straight segments no further than the board's maximum error from the true
curve, every vertex on the curve. Board readers build pads with these, so a pad read from
a file matches the one the KiCad API returns, within rounding. Shapes are centered on the
origin, in whatever frame the caller places them.
"""

from __future__ import annotations

import math
from typing import List, Sequence

DEFAULT_MAX_ERROR_MM = 0.005
MIN_CIRCLE_SEGMENTS = 8
# Rounded corners are cut by Clipper's offset with at least this many segments per circle.
MIN_CORNER_CIRCLE_SEGMENTS = 16


def arc_segments(radius: float, max_error: float, angle_deg: float) -> int:
    """How many segments KiCad cuts an arc into (``GetArcToSegmentCount``)."""
    if radius <= 0:
        return 2
    rel = min(max_error / radius, 1.0)
    step = math.degrees(math.acos(1.0 - rel)) * 2.0
    step = min(360.0 / MIN_CIRCLE_SEGMENTS, step) if step > 0 else 360.0 / MIN_CIRCLE_SEGMENTS
    return max(2, int(round(abs(angle_deg) / step)))


def circle_segments(radius: float, max_error: float) -> int:
    """Segments in a pad's full circle: KiCad rounds the count up to a multiple of 8, so
    the polygon lines up at 45 degrees."""
    n = arc_segments(radius, max_error, 360.0)
    return max(MIN_CIRCLE_SEGMENTS, (n + 7) // 8 * 8)


def circle_points(cx: float, cy: float, r: float, max_error: float) -> List[tuple]:
    n = circle_segments(r, max_error)
    step = 2 * math.pi / n
    return [(cx + r * math.cos(step * (i + 0.5)), cy + r * math.sin(step * (i + 0.5)))
            for i in range(n)]


def corner_segments(radius: float, max_error: float) -> int:
    """Segments in a rounded corner. KiCad rounds a rectangle's corners by growing a
    smaller one with Clipper, which steps ``ceil(n / 4)`` times per quarter circle."""
    n = max(MIN_CORNER_CIRCLE_SEGMENTS, arc_segments(radius, max_error, 360.0))
    return max(1, math.ceil(n / 4.0 - 1e-9))


def _corner_arc(cx, cy, r, start_deg, max_error) -> List[tuple]:
    """A quarter circle from ``start_deg`` (math angle, y down) on, both ends included."""
    n = corner_segments(r, max_error)
    return [(cx + r * math.cos(math.radians(start_deg + 90.0 * i / n)),
             cy + r * math.sin(math.radians(start_deg + 90.0 * i / n))) for i in range(n + 1)]


def rounded_rect(w: float, h: float, radius: float, chamfer: float,
                 chamfered: Sequence[str], max_error: float) -> List[tuple]:
    """A ``w`` x ``h`` rectangle centered on the origin with corners rounded by
    ``radius`` and the ``chamfered`` ones cut by ``chamfer`` instead."""
    hw, hh = w / 2.0, h / 2.0
    radius = max(0.0, min(radius, hw, hh))
    chamfer = max(0.0, min(chamfer, hw, hh) if chamfered else 0.0)
    # Corners clockwise on screen (y down): top-left, top-right, bottom-right, bottom-left;
    # each with its x/y signs and the math angle its rounding starts at.
    corners = (("top_left", -1, -1, 180.0), ("top_right", 1, -1, 270.0),
               ("bottom_right", 1, 1, 0.0), ("bottom_left", -1, 1, 90.0))
    pts: List[tuple] = []
    for name, sx, sy, start in corners:
        x, y = sx * hw, sy * hh
        if chamfer > 0 and name in chamfered:
            # Coming from the previous corner, then leaving toward the next one.
            if sx == sy:       # top-left, bottom-right: arrive along y, leave along x
                pts += [(x, y - sy * chamfer), (x - sx * chamfer, y)]
            else:
                pts += [(x - sx * chamfer, y), (x, y - sy * chamfer)]
        elif radius > 0:
            pts += _corner_arc(x - sx * radius, y - sy * radius, radius, start, max_error)
        else:
            pts.append((x, y))
    return pts


def oval(w: float, h: float, max_error: float) -> List[tuple]:
    """A stadium: a ``min(w, h)`` wide segment along the long side with round ends. Each
    end runs from one side to the other through points half a step off its ends, as
    KiCad cuts it."""
    r = min(w, h) / 2.0
    half = abs(w - h) / 2.0
    if half == 0:
        return circle_points(0.0, 0.0, r, max_error)
    m = circle_segments(r, max_error) // 2
    step = 180.0 / m
    pts: List[tuple] = []
    for end, base in ((half, -90.0), (-half, 90.0)):      # along x: right end, left end
        angles = [base] + [base + step * (i + 0.5) for i in range(m)] + [base + 180.0]
        pts += [(end + r * math.cos(math.radians(a)), r * math.sin(math.radians(a)))
                for a in angles]
    if h > w:                                             # along y instead: turn it
        pts = [(-y, x) for x, y in pts]
    return pts


def trapezoid(w: float, h: float, dx: float, dy: float) -> List[tuple]:
    """KiCad's trapezoid: ``rect_delta`` widens one side and narrows the other."""
    hx, hy, ddx, ddy = w / 2.0, h / 2.0, dx / 2.0, dy / 2.0
    return [(-hx + ddy, -hy - ddx), (hx - ddy, -hy + ddx),
            (hx + ddy, hy - ddx), (-hx - ddy, hy + ddx)]
