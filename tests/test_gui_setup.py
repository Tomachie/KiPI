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
