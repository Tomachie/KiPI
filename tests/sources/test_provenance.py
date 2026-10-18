"""A board says where it came from and what KiPI had to fill in, and keeps both when
saved as a snapshot."""

import os
import time

import pytest

from kipi.core.model import snapshot_io
from kipi.core.model.stackup import DEFAULT_STACKUP_NOTE, build_stackup, default_entries
from synthetic import make_board


def test_a_complete_stackup_assumes_nothing():
    assumed = []
    st = build_stackup(default_entries([3, 34]), [3, 34], {3: "F.Cu", 34: "B.Cu"},
                       assumed=assumed)
    assert st.from_board and assumed == []


def test_an_incomplete_stackup_says_it_is_a_default():
    assumed = []
    st = build_stackup([("cu", 3, 0.035, "", 0.0)], [3, 34], {}, assumed=assumed)
    assert not st.from_board and assumed == [DEFAULT_STACKUP_NOTE]
    assert len(st.copper) == 2


def test_snapshots_keep_the_format_and_placeholders(tmp_path):
    snap = make_board()
    snap.source_format = "kicad-ipc"
    snap.assumed = ["Stackup: made up"]
    snap.file = "/somewhere/else.kipi.json.gz"
    d = snapshot_io.to_dict(snap)
    assert "file" not in d                               # never saved
    back = snapshot_io.from_dict(d)
    assert back.source_format == "kicad-ipc" and back.assumed == ["Stackup: made up"]
    assert back.file == ""
    path = snapshot_io.save(snap, tmp_path / "b.kipi.json.gz")
    assert snapshot_io.load(path).file == str(path)


def test_older_snapshots_came_from_kicad():
    d = snapshot_io.to_dict(make_board())
    for key in ("source_format", "assumed"):
        d.pop(key)
    d["source"] = "kicad"
    old = snapshot_io.from_dict(d)
    assert old.source_format == "kicad-ipc" and old.assumed == []
    assert old.format_name == "KiCad PCB editor"
    d["source"] = "examples/multiphase_buck"            # made by a script: it says so
    assert snapshot_io.from_dict(d).format_name == "examples/multiphase_buck"
    d["source"] = ""
    assert snapshot_io.from_dict(d).format_name == "unknown source"


def test_kicad_boards_say_so_and_what_an_old_kicad_could_not_give():
    pytest.importorskip("kipy")
    from kicad_fake import FakeKiCad
    from kipi.sources.kicad_ipc.extract import extract_board
    new = extract_board(FakeKiCad("10.0.6").board())
    assert new.source_format == "kicad-ipc" and new.assumed == []
    old = extract_board(FakeKiCad("9.0.0").board())       # no padstack presence query
    assert any(a.startswith("Pad copper:") for a in old.assumed)


def test_the_summary_shows_where_from_and_the_placeholders(no_3d, tmp_path):
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    snap = make_board()
    snap.project_dir = str(tmp_path)
    snap.source_format = "kicad-ipc"
    snap.kicad_version = "10.0.7"
    snap.assumed = ["Stackup: made up for this test"]
    snap.file = str(tmp_path / "b.kipi.json.gz")
    w = MainWindow()
    w._set_snapshot(snap)
    t0 = time.time()
    while w._busy() and time.time() - t0 < 30:
        app.processEvents()
    text = w.info.toPlainText()
    assert "From KiCad PCB editor 10.0.7, saved as" in text
    assert "Placeholders" in text and "Stackup: made up for this test" in text
    w.close()
