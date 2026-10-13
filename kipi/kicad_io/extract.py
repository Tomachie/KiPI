"""Read a live KiCad 10 board through the IPC API (kicad-python / kipy) into a
:class:`~kipi.model.board.BoardSnapshot`.

All kipy-specific code lives here so the rest of KiPI never touches protobufs.
"""

from __future__ import annotations

import math
import os
import time
from typing import Dict, Iterable, List, Optional

from kipi.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Footprint, Net, Pad, Poly, Stackup,
    Track, Via, Zone,
)
from kipi.progress import NULL, ProgressReporter

NM_PER_MM = 1_000_000.0
DEFAULT_BOARD_THICKNESS_MM = 1.6
DEFAULT_OUTER_CU_MM = 0.035
DEFAULT_INNER_CU_MM = 0.0175
ARC_SEGMENTS_PER_CIRCLE = 64


def _mm(v) -> float:
    return float(v) / NM_PER_MM


def _pt(vec) -> tuple:
    return (_mm(vec.x), _mm(vec.y))


class ExtractionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(timeout_ms: int = 5000, retries: int = 3, progress: ProgressReporter = NULL):
    """Connect to the running KiCad and return ``(kicad, board)``.

    When launched as a KiCad plugin, ``KICAD_API_SOCKET``/``KICAD_API_TOKEN`` are set
    and kipy picks them up automatically. Run standalone, kipy falls back to the
    default socket, which works when only one KiCad instance is open.
    """
    from kipy import KiCad
    from kipy.errors import ApiError

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            kicad = KiCad(timeout_ms=timeout_ms)
            version = kicad.get_version()
            progress.info(f"Connected to KiCad {version} "
                          f"(kicad-python built for {kicad.get_api_version()})")
            board = kicad.get_board()
            return kicad, board
        except ApiError as e:   # e.g. AS_BUSY or no board open
            last_err = e
        except Exception as e:  # transport errors (KiCad not running / API disabled)
            last_err = e
        progress.warn(f"Connection attempt {attempt} failed: {last_err}")
        time.sleep(0.5 * attempt)
    raise ExtractionError(
        "Could not connect to KiCad. Make sure the PCB editor is open with a board loaded "
        "and that Preferences > Plugins > 'Enable KiCad API' is checked.\n"
        f"Last error: {last_err}")


# ---------------------------------------------------------------------------
# Geometry conversion
# ---------------------------------------------------------------------------

def _arc_points(start, mid, end, include_start: bool = False) -> List[tuple]:
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


def _polyline_points(polyline) -> List[tuple]:
    pts: List[tuple] = []
    for node in polyline.nodes:
        if node.has_point:
            pts.append(_pt(node.point))
        elif node.has_arc:
            arc = node.arc
            s, m, e = _pt(arc.start), _pt(arc.mid), _pt(arc.end)
            if not pts or pts[-1] != s:
                pts.append(s)
            pts.extend(_arc_points(s, m, e))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _poly(pwh) -> Poly:
    return Poly(outline=_polyline_points(pwh.outline),
                holes=[_polyline_points(h) for h in pwh.holes])


# ---------------------------------------------------------------------------
# Stackup
# ---------------------------------------------------------------------------

def _copper_layer_ids(board) -> List[int]:
    from kipy.util.board_layer import is_copper_layer
    from kipy.proto.board.board_types_pb2 import BoardLayer
    ids = [l for l in board.get_enabled_layers() if is_copper_layer(l)]
    # Stack order: F.Cu, In1..In30, B.Cu. Enum values are already in that order.
    return sorted(ids, key=lambda l: (l == BoardLayer.BL_B_Cu, l))


