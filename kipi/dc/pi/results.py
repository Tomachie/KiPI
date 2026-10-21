# Portions Copyright (c) 2026 Tomachie LLC.
"""Results of a power-tree solve: per net group, per load, per VRM."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kipi.dc.solver import NetResult

NOT_CONVERGED = "NOT CONVERGED"
OUT_OF_REGULATION = "VRM OUT OF REGULATION"
DISCONNECTED = "DISCONNECTED"       # no copper path to its VRM: not solved (#88)
INCOMPLETE = "INCOMPLETE"           # no longer given: results files saved before headroom


@dataclass
class GroupResult:
    name: str
    nets: List[str]
    kind: str                 # "rail" or "ground"
    driver: str               # VRM name (rail) or reference VRM name(s) (ground)
    result: NetResult
    iterations: int = 1
    converged: bool = True


BOOST_EPS = 1e-12          # volts; smaller deviations read as a (zero) drop, not a boost


def drop_or_boost(dv: float):
    """Split ``dv`` = nominal - actual into (magnitude, "drop" | "boost").

    A load can sit above its VRM's target voltage: a remote-sensed VRM raises its output pins
    to hold a sense point further down the copper, so pins nearer the VRM see a boost.
    """
    if math.isnan(dv):                 # nothing solved, as for a disconnected load (#88)
        return dv, ""
    return (-dv, "boost") if dv < -BOOST_EPS else (max(dv, 0.0), "drop")


def fmt_amps(amps: float) -> str:
    """Amps for a sentence: "17.6 A" from 1 A up, "20 mA" below it, "350 µA" below 1 mA."""
    a = abs(amps)
    if a >= 1:
        return f"{amps:.3g} A"
    if a >= 1e-3:
        return f"{amps * 1e3:.3g} mA"
    return f"{amps * 1e6:.3g} µA"


def fmt_drop(dv: float, fmt: str = ".3f", scale: float = 1e3, unit: str = " mV") -> str:
    """``dv`` (volts, nominal - actual) as e.g. "1.234 mV drop" or "0.567 mV boost"."""
    mag, kind = drop_or_boost(dv)
    return f"{mag * scale:{fmt}}{unit} {kind}" if kind else ""


@dataclass
class LoadReport:
    name: str
    group: str
    current: float
    nominal: float            # target voltage of the VRM driving the group
    v_supply: float           # mean over the supply pins
    v_supply_min: float
    v_ground: Optional[float]  # mean over the ground pins (None if not modeled)
    v_effective: float        # supply - ground
    drop: float               # nominal - effective, volts (negative: a boost)
    tolerance: str                       # as set, for tables: "±5 %", "3.1 to 3.5 V"
    passed: Optional[bool]               # judged on the worst pin
    v_worst: float = 0.0                 # whichever of v_low / v_high is further from nominal
    # (name, A, V mean, V min, V max) per pin group
    pin_groups: List[tuple] = field(default_factory=list)
    v_low: Optional[float] = None        # lowest supply pin minus highest ground pin
    v_high: Optional[float] = None       # highest supply pin minus lowest ground pin
    status: str = ""                     # why the load was not judged, or was failed outright
    limit_low: Optional[float] = None    # the tolerance in volts, either end None if not set
    limit_high: Optional[float] = None
    basis: str = ""                      # the current solved: typ, max, typ (no max)
    copper_margin: Optional[float] = None  # the load's pre-layout copper margin, volts
    # Amps the rail's loads whose current is not known may add together before this load
    # leaves its tolerance (RailHeadroom); None without a low limit or without such loads.
    unknown_room_a: Optional[float] = None

    def __setstate__(self, state):
        # Results files saved before boosts were reported: only the lowest pin is known,
        # and pin groups have no highest pin. Before asymmetric tolerances: +/- one %.
        t = state.pop("tolerance_pct", None)
        if t is not None and "tolerance" not in state:
            nom = state.get("nominal", 0.0)
            state.update(tolerance=f"±{t:g} %", limit_low=nom - abs(nom) * t / 100,
                         limit_high=nom + abs(nom) * t / 100)
        state = {"v_low": state.get("v_worst"), "v_high": None, "status": "",
                 "tolerance": "", "limit_low": None, "limit_high": None,
                 "copper_margin": None, "unknown_room_a": None, **state}
        state["pin_groups"] = [tuple(g) + (None,) * (5 - len(g))
                               for g in state.get("pin_groups", [])]
        self.__dict__.update(state)

    @property
    def drop_pct(self) -> float:
        return 100.0 * self.drop / abs(self.nominal) if self.nominal else 0.0

    @property
    def drop_worst(self) -> float:
        """Nominal minus the worst pin, volts (negative: a boost)."""
        return self.nominal - self.v_worst

    @property
    def drop_worst_pct(self) -> float:
        return 100.0 * self.drop_worst / abs(self.nominal) if self.nominal else 0.0

    @property
    def margin_low(self) -> Optional[float]:
        """Volts the lowest pin is above the tolerance's low end (negative: below it);
        None without a low end."""
        if self.limit_low is None or self.v_low is None or self.v_low != self.v_low:
            return None
        return self.v_low - self.limit_low

    @property
    def verdict(self) -> str:
        if self.status:
            return self.status
        return "" if self.passed is None else ("PASS" if self.passed else "FAIL")


@dataclass
class PhaseReport:
    index: int                # 1-based
    v_out: float              # mean over the phase's output pins
    i_out: float
    v_in: Optional[float]
    i_in: Optional[float]

    @property
    def p_out(self) -> float:
        return self.v_out * self.i_out

    @property
    def p_in(self) -> Optional[float]:
        return None if self.v_in is None else self.v_in * self.i_in

    @property
    def p_loss(self) -> Optional[float]:
        return None if self.p_in is None else self.p_in - self.p_out


@dataclass
class VrmReport:
    name: str
    kind: str
    group: str
    v_set: float
    v_out: float              # at the output pins (after sense correction); multi-phase:
                              # the mean of the phases
    v_sense: Optional[float]
    i_out: float
    v_in: Optional[float]
    i_in: Optional[float]
    p_out: float
    p_in: Optional[float]
    v_ground_sense: Optional[float] = None   # at the ground sense pins, if any
    phases: List[PhaseReport] = field(default_factory=list)   # multi-phase VRMs only
    current_limit_a: Optional[float] = None   # total output current limit, if one is set
    passed: Optional[bool] = None             # |I out| within the limit
    regulation: str = ""                     # why the VRM cannot hold its target voltage
    load_line_mohm: float = 0.0               # load line (#94), 0 = none
    v_droop: float = 0.0                      # load line x I out: how far v_target is
                                              # below v_set

    def __setstate__(self, state):
        # Results files saved before multi-phase VRMs, current limits, regulation
        # checks or load lines lack those fields.
        self.__dict__.update({"phases": [], "current_limit_a": None, "passed": None,
                              "regulation": "", "load_line_mohm": 0.0,
                              "v_droop": 0.0, **state})

    @property
    def v_target(self) -> float:
        """The voltage the VRM regulates to: its target voltage less the load-line droop."""
        return self.v_set - self.v_droop

    @property
    def p_loss(self) -> Optional[float]:
        return None if self.p_in is None else self.p_in - self.p_out


@dataclass
class RailHeadroom:
    """What a rail's loads whose current is not known may draw, together, before a load
    on the rail leaves its tolerance: the designer's unknowns (what a header or a
    translator's lines will carry) as a budget, from the margin the known currents leave.
    Supply copper and, where the unknown has ground pins, its ground return; the VRM
    holds what it held (a load line's droop is not counted)."""
    group: str                          # the rail's label
    vrm: str                            # the VRM that drives it
    known_a: float                      # what the VRM supplies at the known currents
    unknown: List[str]                  # the loads whose current is not known
    tightest: str = ""                  # the load with the least room ("" when none is judged)
    margin_v: Optional[float] = None    # its lowest pin above its tolerance's low end
    room_a: Optional[float] = None      # what the unknowns may add together (0: already out)
    costliest: str = ""                 # the unknown whose current costs that load the most
    per_amp_v: float = 0.0              # volts the tightest load loses per amp drawn there
    upstream: List[str] = field(default_factory=list)   # rails the same current also loads
    vrm_room_a: Optional[float] = None  # what the VRM's output current limit leaves, if set

    def sentence(self) -> str:
        """The budget in one sentence, for the notes and the report."""
        who = ", ".join(self.unknown)
        s = f"{self.group}: {fmt_amps(self.known_a)} known. "
        if self.room_a is None:
            return s + (f"Not known: {who}. No load on this rail has a minimum to judge "
                        "against, so the room for them cannot be given.")
        if self.margin_v is not None and self.margin_v <= 0:
            return s + (f"{self.tightest} is already {-self.margin_v * 1e3:.4g} mV below "
                        f"its minimum at the known currents; nothing can be added for {who}.")
        s += (f"Tightest load: {self.tightest}, {self.margin_v * 1e3:.4g} mV above its "
              f"minimum. Not known: {who} - together they must stay under "
              f"{fmt_amps(self.room_a)} for the copper (worst case, all of it at "
              f"{self.costliest}: {self.per_amp_v * 1e3:.4g} mV per A), or {self.tightest} "
              "leaves its tolerance.")
        if self.vrm_room_a is not None:
            s += (f" {self.vrm}'s output current limit leaves {fmt_amps(self.vrm_room_a)}."
                  if self.vrm_room_a > 0 else
                  f" {self.vrm} is already at its output current limit.")
        if self.upstream:
            s += (f" Through {self.vrm} the same current also loads "
                  f"{', '.join(self.upstream)}; not counted there.")
        return s


@dataclass
class TreeResult:
    groups: Dict[str, GroupResult] = field(default_factory=dict)
    loads: List[LoadReport] = field(default_factory=list)
    vrms: List[VrmReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    solve_s: float = 0.0
    thermal: Optional[object] = None          # kipi.dc.thermal.cosim.ThermalResult, if run
    report_path: Optional[object] = None      # HTML report written for this solve, if any
    corner: str = "typ"                       # "typ" or "max": kipi.dc.pi.corner
    headroom: List[RailHeadroom] = field(default_factory=list)   # rails with unknowns
