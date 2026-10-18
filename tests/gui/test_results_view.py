"""The main window's result view: getting back from "Show copper", opening results with no
board, and identifying pads in a group that joins several nets."""

import os
import time
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvista")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Signal  # noqa: E402
from qtpy.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402
from shapely.geometry import box  # noqa: E402

from kipi.core.model.board import Net, Zone  # noqa: E402
from kipi.dc.pi.model import DcLink, Load, PinRef  # noqa: E402
from kipi.dc.pi.tree import solve_tree  # noqa: E402
from kipi.dc.viz.results import build_result_scene  # noqa: E402
from tree_boards import F, board, pad, poly, setup  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    """A MainWindow whose 3D view only records what it was asked to show."""
    from kipi.gui import main_window

    class ResultView(QWidget):
        zscale = 1.0
        models_shown = models_wanted = True
        result_shown = Signal(bool)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.result, self.field, self._nets = None, None, []
            self.plotter = NS(set_focus=lambda *a: None, render=lambda: None)

        def nets(self):
            return list(self._nets)

        def add_net(self, ns, render=True):
            self._nets.append(ns.net)

        def show_result(self, scene, field=None, limits=None):
            self.result, self.field, self.limits = scene, field or self.field, limits

        def set_result_field(self, field, limits=None):
            self.field, self.limits = field, limits

        def set_color_limits(self, limits):
            self.limits = limits

        def color_range(self):
            return (self.limits or (0.0, 1.0)) if self.result is not None else None

        def clear_result(self):
            self.result = None

        def __getattr__(self, name):          # every other view call is a no-op
            return lambda *a, **k: None

    monkeypatch.setattr(main_window, "Viewer3D", ResultView)
    w = main_window.MainWindow()
    yield w
    w.close()


def _bridged_board():
    """The tree test board plus +3V3B, joined to +3V3 by R1 (pads R1.1 / R1.2)."""
    snap = board()
    snap.nets.append(Net(4, "+3V3B"))
    snap.pads += [pad("r1_1", "+3V3", "R1", "1", box(20, 0, 21, 1)),
                  pad("r1_2", "+3V3B", "R1", "2", box(24, 0, 25, 1)),
                  pad("u3_1", "+3V3B", "U3", "1", box(29, 0, 30, 1))]
    snap.zones.append(Zone("d", "+3V3B", "", 0, {F: [poly(box(24, 0, 30, 1))]}))
    snap.invalidate_index()
    s = setup()
    s.links.append(DcLink("R1", PinRef("R1", "+3V3", ["1"]), PinRef("R1", "+3V3B", ["2"]),
                          0.01))
    s.loads.append(Load("U3", PinRef("U3", "+3V3B", ["1"]), 0.5))
    return snap, s


def _solved(app, w, snap, s):
    w._set_snapshot(snap)
    t0 = time.time()
    while w._busy() or w._sync_timer.isActive():
        app.processEvents()
        assert time.time() - t0 < 30, "window did not go idle"
    tree = solve_tree(snap, s)
    w._tree = tree
    w._scenes = {n: build_result_scene(snap, g.result) for n, g in tree.groups.items()}
    first = tree.loads[0].group
    w.solve_panel.show_results(tree, first)
    w._show_group(first)
    return tree, first


def test_results_come_back_after_show_copper(app, window):
    _, first = _solved(app, window, *_bridged_board())
    view, panel = window.viewer, window.solve_panel
    assert view.result is window._scenes[first]

    window._show_geometry()                              # "Show copper"
    assert view.result is None
    panel.cmb_group.activated.emit(panel.cmb_group.currentIndex())   # the same group again
    assert view.result is window._scenes[first]

    window._show_geometry()
    panel.cmb_field.setCurrentIndex(panel.cmb_field.findData("current_density"))
    assert view.result is window._scenes[first] and view.field == "current_density"

    window._show_geometry()
    window._show_via(first, "v1")                # a via-table row in the group selected
    assert view.result is window._scenes[first]


def test_opening_results_needs_a_board(app, window, monkeypatch):
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: told.append(a[2]))
    monkeypatch.setattr("qtpy.QtWidgets.QFileDialog.getOpenFileName",
                        lambda *a, **k: pytest.fail("asked for a file with no board"))
    window.open_results()
    assert told and "Load or open the board" in told[0]
    assert window._tree is None


def test_pads_are_identified_in_a_group_of_several_nets(app, window):
    snap, s = _bridged_board()
    tree, _ = _solved(app, window, snap, s)
    group = next(n for n, g in tree.groups.items() if "+3V3B" in g.nets)
    assert len(tree.groups[group].nets) == 2
    window.solve_panel.cmb_group.setCurrentText(group)
    assert window.viewer.result is window._scenes[group]
    z = snap.stackup.layer(F).z_mid_mm
    for x, label, net in ((24.5, "R1.2", "+3V3B"), (20.5, "R1.1", "+3V3")):
        window._on_pick((x, 0.5, z))
        text = window.info.toPlainText()
        assert f"Pad {label}" in text and f"Net: {net}" in text


