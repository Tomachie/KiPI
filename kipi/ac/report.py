"""AC results to files.

* :func:`write_csv`  |Z|, R and X at every probe and sweep frequency
* :func:`save_results` / :func:`load_results`  reopen solved rails without solving again
"""

from __future__ import annotations

import csv
import gzip
import pickle
import time
from pathlib import Path
from typing import Dict

from kipi import compat

RESULTS_FORMAT = 1
RESULTS_KIND = "ac"
RESULTS_SUFFIX = ".kipi-ac-results"


def write_csv(path, result) -> Path:
    """One row per sweep frequency: |Z|, R and X at every probe, in ohms."""
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([f"# KiPI AC impedance, rail {result.rail} "
                    f"({' + '.join(result.rail_nets)}) against {result.ground}"])
        head = ["frequency_hz"]
        for c in result.probes:
            head += [f"{c.name} |Z| (ohm)", f"{c.name} R (ohm)", f"{c.name} X (ohm)"]
        w.writerow(head)
        for k, f in enumerate(result.freqs):
            row = [f"{f:.6g}"]
            for c in result.probes:
                z = c.z[k]
                row += [f"{abs(z):.6g}", f"{z.real:.6g}", f"{z.imag:.6g}"]
            w.writerow(row)
    return path


def save_results(path, results: Dict[str, object], board_name: str, setup) -> Path:
    """Write every solved rail's :class:`ACResult` (keyed by VRM name) to a results file
    (gzipped pickle). Only open results files you created."""
    path = Path(path)
    payload = {"format": RESULTS_FORMAT, "kind": RESULTS_KIND,
               "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "board": board_name,
               "results": dict(results), "setup": setup}
    with gzip.open(path, "wb", compresslevel=4) as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return path


def load_results(path) -> dict:
    with gzip.open(Path(path), "rb") as fh:
        payload = compat.Unpickler(fh).load()
    if not isinstance(payload, dict) or payload.get("format", 0) > RESULTS_FORMAT:
        raise ValueError("Not a KiPI results file, or from a newer KiPI")
    if payload.get("kind") != RESULTS_KIND:
        raise ValueError("This holds DC results: open it in DC mode")
    for result in payload["results"].values():
        # The HTML report is a separate file; offer it only if it is still there.
        report = getattr(result, "report_path", None)
        if report is not None and not Path(report).is_file():
            result.report_path = None
    return payload
