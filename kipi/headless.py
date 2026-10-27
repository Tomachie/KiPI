"""Solve a board's power tree with no window and write ``pdn_results.json``
(:mod:`kipi.pdn_results`) and an AC report per swept rail.

    python -m kipi --solve OUT_DIR BOARD --pdn-json PDN.json [--ac all|none|U35 VCC_CORE,...]
                   [--coarse] [--threads N] [--memory-gb GB] [--normal-priority]

On a shared machine the solve runs below normal priority, with at most N solver threads
and GB of memory (kipi.core.limits): a rail that needs more is listed not solved, out of
memory, and the next rail is tried.

DC is solved at the typical and the worst case. AC sweeps each rail asked for at the
typical case, with the capacitors' DC bias from the typical solve; a rail that cannot be
swept (no control loop bandwidth, a capacitor model without a value) is listed with why.
"""

from __future__ import annotations

import gc
import json
import os
import time
from pathlib import Path
from typing import List, Optional

from kipi import pdn_results
from kipi.core import limits
from kipi.core.progress import ProgressReporter

# A coarser mesh for boards with thousands of vias, DC and AC: what a 10-layer board
# needs to solve in minutes rather than hours (2026-10-26: 1.9M unknowns instead of 8.5M
# for DC; 3.3M instead of 4.75M for AC, with 6 copper solves a decade, not 20).
COARSE_DC = dict(max_edge_mm=1.5, min_edge_mm=0.1, grading=0.5)
# A rail with a hundred parts spends as long again on the sweep after the copper solves
# (a dense solve over all its parts at each sweep point) as on them: 50 points a decade.
COARSE_AC = dict(max_edge_mm=2.0, min_edge_mm=0.1, grading=0.5, field_points_per_decade=6,
                 points_per_decade=50)


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


def _gb(x: Optional[float]) -> Optional[float]:
    return round(x, 2) if x is not None else None


def _failure(e: BaseException) -> str:
    """Why a solve stopped, for the file: out of memory said as such (a MemoryError, or
    PARDISO's own error -2 when the memory cap stops its factor)."""
    text = str(e) or type(e).__name__
    if isinstance(e, MemoryError) or "memory" in text.lower() or "error code -2" in text:
        return f"out of memory ({type(e).__name__}: {text})"
    return f"{type(e).__name__}: {text}"


def _incomplete_off_rail(setup, snap, typ, vrm: str) -> list:
    """Capacitor models missing a value (no ESL in the file) with no part on ``vrm``'s
    rail: they would stop its sweep without being in it."""
    nets = {n for g in typ.groups.values() if g.kind == "rail" and g.driver == vrm
            for n in g.nets}
    on_rail = {p.footprint for p in snap.pads if p.net in nets}
    return [m for m in setup.caps
            if m.refs and not all(v and v > 0 for v in (m.capacitance_f, m.esl_h, m.esr_ohm))
            and not set(m.refs) & on_rail]


def run(out_dir, board, pdn_json, ac: str = "all", coarse: bool = False,
        progress: Optional[ProgressReporter] = None,
        memory_gb: Optional[float] = None) -> Path:
    """``memory_gb``: the cap :func:`kipi.core.limits.apply` set, recorded in the file."""
    from kipi.ac.html_report import write_ac_report
    from kipi.ac.solver.sweep import ACError, solve_ac
    from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter, source_text
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
    setup.import_source = source_text(pdn, pdn_json)
    if coarse:
        for k, v in COARSE_DC.items():
            setattr(setup.settings, k, v)
        for k, v in COARSE_AC.items():
            setattr(setup.ac, k, v)

    # The worst case first, let go once written; then the typical, kept for the AC sweeps'
    # DC bias. Holding both at once doubled the peak (a 4 GB cap stopped the second).
    cases, typ, mesh_cache, dc_error = [], None, {}, None
    for corner in ("max", "typ"):
        setup.corner = corner
        progress.stage(f"DC, {pdn_results.CASE_NAMES[corner]} case")
        try:
            tree = solve_tree(snap, setup, progress, mesh_cache=mesh_cache)
        except Exception as e:  # noqa: BLE001 - said in the file; out of memory above all
            dc_error = f"{pdn_results.CASE_NAMES[corner]} case: {_failure(e)}"
            progress.message(f"DC not solved: {dc_error}")
            gc.collect()
            continue
        case = pdn_results.dc_case(tree, setup, corner)
        case["peak_memory_gb"] = _gb(limits.peak_memory_gb())
        cases.insert(0 if corner == "typ" else len(cases), case)
        progress.message(tree.summary())
        if corner == "typ":
            typ = tree
        del tree
        gc.collect()

    setup.corner = "typ"
    # Without the typical DC solve there is no DC bias to sweep with: each rail asked for
    # is listed with why, rather than left out of the file.
    rails = [] if typ is not None else [
        pdn_results.ac_not_solved(name, "no typical DC solve to take the DC bias from")
        for name in _ac_rails(setup, ac)]
    for name in (_ac_rails(setup, ac) if typ is not None else []):
        v = next(x for x in setup.vrms if x.name == name)
        progress.stage(f"AC, {name}")
        left_out = _incomplete_off_rail(setup, snap, typ, name)
        for m in left_out:
            setup.caps.remove(m)
            progress.message(f"{name}: capacitor model {m.name} ({', '.join(m.refs)}) is "
                             "missing a value and is on another rail; left out of this sweep")
        try:
            r = solve_ac(snap, setup, name, v.ground.net if v.ground else "",
                         progress, dc_result=typ)
            html = write_ac_report(out_dir / f"ac_{name.split()[0]}.html", r, snap, setup)
            rails.append(pdn_results.ac_rail(r, setup, Path(html).name))
            rails[-1].update(unknowns=r.stats.n_unknowns,
                             peak_memory_gb=_gb(limits.peak_memory_gb()))
            del r                       # its factor and fields, before the next rail
        except ACError as e:
            rails.append(pdn_results.ac_not_solved(name, str(e)))
            progress.message(f"{name}: not swept: {e}")
        except Exception as e:  # noqa: BLE001 - out of memory above all; the next rail
            rails.append(pdn_results.ac_not_solved(name, _failure(e)))  # may still fit
            progress.message(f"{name}: not swept: {_failure(e)}")
        finally:
            setup.caps.extend(left_out)
            gc.collect()
        if left_out:
            rails[-1]["capacitor_models_left_out"] = [
                {"model": m.name, "refdes": list(m.refs), "why": "missing_value_other_rail"}
                for m in left_out]

    solve = {"dc_mesh": {k: getattr(setup.settings, k) for k in COARSE_DC},
             "ac_mesh": {k: getattr(setup.ac, k) for k in COARSE_AC},
             "seconds": round(time.monotonic() - t0, 1),
             "limits": {"threads": int(os.environ["OMP_NUM_THREADS"])
                        if os.environ.get("OMP_NUM_THREADS", "").isdigit() else None,
                        "memory_gb": memory_gb},
             "peak_memory_gb": _gb(limits.peak_memory_gb())}
    if dc_error:
        solve["dc_error"] = dc_error
    doc = pdn_results.document("KiPI", VERSION, pdn, pdn_json, board,
                               fmt.name if fmt else "", solve, cases, rails, notes)
    path = pdn_results.write(out_dir / "pdn_results.json", doc)
    progress.message(f"Wrote {path}")
    return path