def extract_stackup(board, progress: ProgressReporter = NULL) -> Stackup:
    from kipy.proto.board import board_pb2

    cu_ids = _copper_layer_ids(board)
    names = {l: board.get_layer_name(l) for l in cu_ids}

    entries = []  # (kind, layer_id, thickness_mm, material, epsilon_r), top to bottom
    try:
        for sl in board.get_stackup().layers:
            if not sl.enabled:
                continue
            if sl.type == board_pb2.BoardStackupLayerType.BSLT_COPPER and sl.layer in names:
                entries.append(("cu", sl.layer, _mm(sl.thickness), "", 0.0))
            elif sl.type == board_pb2.BoardStackupLayerType.BSLT_DIELECTRIC:
                diel = sl.dielectric
                subs = diel.layers if diel is not None else []
                t = sum(_mm(s.thickness) for s in subs) or _mm(sl.thickness)
                mat = subs[0].material_name if subs else sl.material_name
                er = subs[0].epsilon_r if subs else 0.0
                entries.append(("diel", -1, t, mat, er))
    except Exception as e:  # noqa: BLE001 - stackup is optional; fall back below
        progress.warn(f"Could not read board stackup ({e}); using defaults")
        entries = []

    from_board = sum(1 for e in entries if e[0] == "cu") == len(cu_ids) and len(cu_ids) > 0
    if not from_board:
        progress.warn("Stackup incomplete; synthesizing a default "
                      f"{DEFAULT_BOARD_THICKNESS_MM} mm stackup")
        entries = []
        n = len(cu_ids)
        cu_t = [DEFAULT_OUTER_CU_MM if i in (0, n - 1) else DEFAULT_INNER_CU_MM for i in range(n)]
        diel_t = (DEFAULT_BOARD_THICKNESS_MM - sum(cu_t)) / max(1, n - 1)
        for i, lid in enumerate(cu_ids):
            entries.append(("cu", lid, cu_t[i], "", 0.0))
            if i < n - 1:
                entries.append(("diel", -1, diel_t, "FR4", 4.5))

    total = sum(e[2] for e in entries)
    z = total
    copper: List[CopperLayer] = []
    dielectrics: List[DielectricLayer] = []
    last_cu = -1
    pending_diel: List[DielectricLayer] = []
    for kind, lid, t, mat, er in entries:
        z -= t
        if kind == "cu":
            copper.append(CopperLayer(id=lid, name=names.get(lid, str(lid)), index=len(copper),
                                      thickness_mm=t, z_bottom_mm=z))
            for d in pending_diel:
                d.below_layer = lid
            pending_diel = []
            last_cu = lid
        else:
            d = DielectricLayer(thickness_mm=t, material=mat, epsilon_r=er, above_layer=last_cu)
            dielectrics.append(d)
            pending_diel.append(d)

    progress.info(f"Stackup: {len(copper)} copper layers, {total:.3f} mm total" +
                  ("" if from_board else " (default)"))
    for c in copper:
        progress.info(f"  {c.name:<8} t={c.thickness_mm * 1000:.1f} um  z={c.z_mid_mm:.3f} mm")
    return Stackup(copper=copper, dielectrics=dielectrics, board_thickness_mm=total,
                   from_board=from_board)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

_VIA_TYPES = {1: "through", 2: "blind_buried", 3: "micro", 4: "blind", 5: "buried"}
_PAD_TYPES = {1: "pth", 2: "smd", 3: "edge", 4: "npth"}


def _netclass_map(board, nets) -> Dict[str, str]:
    try:
        classes = board.get_netclass_for_nets(nets)
        return {name: nc.name for name, nc in classes.items()}
    except Exception:  # noqa: BLE001 - net classes are a nicety
        return {}


def _field_text(field) -> str:
    try:
        return field.text.value
    except Exception:  # noqa: BLE001
        try:
            return field.text.as_text().value
        except Exception:  # noqa: BLE001
            return ""


def _pad_shapes(board, pads, layer_ids: Iterable[int], progress) -> Dict[str, Dict[int, List[Poly]]]:
    """Pad copper polygon per layer, keyed by pad id.

    Calls the GetPadShapeAsPolygon command directly: kipy's list helper drops pads
    with no shape, which misaligns its result with the input list.
    """
    from kipy.proto.board import board_commands_pb2

    out: Dict[str, Dict[int, List[Poly]]] = {}
    if not pads:
        return out
    presence = {}
    try:
        pres = board.check_padstack_presence_on_layers(pads, list(layer_ids))
        presence = {item.id.value: layers for item, layers in pres.items()}
    except Exception as e:  # noqa: BLE001 - older KiCad: assume present everywhere it has shape
        progress.warn(f"Padstack presence query failed ({e}); using raw pad shapes")

    for lid in layer_ids:
        cmd = board_commands_pb2.GetPadShapeAsPolygon()
        cmd.board.CopyFrom(board.document)
        cmd.layer = lid
        cmd.pads.extend([p.id for p in pads])
        resp = board.client.send(cmd, board_commands_pb2.PadShapeAsPolygonResponse)
        from kipy.geometry import PolygonWithHoles
        for pid, poly in zip(resp.pads, resp.polygons):
            if presence and not presence.get(pid.value, {}).get(lid, False):
                continue
            p = _poly(PolygonWithHoles(poly))
            if len(p.outline) >= 3:
                out.setdefault(pid.value, {}).setdefault(lid, []).append(p)
    return out


def _pad_angle(pad) -> Optional[float]:
    """The pad's orientation on the board in degrees. KiCad keeps the padstack's angle
    absolute (the footprint's rotation is included); the drill size is unrotated."""
    try:
        return float(pad.padstack.angle.degrees)
    except Exception:  # noqa: BLE001 - older API: slots fall back to the pad's shape
        return None


