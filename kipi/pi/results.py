"""Results of a power-tree solve: per net group, per load, per VRM."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kipi.solver import NetResult

NOT_CONVERGED = "NOT CONVERGED"
OUT_OF_REGULATION = "VRM OUT OF REGULATION"


@dataclass
class GroupResult:
    name: str
    nets: List[str]
    kind: str                 # "rail" or "ground"
    driver: str               # VRM name (rail) or reference VRM name(s) (ground)
    result: NetResult
    iterations: int = 1
    converged: bool = True


@dataclass
class LoadReport:
    name: str
    group: str
    current: float
    nominal: float            # set point of the VRM driving the group
    v_supply: float           # mean over the supply pins
    v_supply_min: float
    v_ground: Optional[float]  # mean over the ground pins (None if not modeled)
    v_effective: float        # supply - ground
    drop: float               # nominal - effective, volts
    tolerance_pct: Optional[float]
    passed: Optional[bool]               # judged on the worst pin, low and high
    v_worst: float = 0.0                 # lowest supply pin minus highest ground pin
    pin_groups: List[tuple] = field(default_factory=list)  # (name, A, V mean, V min)
    v_best: float = 0.0                  # highest supply pin minus lowest ground pin
    status: str = ""                     # why the load was not judged, or was failed outright
    min_v: Optional[float] = None        # the load's datasheet minimum supply voltage

    @property
    def drop_pct(self) -> float:
        return 100.0 * self.drop / self.nominal if self.nominal else 0.0

    @property
    def drop_worst(self) -> float:
        return self.nominal - self.v_worst

    @property
    def drop_worst_pct(self) -> float:
        return 100.0 * self.drop_worst / self.nominal if self.nominal else 0.0

    @property
    def rise_pct(self) -> float:
        """How far the best pin sits above nominal (remote sense can push it there)."""
        return 100.0 * (self.v_best - self.nominal) / self.nominal if self.nominal else 0.0

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
    regulation: str = ""                     # why the VRM cannot hold its set point

    def __setstate__(self, state):
        # Results files saved before multi-phase VRMs have no phases.
        self.__dict__.update({"phases": [], **state})

    @property
    def p_loss(self) -> Optional[float]:
        return None if self.p_in is None else self.p_in - self.p_out


@dataclass
class TreeResult:
    groups: Dict[str, GroupResult] = field(default_factory=dict)
    loads: List[LoadReport] = field(default_factory=list)
    vrms: List[VrmReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    solve_s: float = 0.0
    thermal: Optional[object] = None          # kipi.thermal.cosim.ThermalResult, if run
    report_path: Optional[object] = None      # HTML report written for this solve, if any
