"""Results files (#99): JSON and NumPy arrays in a zip, which open on any machine and cannot
run code, and the safer reading of the pickles KiPI wrote before."""

import gzip
import io
import json
import math
import os
import pickle
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import List

import numpy as np
import pytest

from kipi import compat
from kipi.core import results_io
from kipi.core.model.board import CopperLayer, Stackup
from kipi.dc.solver.post import SeriesResult, TerminalResult
from kipi.dc.solver.setup import NetProblem, VoltageSource


def _round_trip(tmp_path, data):
    path = results_io.write(tmp_path / "r.kipi-results", {"format": 2}, data)
    return results_io.read(path)["data"]


def test_python_types_json_lacks_come_back(tmp_path):
    data = {"t": (1, 2.5), 3: "int key", "$x": 1, "c": 1 + 2j, "nan": math.nan,
            "inf": -math.inf, "p": Path("a") / "b.html", "s": {"a"}, "n": np.float64(0.5),
            "b": np.bool_(True), "l": [None, "x"]}
    back = _round_trip(tmp_path, data)
    assert back["t"] == (1, 2.5) and back[3] == "int key" and back["$x"] == 1
    assert back["c"] == 1 + 2j and math.isnan(back["nan"]) and back["inf"] == -math.inf
    assert back["p"] == Path("a") / "b.html" and back["s"] == {"a"}
    assert back["n"] == 0.5 and type(back["n"]) is float and back["b"] is True
    assert back["l"] == [None, "x"]


def test_arrays_and_dataclasses_come_back(tmp_path):
    a = np.arange(12, dtype=np.int32).reshape(3, 4)
    z = np.array([1 + 1j, np.nan])
    st = Stackup([CopperLayer(3, "F.Cu", 0, 0.035, 1.5), CopperLayer(34, "B.Cu", 1, 0.035, 0.0)],
                 [], 1.6)
    back = _round_trip(tmp_path, {"a": a, "z": z, "a2": a, "st": st, "st2": st})
    np.testing.assert_array_equal(back["a"], a)
    assert back["a"].dtype == np.int32 and back["a2"] is back["a"]       # stored once
    np.testing.assert_array_equal(back["z"], z)
    assert back["st"] == st and back["st"].layer(34).name == "B.Cu"
    assert back["st2"] is back["st"]                                      # shared stays shared


def test_live_functions_are_left_out(tmp_path):
    p = NetProblem("+3V3", [VoltageSource("U1", ["p1"], 3.3)], temperature=lambda xy: 25.0)
    back = _round_trip(tmp_path, p)
    assert back.temperature is None and back.sources == p.sources and back.nets == ["+3V3"]


def test_fields_added_later_get_their_defaults(tmp_path):
    path = results_io.write(tmp_path / "r", {}, TerminalResult("U1", "load", 1.0, [3.3], 3.3, 3.3))
    _rewrite(path, lambda doc: [doc["data"]["f"].pop(k) for k in ("pad_ids", "i_pads")])
    back = results_io.read(path)["data"]
    assert back.pad_ids == [] and back.i_pads == [] and back.name == "U1"
    # A field this KiPI no longer has is dropped.
    _rewrite(path, lambda doc: doc["data"]["f"].update(gone=1))
    assert not hasattr(results_io.read(path)["data"], "gone")
    # A class whose __setstate__ fills in what older files lack still gets to.
    path = results_io.write(tmp_path / "s", {}, SeriesResult("R1", 0.1, 1.0, 1.0, 0.9, 0.1))
    _rewrite(path, lambda doc: doc["data"]["f"].pop("pads_a"))
    assert results_io.read(path)["data"].pads_a == []


def test_the_file_is_a_zip_of_strict_json_and_plain_arrays(tmp_path):
    path = results_io.write(tmp_path / "r", {"kind": "dc"},
                            {"v": np.linspace(0, 1, 5), "x": math.nan})
    with zipfile.ZipFile(path) as z:
        assert set(z.namelist()) == {"results.json", "a/0.npy"}
        doc = json.loads(z.read("results.json"), parse_constant=pytest.fail)
        np.load(io.BytesIO(z.read("a/0.npy")), allow_pickle=False)
    assert doc["kind"] == "dc" and doc["data"]["x"] == {"$f": "nan"}
    assert not results_io.is_legacy(path)


