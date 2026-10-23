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


GRAPH_SIG_FIGS = 4       # a value read off a datasheet graph is good to no more than this


def ac_value(field, unit_key: str) -> Tuple[Optional[float], str]:
    """``(value, basis)`` of an AC field ``{unit_key: v, "basis": ...}``; ``(None, "")``
    for "unknown" or none. A value read off a graph is rounded to the figures a graph
    can give."""
    if not isinstance(field, dict):
        return None, ""
    v = number(field.get(unit_key))
    if not (v and v > 0):
        return None, ""
    basis = str(field.get("basis", ""))
    return (float(f"{v:.{GRAPH_SIG_FIGS}g}") if basis == "graph" else v), basis


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
    src = m.values_from
    for ac in blocks:
        own_esl, basis = ac_value(ac.get("esl"), "h")
        if own_esl and m.esl_h in (0.0, file_esl) and src.get("ESL") != "entered":
            m.esl_h, src["ESL"] = own_esl, basis or "graph"
        kept, basis = ac_value(ac.get("kept_at_rated"), "pct")
        if kept and m.kept_at_rated_pct == DEFAULT_KEPT_AT_RATED_PCT:
            m.kept_at_rated_pct, src["kept"] = kept, basis or "graph"
        if not m.esr_ohm:
            esr, basis = ac_value(ac.get("esr"), "ohm")
            if esr:
                m.esr_ohm, src["ESR"] = esr, basis
                esr_filled, esr_estimated = True, basis == "estimated"
        if m.rated_v is None:
            m.rated_v = _positive(ac.get("rated_v"))
        if not m.dielectric:
            m.dielectric = dielectric(ac.get("dielectric"))
            if m.dielectric and not m.aging_pct_per_decade:
                m.aging_pct_per_decade = AGING_PCT_PER_DECADE[m.dielectric]
        if m.mfg_tolerance_pct is None:
            m.mfg_tolerance_pct = _positive(ac.get("tolerance_pct"))
        if ac.get("defaults_because") and not m.defaults_because:
            m.defaults_because = str(ac["defaults_because"])
        maker = ac.get("maker_model") if isinstance(ac.get("maker_model"), dict) else {}
        if "equivalent_maker_model" in src.values() and maker.get("equivalent"):
            m.equivalent = m.equivalent or str(maker["equivalent"])
        if ac.get("doubt") and not m.doubt:
            m.doubt = str(ac["doubt"])
    return esr_filled, esr_estimated


def doubt_notes(models: List[CapModel]) -> List[str]:
    """One import note for the models whose maker's own figure is used but not trusted:
    the reason first, then each model and why on the lines after."""
    doubted = [m for m in models if m.doubt]
    if not doubted:
        return []
    many = "model" if len(doubted) == 1 else "models"
    # The part count, not the references: a long list would push the reason out of view
    # (the model's parts are in its editor).
    lines = [f"{m.name} ({len(m.refs)} part{'' if len(m.refs) == 1 else 's'}): {m.doubt}"
             for m in doubted]
    return [f"Capacitor AC values (AC): the maker's own figure is used but not trusted for "
            f"{len(doubted)} capacitor {many}:\n" + "\n".join(lines)]


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
    defaults (estimates): the count and the reason first, so a long list does not push
    the cause out of view, then the capacitors on the line after."""
    notes = []
    for d in pdn.get("ac_defaults") or []:
        refs = d.get("refdes") or []
        if refs:
            why = DEFAULTS_BECAUSE.get(d.get("because"), str(d.get("because")))
            many = "capacitor" if len(refs) == 1 else "capacitors"
            notes.append(f"Capacitor AC values (AC): defaults are used for {len(refs)} {many} "
                         f"- {why}:\n{', '.join(refs)}")
    return notes
