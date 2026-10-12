"""Solve a whole power tree defined in a :class:`~kipi.pi.model.PISetup`.

Each net group (nets joined by DC connections) is one FEM problem driven by its single
VRM. Groups are solved downstream first: a VRM's input current depends on what its
output delivers, and its input pins are a load on the group feeding it.

* LDO:       I_in = I_out + I_q
* Switcher:  I_in = V_out * I_out / (efficiency * V_in) + I_q, where V_in is the voltage
             at the VRM's input pins, which depends on I_in, so the feeding group is
             iterated to a fixed point (the factorized system is reused).
* Remote sense: the VRM's output is corrected until the sense pins sit at the set point.
  With ground sense, the set point is held above the ground sense pins instead of 0 V;
  their voltage comes from the ground solve, so rails and grounds are iterated together.
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
from kipi.pi.model import PISetup, PinRef, Vrm, net_groups, validate
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
    passed: Optional[bool]               # judged on the worst pin
    v_worst: float = 0.0                 # lowest supply pin minus highest ground pin
    pin_groups: List[tuple] = field(default_factory=list)  # (name, A, V mean, V min)

    @property
    def drop_pct(self) -> float:
        return 100.0 * self.drop / self.nominal if self.nominal else 0.0

    @property
    def drop_worst(self) -> float:
        return self.nominal - self.v_worst

    @property
    def drop_worst_pct(self) -> float:
        return 100.0 * self.drop_worst / self.nominal if self.nominal else 0.0


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
    v_ground_sense: Optional[float] = None   # at the ground sense pins, if any

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


def _max_v(sol, pad_ids: List[str]) -> float:
    return float(max(sol.node_v[next(iter(sol.mesh.pad_nodes[p].values()))[0]]
                     for p in pad_ids))


def _group_load_name(load, index: int) -> str:
    g = load.groups[index]
    return f"{load.name} [{g.name or ','.join(g.pins)}]"


def _input_current(v: Vrm, v_out: float, i_out: float, v_in: float) -> float:
    if v.kind == "ldo":
        return i_out + v.quiescent_a
    if v.kind == "smps":
        if v_in <= 0:
            raise SolveError(f"VRM {v.name}: input voltage collapsed to {v_in:.3f} V")
        return v_out * i_out / (v.efficiency * v_in) + v.quiescent_a
    return 0.0


def solve_tree(snap: BoardSnapshot, setup: PISetup,
               progress: ProgressReporter = NULL, temperature=None,
               mesh_cache: Optional[dict] = None) -> TreeResult:
    """Solve every rail and ground group. ``temperature`` (optional) maps KiCad xy (mm) to
    copper temperature in degC for temperature-dependent resistivity. ``mesh_cache`` (a
    dict) lets repeated solves of the same setup reuse their meshes."""
    t0 = time.perf_counter()
    errors = validate(setup)
    if errors:
        raise SolveError("Setup problems:\n- " + "\n- ".join(errors))
    pads = _Pads(snap)
    groups = net_groups(setup)
    s = setup.settings
    out = TreeResult()

    driver = {groups[v.output.net]: v for v in setup.vrms}
    pad_count: Dict[str, int] = {}
    for p in snap.pads:
        pad_count[p.net] = pad_count.get(p.net, 0) + 1

    def label_of(g) -> str:
        """Short display name: the group's busiest net, plus how many nets it joins."""
        main = max(sorted(g), key=lambda n: pad_count.get(n, 0))
        return main if len(g) == 1 else f"{main} (+{len(g) - 1} net{'s' if len(g) > 2 else ''})"
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
        prob = NetProblem(label, sources, loads, s, series=links_in(g), nets=nets,
                          temperature=temperature)
        key = (tuple(nets), tuple(sorted(prob.terminal_pad_ids())), s.max_edge_mm, s.grading,
               s.min_edge_mm, s.circle_quad_segs, s.simplify_mm, s.min_angle_deg)
        mesh = mesh_cache.get(key) if mesh_cache is not None else None
        if mesh is None:
            with progress.timed(f"Meshing {label}"):
                mesh = mesh_nets(snap, nets, prob.terminal_pad_ids(), name=label,
                                 max_edge_mm=s.max_edge_mm, min_angle_deg=s.min_angle_deg,
                                 simplify_mm=s.simplify_mm, circle_quad_segs=s.circle_quad_segs,
                                 min_edge_mm=s.min_edge_mm, grading=s.grading,
                                 progress=progress)
            if mesh_cache is not None:
                mesh_cache[key] = mesh
        return prob, mesh, LinearSystem(snap, mesh, prob, progress)

    built = {}                                   # group -> (mesh, system), reused per pass

    def system_for(g, sources, loads, label):
        if g not in built:
            _, mesh, system = build(g, sources, loads, label)
            built[g] = (mesh, system)
        return built[g]

    v_out: Dict[str, float] = {}
    i_out: Dict[str, float] = {}
    v_in: Dict[str, float] = {}
    i_in: Dict[str, float] = {}
    v_sense: Dict[str, float] = {}
    # Ground-sensing VRMs regulate against their ground sense pins, whose voltage comes
    # from the ground solve, which depends on the rails' currents: iterate both to a
    # fixed point (passes after the first only re-solve the factorized systems).
    v_gsense: Dict[str, float] = {v.name: 0.0 for v in setup.vrms if v.ground_sense}
    rail_solutions = {}
    ground_solutions = {}
    ground_groups = []
    for v in setup.vrms:
        if v.ground and groups[v.ground.net] not in ground_groups:
            ground_groups.append(groups[v.ground.net])
    for l in setup.loads:
        if l.ground and groups[l.ground.net] not in ground_groups:
            ground_groups.append(groups[l.ground.net])

    for tree_pass in range(1, MAX_TREE_ITERATIONS + 1):
        solved = []                              # (label, nets, kind, driver, mesh, sol, it)
        pass_warnings: List[str] = []
        suffix = f" (pass {tree_pass})" if tree_pass > 1 else ""

        # ------------------------------------------------------------ rails
        for gi, g in enumerate(order):
            progress.check_cancel()
            vrm = driver[g]
            label = label_of(g)
            progress.stage(f"Rail {gi + 1}/{len(order)}: {label} (from {vrm.name}){suffix}")
            loads = []
            for l in setup.loads:
                if l.supply.net not in g:
                    continue
                if not l.groups:
                    loads.append(CurrentLoad(l.name, pads(l.supply), l.current))
                    continue
                for gi, grp in enumerate(l.groups):
                    ref = PinRef(l.supply.ref, l.supply.net, list(grp.pins))
                    loads.append(CurrentLoad(_group_load_name(l, gi), pads(ref), grp.current))
            children = fed_by.get(g, [])
            child_load = {c.name: f"{c.name} (input)" for c in children}
            for c in children:
                loads.append(CurrentLoad(child_load[c.name], pads(c.input), 0.0))
            sense_pads = pads(vrm.sense) if vrm.sense else []
            if sense_pads:
                loads.append(CurrentLoad(f"{vrm.name} (sense)", sense_pads, 0.0))
            src_pads = pads(vrm.output)
            mesh, system = system_for(g, [VoltageSource(vrm.name, src_pads, vrm.voltage)],
                                      loads, label)

            # The VRM holds (sense or output pins) - (ground sense pins) at its set point.
            target = vrm.voltage + v_gsense.get(vrm.name, 0.0)
            v_src = target
            vin_est = {c.name: v_in.get(c.name, vrm.voltage) for c in children}
            sol = None
            for it in range(1, MAX_TREE_ITERATIONS + 1):
                currents = {child_load[c.name]: _input_current(c, v_out[c.name],
                                                               i_out[c.name], vin_est[c.name])
                            for c in children}
                sol = system.solve({vrm.name: v_src}, currents, progress=NULL)
                change = 0.0
                for c in children:
                    new = _mean_v(sol, pads(c.input))
                    change = max(change, abs(new - vin_est[c.name]))
                    vin_est[c.name] = new
                if sense_pads:
                    vs = _mean_v(sol, sense_pads)
                    step = target - vs
                    v_src += step
                    change = max(change, abs(step))
                if change < V_TOL:
                    break
            else:
                pass_warnings.append(f"{label}: power-tree iteration did not converge "
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
            solved.append((label, sorted(g), "rail", vrm.name, mesh, sol, it))
            pass_warnings.extend(f"{label}: {w}" for w in sol.warnings)
            rail_solutions[g] = sol

        # ------------------------------------------------------------ grounds
        for g in ground_groups:
            progress.check_cancel()
            label = label_of(g)
            progress.stage(f"Ground: {label}{suffix}")
            refs = [v for v in setup.vrms
                    if v.kind == "source" and v.ground and v.ground.net in g]
            sources = [VoltageSource(f"{v.name} (gnd)", pads(v.ground), 0.0) for v in refs]
            loads = [CurrentLoad(f"{l.name} (return)", pads(l.ground), -l.total_current())
                     for l in setup.loads if l.ground and l.ground.net in g]
            currents = {}
            for v in setup.vrms:
                if v.ground and v.ground.net in g and v.kind != "source":
                    # Output current comes back in, input current leaves to its own source.
                    currents[f"{v.name} (gnd)"] = i_out[v.name] - i_in[v.name]
                    loads.append(CurrentLoad(f"{v.name} (gnd)", pads(v.ground), 0.0))
                if v.ground_sense and v.ground_sense.net in g:
                    loads.append(CurrentLoad(f"{v.name} (gnd sense)", pads(v.ground_sense),
                                             0.0))
            mesh, system = system_for(g, sources, loads, label)
            sol = system.solve(currents=currents, progress=NULL)
            solved.append((label, sorted(g), "ground", ", ".join(v.name for v in refs),
                           mesh, sol, 1))
            pass_warnings.extend(f"{label}: {w}" for w in sol.warnings)
            ground_solutions[g] = sol

        change = 0.0
        for v in setup.vrms:
            if v.ground_sense:
                new = _mean_v(ground_solutions[groups[v.ground.net]], pads(v.ground_sense))
                change = max(change, abs(new - v_gsense[v.name]))
                v_gsense[v.name] = new
        if change < V_TOL:
            break
    else:
        pass_warnings.append(f"Ground-sense iteration did not converge "
                             f"(last change {change:.2e} V)")
    if tree_pass > 1:
        progress.info(f"Ground sense: converged in {tree_pass} power-tree passes")
    for label, nets, kind, drv, mesh, sol, it in solved:
        out.groups[label] = GroupResult(label, nets, kind, drv,
                                        NetResult(mesh, sol, compute_fields(sol)), it)
    out.warnings.extend(pass_warnings)

    # ---------------------------------------------------------------- reports
    for l in setup.loads:
        g = groups[l.supply.net]
        sol = rail_solutions[g]
        if l.groups:
            sp = [pid for grp in l.groups
                  for pid in pads(PinRef(l.supply.ref, l.supply.net, list(grp.pins)))]
        else:
            sp = pads(l.supply)
        vs, vmin = _mean_v(sol, sp), _min_v(sol, sp)
        vg = vg_max = None
        if l.ground:
            gsol = ground_solutions[groups[l.ground.net]]
            gp = pads(l.ground)
            vg, vg_max = _mean_v(gsol, gp), _max_v(gsol, gp)
        eff = vs - (vg or 0.0)
        worst = vmin - (vg_max or 0.0)
        nominal = driver[g].voltage
        drop = nominal - eff
        passed = None
        if l.tolerance_pct is not None and nominal:
            passed = 100.0 * (nominal - worst) / nominal <= l.tolerance_pct
        pin_groups = []
        for gi, grp in enumerate(l.groups):
            gp_ids = pads(PinRef(l.supply.ref, l.supply.net, list(grp.pins)))
            pin_groups.append((grp.name or ",".join(grp.pins), grp.current,
                               _mean_v(sol, gp_ids), _min_v(sol, gp_ids)))
        out.loads.append(LoadReport(l.name, label_of(g), l.total_current(), nominal, vs, vmin,
                                    vg, eff, drop, l.tolerance_pct, passed, worst, pin_groups))
    for v in setup.vrms:
        p_out = v_out[v.name] * i_out[v.name]
        vi, ii = v_in.get(v.name), i_in.get(v.name)
        out.vrms.append(VrmReport(v.name, v.kind, label_of(groups[v.output.net]), v.voltage,
                                  v_out[v.name], v_sense.get(v.name), i_out[v.name], vi, ii,
                                  p_out, None if vi is None else vi * ii,
                                  v_gsense.get(v.name)))
    out.solve_s = time.perf_counter() - t0
    progress.info(f"Power tree solved: {len(order)} rail(s), {len(ground_groups)} ground(s) "
                  f"in {out.solve_s:.2f} s")
    return out