@pytest.mark.parametrize("name", [
    "os.system",                                    # not KiPI at all
    "builtins.eval",
    "kipi.gui.main_window.MainWindow",              # KiPI, but not results
    "kipi.launcher.install.Installer",
    "kipi.core.progress.ProgressReporter",          # results package, but not a dataclass
    "kipi.dc.pi.report.save_results",               # a function
    "kipi.core._private.X",
])
def test_only_kipis_result_classes_are_built(tmp_path, name):
    path = results_io.write(tmp_path / "r", {}, None)
    _rewrite(path, lambda doc: doc.update(data={"$o": name, "f": {}}))
    with pytest.raises(ValueError, match="does not save"):
        results_io.read(path)


def test_things_results_never_hold_are_refused_when_saving(tmp_path):
    old = tmp_path / "r.kipi-results"
    old.write_bytes(b"earlier results")
    with pytest.raises(TypeError):
        results_io.write(old, {}, {"x": object()})
    with pytest.raises(TypeError):
        results_io.write(old, {}, np.array([object()]))

    @dataclass
    class Local:
        x: List[int] = field(default_factory=list)
    with pytest.raises(TypeError):
        results_io.write(old, {}, Local())
    # A failed save leaves the file that was there.
    assert old.read_bytes() == b"earlier results" and os.listdir(tmp_path) == [old.name]


def test_not_a_results_file(tmp_path):
    (tmp_path / "x").write_bytes(b"PK\x03\x04 not really")
    with pytest.raises(ValueError, match="Not a KiPI results file"):
        results_io.read(tmp_path / "x")
    with zipfile.ZipFile(tmp_path / "y", "w") as z:
        z.writestr("other.txt", "x")
    with pytest.raises(ValueError, match="Not a KiPI results file"):
        results_io.read(tmp_path / "y")


# ---------------------------------------------------------------------------
# Pickles from KiPI 0.7 and earlier
# ---------------------------------------------------------------------------

class _Evil:
    def __reduce__(self):
        return (os.system, ("echo pwned",))


def _legacy(path, payload):
    with gzip.open(path, "wb") as fh:
        pickle.dump(payload, fh)
    return path


def test_legacy_pickles_cannot_name_functions(tmp_path):
    path = _legacy(tmp_path / "r.kipi-results", {"format": 1, "tree": _Evil()})
    assert results_io.is_legacy(path)
    with gzip.open(path) as fh, pytest.raises(pickle.UnpicklingError, match="system"):
        compat.Unpickler(fh).load()


def test_legacy_pickles_still_hold_what_results_held(tmp_path):
    from collections import defaultdict
    st = Stackup([CopperLayer(3, "F.Cu", 0, 0.035, 1.5)], [], 1.6)
    payload = {"st": st, "a": np.arange(3.0), "d": defaultdict(list, {1: [2]}), "s": {1},
               "p": Path("r.html")}
    path = _legacy(tmp_path / "r", payload)
    with gzip.open(path) as fh:
        back = compat.Unpickler(fh).load()
    assert back["st"] == st and back["d"] == {1: [2]} and back["s"] == {1}
    np.testing.assert_array_equal(back["a"], payload["a"])


@pytest.mark.parametrize("module", ["pathlib", "pathlib._local"])     # 3.13 and later
def test_paths_from_any_python_open(module):
    blob = f"c{module}\nPurePosixPath\n(Vr.html\ntR.".encode()      # PurePosixPath("r.html")
    assert compat.Unpickler(io.BytesIO(blob)).load() == Path("r.html")


def test_a_windows_path_in_a_legacy_pickle_opens_anywhere():
    blob = pickle.dumps({"p": PureWindowsPath(r"C:\x\r.html")})
    # This machine's Path: C:\x\r.html on Windows. Elsewhere it opens without an error, as
    # one name or as parts depending on how this Python pickles paths (3.13 changed it).
    p = compat.Unpickler(io.BytesIO(blob)).load()["p"]
    assert isinstance(p, Path) and str(p).replace("\\", "/").endswith("x/r.html")
    if sys.platform == "win32":
        assert p == Path(r"C:\x\r.html")


def _rewrite(path, change):
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    doc = json.loads(files["results.json"])
    change(doc)
    files["results.json"] = json.dumps(doc).encode()
    with zipfile.ZipFile(path, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