def _via_layers(via, stackup: Stackup) -> List[int]:
    drill = via.padstack.drill
    a, b = drill.start_layer, drill.end_layer
    span = stackup.layers_between(a, b)
    if span:
        return span
    return [c.id for c in stackup.copper]


def _outline(board, progress) -> List[Poly]:
    """Board outline polygons from Edge.Cuts graphics (best effort)."""
    from kipy.board_types import BoardArc, BoardCircle, BoardPolygon, BoardRectangle, BoardSegment
    from kipy.proto.board.board_types_pb2 import BoardLayer
    try:
        from shapely.geometry import LineString, Point as SPoint, Polygon as SPolygon
        from shapely.ops import polygonize, unary_union
    except ImportError:
        return []

    lines, closed = [], []
    for s in board.get_shapes():
        if s.layer != BoardLayer.BL_Edge_Cuts:
            continue
        if isinstance(s, BoardSegment):
            lines.append(LineString([_pt(s.start), _pt(s.end)]))
        elif isinstance(s, BoardArc):
            st, md, en = _pt(s.start), _pt(s.mid), _pt(s.end)
            lines.append(LineString([st] + _arc_points(st, md, en)))
        elif isinstance(s, BoardCircle):
            c = _pt(s.center)
            closed.append(SPoint(c).buffer(_mm(s.radius()), quad_segs=32))
        elif isinstance(s, BoardRectangle):
            (x1, y1), (x2, y2) = _pt(s.top_left), _pt(s.bottom_right)
            closed.append(SPolygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)]))
        elif isinstance(s, BoardPolygon):
            for pwh in s.polygons:
                p = _poly(pwh)
                closed.append(SPolygon(p.outline, p.holes))
    polys = list(polygonize(unary_union(lines))) if lines else []
    polys += closed
    if not polys:
        progress.warn("No closed board outline found on Edge.Cuts")
        return []
    # Even-odd: the largest polygon is the board, the others inside it are cutouts.
    polys.sort(key=lambda p: p.area, reverse=True)
    board_poly = polys[0]
    for p in polys[1:]:
        board_poly = board_poly.symmetric_difference(p)
    geoms = getattr(board_poly, "geoms", [board_poly])
    return [Poly(outline=list(g.exterior.coords)[:-1],
                 holes=[list(r.coords)[:-1] for r in g.interiors]) for g in geoms]


def _kicad_busy(board) -> bool:
    """True if the PCB editor cannot serve board requests right now.

    ``Ping`` is answered even while a zone fill runs, so it cannot tell when the fill
    is done. A cheap ``GetItems`` goes through the editor's busy check: it returns
    ``AS_BUSY`` while the fill runs, or times out if the fill blocks the event loop.
    """
    from kipy.errors import ApiError
    from kipy.proto.common.envelope_pb2 import ApiStatusCode
    from kipy.proto.common.types import KiCadObjectType

    try:
        board.get_items(KiCadObjectType.KOT_PCB_GROUP)
    except ApiError as e:
        if e.code == ApiStatusCode.AS_BUSY:
            return True
        raise
    except OSError:     # transport timeout (kipy raises ConnectionError)
        return True
    return False


def refill_and_wait(board, progress: ProgressReporter = NULL, timeout_s: float = 600.0,
                    grace_s: float = 3.0, poll_s: float = 0.25) -> None:
    """Refill all zones in KiCad and return once the fill has finished.

    KiCad only queues the refill when it acknowledges the request, so the editor is
    briefly idle before the fill starts. kipy's own ``refill_zones(block=True)`` polls
    with ``Ping``, which succeeds in that window (and during the fill), so it returned
    before the fill and the following reads hit ``AS_BUSY`` or timed out. Here we wait
    until we have seen the editor busy and then idle again, or, for a fill too quick
    to catch, until it has stayed idle for ``grace_s``.
    """
    board.refill_zones(block=False)
    t0 = time.perf_counter()
    seen_busy = False
    while True:
        progress.check_cancel()
        time.sleep(poll_s)
        elapsed = time.perf_counter() - t0
        if _kicad_busy(board):
            seen_busy = True
        elif seen_busy or elapsed >= grace_s:
            return
        if elapsed > timeout_s:
            raise ExtractionError(f"KiCad was still filling zones after {timeout_s:.0f} s")


