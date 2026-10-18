"""The CI workflow's KiCad versions job (#106) names every KiCad release the fake KiCad
answers as, and every fixture board, so a version added to the tests shows in the pipeline."""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from kicad_fake import VERSIONS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _steps():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    return ci["jobs"]["kicad"]["steps"]


def test_a_step_per_kicad_version():
    named = [m.group(1) for s in _steps() if (m := re.fullmatch(r"KiCad (\S+) \(IPC API\)",
                                                               s.get("name", "")))]
    assert named == VERSIONS
    for s in _steps():
        if s.get("name", "").endswith("(IPC API)"):
            version = s["name"].split()[1]
            assert f'-k "[{version}]"' in s["run"] and "test_kicad_versions.py" in s["run"]


def test_a_step_per_fixture_board():
    boards = sorted(p.name[:-len(".kicad_pcb")]
                    for p in (ROOT / "tests" / "data" / "kicad_pcb").glob("*.kicad_pcb"))
    run = [re.search(r'-k "\[(\w+)\]"', s["run"]).group(1) for s in _steps()
           if "test_kicad_pcb_reader.py" in s.get("run", "")]
    assert sorted(run) == boards


def test_every_version_runs_even_after_one_fails():
    for s in _steps():
        if "pytest" in s.get("run", ""):
            assert "!cancelled()" in s["if"]
