"""The setup panel must only change the setup when the user edits it."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.gui.pi_panel import PIPanel  # noqa: E402
from test_multiphase import board, two_phase  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_ticking_nets_does_not_edit_the_selected_items(app):
    setup = two_phase(ground=True, sense=True)
    before = setup.to_dict()
    panel = PIPanel()
    panel.set_board(board(), setup, "test")
    for row in (0, 1):                       # the source, then the 2-phase VRM on phase 2
        panel.vrm_list.lst.setCurrentRow(row)
        if row == 1:
            panel.vrm_list.editor.cmb_phase.setCurrentIndex(1)
        for nets in (["VOUT"], ["GND"], ["VIN", "VOUT", "GND"]):
            panel.set_nets(nets)
    panel.load_list.lst.setCurrentRow(0)
    panel.set_nets(["VIN"])
    assert setup.to_dict() == before


def test_vrm_current_limit_edits_and_shows_in_results(app):
    from kipi.pi.tree import solve_tree
    setup = two_phase()
    panel = PIPanel()
    panel.set_board(board(), setup, "test")
    panel.vrm_list.lst.setCurrentRow(1)
    ed = panel.vrm_list.editor
    assert ed.i_limit.value() == 0 and ed.i_limit.text() == "no limit"
    ed.i_limit.setValue(1.5)
    assert setup.vrms[1].current_limit_a == 1.5
    tree = solve_tree(board(), setup)
    panel.show_results(tree, next(iter(tree.groups)))
    t = panel.tbl_vrms
    row = {t.item(r, 0).text(): r for r in range(t.rowCount())}
    assert t.item(row["U1"], 8).text() == "1.5" and t.item(row["U1"], 9).text() == "FAIL"
    assert t.item(row["J1"], 9).text() == ""
    ed.i_limit.setValue(0)
    assert setup.vrms[1].current_limit_a is None
    panel.close()
