"""The AC values of a Tomachie ``.pdn.json`` (spec revision 9): what an impedance sweep
takes per capacitor, per regulator and per load.

Tomachie writes each value with its basis: ``datasheet`` (the part's datasheet prints
it, with the page) or ``estimated`` (``from`` says from what); a value it could not
determine is ``"unknown"``. Nothing is estimated here: a value the file does not give
stays as the setup has it, and the import notes say how many came from estimates.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from kipi.ac.model import AGING_PCT_PER_DECADE, CapModel
from kipi.dc.pi.importers.pdn_figures import number

# Tomachie writes a C0G dielectric as the decoder names it ("C0G(NP0)").
_DIELECTRIC_ALIASES = {"C0G(NP0)": "C0G", "NP0": "C0G", "COG": "C0G"}


def ac_value(field, unit_key: str) -> Tuple[Optional[float], str]:
    """``(value, basis)`` of an AC field ``{unit_key: v, "basis": ...}``; ``(None, "")``
    for "unknown" or none."""
    if not isinstance(field, dict):
        return None, ""
    v = number(field.get(unit_key))
    return (v, str(field.get("basis", ""))) if v and v > 0 else (None, "")


def _positive(v) -> Optional[float]:
    n = number(v)
    return n if n and n > 0 else None


def dielectric(name) -> str:
    """A dielectric KiPI's ageing and temperature tables know, else ""."""
    d = _DIELECTRIC_ALIASES.get(str(name or "").upper(), str(name or "").upper())
    return d if d in AGING_PCT_PER_DECADE else ""


def fill_cap_model(m: CapModel, caps: List[dict]) -> Tuple[bool, bool]:
    """Fill what ``m`` lacks from its capacitors' ``ac`` blocks: ESR, rating, dielectric
    and tolerance (the capacitance and ESL are filled from ``value`` / ``esl`` as before).
    Returns ``(esr_filled, esr_estimated)``."""
    blocks = [c["ac"] for c in caps if isinstance(c.get("ac"), dict)]
    esr_filled = esr_estimated = False
    for ac in blocks:
        if not m.esr_ohm:
            esr, basis = ac_value(ac.get("esr"), "ohm")
            if esr:
                m.esr_ohm = esr
                esr_filled, esr_estimated = True, basis == "estimated"
        if m.rated_v is None:
            m.rated_v = _positive(ac.get("rated_v"))
        if not m.dielectric:
            m.dielectric = dielectric(ac.get("dielectric"))
            if m.dielectric and not m.aging_pct_per_decade:
                m.aging_pct_per_decade = AGING_PCT_PER_DECADE[m.dielectric]
        if m.mfg_tolerance_pct is None:
            m.mfg_tolerance_pct = _positive(ac.get("tolerance_pct"))
    return esr_filled, esr_estimated


def vrm_bandwidth_khz(reg: dict) -> Tuple[Optional[float], str]:
    """A regulator's control loop bandwidth in kHz and its basis, from its ``ac`` block."""
    ac = reg.get("ac") if isinstance(reg.get("ac"), dict) else {}
    hz, basis = ac_value(ac.get("bandwidth"), "hz")
    return (hz / 1e3, basis) if hz else (None, "")


def load_step(load: dict) -> Tuple[Optional[float], str]:
    """A load's current step in amps and its basis, from its ``ac`` block."""
    ac = load.get("ac") if isinstance(load.get("ac"), dict) else {}
    return ac_value(ac.get("i_step"), "amps")

