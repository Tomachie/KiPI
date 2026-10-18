"""Positions and drawn items in a ``.kicad_pcb`` file.

Footprint contents are stored in the footprint's own frame (unrotated, relative to its
anchor); pads give their position in that frame but their angle on the board. A
:class:`Frame` places such coordinates on the board. KiCad works in whole nanometers,
so placed points are rounded to them, as KiCad rounds its own.
"""

from __future__ import annotations

import math
from typing import List, Optional

from kipi.core.geometry.arcs import arc_points
from kipi.core.geometry.shapes import SHAPE_QUAD_SEGS, area, bezier_points, stroke
from kipi.sources.kicad_pcb.sexpr import Node, child, number, value, xy

NM = 6        # decimals of a millimeter in a nanometer


class Frame:
    """A position and angle on the board. Angles are degrees counter-clockwise as KiCad
    shows them, with y pointing down."""

    def __init__(self, origin=(0.0, 0.0), angle_deg: float = 0.0):
        self.origin = origin
        self.angle_deg = angle_deg
        a = math.radians(angle_deg)
        self._c, self._s = math.cos(a), math.sin(a)

    def __call__(self, p) -> tuple:
        x, y = p
        return (round(self.origin[0] + x * self._c + y * self._s, NM),
                round(self.origin[1] - x * self._s + y * self._c, NM))

    def points(self, pts) -> List[tuple]:
        return [self(p) for p in pts]


BOARD = Frame()


def at(node: Node):
    """``(at x y [angle])`` -> ((x, y), angle)."""
    a = child(node, "at")
    if a is None:
        return (0.0, 0.0), 0.0
    angle = float(a[3]) if len(a) > 3 and a[3] not in ("unlocked", "locked") else 0.0
    return xy(a), angle


def uuid(node: Node) -> str:
    """The item's id: KiCad 7 calls it ``tstamp`` in places, later versions ``uuid``."""
    return value(node, "uuid") or value(node, "tstamp") or ""


def stroke_width(node: Node) -> float:
    s = child(node, "stroke")
    if s is not None:
        return number(s, "width")
    return number(node, "width")


def is_filled(node: Node) -> bool:
    f = child(node, "fill")
    if f is None or len(f) < 2:
        return False
    return f[1] in ("yes", "solid", "true")


def polyline(pts_node: Optional[Node], frame: Frame = BOARD) -> List[tuple]:
    """The points of a ``(pts (xy ..) (arc (start ..) (mid ..) (end ..)) ..)`` list, with
    arcs made into points, placed by ``frame``."""
    out: List[tuple] = []
    if pts_node is None:
        return out
    for c in pts_node[1:]:
        if type(c) is not list or not c:
            continue
        if c[0] == "xy":
            out.append(frame(xy(c)))
        elif c[0] == "arc":
            s, m, e = (frame(xy(child(c, k))) for k in ("start", "mid", "end"))
            if not out or out[-1] != s:
                out.append(s)
            out.extend(arc_points(s, m, e))
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def kind(node: Node) -> str:
    """``gr_line``/``fp_line`` -> ``line``."""
    head = node[0]
    return head[3:] if head[:3] in ("gr_", "fp_") else head


def _ends(node: Node, frame: Frame):
    return frame(xy(child(node, "start"))), frame(xy(child(node, "end")))


def _rect(node: Node, frame: Frame):
    (x1, y1), (x2, y2) = xy(child(node, "start")), xy(child(node, "end"))
    return frame.points([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])


def path_points(node: Node, frame: Frame = BOARD) -> Optional[List[tuple]]:
    """The points along an open drawn path (line, arc, bezier), else None."""
    k = kind(node)
    if k == "line":
        return list(_ends(node, frame))
    if k == "arc":
        if child(node, "mid") is None:
            return None
        s, m, e = (frame(xy(child(node, n))) for n in ("start", "mid", "end"))
        return [s] + arc_points(s, m, e)
    if k in ("curve", "bezier"):
        pts = polyline(child(node, "pts"), frame)
        if len(pts) == 4:
            return bezier_points(*pts)
    return None


def closed_shape(node: Node, frame: Frame = BOARD, quad_segs: int = SHAPE_QUAD_SEGS):
    """The area a closed drawn shape (circle, rectangle, polygon) encloses, as a shapely
    polygon, else None."""
    from shapely.geometry import Point as SPoint, Polygon as SPolygon
    k = kind(node)
    if k == "circle":
        c = frame(xy(child(node, "center")))
        e = frame(xy(child(node, "end")))
        return SPoint(c).buffer(math.dist(c, e), quad_segs=quad_segs)
    if k == "rect":
        return SPolygon(_rect(node, frame))
    if k == "poly":
        pts = polyline(child(node, "pts"), frame)
        return SPolygon(pts) if len(pts) >= 3 else None
    return None


def drawn_copper(node: Node, frame: Frame = BOARD):
    """The copper of a drawn shape on a copper layer (shapely), or None if it has none:
    open paths are stroked, closed shapes are their stroke plus, if filled, their inside."""
    width = stroke_width(node)
    pts = path_points(node, frame)
    if pts is not None:
        return stroke(pts, width)
    shape = closed_shape(node, frame)
    if shape is None or shape.is_empty:
        return None
    if not shape.is_valid:
        shape = shape.buffer(0)
    return area(shape, width, is_filled(node))


def drawn_items(node: Node, prefix: str):
    """The drawn items directly in ``node`` (``gr_*`` on the board, ``fp_*`` in a
    footprint)."""
    for c in node:
        if type(c) is list and c and isinstance(c[0], str) and c[0].startswith(prefix) \
                and c[0] not in ("gr_text", "fp_text", "gr_text_box", "fp_text_box"):
            yield c

