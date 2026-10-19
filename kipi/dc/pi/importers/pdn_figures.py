"""Numbers out of a power-tree file's value fields and datasheet figures.

A value field holds a number or a word ("unknown", "not_specified"); a current is an
object with ``amps`` or one of those words. Datasheet figures are printed strings.

A series part's DC resistance comes only from figures that ARE a DC resistance. A
ferrite's datasheet also prints its impedance in ohms, at 100 MHz; taking that for its DC
resistance would be wrong by orders of magnitude, so figures are chosen by what they
are, never by their unit alone.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

DC_RESISTANCE_SPECS = ("dcr", "dc_resistance", "resistance", "rds_on", "jumper_resistance",
                       "cold_resistance", "r_dc", "rdc", "on_resistance")
OHMS_PER_UNIT = {"ohm": 1.0, "ohms": 1.0, "Ω": 1.0, "mohm": 1e-3, "mΩ": 1e-3,
                 "kohm": 1e3, "kΩ": 1e3, "uohm": 1e-6, "µΩ": 1e-6}
_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def number(value) -> Optional[float]:
    """A value field as a float; None for a word such as "unknown"."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def amps(current) -> Optional[float]:
    """A current object's amps; None for "unknown" or a missing current."""
    return number(current.get("amps")) if isinstance(current, dict) else None


def _printed(text) -> Optional[float]:
    m = _NUMBER.search(str(text or ""))
    return float(m.group()) if m else None


def _ohms(fig: dict, column: str) -> Optional[float]:
    unit = str(fig.get("unit", "")).strip()
    # Exact first: lower-casing turns the ohm sign into a Greek small omega.
    scale = OHMS_PER_UNIT.get(unit, OHMS_PER_UNIT.get(unit.lower()))
    value = _printed(fig.get(column))
    return None if value is None or scale is None else abs(value) * scale


def dc_resistance(element: dict) -> Tuple[Optional[float], Optional[float], List[str]]:
    """(typical, maximum, notes) of a series part's DC resistance in ohms.

    A resistor's own value is its resistance. Otherwise the DC-resistance figures of its
    datasheet: where several are printed (different conditions) the highest is used and
    said so."""
    own = number(element.get("resistance_ohm"))
    if own is not None:
        return own, None, []
    figs = [f for f in element.get("datasheet_figures", [])
            if str(f.get("spec", "")).lower() in DC_RESISTANCE_SPECS]
    typs = [v for v in (_ohms(f, "typ") for f in figs) if v is not None]
    maxs = [v for v in (_ohms(f, "max") for f in figs) if v is not None]
    notes = []
    if len(typs) > 1 or len(maxs) > 1:
        notes.append("several DC-resistance figures printed; the highest of each is used")
    return (max(typs) if typs else None), (max(maxs) if maxs else None), notes


# A diode's forward-voltage figures, under whatever key the datasheet read gave them.
_FORWARD_V_SPEC = re.compile(r"(?i)^(v_?f\w*|v_?forward\w*|forward_voltage\w*)$")
_AMPS_PER_UNIT = {"a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6}


def _test_amps(text) -> Optional[float]:
    """The test current printed with a figure ("5A", "500 mA"), in amps."""
    m = re.match(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([mµu]?A)\b", str(text or ""))
    return float(m.group(1)) * _AMPS_PER_UNIT[m.group(2).lower()] if m else None


def forward_drop(element: dict) -> Tuple[Optional[float], Optional[float], float, List[str]]:
    """(forward voltage, its maximum, slope resistance in ohms, notes) of a series diode
    from its datasheet's forward-voltage figures; (None, None, 0, []) when none is printed.

    Two figures at different test currents give a drop and a slope (V = Vf + I x R),
    fitted on the typical figures, else the maximum ones. One figure is a constant drop:
    at currents below its test current the diode drops less, so it reads high there."""
    figs = [f for f in element.get("datasheet_figures", [])
            if _FORWARD_V_SPEC.match(str(f.get("spec", "")))
            and str(f.get("unit", "")).strip().upper() == "V"]
    cols = {c: [(_test_amps(f.get("if")), _printed(f.get(c))) for f in figs] for c in ("typ", "max")}
    cols = {c: [(i, v) for i, v in pts if v is not None] for c, pts in cols.items()}
    if not cols["typ"] and not cols["max"]:
        return None, None, 0.0, []
    fit_col = "typ" if cols["typ"] else "max"
    pts = sorted((i, v) for i, v in cols[fit_col] if i is not None)
    if len(pts) >= 2 and pts[-1][0] > pts[0][0]:
        (i1, v1), (i2, v2) = pts[0], pts[-1]
        slope = max((v2 - v1) / (i2 - i1), 0.0)
        drop = v1 - i1 * slope
        how = (f"forward drop {drop:.3g} V + {slope * 1e3:.3g} mOhm slope, from the "
               f"{fit_col} figures at {i1:g} A and {i2:g} A")
    else:
        i_at, drop = max(cols[fit_col], key=lambda p: p[1])
        slope = 0.0
        at = f" at {i_at:g} A" if i_at is not None else ""
        how = (f"forward drop {drop:g} V ({fit_col}{at}) as a constant: below that "
               "current it drops less, so the rail after it reads somewhat low")
    vf_max = None
    if fit_col == "typ" and cols["max"]:
        vf_max = drop + max(v for _, v in cols["max"]) - max(v for _, v in cols["typ"])
    return drop, vf_max, slope, [how]
