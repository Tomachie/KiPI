"""Embedded 3D viewport (pyvistaqt QtInteractor) for board copper and, later, results."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
from pyvistaqt import QtInteractor
from qtpy.QtWidgets import QVBoxLayout, QWidget

from kipi.viz.meshes import SceneData

_NET_COLORS = [(0.90, 0.35, 0.25), (0.25, 0.60, 0.90), (0.95, 0.75, 0.20), (0.40, 0.80, 0.45),
               (0.80, 0.45, 0.85), (0.30, 0.80, 0.80), (0.95, 0.55, 0.30), (0.65, 0.65, 0.65)]
_VIA_COLOR = (0.85, 0.85, 0.80)
_PAD_COLOR = (1.0, 0.85, 0.35)
_BOARD_COLOR = (0.15, 0.40, 0.20)


class Viewer3D(QWidget):
    """3D view of a :class:`SceneData`, with layer, net and feature visibility controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self)
        layout.addWidget(self.plotter.interactor)
        self.plotter.set_background("#1e1f24", top="#3a3d46")
        self.plotter.enable_anti_aliasing("ssaa")
        self.plotter.add_axes()

        self._copper_actors: List[Tuple[str, int, object]] = []  # (net, layer, actor)
        self._pad_actors: Dict[int, object] = {}
        self._via_actor = None
        self._board_actors: List[object] = []
        self._hidden_layers: set = set()
        self._hidden_nets: set = set()
        self._show_pads = True
        self._show_vias = True
        self._show_board = True
        self._color_mode = "layer"
        self._nets: List[str] = []
        self._zscale = 1.0
        self._scene: Optional[SceneData] = None
        self._pick_cb: Optional[Callable[[Tuple[float, float, float]], None]] = None
        self.plotter.enable_surface_point_picking(
            callback=self._on_pick, show_message=False, left_clicking=True,
            show_point=True, point_size=12, color="white", clear_on_no_selection=True)

    # ------------------------------------------------------------------ scene
    def set_scene(self, scene: SceneData, reset_camera: bool = True):
        p = self.plotter
        p.clear_actors()
        self._copper_actors.clear()
        self._pad_actors.clear()
        self._board_actors.clear()
        self._via_actor = None
        self._scene = scene
        self._nets = list(dict.fromkeys(lm.net for lm in scene.copper))

        for lm in scene.copper:
            actor = p.add_mesh(lm.mesh, color=self._copper_color(lm.net, lm.color),
                               smooth_shading=False, specular=0.4, specular_power=20,
                               pbr=False, name=f"cu:{lm.net}:{lm.layer_id}", reset_camera=False)
            self._copper_actors.append((lm.net, lm.layer_id, actor))
        for lid, m in scene.pads.items():
            self._pad_actors[lid] = p.add_mesh(m, color=_PAD_COLOR, opacity=0.9,
                                               name=f"pads:{lid}", reset_camera=False)
        if scene.vias is not None:
            self._via_actor = p.add_mesh(scene.vias, color=_VIA_COLOR, specular=0.6,
                                         name="vias", reset_camera=False)
        if scene.board is not None:
            self._board_actors.append(p.add_mesh(scene.board, color=_BOARD_COLOR, opacity=0.18,
                                                 name="board", pickable=False,
                                                 reset_camera=False))
        if scene.outline_lines is not None:
            self._board_actors.append(p.add_mesh(scene.outline_lines, color="#9fd39f",
                                                 line_width=1.5, name="outline",
                                                 pickable=False, reset_camera=False))
        p.set_scale(zscale=self._zscale)
        self._apply_visibility()
        if reset_camera:
            self.view_iso()
        p.render()

    def clear(self):
        self.plotter.clear_actors()
        self._copper_actors.clear()
        self._pad_actors.clear()
        self._board_actors.clear()
        self._via_actor = None
        self.plotter.render()

    # ------------------------------------------------------------ appearance
    def _copper_color(self, net: str, layer_color):
        if self._color_mode == "net" and net in self._nets:
            return _NET_COLORS[self._nets.index(net) % len(_NET_COLORS)]
        return layer_color

    def set_color_mode(self, mode: str):
        self._color_mode = mode
        if self._scene is None:
            return
        colors = {(lm.net, lm.layer_id): lm.color for lm in self._scene.copper}
        for net, lid, actor in self._copper_actors:
            actor.prop.color = self._copper_color(net, colors[(net, lid)])
        self.plotter.render()

    def set_layer_visible(self, layer_id: int, visible: bool):
        (self._hidden_layers.discard if visible else self._hidden_layers.add)(layer_id)
        self._apply_visibility()

    def set_net_visible(self, net: str, visible: bool):
        (self._hidden_nets.discard if visible else self._hidden_nets.add)(net)
        self._apply_visibility()

    def set_feature_visible(self, feature: str, visible: bool):
        setattr(self, f"_show_{feature}", visible)
        self._apply_visibility()

    def _apply_visibility(self):
        for net, lid, actor in self._copper_actors:
            actor.SetVisibility(lid not in self._hidden_layers and net not in self._hidden_nets)
        for lid, actor in self._pad_actors.items():
            actor.SetVisibility(self._show_pads and lid not in self._hidden_layers)
        if self._via_actor is not None:
            self._via_actor.SetVisibility(self._show_vias)
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