def extract_board(board, refill_zones: bool = False,
                  progress: ProgressReporter = NULL) -> BoardSnapshot:
    """Pull everything KiPI needs from an open kipy ``Board``."""
    t0 = time.perf_counter()

    if refill_zones:
        with progress.timed("Refilling zones"):
            refill_and_wait(board, progress)

    with progress.timed("Reading stackup"):
        stackup = extract_stackup(board, progress)
    cu_ids = [c.id for c in stackup.copper]
    progress.check_cancel()

    with progress.timed("Reading nets"):
        knets = board.get_nets()
        nc = _netclass_map(board, knets)
        nets = [Net(code=n.code, name=n.name, netclass=nc.get(n.name, "")) for n in knets]
        progress.info(f"{len(nets)} nets")
    progress.check_cancel()

    with progress.timed("Reading tracks"):
        tracks: List[Track] = []
        from kipy.board_types import ArcTrack
        for t in board.get_tracks():
            if t.layer not in cu_ids:
                continue
            tracks.append(Track(
                net=t.net.name, layer=t.layer, width_mm=_mm(t.width),
                start=_pt(t.start), end=_pt(t.end),
                mid=_pt(t.mid) if isinstance(t, ArcTrack) else None))
        n_arcs = sum(1 for t in tracks if t.is_arc)
        progress.info(f"{len(tracks)} track segments ({n_arcs} arcs)")
    progress.check_cancel()

    with progress.timed("Reading vias"):
        vias = []
        for v in board.get_vias():
            try:
                dia = _mm(v.diameter)
            except ValueError:
                dia = _mm(v.drill_diameter) * 2
            vias.append(Via(id=v.id.value, net=v.net.name, position=_pt(v.position),
                            drill_mm=_mm(v.drill_diameter), diameter_mm=dia,
                            layers=_via_layers(v, stackup),
                            via_type=_VIA_TYPES.get(v.type, "through")))
        progress.info(f"{len(vias)} vias")
    progress.check_cancel()

    with progress.timed("Reading footprints and pads"):
        footprints: List[Footprint] = []
        kpads = []
        pad_ref: Dict[str, str] = {}
        for fp in board.get_footprints():
            ref = _field_text(fp.reference_field)
            fpads = list(fp.definition.pads)
            from kipy.proto.board.board_types_pb2 import BoardLayer
            footprints.append(Footprint(
                id=fp.id.value, reference=ref, value=_field_text(fp.value_field),
                position=_pt(fp.position),
                side="bottom" if fp.layer == BoardLayer.BL_B_Cu else "top",
                pad_ids=[p.id.value for p in fpads]))
            for p in fpads:
                pad_ref[p.id.value] = ref
            kpads.extend(fpads)
        progress.info(f"{len(footprints)} footprints, {len(kpads)} pads")
        progress.info("Querying pad copper shapes per layer...")
        shapes = _pad_shapes(board, kpads, cu_ids, progress)
        pads: List[Pad] = []
        for p in kpads:
            d = p.padstack.drill.diameter
            pads.append(Pad(
                id=p.id.value, net=p.net.name, footprint=pad_ref.get(p.id.value, ""),
                number=p.number, position=_pt(p.position),
                pad_type=_PAD_TYPES.get(p.pad_type, "unknown"),
                drill_mm=(_mm(d.x), _mm(d.y)),
                shapes=shapes.get(p.id.value, {}),
                angle_deg=_pad_angle(p)))
    progress.check_cancel()

    with progress.timed("Reading zone fills"):
        zones: List[Zone] = []
        unfilled = 0
        for z in board.get_zones():
            if z.is_rule_area():
                continue
            net = z.net.name if z.net is not None else ""
            filled = {lid: [_poly(p) for p in polys]
                      for lid, polys in z.filled_polygons.items() if lid in cu_ids}
            if not filled:
                unfilled += 1
            zones.append(Zone(id=z.id.value, net=net, name=z.name, priority=z.priority,
                              filled=filled))
        n_polys = sum(len(v) for z in zones for v in z.filled.values())
        progress.info(f"{len(zones)} zones, {n_polys} filled polygons")
        if unfilled:
            progress.warn(f"{unfilled} zones have no fill; refill zones (B) for accurate results")

    with progress.timed("Reading board outline"):
        outline = _outline(board, progress)

    project_dir = ""
    try:
        project_dir = board.document.project.path
    except Exception:  # noqa: BLE001
        pass

    snap = BoardSnapshot(
        board_name=board.name, project_dir=project_dir, stackup=stackup, nets=nets,
        tracks=tracks, vias=vias, pads=pads, footprints=footprints, zones=zones,
        outline=outline, source="kicad")
    progress.info(f"Extraction complete in {time.perf_counter() - t0:.2f} s")
    return snap


def default_project_dir(snapshot: BoardSnapshot) -> str:
    return snapshot.project_dir or os.getcwd()
