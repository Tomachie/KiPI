"""Embedded 3D viewport (pyvistaqt QtInteractor) for board copper and, later, results."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
from pyvistaqt import QtInteractor
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
from qtpy.QtWidgets import QVBoxLayout, QWidget

from kipi.viz.batch import Chunk, MeshBatcher
from kipi.viz.meshes import NetScene

_NET_COLORS = [(0.90, 0.35, 0.25), (0.25, 0.60, 0.90), (0.95, 0.75, 0.20), (0.40, 0.80, 0.45),
               (0.80, 0.45, 0.85), (0.30, 0.80, 0.80), (0.95, 0.55, 0.30), (0.65, 0.65, 0.65)]
_VIA_COLOR = (0.85, 0.85, 0.80)
_PAD_COLOR = (1.0, 0.85, 0.35)
_BOARD_COLOR = (0.15, 0.40, 0.20)


# Surface properties per mesh category: (opacity, specular)
_CATEGORY_LOOK = {"cu": (1.0, 0.4), "pad": (0.9, 0.0), "via": (1.0, 0.6)}


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
        self.plotter.enable_surface_point_picking(
            callback=self._on_pick, show_message=False, left_clicking=True,
            show_point=True, point_size=12, color="white", clear_on_no_selection=True)

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
        for chunk, actor in self._chunk_actors.items():
            self._apply_actor_visibility(chunk.category, actor)
        for a in self._board_actors:
            a.SetVisibility(self._show_board)
        self.plotter.render()

    def set_zscale(self, s: float):
        self._zscale = max(1.0, float(s))
        for a in self._board_actors:
            a.SetScale(1.0, 1.0, self._zscale)
        for a in self._chunk_actors.values():
            a.SetScale(1.0, 1.0, self._zscale)
        self.plotter.reset_camera_clipping_range()
        self.plotter.render()

    @property
    def zscale(self) -> float:
        return self._zscale

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
    def on_pick(self, cb: Callable[[Tuple[float, float, float]], None]):
        """Register ``cb(x_mm, y_mm_kicad, z_mm)`` for left-click picks."""
        self._pick_cb = cb

    def _on_pick(self, point):
        if self._pick_cb is None or point is None:
            return
        x, y, z = np.asarray(point, dtype=float)
        self._pick_cb((x, -y, z / self._zscale))

    def close(self):
        self.plotter.close()
        super().close()
