"""The results scale bar: by the view's right edge (#103), tick values left of the bar,
title clear above, and a color range over the whole result or the user's limits (#101)."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

pv = pytest.importorskip("pyvista")
pytest.importorskip("pyvistaqt")

from kipi.gui.viewer3d import Viewer3D, _copper_clim, apply_limits  # noqa: E402
from kipi.dc.pi.tree import solve_tree  # noqa: E402
from kipi.dc.viz.results import FIELDS, build_result_scene  # noqa: E402
from phase_boards import board, two_phase  # noqa: E402


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
    v = _view(scene, field)
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


def _scene_with_outlier_layer():
    """The test board's copper plus a copy on another layer sitting 1 V higher."""
    snap = board()
    tree = solve_tree(snap, two_phase())
    scene = build_result_scene(snap, next(iter(tree.groups.values())).result)
    lid, lr = next(iter(scene.layers.items()))
    mesh = lr.mesh.copy()
    mesh.point_data["V"] = mesh.point_data["V"] + 1.0
    scene.layers[99] = replace(lr, layer_id=99, mesh=mesh)
    return scene, lid


def _view(scene, field="voltage", limits=None):
    v = NS(plotter=pv.Plotter(off_screen=True), _result=scene, _result_field=field,
           _result_actors={}, _result_via_actor=None, _scalar_bars=[], _bar_titles=[],
           _zscale=1.0, _hidden_layers=set(), _layer_opacity=1.0, _color_limits=limits,
           _clim=None, _arrow_actors={})
    v._style_bar = lambda title: Viewer3D._style_bar(v, title)
    v._apply_result_opacity = lambda actor: Viewer3D._apply_result_opacity(v, actor)
    return v


def test_color_range_covers_every_layer_shown_or_not():
    # #101: the scale no longer follows the layers shown; the user sets limits instead.
    scene, lid = _scene_with_outlier_layer()
    v_lo, v_hi = (float(f(scene.layers[lid].mesh.point_data["V"])) for f in (min, max))
    lo, hi, _ = _copper_clim(scene, "voltage")
    assert (lo, hi) == pytest.approx((v_lo, v_hi + 1.0))
    v = _view(scene)
    Viewer3D._draw_copper_result(v, scene, "V", "point", "Voltage (V)")
    v._apply_visibility = lambda: None
    Viewer3D.set_layer_visible(v, 99, False)
    for actor in v._result_actors.values():
        assert actor.GetMapper().GetScalarRange() == pytest.approx((v_lo, v_hi + 1.0))
    v.plotter.close()


@pytest.mark.parametrize("field,arr,assoc", [("voltage", "V", "point"),
                                             ("current_density", "J", "cell")])
def test_typed_limits_color_the_result(field, arr, assoc):
    scene, _ = _scene_with_outlier_layer()
    v = _view(scene, field, limits=(0.5, 0.75))
    Viewer3D._draw_copper_result(v, scene, arr, assoc, "t")
    assert v._clim == (0.5, 0.75)
    for actor in v._result_actors.values():
        assert actor.GetMapper().GetScalarRange() == pytest.approx((0.5, 0.75))
    v.plotter.close()


def test_typed_limits_color_the_vias():
    scene, _ = _scene_with_outlier_layer()
    scene.vias = pv.Cylinder()
    scene.vias.cell_data["I_mA"] = range(scene.vias.n_cells)
    scene.ranges["via_current"] = (0.0, float(scene.vias.n_cells))
    v = _view(scene, "via_current", limits=(2.0, 5.0))
    Viewer3D._draw_via_result(v, scene, "I_mA", "Via current (mA)")
    assert v._result_via_actor.GetMapper().GetScalarRange() == pytest.approx((2.0, 5.0))
    v.plotter.close()


def test_limits_are_kept_sane():
    assert apply_limits(0.0, 1.0, False, None) == (0.0, 1.0)
    assert apply_limits(0.0, 1.0, False, (0.2, 0.4)) == (0.2, 0.4)
    lo, hi = apply_limits(0.0, 1.0, False, (0.4, 0.4))        # no span: still drawable
    assert hi > lo
    assert apply_limits(1e-3, 10.0, True, (0.0, 5.0)) == pytest.approx((5e-4, 5.0))


def test_bar_sits_by_the_right_edge_with_its_title_inside_the_view():
    scene, _ = _scene_with_outlier_layer()
    v = _view(scene, "current_density")
    Viewer3D._draw_copper_result(v, scene, "J", "cell", "Current density (A/mm²)")
    bar = v.plotter.scalar_bars["Current density (A/mm²)"]
    x, _ = bar.GetPosition()
    w, _ = bar.GetPosition2()
    assert 0.95 <= x + w < 1.0                      # against the edge, with a gap
    (title,) = v._bar_titles
    assert title.GetInput() == "Current density (A/mm²)"
    assert title.GetTextProperty().GetJustification() == 2       # right: ends in view
    assert title.GetPosition()[0] == pytest.approx(x + w)
    Viewer3D._clear_result_actors(v)
    assert not v._bar_titles and not v.plotter.renderer.HasViewProp(title)
    v.plotter.close()
