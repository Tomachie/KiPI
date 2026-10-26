"""Solve a board's power tree with no window and write ``pdn_results.json``
(:mod:`kipi.pdn_results`) and an AC report per swept rail.

    python -m kipi --solve OUT_DIR BOARD --pdn-json PDN.json [--ac all|none|U35 VCC_CORE,...]
                   [--coarse]

DC is solved at the typical and the worst case. AC sweeps each rail asked for at the
typical case, with the capacitors' DC bias from the typical solve; a rail that cannot be
swept (no control loop bandwidth, a capacitor model without a value) is listed with why.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

from kipi import pdn_results
from kipi.core.progress import ProgressReporter

# A coarser mesh for boards with thousands of vias, DC and AC: what a 10-layer board
# needs to solve in minutes rather than hours (2026-10-26: 1.9M unknowns instead of 8.5M
# for DC; 3.3M instead of 4.75M for AC, with 6 copper solves a decade, not 20).
COARSE_DC = dict(max_edge_mm=1.5, min_edge_mm=0.1, grading=0.5)
COARSE_AC = dict(max_edge_mm=2.0, min_edge_mm=0.1, grading=0.5, field_points_per_decade=6)


class _Print(ProgressReporter):
    """Every message and stage with the time since start, on stdout."""

    def __init__(self):
        super().__init__()
        self.t0 = time.monotonic()

    def message(self, text, level=20):
        if level < 20:                  # debug detail: the log file's, not the console's
            return
        print(f"[{time.monotonic() - self.t0:7.0f} s] {text}", flush=True)

    def stage(self, name):
        print(f"[{time.monotonic() - self.t0:7.0f} s] == {name}", flush=True)


def _ac_rails(setup, ask: str) -> List[str]:
    if ask == "none":
        return []
    if ask == "all":
        return [v.name for v in setup.vrms]
    names = [n.strip() for n in ask.split(",") if n.strip()]
    known = {v.name for v in setup.vrms}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit(f"--ac: no regulator named {', '.join(unknown)} "
                         f"(the file has {', '.join(sorted(known))})")
    return names


def run(out_dir, board, pdn_json, ac: str = "all", coarse: bool = False,
        progress: Optional[ProgressReporter] = None) -> Path:
    from kipi.ac.html_report import write_ac_report
    from kipi.ac.solver.sweep import ACError, solve_ac
    from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
    from kipi.dc.pi.tree import solve_tree
    from kipi.fork import VERSION
    from kipi.sources import registry

    progress = progress or _Print()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    fmt = registry.format_for(board)
    snap = registry.open_board(board).load(progress)
    pdn = json.loads(Path(pdn_json).read_text(encoding="utf-8"))
    setup, notes = TomachiePdnImporter(pdn, snap).run()
    if coarse:
        for k, v in COARSE_DC.items():
            setattr(setup.settings, k, v)
        for k, v in COARSE_AC.items():
            setattr(setup.ac, k, v)

    cases, trees, mesh_cache = [], {}, {}
    for corner in ("typ", "max"):
        setup.corner = corner
        progress.stage(f"DC, {pdn_results.CASE_NAMES[corner]} case")
        trees[corner] = solve_tree(snap, setup, progress, mesh_cache=mesh_cache)
        cases.append(pdn_results.dc_case(trees[corner], setup, corner))
        progress.message(trees[corner].summary())

    setup.corner = "typ"
    rails = []
    for name in _ac_rails(setup, ac):
        v = next(x for x in setup.vrms if x.name == name)
        progress.stage(f"AC, {name}")
        try:
            r = solve_ac(snap, setup, name, v.ground.net if v.ground else "",
                         progress, dc_result=trees["typ"])
        except ACError as e:
            rails.append(pdn_results.ac_not_solved(name, str(e)))
            progress.message(f"{name}: not swept: {e}")
            continue
        html = write_ac_report(out_dir / f"ac_{name.split()[0]}.html", r, snap, setup)
        rails.append(pdn_results.ac_rail(r, setup, Path(html).name))

    solve = {"dc_mesh": {k: getattr(setup.settings, k) for k in COARSE_DC},
             "ac_mesh": {k: getattr(setup.ac, k) for k in COARSE_AC},
             "seconds": round(time.monotonic() - t0, 1)}
    doc = pdn_results.document("KiPI", VERSION, pdn, pdn_json, board,
                               fmt.name if fmt else "", solve, cases, rails, notes)
    path = pdn_results.write(out_dir / "pdn_results.json", doc)
    progress.message(f"Wrote {path}")
    return path
