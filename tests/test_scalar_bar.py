"""The results scale bar keeps its tick values left of the bar and its title clear above."""

from types import SimpleNamespace as NS

import pytest

pv = pytest.importorskip("pyvista")
pytest.importorskip("pyvistaqt")

from kipi.gui.viewer3d import Viewer3D  # noqa: E402
from kipi.pi.tree import solve_tree  # noqa: E402
from kipi.viz.results import FIELDS, build_result_scene  # noqa: E402
from test_multiphase import board, two_phase  # noqa: E402


@pytest.mark.parametrize("field", ["voltage", "current_density", "power_density",
                                   "via_current"])
def test_scale_bar_layout(field):
    snap = board()
    tree = solve_tree(snap, two_phase())
    scene = build_result_scene(snap, next(iter(tree.groups.values())).result)
    if scene.vias is None:                   # the test board has none: stand one in
        scene.vias = pv.Cylinder()
        scene.vias.cell_data["I_mA"] = range(scene.vias.n_cells)
        scene.ranges["via_current"] = (0.0, float(scene.vias.n_cells))
    # Just the drawing state Viewer3D needs, on an off-screen plotter (no Qt window).
    v = NS(plotter=pv.Plotter(off_screen=True), _result=scene, _result_field=field,
           _result_actors={}, _result_via_actor=None, _scalar_bars=[], _zscale=1.0)
    v._style_bar = lambda title: Viewer3D._style_bar(v, title)
    _, assoc, label, unit = FIELDS[field]
    title = f"{label} ({unit})"
    if assoc == "via":
        Viewer3D._draw_via_result(v, scene, FIELDS[field][0], title)
    else:
        Viewer3D._draw_copper_result(v, scene, FIELDS[field][0], assoc, title)
    bar = v.plotter.scalar_bars[title]
    assert bar.GetTextPosition() == bar.PrecedeScalarBar
    assert bar.GetVerticalTitleSeparation() > 0
    assert v._scalar_bars == [title]
    v.plotter.close()
