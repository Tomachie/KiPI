"""KiCad API geometry (nanometer vectors, polylines with arcs) to KiPI millimeter tuples
and :class:`~kipi.core.model.board.Poly`. Shared by every reader of kipy items."""

from __future__ import annotations

from typing import List

from kipi.core.geometry.arcs import arc_points
from kipi.core.model.board import Poly

NM_PER_MM = 1_000_000.0


def mm(v) -> float:
    return float(v) / NM_PER_MM


def pt(vec) -> tuple:
    return (mm(vec.x), mm(vec.y))


def polyline_points(polyline) -> List[tuple]:
    pts: List[tuple] = []
    for node in polyline.nodes:
        if node.has_point:
            pts.append(pt(node.point))
        elif node.has_arc:
            arc = node.arc
            s, m, e = pt(arc.start), pt(arc.mid), pt(arc.end)
            if not pts or pts[-1] != s:
                pts.append(s)
            pts.extend(arc_points(s, m, e))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def poly(pwh) -> Poly:
    return Poly(outline=polyline_points(pwh.outline),
                holes=[polyline_points(h) for h in pwh.holes])
