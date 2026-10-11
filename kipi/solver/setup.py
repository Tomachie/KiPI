"""What to solve: sources, loads, series parts and numerical settings.

A problem covers one *extended net*: one or more nets joined by series parts (sense
resistors, ferrites, fuses). Terminals refer to pads by id. A terminal with several
pads ties them to one condition: a source holds all of its pads at its voltage; a load
draws its current split equally across its pads; each side of a series part is one
node (its pads tied together).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

# Annealed copper (IACS) at 20 degC, in ohm*mm.
RHO_CU_OHM_MM = 1.724e-5


@dataclass
class VoltageSource:
    name: str
    pad_ids: List[str]
    voltage: float


@dataclass
class CurrentLoad:
    name: str
    pad_ids: List[str]
    current: float          # amps leaving the net at these pads (negative = injected)


@dataclass
class SeriesLink:
    name: str
    pads_a: List[str]
    pads_b: List[str]
    resistance: float       # ohms; at or below SHORT_OHMS the two sides are merged


SHORT_OHMS = 1e-9


@dataclass
class SolveSettings:
    max_edge_mm: float = 0.5        # largest triangle edge; planes mesh at roughly this size
    min_angle_deg: float = 28.0     # triangle quality bound (Shewchuk's 'q' switch)
    simplify_mm: float = 0.003      # drop polygon detail finer than this before meshing
    circle_quad_segs: int = 4       # 16 segments per via/drill circle (an 8-gon reads ~0.8% high)
    min_edge_mm: float = 0.02       # smallest element, at plated holes
    grading: float = 0.2            # element size growth per mm away from a hole (0 = off)
    plating_mm: float = 0.025       # via / PTH barrel plating thickness
    rho_ohm_mm: float = RHO_CU_OHM_MM
    tol: float = 1e-10              # relative residual for the iterative solver
    max_iter: int = 2000


@dataclass
class NetProblem:
    net: str                                  # display name (the nets joined with " + ")
    sources: List[VoltageSource] = field(default_factory=list)
    loads: List[CurrentLoad] = field(default_factory=list)
    settings: SolveSettings = field(default_factory=SolveSettings)
    series: List[SeriesLink] = field(default_factory=list)
    nets: List[str] = field(default_factory=list)   # copper nets meshed; default [net]

    def __post_init__(self):
        if not self.nets:
            self.nets = [self.net]

    def terminal_pad_ids(self) -> List[str]:
        ids: List[str] = []
        for t in [*self.sources, *self.loads]:
            ids.extend(t.pad_ids)
        for s in self.series:
            ids.extend(s.pads_a)
            ids.extend(s.pads_b)
        return list(dict.fromkeys(ids))
