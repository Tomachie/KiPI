"""Embedded 3D viewport (pyvistaqt QtInteractor) for board copper and, later, results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
from pyvistaqt import QtInteractor
from qtpy.QtWidgets import QVBoxLayout, QWidget

from kipi.viz.meshes import NetScene

_NET_COLORS = [(0.90, 0.35, 0.25), (0.25, 0.60, 0.90), (0.95, 0.75, 0.20), (0.40, 0.80, 0.45),
               (0.80, 0.45, 0.85), (0.30, 0.80, 0.80), (0.95, 0.55, 0.30), (0.65, 0.65, 0.65)]
_VIA_COLOR = (0.85, 0.85, 0.80)
_PAD_COLOR = (1.0, 0.85, 0.35)
_BOARD_COLOR = (0.15, 0.40, 0.20)


@dataclass
class _NetActors:
    color_index: int
    copper: List[Tuple[int, object, Tuple[float, float, float]]] = field(default_factory=list)
    pads: Dict[int, object] = field(default_factory=dict)   # layer id -> actor
    vias: Optional[object] = None

    def all(self):
        return ([a for _, a, _ in self.copper] + list(self.pads.values()) +
                ([self.vias] if self.vias is not None else []))


class Viewer3D(QWidget):
    """3D view of the board body plus individually added nets, with visibility controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self)
        layout.addWidget(self.plotter.interactor)
        self.plotter.set_background("#1e1f24", top="#3a3d46")
        self.plotter.enable_anti_aliasing("ssaa")
        self.plotter.add_axes()

        self._net_actors: Dict[str, _NetActors] = {}
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
        p.clear_actors()
        self._net_actors.clear()
        self._board_actors.clear()
        self._hidden_layers.clear()
        self._next_color = 0
        if body is not None:
            self._board_actors.append(p.add_mesh(body, color=_BOARD_COLOR, opacity=0.18,
                                                 name="board", pickable=False,
                                                 reset_camera=False))
        if outline is not None:
            self._board_actors.append(p.add_mesh(outline, color="#9fd39f", line_width=1.5,
                                                 name="outline", pickable=False,
                                                 reset_camera=False))
        p.set_scale(zscale=self._zscale)
        self._apply_visibility()
        self.view_iso()

    def nets(self) -> List[str]:
        return list(self._net_actors)

    def add_net(self, ns: NetScene):
        """Add (or replace) one net's meshes without touching the others."""
        p = self.plotter
        old = self._net_actors.get(ns.net)
        if old is not None:
            self._remove_actors(old)
            color_index = old.color_index
        else:
            color_index = self._next_color
            self._next_color += 1
        na = _NetActors(color_index=color_index)
        for lm in ns.copper:
            actor = p.add_mesh(lm.mesh, color=self._copper_color(na, lm.color),
                               smooth_shading=False, specular=0.4, specular_power=20,
                               name=f"cu:{ns.net}:{lm.layer_id}", reset_camera=False)
            na.copper.append((lm.layer_id, actor, lm.color))
        for lid, m in ns.pads.items():
            na.pads[lid] = p.add_mesh(m, color=_PAD_COLOR, opacity=0.9,
                                      name=f"pads:{ns.net}:{lid}", reset_camera=False)
        if ns.vias is not None:
            na.vias = p.add_mesh(ns.vias, color=_VIA_COLOR, specular=0.6,
                                 name=f"vias:{ns.net}", reset_camera=False)
        for a in na.all():
            a.SetScale(1.0, 1.0, self._zscale)
        self._net_actors[ns.net] = na
        self._apply_visibility()

    def remove_net(self, net: str):
        na = self._net_actors.pop(net, None)
        if na is not None:
            self._remove_actors(na)
            self.plotter.render()

    def _remove_actors(self, na: _NetActors):
        for a in na.all():
            self.plotter.remove_actor(a, render=False)

    def clear(self):
        self.set_board(None, None)

    # ------------------------------------------------------------ appearance
    def _copper_color(self, na: _NetActors, layer_color):
        if self._color_mode == "net":
            return _NET_COLORS[na.color_index % len(_NET_COLORS)]
        return layer_color

    def set_color_mode(self, mode: str):
        self._color_mode = mode
        for na in self._net_actors.values():
            for _, actor, layer_color in na.copper:
                actor.prop.color = self._copper_color(na, layer_color)
        self.plotter.render()

    def set_layer_visible(self, layer_id: int, visible: bool):
        (self._hidden_layers.discard if visible else self._hidden_layers.add)(layer_id)
        self._apply_visibility()

    def set_feature_visible(self, feature: str, visible: bool):
        setattr(self, f"_show_{feature}", visible)
        self._apply_visibility()

    def _apply_visibility(self):
        for na in self._net_actors.values():
            for lid, actor, _ in na.copper:
                actor.SetVisibility(lid not in self._hidden_layers)
            for lid, actor in na.pads.items():
                actor.SetVisibility(self._show_pads and lid not in self._hidden_layers)
            if na.vias is not None:
                na.vias.SetVisibility(self._show_vias)
        for a in self._board_actors:
            a.SetVisibility(self._show_board)
        self.plotter.render()

    def set_zscale(self, s: float):
        self._zscale = max(1.0, float(s))
        self.plotter.set_scale(zscale=self._zscale)
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
