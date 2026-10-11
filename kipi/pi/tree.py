"""Solve a whole power tree defined in a :class:`~kipi.pi.model.PISetup`.

Each net group (nets joined by DC connections) is one FEM problem driven by its single
VRM. Groups are solved downstream first: a VRM's input current depends on what its
output delivers, and its input pins are a load on the group feeding it.

* LDO:       I_in = I_out + I_q
* Switcher:  I_in = V_out * I_out / (efficiency * V_in) + I_q, where V_in is the voltage
             at the VRM's input pins, which depends on I_in, so the feeding group is
             iterated to a fixed point (the factorized system is reused).
* Remote sense: the VRM's output is corrected until the sense pins sit at the set point.
* Ground: groups used as ground are referenced to 0 V at the source VRM's ground pins.
  Loads return their current at their ground pins; every other VRM returns
  I_in - I_out at its ground pins.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from kipi.mesh.mesher import mesh_nets
from kipi.model.board import BoardSnapshot
from kipi.pi.model import PISetup, PinRef, Vrm, group_name, net_groups, validate
from kipi.progress import NULL, ProgressReporter
from kipi.solver import NetResult, pads_for
from kipi.solver.fem import LinearSystem, SolveError
from kipi.solver.post import compute_fields
from kipi.solver.setup import CurrentLoad, NetProblem, SeriesLink, VoltageSource

MAX_TREE_ITERATIONS = 50
V_TOL = 1e-9


@dataclass
class GroupResult:
    name: str
    nets: List[str]
    kind: str                 # "rail" or "ground"
    driver: str               # VRM name (rail) or reference VRM name(s) (ground)
    result: NetResult
    iterations: int = 1


@dataclass
class LoadReport:
    name: str
    group: str
    current: float
    nominal: float            # set point of the VRM driving the group
    v_supply: float           # mean over the supply pins
    v_supply_min: float
    v_ground: Optional[float]  # mean over the ground pins (None if not modelled)
    v_effective: float        # supply - ground
    drop: float               # nominal - effective, volts
    tolerance_pct: Optional[float]
    passed: Optional[bool]

    @property
    def drop_pct(self) -> float:
        return 100.0 * self.drop / self.nominal if self.nominal else 0.0


@dataclass
class VrmReport:
    name: str
    kind: str
    group: str
    v_set: float
    v_out: float              # voltage applied at the output pins (after sense correction)
    v_sense: Optional[float]
    i_out: float
    v_in: Optional[float]
    i_in: Optional[float]
    p_out: float
    p_in: Optional[float]

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


class _Pads:
    def __init__(self, snap: BoardSnapshot):
        self.snap = snap

    def __call__(self, ref: PinRef) -> List[str]:
        return [p.id for p in pads_for(self.snap, ref.net, ref.ref, ref.pins or None)]


def _mean_v(sol, pad_ids: List[str]) -> float:
    vals = [sol.node_v[next(iter(sol.mesh.pad_nodes[p].values()))[0]] for p in pad_ids]
    return float(np.mean(vals))


def _min_v(sol, pad_ids: List[str]) -> float:
    return float(min(sol.node_v[next(iter(sol.mesh.pad_nodes[p].values()))[0]]
                     for p in pad_ids))


def _input_current(v: Vrm, v_out: float, i_out: float, v_in: float) -> float:
    if v.kind == "ldo":
        return i_out + v.quiescent_a
    if v.kind == "smps":
        if v_in <= 0:
            raise SolveError(f"VRM {v.name}: input voltage collapsed to {v_in:.3f} V")
        return v_out * i_out / (v.efficiency * v_in) + v.quiescent_a
    return 0.0


def solve_tree(snap: BoardSnapshot, setup: PISetup,
               progress: ProgressReporter = NULL) -> TreeResult:
    t0 = time.perf_counter()
    errors = validate(setup)
    if errors:
        raise SolveError("Setup problems:\n- " + "\n- ".join(errors))
    pads = _Pads(snap)
    groups = net_groups(setup)
    s = setup.settings
    out = TreeResult()

    driver = {groups[v.output.net]: v for v in setup.vrms}
    fed_by: Dict[frozenset, List[Vrm]] = {}      # rail group -> VRMs whose input is in it
    for v in setup.vrms:
        if v.input:
            fed_by.setdefault(groups[v.input.net], []).append(v)

    # Downstream first: a group is solved after every group its VRM inputs feed.
    order: List[frozenset] = []
    seen = set()

    def visit(g):
        if g in seen:
            return
        seen.add(g)
        for child in fed_by.get(g, []):
            visit(groups[child.output.net])
        order.append(g)
    for g in driver:
        visit(g)

    def links_in(g):
        return [SeriesLink(k.name, pads(k.side_a), pads(k.side_b), k.resistance)
                for k in setup.links if k.side_a.net in g]

    def build(g, sources, loads, label):
        nets = sorted(g)
        prob = NetProblem(label, sources, loads, s, series=links_in(g), nets=nets)
        with progress.timed(f"Meshing {label}"):
            mesh = mesh_nets(snap, nets, prob.terminal_pad_ids(), name=label,
                             max_edge_mm=s.max_edge_mm, min_angle_deg=s.min_angle_deg,
                             simplify_mm=s.simplify_mm, circle_quad_segs=s.circle_quad_segs,
                             min_edge_mm=s.min_edge_mm, grading=s.grading, progress=progress)
        return prob, mesh, LinearSystem(snap, mesh, prob, progress)

    v_out: Dict[str, float] = {}
    i_out: Dict[str, float] = {}
    v_in: Dict[str, float] = {}
    i_in: Dict[str, float] = {}
    v_sense: Dict[str, float] = {}
    rail_solutions = {}

    # ---------------------------------------------------------------- rails
    for gi, g in enumerate(order):
        progress.check_cancel()
        vrm = driver[g]
        label = group_name(g)
        progress.stage(f"Rail {gi + 1}/{len(order)}: {label} (from {vrm.name})")
        loads = [CurrentLoad(l.name, pads(l.supply), l.current)
                 for l in setup.loads if l.supply.net in g]
        children = fed_by.get(g, [])
        child_load = {c.name: f"{c.name} (input)" for c in children}
        for c in children:
            loads.append(CurrentLoad(child_load[c.name], pads(c.input), 0.0))
        sense_pads = pads(vrm.sense) if vrm.sense else []
        if sense_pads:
            loads.append(CurrentLoad(f"{vrm.name} (sense)", sense_pads, 0.0))
        src_pads = pads(vrm.output)
        prob, mesh, system = build(g, [VoltageSource(vrm.name, src_pads, vrm.voltage)], loads,
                                   label)

        v_src = vrm.voltage
        vin_est = {c.name: vrm.voltage for c in children}
        sol = None
        for it in range(1, MAX_TREE_ITERATIONS + 1):
            currents = {child_load[c.name]: _input_current(c, v_out[c.name], i_out[c.name],
                                                           vin_est[c.name])
                        for c in children}
            sol = system.solve({vrm.name: v_src}, currents, progress=NULL)
            change = 0.0
            for c in children:
                new = _mean_v(sol, pads(c.input))
                change = max(change, abs(new - vin_est[c.name]))
                vin_est[c.name] = new
            if sense_pads:
                vs = _mean_v(sol, sense_pads)
                step = vrm.voltage - vs
                v_src += step
                change = max(change, abs(step))
            if change < V_TOL:
                break
        else:
            out.warnings.append(f"{label}: power-tree iteration did not converge "
                                f"(last change {change:.2e} V)")
        if sense_pads:
            sol = system.solve({vrm.name: v_src}, currents, progress=NULL)
            v_sense[vrm.name] = _mean_v(sol, sense_pads)
        progress.info(f"{label}: {it} iteration(s), {vrm.name} supplies "
                      f"{sol.source_current[vrm.name] * 1e3:.2f} mA at {v_src:.5f} V")

        v_out[vrm.name] = v_src
        i_out[vrm.name] = sol.source_current[vrm.name]
        for c in children:
            v_in[c.name] = vin_est[c.name]
            i_in[c.name] = currents[child_load[c.name]]
        fields = compute_fields(sol)
        out.groups[label] = GroupResult(label, sorted(g), "rail", vrm.name,
                                        NetResult(mesh, sol, fields), it)
        out.warnings.extend(f"{label}: {w}" for w in sol.warnings)
        rail_solutions[g] = sol

    # ---------------------------------------------------------------- grounds
    ground_groups = []
    for v in setup.vrms:
        if v.ground and groups[v.ground.net] not in ground_groups:
            ground_groups.append(groups[v.ground.net])
    for l in setup.loads:
        if l.ground and groups[l.ground.net] not in ground_groups:
            ground_groups.append(groups[l.ground.net])
    ground_solutions = {}
    for g in ground_groups:
        progress.check_cancel()
        label = group_name(g)
        progress.stage(f"Ground: {label}")
        refs = [v for v in setup.vrms if v.kind == "source" and v.ground and v.ground.net in g]
        sources = [VoltageSource(f"{v.name} (gnd)", pads(v.ground), 0.0) for v in refs]
        loads = [CurrentLoad(f"{l.name} (return)", pads(l.ground), -l.current)
                 for l in setup.loads if l.ground and l.ground.net in g]
        for v in setup.vrms:
            if v.ground and v.ground.net in g and v.kind != "source":
                # Output current comes back in, input current leaves to its own source.
                loads.append(CurrentLoad(f"{v.name} (gnd)", pads(v.ground),
                                         i_out[v.name] - i_in[v.name]))
        prob, mesh, system = build(g, sources, loads, label)
        sol = system.solve(progress=NULL)
        out.groups[label] = GroupResult(label, sorted(g), "ground",
                                        ", ".join(v.name for v in refs),
                                        NetResult(mesh, sol, compute_fields(sol)))
        out.warnings.extend(f"{label}: {w}" for w in sol.warnings)
        ground_solutions[g] = sol

    # ---------------------------------------------------------------- reports
    for l in setup.loads:
        g = groups[l.supply.net]
        sol = rail_solutions[g]
        sp = pads(l.supply)
        vs, vmin = _mean_v(sol, sp), _min_v(sol, sp)
        vg = _mean_v(ground_solutions[groups[l.ground.net]], pads(l.ground)) if l.ground else None
        eff = vs - (vg or 0.0)
        nominal = driver[g].voltage
        drop = nominal - eff
        passed = None
        if l.tolerance_pct is not None and nominal:
            passed = 100.0 * drop / nominal <= l.tolerance_pct
        out.loads.append(LoadReport(l.name, group_name(g), l.current, nominal, vs, vmin, vg,
                                    eff, drop, l.tolerance_pct, passed))
    for v in setup.vrms:
        p_out = v_out[v.name] * i_out[v.name]
        vi, ii = v_in.get(v.name), i_in.get(v.name)
        out.vrms.append(VrmReport(v.name, v.kind, group_name(groups[v.output.net]), v.voltage,
                                  v_out[v.name], v_sense.get(v.name), i_out[v.name], vi, ii,
                                  p_out, None if vi is None else vi * ii))
    out.solve_s = time.perf_counter() - t0
    progress.info(f"Power tree solved: {len(order)} rail(s), {len(ground_groups)} ground(s) "
                  f"in {out.solve_s:.2f} s")
    return out