def test_color_scale_limits_are_typed_kept_and_reset(app, window):
    # #101: the Results pane's scale fields set the 3D color range, per group and field.
    snap, s = _bridged_board()
    tree, first = _solved(app, window, snap, s)
    view, panel = window.viewer, window.solve_panel
    assert view.limits is None and panel.spin_scale_min.isEnabled()
    assert not panel.btn_scale_auto.isEnabled()
    panel.spin_scale_max.setValue(5.0)
    panel.spin_scale_min.setValue(2.0)
    assert view.limits == (2.0, 5.0) and panel.btn_scale_auto.isEnabled()
    assert panel.spin_scale_min.suffix() == " V"

    panel.cmb_field.setCurrentIndex(panel.cmb_field.findData("current_density"))
    assert view.limits is None                    # another field starts on its whole range
    assert panel.spin_scale_min.suffix() == " A/mm²"
    other = next(n for n in tree.groups if n != first)
    panel.cmb_group.setCurrentText(other)
    panel.cmb_field.setCurrentIndex(panel.cmb_field.findData("voltage"))
    assert view.limits is None                    # and so does another group
    panel.cmb_group.setCurrentText(first)
    assert view.limits == (2.0, 5.0)              # kept for the group and field set
    assert panel.spin_scale_min.value() == 2.0

    panel.spin_scale_min.setValue(6.0)            # above the maximum: not a range
    assert view.limits == (2.0, 5.0)
    panel.btn_scale_auto.click()
    assert view.limits is None and panel.color_limits(first, "voltage") is None
    assert not panel.btn_scale_auto.isEnabled()


def test_temperature_limits_are_shared_by_every_group(app, window):
    panel = window.solve_panel
    panel._scale_key = panel._limits_key("a", "temperature")
    panel.spin_scale_max.setValue(80.0)
    panel.spin_scale_min.setValue(25.0)
    assert panel.color_limits("b", "temperature") == (25.0, 80.0)
    assert panel.color_limits("b", "voltage") is None


def _idle(app, w):
    t0 = time.time()
    while w._busy():
        app.processEvents()
        assert time.time() - t0 < 30, "window did not go idle"
    app.processEvents()


def test_results_save_and_open_in_the_background(app, window, monkeypatch, tmp_path):
    from kipi.core import results_io
    tree, first = _solved(app, window, *_bridged_board())
    path = tmp_path / "r.kipi-dc-results"
    offered = []

    def save_as(parent, title, name, filt):
        offered.append((name, filt))
        return str(path), ""
    monkeypatch.setattr("qtpy.QtWidgets.QFileDialog.getSaveFileName", save_as)
    window._export("results")
    _idle(app, window)
    assert path.is_file() and not results_io.is_legacy(path)
    # The suggested name says what was solved: DC alone here.
    assert offered[0][0].endswith(".kipi-dc-results") and "DC results" in offered[0][1]

    window._tree, window._scenes = None, {}
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(str(a[2])))
    filters = []

    def open_file(parent, title, start, filt):
        filters.append(filt)
        return str(path), ""
    monkeypatch.setattr("qtpy.QtWidgets.QFileDialog.getOpenFileName", open_file)
    window.open_results()
    # Both kinds open, and so do results named as before 0.8.0.
    assert all(f"*{s}" in filters[0] for s in (".kipi-dc-results", ".kipi-dc-thermal-results",
                                               ".kipi-results"))
    _idle(app, window)
    assert set(window._scenes) == set(tree.groups)        # the 3D views are rebuilt
    assert window.viewer.result is window._scenes[first]
    assert [l.drop for l in window._tree.loads] == [l.drop for l in tree.loads]


def test_results_pickled_by_an_older_kipi_open_only_when_confirmed(app, window, monkeypatch,
                                                                   tmp_path):
    import gzip
    import pickle
    tree, first = _solved(app, window, *_bridged_board())
    path = tmp_path / "old.kipi-results"
    with gzip.open(path, "wb") as fh:
        pickle.dump({"format": 1, "kind": "dc", "saved": "2026-01-01 00:00:00",
                     "board": window.snapshot.board_name, "tree": tree,
                     "scenes": window._scenes, "setup": None}, fh)
    window._tree, window._scenes = None, {}
    monkeypatch.setattr("qtpy.QtWidgets.QFileDialog.getOpenFileName",
                        lambda *a, **k: (str(path), ""))
    asked = []

    def answer(button):
        def warning(parent, title, text, *a):
            asked.append(text)
            return button
        return warning
    monkeypatch.setattr(QMessageBox, "warning", answer(QMessageBox.Cancel))
    window.open_results()
    _idle(app, window)
    assert "pickle" in asked[0] and window._tree is None

    monkeypatch.setattr(QMessageBox, "warning", answer(QMessageBox.Open))
    window.open_results()
    _idle(app, window)
    assert len(asked) == 2 and set(window._scenes) == set(tree.groups)
    assert "save them again" in window.log_view.toPlainText()
