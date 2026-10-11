"""Build PyVista meshes from board geometry. This module has no Qt dependency.

Copper is drawn as slabs at the true stackup Z. Vias are tubes. The viewer
applies any Z exaggeration as a plotter scale, so the meshes here stay in
real millimetres.

KiCad's y axis points down. The meshes flip it (y -> -y) so the board reads
correctly when viewed from +Z.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
import shapely
from shapely.geometry.base import BaseGeometry

from kipi.geometry.copper import drill_holes, iter_polygons, poly_to_shapely
from kipi.model.board import BoardSnapshot, CopperLayer

# KiCad-like default layer colours, top to bottom (outer layers fixed).
_TOP = (0.78, 0.20, 0.20)
_BOTTOM = (0.30, 0.45, 0.85)
_INNER = [(0.85, 0.75, 0.25), (0.35, 0.75, 0.40), (0.85, 0.45, 0.80), (0.30, 0.75, 0.80),
          (0.95, 0.55, 0.25), (0.60, 0.50, 0.90)]


def layer_color(layer: CopperLayer, n_layers: int) -> Tuple[float, float, float]:
    if layer.index == 0:
        return _TOP
    if layer.index == n_layers - 1:
        return _BOTTOM
    return _INNER[(layer.index - 1) % len(_INNER)]


def triangulate(geom: BaseGeometry) -> Tuple[np.ndarray, np.ndarray]:
    """Constrained Delaunay triangulation of a (multi)polygon.

    Returns ``(points[N,2], triangles[M,3])`` with shared vertices merged.
    """
    pts_list, tri_list, offset = [], [], 0
    for poly in iter_polygons(geom):
        tris = shapely.get_parts(shapely.constrained_delaunay_triangles(poly))
        if len(tris) == 0:
            continue
        coords = shapely.get_coordinates(tris).reshape(-1, 4, 2)[:, :3, :].reshape(-1, 2)
        uniq, inverse = np.unique(np.round(coords, 7), axis=0, return_inverse=True)
        pts_list.append(uniq)
        tri_list.append(inverse.reshape(-1, 3) + offset)
        offset += len(uniq)
    if not pts_list:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    return np.vstack(pts_list), np.vstack(tri_list)


def sheet(geom: BaseGeometry, z: float) -> Optional[pv.PolyData]:
    pts2, tris = triangulate(geom)
    if len(tris) == 0:
        return None
    pts = np.column_stack([pts2[:, 0], -pts2[:, 1], np.full(len(pts2), z)])
    faces = np.column_stack([np.full(len(tris), 3), tris]).ravel()
    return pv.PolyData(pts, faces)


def slab(geom: BaseGeometry, z_bottom: float, thickness: float) -> Optional[pv.PolyData]:
    """Copper slab: triangulated bottom face extruded up by ``thickness``."""
    base = sheet(geom, z_bottom)
    if base is None:
        return None
    if thickness <= 0:
        return base
    s = base.extrude((0, 0, thickness), capping=True).clean().triangulate()
    # Extrusion leaves one cap facing inward; fix winding so shading and volume are right.
    return s.compute_normals(cell_normals=True, point_normals=False, consistent_normals=True,
                             auto_orient_normals=True, split_vertices=False)


@dataclass
class LayerMesh:
    net: str
    layer_id: int
    mesh: pv.PolyData
    color: Tuple[float, float, float]
    area_mm2: float


@dataclass
class SceneData:
    """Everything the viewer needs for one display build."""
    copper: List[LayerMesh] = field(default_factory=list)
    vias: Optional[pv.PolyData] = None
    pads: Dict[int, pv.PolyData] = field(default_factory=dict)   # layer id -> pad tops
    board: Optional[pv.PolyData] = None
    outline_lines: Optional[pv.PolyData] = None


def via_tubes(snap: BoardSnapshot, nets: List[str], n_sides: int = 12) -> Optional[pv.PolyData]:
    """Via barrels as one tube mesh, colour-ready (``net_index`` cell data)."""
    vias = [v for v in snap.vias if v.net in nets]
    if not vias:
        return None
    st = snap.stackup
    blocks = []
    for v in vias:
        layers = [st.layer(l) for l in v.layers if st.layer(l) is not None]
        if not layers:
            continue
        z_top = max(l.z_top_mm for l in layers)
        z_bot = min(l.z_bottom_mm for l in layers)
        x, y = v.position
        cyl = pv.Cylinder(center=(x, -y, 0.5 * (z_top + z_bot)), direction=(0, 0, 1),
                          radius=max(v.drill_mm, 0.05) / 2.0, height=max(z_top - z_bot, 1e-3),
                          resolution=n_sides, capping=True)
        cyl.cell_data["net_index"] = np.full(cyl.n_cells, nets.index(v.net))
        blocks.append(cyl)
    if not blocks:
        return None
    return pv.merge(blocks)


def pad_tops(snap: BoardSnapshot, nets: List[str]) -> Dict[int, pv.PolyData]:
    """Pad shapes as sheets sitting just above their copper layer (for highlighting)."""
    st = snap.stackup
    out: Dict[int, pv.PolyData] = {}
    for cu in st.copper:
        polys = []
        for p in snap.pads:
            if p.net in nets:
                for poly in p.shapes.get(cu.id, []):
                    g = poly_to_shapely(poly)
                    if g is not None and not g.is_empty:
                        polys.append(g)
        if not polys:
            continue
        geom = shapely.union_all(polys)
        holes = [h for h in (drill_holes(snap, n, cu.id) for n in nets) if h is not None]
        if holes:
            geom = geom.difference(shapely.union_all(holes))
        lift = 0.002
        z = cu.z_top_mm + lift if cu.index == 0 else (
            cu.z_bottom_mm - lift if cu.index == len(st.copper) - 1 else cu.z_top_mm + lift)
        m = sheet(geom, z)
        if m is not None:
            out[cu.id] = m
    return out


def board_body(snap: BoardSnapshot) -> Tuple[Optional[pv.PolyData], Optional[pv.PolyData]]:
    """Translucent laminate body and its outline edges."""
    if not snap.outline:
        return None, None
    geoms = [poly_to_shapely(p) for p in snap.outline]
    geom = shapely.union_all([g for g in geoms if g is not None])
    t = snap.stackup.board_thickness_mm
    body = slab(geom, 0.0, t)
    lines = []
    for poly in iter_polygons(geom):
        for ring in [poly.exterior, *poly.interiors]:
            xy = np.asarray(ring.coords)
            for z in (0.0, t):
                pts = np.column_stack([xy[:, 0], -xy[:, 1], np.full(len(xy), z)])
                lines.append(pv.lines_from_points(pts))
    return body, (pv.merge(lines) if lines else None)


def build_scene(snap: BoardSnapshot, copper: Dict[str, Dict[int, BaseGeometry]],
                progress=None) -> SceneData:
    """Build every mesh for the given per-net copper (``net -> layer -> geometry``)."""
    st = snap.stackup
    n = len(st.copper)
    scene = SceneData()
    nets = list(copper.keys())
    for net, layers in copper.items():
        for lid, geom in layers.items():
            cu = st.layer(lid)
            if cu is None:
                continue
            m = slab(geom, cu.z_bottom_mm, cu.thickness_mm)
            if m is None:
                continue
            scene.copper.append(LayerMesh(net=net, layer_id=lid, mesh=m,
                                          color=layer_color(cu, n), area_mm2=geom.area))
            if progress is not None:
                progress.info(f"  mesh {net} / {cu.name}: {m.n_cells} cells")
                progress.check_cancel()
    scene.vias = via_tubes(snap, nets)
    scene.pads = pad_tops(snap, nets)
    scene.board, scene.outline_lines = board_body(snap)
    return scene
