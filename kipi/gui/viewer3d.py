"""Embedded 3D viewport (pyvistaqt QtInteractor) for board copper and, later, results."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
from pyvistaqt import QtInteractor
from vtkmodules.vtkRenderingCore import vtkActor, vtkCellPicker, vtkPolyDataMapper
from qtpy.QtWidgets import QVBoxLayout, QWidget

from kipi.viz.batch import Chunk, MeshBatcher
from kipi.viz.results import FIELDS, ResultScene
from kipi.viz.meshes import NetScene

_NET_COLORS = [(0.90, 0.35, 0.25), (0.25, 0.60, 0.90), (0.95, 0.75, 0.20), (0.40, 0.80, 0.45),
               (0.80, 0.45, 0.85), (0.30, 0.80, 0.80), (0.95, 0.55, 0.30), (0.65, 0.65, 0.65)]
_VIA_COLOR = (0.85, 0.85, 0.80)
_PAD_COLOR = (1.0, 0.85, 0.35)
_BOARD_COLOR = (0.15, 0.40, 0.20)


# Surface properties per mesh category: (opacity, specular)
_CATEGORY_LOOK = {"cu": (1.0, 0.4), "pad": (0.9, 0.0), "via": (1.0, 0.6)}


def _bar_format(lo: float, hi: float, log_scale: bool) -> str:
    """Tick label format with enough decimals to tell the labels apart.

    A 3.3 V rail that sags by 0.4 mV needs five decimals, or every tick reads "3.3".
    """
    if log_scale:
        return "%.2g"
    span = abs(hi - lo)
    if span <= 0:
        return "%.4g"
    step_decimals = max(0, int(np.ceil(-np.log10(span / 5))) + 1)
    if max(abs(lo), abs(hi)) >= 1e4 or step_decimals > 9:
        return "%.4g"
    return f"%.{step_decimals}f"


def _chunk_actor(category) -> vtkActor:
    """A bare VTK actor coloured by the chunk's per-cell ``rgb`` array.

    Chunk actors bypass ``Plotter.add_mesh``: its name bookkeeping walks every actor in
    the renderer on each call, and pyvista's mapper wrapper builds colour lookup tables
    that direct-RGB meshes never use.
    """
    mapper = vtkPolyDataMapper()
    mapper.SetScalarModeToUseCellFieldData()
    mapper.SelectColorArray("rgb")
    mapper.SetColorModeToDirectScalars()
    mapper.ScalarVisibilityOn()
    actor = vtkActor()
    actor.SetMapper(mapper)
    opacity, specular = _CATEGORY_LOOK[category[0]]
    prop = actor.GetProperty()
    prop.SetOpacity(opacity)
    prop.SetSpecular(specular)
    prop.SetSpecularPower(20.0)
    prop.SetInterpolationToFlat()
    return actor


class Viewer3D(QWidget):
    """3D view of the board body plus individually added nets, with visibility controls.

    Nets are packed into a few merged meshes by :class:`MeshBatcher`; edits mark chunks
    dirty and :meth:`render` rebuilds only those before drawing.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self)
        layout.addWidget(self.plotter.interactor)
        self.plotter.set_background("#1e1f24", top="#3a3d46")
        # FXAA is a cheap post-process; SSAA re-renders the scene several times per frame.
        self.plotter.enable_anti_aliasing("fxaa")
        self.plotter.add_axes()

        self._batch = MeshBatcher()
        self._chunk_actors: Dict[Chunk, vtkActor] = {}
        self._net_color: Dict[str, int] = {}                  # net -> colour index
        self._layer_color: Dict[Tuple[str, int], tuple] = {}  # (net, layer) -> layer colour
        self._next_color = 0
        self._board_actors: List[object] = []
        self._hidden_layers: set = set()
        self._show_pads = True
        self._show_vias = True
        self._show_board = True
        self._color_mode = "layer"
        self._zscale = 1.0
        self._pick_cb: Optional[Callable[[Tuple[float, float, float]], None]] = None
        self._result: Optional[ResultScene] = None
        self._result_field = "voltage"
        self._result_actors: Dict[int, object] = {}      # layer id -> actor
        self._result_via_actor = None
        self._scalar_bars: List[str] = []
        # Clicks are handled here rather than with pyvista's point picking, so a click can
        # also select a via by its on-screen distance (vias are only a few pixels wide).
        self._picker = vtkCellPicker()
        self._picker.SetTolerance(0.0005)
        self._press_pos = None
        iren = self.plotter.iren
        iren.add_observer("LeftButtonPressEvent", self._on_left_press)
        iren.add_observer("LeftButtonReleaseEvent", self._on_left_release)

    # ------------------------------------------------------------------ scene
    def set_board(self, body: Optional[pv.PolyData], outline: Optional[pv.PolyData]):
        """Start a new board: drop every actor, draw the board body and frame the camera."""
        p = self.plotter
        renderer = p.renderer
        for a in self._chunk_actors.values():
            renderer.RemoveActor(a)
        self._chunk_actors.clear()
        self._batch = MeshBatcher()
        self._net_color.clear()
        self._layer_color.clear()
        self._clear_result_actors()
        self._result = None
        p.clear_actors()
        self._board_actors.clear()
        self._hidden_layers.clear()
        self._next_color = 0
        if body is not None:
            self._board_actors.append(p.add_mesh(body, color=_BOARD_COLOR, opacity=0.18,
                                                 name="board", pickable=False,
                                                 reset_camera=False, render=False))
        if outline is not None:
            self._board_actors.append(p.add_mesh(outline, color="#9fd39f", line_width=1.5,
                                                 name="outline", pickable=False,
                                                 reset_camera=False, render=False))
        for a in self._board_actors:
            a.SetScale(1.0, 1.0, self._zscale)
        self._apply_visibility()
        self.view_iso()

    def nets(self) -> List[str]:
        return list(self._net_color)

    def add_net(self, ns: NetScene, render: bool = True):
        """Add (or replace) one net's meshes without touching the others.

        Pass ``render=False`` when adding several nets and call :meth:`render` once.
        """
        if ns.net in self._net_color:
            self._batch.remove(ns.net)
        else:
            self._net_color[ns.net] = self._next_color
            self._next_color += 1
        for lm in ns.copper:
            self._layer_color[(ns.net, lm.layer_id)] = lm.color
            self._batch.add(ns.net, ("cu", lm.layer_id), lm.mesh,
                            self._copper_color(ns.net, lm.color))
        for lid, m in ns.pads.items():
            self._batch.add(ns.net, ("pad", lid), m, _PAD_COLOR)
        if ns.vias is not None:
            self._batch.add(ns.net, ("via", -1), ns.vias, _VIA_COLOR)
        if render:
            self.render()

    def remove_net(self, net: str, render: bool = True):
        if net not in self._net_color:
            return
        self._batch.remove(net)
        del self._net_color[net]
        for key in [k for k in self._layer_color if k[0] == net]:
            del self._layer_color[key]
        if render:
            self.render()

    def _flush(self):
        """Rebuild changed chunks and sync their actors."""
        renderer = self.plotter.renderer
        for chunk in self._batch.take_dirty():
            actor = self._chunk_actors.get(chunk)
            mesh = chunk.build()
            if mesh is None:
                if actor is not None:
                    renderer.RemoveActor(actor)
                    del self._chunk_actors[chunk]
                continue
            if actor is None:
                actor = _chunk_actor(chunk.category)
                actor.SetScale(1.0, 1.0, self._zscale)
                renderer.AddActor(actor)
                self._chunk_actors[chunk] = actor
                self._apply_actor_visibility(chunk.category, actor)
            actor.GetMapper().SetInputData(mesh)

    def render(self):
        self._flush()
        self.plotter.render()

    def clear(self):
        self.set_board(None, None)

    # ------------------------------------------------------------ appearance
    def _copper_color(self, net: str, layer_color):
        if self._color_mode == "net":
            return _NET_COLORS[self._net_color[net] % len(_NET_COLORS)]
        return layer_color

    def set_color_mode(self, mode: str):
        self._color_mode = mode
        for (net, lid), layer_color in self._layer_color.items():
            self._batch.set_color(net, ("cu", lid), self._copper_color(net, layer_color))
        self.render()

    def set_layer_visible(self, layer_id: int, visible: bool):
        (self._hidden_layers.discard if visible else self._hidden_layers.add)(layer_id)
        self._apply_visibility()

    def set_feature_visible(self, feature: str, visible: bool):
        setattr(self, f"_show_{feature}", visible)
        self._apply_visibility()

    def _apply_actor_visibility(self, category, actor):
        kind, lid = category
        if kind == "cu":
            visible = lid not in self._hidden_layers
        elif kind == "pad":
            visible = self._show_pads and lid not in self._hidden_layers
        else:
            visible = self._show_vias
        actor.SetVisibility(visible)

    def _apply_visibility(self):
        showing_result = self._result is not None
        for chunk, actor in self._chunk_actors.items():
            if showing_result:
                actor.SetVisibility(False)
            else:
                self._apply_actor_visibility(chunk.category, actor)
        for lid, actor in self._result_actors.items():
            actor.SetVisibility(lid not in self._hidden_layers)
        if self._result_via_actor is not None:
            # In the via-current view the vias are the result, so always show them.
            self._result_via_actor.SetVisibility(self._show_vias
                                                 or self._result_field == "via_current")
        for a in self._board_actors:
            a.SetVisibility(self._show_board)
        self.plotter.render()

    def set_zscale(self, s: float):
        self._zscale = max(1.0, float(s))
        for a in self._board_actors:
            a.SetScale(1.0, 1.0, self._zscale)
        for a in self._chunk_actors.values():
            a.SetScale(1.0, 1.0, self._zscale)
        for a in self._result_actors.values():
            a.SetScale(1.0, 1.0, self._zscale)
        if self._result_via_actor is not None:
            self._result_via_actor.SetScale(1.0, 1.0, self._zscale)
        self.plotter.reset_camera_clipping_range()
        self.plotter.render()

    @property
    def zscale(self) -> float:
        return self._zscale

    # ---------------------------------------------------------------- results
    @property
    def result(self) -> Optional[ResultScene]:
        return self._result

    def show_result(self, scene: ResultScene, field: Optional[str] = None):
        """Replace the copper view with solver results coloured by ``field``."""
        self._clear_result_actors()
        self._result = scene
        if field is not None:
            self._result_field = field
        self._draw_result()

    def set_result_field(self, field: str):
        self._result_field = field
        if self._result is not None:
            self._clear_result_actors()
            self._draw_result()

    def clear_result(self):
        self._clear_result_actors()
        self._result = None
        self._apply_visibility()

    def _clear_result_actors(self):
        p = self.plotter
        for a in self._result_actors.values():
            p.remove_actor(a, render=False)
        if self._result_via_actor is not None:
            p.remove_actor(self._result_via_actor, render=False)
        for title in self._scalar_bars:
            try:
                p.remove_scalar_bar(title, render=False)
            except (KeyError, ValueError, StopIteration):
                pass
        self._result_actors.clear()
        self._result_via_actor = None
        self._scalar_bars.clear()

    def _draw_result(self):
        scene = self._result
        arr, assoc, label, unit = FIELDS[self._result_field]
        if assoc == "via":
            self._draw_via_result(scene, arr, f"{label} ({unit})")
        else:
            self._draw_copper_result(scene, arr, assoc, f"{label} ({unit})")
        self._apply_visibility()

    def _draw_copper_result(self, scene, arr, assoc, title):
        """Copper coloured by a field; vias drawn plain for context."""
        p = self.plotter
        lo, hi = scene.ranges[self._result_field]
        if self._result_field in ("current_density", "power_density"):
            # Hot spots at pad corners dwarf everything else; clip the scale at the
            # 99.5th percentile (the probe still reports true values).
            vals = np.concatenate([
                lr.mesh.cell_data[arr][np.isfinite(lr.mesh.cell_data[arr])]
                for lr in scene.layers.values()])
            if len(vals):
                hi = float(np.percentile(vals, 99.5))
        if hi <= lo:
            hi = lo + 1e-12
        cmap = "turbo" if assoc == "point" else "inferno"
        log_scale = self._result_field == "power_density"
        if log_scale:
            lo = max(hi * 1e-4, 1e-30)      # four decades: loss is very concentrated
        bar = dict(title=title, vertical=True, position_x=0.88, position_y=0.25,
                   height=0.5, width=0.05, fmt=_bar_format(lo, hi, log_scale),
                   title_font_size=14,
                   label_font_size=12, n_labels=6)
        first = True
        for lid, lr in scene.layers.items():
            actor = p.add_mesh(lr.mesh, scalars=arr, preference=assoc, cmap=cmap,
                               clim=(lo, hi), log_scale=log_scale, nan_color="#555555",
                               smooth_shading=False,
                               show_scalar_bar=first, scalar_bar_args=bar,
                               name=f"result:{lid}", reset_camera=False, render=False)
            actor.SetScale(1.0, 1.0, self._zscale)
            self._result_actors[lid] = actor
            first = False
        self._scalar_bars.append(title)
        if scene.vias is not None:
            self._result_via_actor = p.add_mesh(
                scene.vias, color=_VIA_COLOR, specular=0.6, name="result:vias",
                reset_camera=False, render=False)
            self._result_via_actor.SetScale(1.0, 1.0, self._zscale)

    def _draw_via_result(self, scene, arr, title):
        """Vias coloured by current; copper drawn as muted grey for context."""
        p = self.plotter
        for lid, lr in scene.layers.items():
            actor = p.add_mesh(lr.mesh, color="#7a7f88", opacity=0.35, smooth_shading=False,
                               name=f"result:{lid}", reset_camera=False, render=False,
                               pickable=True)
            actor.SetScale(1.0, 1.0, self._zscale)
            self._result_actors[lid] = actor
        if scene.vias is None:
            return
        lo, hi = scene.ranges["via_current"]
        if hi <= lo:
            hi = lo + 1e-9
        self._result_via_actor = p.add_mesh(
            scene.vias, scalars=arr, cmap="viridis", clim=(lo, hi), specular=0.4,
            show_scalar_bar=True, name="result:vias", reset_camera=False, render=False,
            scalar_bar_args=dict(title=title, vertical=True, position_x=0.88, position_y=0.25,
                                 height=0.5, width=0.05, fmt=_bar_format(lo, hi, False),
                                 title_font_size=14, label_font_size=12, n_labels=6))
        self._result_via_actor.SetScale(1.0, 1.0, self._zscale)
        self._scalar_bars.append(title)

    # ------------------------------------------------------------------ views
    def _fit(self, zoom: float = 1.0):
        self.plotter.reset_camera()
        self.plotter.camera.zoom(zoom)
        self.plotter.render()

    def view_top(self):
        self.plotter.view_xy()
        self._fit(1.3)

    def view_bottom(self):
        self.plotter.view_xy(negative=True)
        self._fit(1.3)

    def view_iso(self):
        """Three-quarter view from the front-left, looking down at about 40 degrees."""
        self.plotter.view_vector((-0.45, -0.75, 0.75), viewup=(0, 0, 1))
        self._fit(1.4)

    def screenshot(self, path: str):
        self.plotter.screenshot(path, transparent_background=False)

    # ---------------------------------------------------------------- picking
    def on_click(self, cb):
        """Register ``cb(point, screen_xy)`` for left clicks (not drags).

        ``point`` is the clicked copper surface point as (x_mm, y_mm_kicad, z_mm), or None
        if the click hit no copper; ``screen_xy`` is the click in display pixels, for use
        with :meth:`screen_distance_to_segments`.
        """
        self._pick_cb = cb

    def _event_pos(self):
        return tuple(self.plotter.iren.interactor.GetEventPosition())

    def _on_left_press(self, *_):
        self._press_pos = self._event_pos()

    def _on_left_release(self, *_):
        start, self._press_pos = self._press_pos, None
        if start is None or self._pick_cb is None:
            return
        x, y = self._event_pos()
        if abs(x - start[0]) + abs(y - start[1]) > 4:     # a drag (rotate/pan), not a click
            return
        point = None
        if self._picker.Pick(x, y, 0, self.plotter.renderer) and self._picker.GetCellId() >= 0:
            wx, wy, wz = self._picker.GetPickPosition()
            point = (wx, -wy, wz / self._zscale)
            self._show_marker((wx, wy, wz))
        self._pick_cb(point, (x, y))

    def _show_marker(self, world_xyz):
        self.plotter.add_points(np.array([world_xyz]), color="white", point_size=12,
                                render_points_as_spheres=True, name="pick_marker",
                                pickable=False, reset_camera=False, render=False)
        self.plotter.render()

    def screen_distance_to_segments(self, segments: np.ndarray, screen_xy) -> np.ndarray:
        """Pixel distance from ``screen_xy`` to each 3D segment (N, 2, 3), KiCad coords."""
        if len(segments) == 0:
            return np.zeros(0)
        ren = self.plotter.renderer
        m = ren.GetActiveCamera().GetCompositeProjectionTransformMatrix(
            ren.GetTiledAspectRatio(), -1, 1)
        M = np.array([[m.GetElement(i, j) for j in range(4)] for i in range(4)])
        P = segments.reshape(-1, 3).astype(float).copy()
        P[:, 1] *= -1.0                       # KiCad y down -> view y up
        P[:, 2] *= self._zscale
        H = np.column_stack([P, np.ones(len(P))]) @ M.T
        behind = H[:, 3] <= 0
        ndc = H[:, :2] / np.where(behind, 1.0, H[:, 3])[:, None]
        w, h = ren.GetSize()
        ox, oy = ren.GetOrigin()
        xy = np.column_stack([(ndc[:, 0] + 1) / 2 * w + ox,
                              (ndc[:, 1] + 1) / 2 * h + oy]).reshape(-1, 2, 2)
        a, b = xy[:, 0], xy[:, 1]
        ab = b - a
        p = np.asarray(screen_xy, dtype=float)
        t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        d = np.linalg.norm(p - (a + t[:, None] * ab), axis=1)
        d[behind.reshape(-1, 2).any(axis=1)] = np.inf
        return d

    def close(self):
        self.plotter.close()
        super().close()
