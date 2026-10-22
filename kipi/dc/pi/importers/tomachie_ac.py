"""The AC values of a Tomachie ``.pdn.json`` (spec revision 9): what an impedance sweep
takes per capacitor, per regulator and per load.

Tomachie writes each value with its basis: ``datasheet`` (the part's datasheet prints
it, with the page) or ``estimated`` (``from`` says from what); a value it could not
determine is ``"unknown"``. Nothing is estimated here: a value the file does not give
stays as the setup has it, and the import notes say how many came from estimates.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from kipi.ac.model import AGING_PCT_PER_DECADE, DEFAULT_KEPT_AT_RATED_PCT, CapModel
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


def fill_cap_model(m: CapModel, caps: List[dict], file_esl: float = 0.0) -> Tuple[bool, bool]:
    """Fill what ``m`` lacks from its capacitors' ``ac`` blocks: ESR, rating, dielectric,
    tolerance and the capacitance kept at the rated voltage (the capacitance and ESL are
    filled from ``value`` / ``esl`` as before). The part's own ESL from its curve
    replaces the case estimate ``file_esl``, never a value the user entered.
    Returns ``(esr_filled, esr_estimated)``."""
    blocks = [c["ac"] for c in caps if isinstance(c.get("ac"), dict)]
    esr_filled = esr_estimated = False
    for ac in blocks:
        own_esl, _ = ac_value(ac.get("esl"), "h")
        if own_esl and m.esl_h in (0.0, file_esl):
            m.esl_h = own_esl
        kept, _ = ac_value(ac.get("kept_at_rated"), "pct")
        if kept and m.kept_at_rated_pct == DEFAULT_KEPT_AT_RATED_PCT:
            m.kept_at_rated_pct = kept
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



# Why a capacitor's AC values are defaults (spec revision 9 "ac_defaults").
DEFAULTS_BECAUSE = {
    "no_part_number": "the schematic does not use a part number so a datasheet could not "
                      "be looked up; add part numbers to the schematic",
    "no_datasheet": "no datasheet was found for their part numbers",
    "not_in_datasheet": "their datasheets print no ESR",
}


def defaults_notes(pdn: dict) -> List[str]:
    """One import note per reason the file gives for capacitors whose AC values are
    defaults (estimates), naming them, so the cause is in front of the user."""
    notes = []
    for d in pdn.get("ac_defaults") or []:
        refs = d.get("refdes") or []
        if refs:
            why = DEFAULTS_BECAUSE.get(d.get("because"), str(d.get("because")))
            notes.append(f"Capacitor AC values (AC): defaults are used for {', '.join(refs)} - "
                         f"{why}")
    return notes
