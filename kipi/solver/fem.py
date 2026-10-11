"""2.5D DC conduction: P1 finite elements on each copper sheet, plated barrels between
layers, voltage sources and current loads on pads.

On a copper sheet of thickness t and resistivity rho the potential obeys
div(G_s grad V) = 0 with sheet conductance G_s = t / rho (siemens per square). A
linear triangle contributes G_s / (4A) * (b b^T + c c^T), where b, c are the usual
shape-function coefficients. A barrel segment between two layers is a resistor of
rho * dz / (pi * (d * tp - tp^2)).

Equipotential regions (a drill ring on one layer, a terminal pad) are merged into a
single unknown, which is exact for "perfect conductor" boundaries and keeps the
matrix well conditioned (no huge penalty conductances). Sources are eliminated as
Dirichlet unknowns, so the reduced system stays symmetric positive definite.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components

from kipi.mesh.mesher import NetMesh
from kipi.model.board import BoardSnapshot
from kipi.progress import NULL, ProgressReporter
from kipi.solver.setup import NetProblem

DIRECT_SOLVE_MAX_UNKNOWNS = 30_000


class SolveError(RuntimeError):
    pass


@dataclass
class BarrelElement:
    barrel: int        # index into mesh.barrels
    layer_a: int
    layer_b: int
    node_a: int        # merged node ids
    node_b: int
    g: float           # siemens


@dataclass
class Solution:
    problem: NetProblem
    mesh: NetMesh
    node_v: np.ndarray                  # (N,) volts per mesh node; NaN on floating copper
    merged_of: np.ndarray               # (N,) mesh node -> merged unknown
    v_merged: np.ndarray                # (Nm,) volts per merged unknown
    sheet_g: Dict[int, float]           # layer -> sheet conductance (S/square)
    barrel_elements: List[BarrelElement]
    source_current: Dict[str, float]    # A delivered by each source
    stats: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def triangle_geometry(points: np.ndarray, tris: np.ndarray):
    """Shape-function coefficients b, c (M,3) and signed doubled area (M,)."""
    x = points[tris, 0]
    y = points[tris, 1]
    b = np.stack([y[:, 1] - y[:, 2], y[:, 2] - y[:, 0], y[:, 0] - y[:, 1]], axis=1)
    c = np.stack([x[:, 2] - x[:, 1], x[:, 0] - x[:, 2], x[:, 1] - x[:, 0]], axis=1)
    two_a = (x * b).sum(axis=1)
    return b, c, two_a


def _merge_groups(n: int, groups: List[np.ndarray]) -> Tuple[np.ndarray, int]:
    """Union equipotential node groups. Returns (merged id per node, count)."""
    rows, cols = [], []
    for g in groups:
        if len(g) > 1:
            rows.append(np.full(len(g) - 1, g[0]))
            cols.append(g[1:])
    if not rows:
        return np.arange(n), n
    r = np.concatenate(rows)
    c = np.concatenate(cols)
    graph = sp.coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n))
    count, labels = connected_components(graph, directed=False)
    return labels, count


def assemble(snap: BoardSnapshot, mesh: NetMesh, problem: NetProblem):
    s = problem.settings
    st = snap.stackup
    sheet_g = {lid: st.layer(lid).thickness_mm / s.rho_ohm_mm for lid in mesh.layers}

    # Equipotential groups: drill rings per layer and terminal pads (all their layers).
    groups = [nodes for b in mesh.barrels for nodes in b.ring_nodes.values()]
    pad_groups = {pid: np.concatenate(list(per_layer.values()))
                  for pid, per_layer in mesh.pad_nodes.items()}
    groups.extend(pad_groups.values())
    merged_of, nm = _merge_groups(mesh.n_nodes, groups)

    b, c, two_a = triangle_geometry(mesh.points, mesh.tris)
    area = np.abs(two_a) / 2
    if np.any(area <= 0):
        raise SolveError("Degenerate triangles in mesh")
    gs = np.array([sheet_g[l] for l in mesh.tri_layer]) if len(set(mesh.tri_layer)) > 1 \
        else np.full(len(mesh.tris), sheet_g[mesh.tri_layer[0]])
    coef = gs / (4 * area)
    ke = coef[:, None, None] * (b[:, :, None] * b[:, None, :] + c[:, :, None] * c[:, None, :])
    m = merged_of[mesh.tris]
    rows = np.repeat(m, 3, axis=1).ravel()
    cols = np.tile(m, (1, 3)).ravel()
    data = ke.ravel()

    # Barrels: resistors between ring unknowns on consecutive present layers.
    order = {cu.id: i for i, cu in enumerate(st.copper)}
    elements: List[BarrelElement] = []
    tp = s.plating_mm
    for bi, bar in enumerate(mesh.barrels):
        layers = sorted(bar.ring_nodes, key=order.get)
        d = bar.drill_mm
        a_barrel = math.pi * (d * tp - tp * tp) if d > 2 * tp else math.pi * d * d / 4
        for la, lb in zip(layers[:-1], layers[1:]):
            dz = abs(st.layer(la).z_mid_mm - st.layer(lb).z_mid_mm)
            g = a_barrel / (s.rho_ohm_mm * max(dz, 1e-6))
            na = merged_of[bar.ring_nodes[la][0]]
            nb = merged_of[bar.ring_nodes[lb][0]]
            if na == nb:
                continue   # e.g. a terminal PTH pad already merged across layers
            elements.append(BarrelElement(bi, la, lb, int(na), int(nb), g))
    if elements:
        ea = np.array([e.node_a for e in elements])
        eb = np.array([e.node_b for e in elements])
        eg = np.array([e.g for e in elements])
        rows = np.concatenate([rows, ea, eb, ea, eb])
        cols = np.concatenate([cols, ea, eb, eb, ea])
        data = np.concatenate([data, eg, eg, -eg, -eg])

    K = sp.coo_matrix((data, (rows, cols)), shape=(nm, nm)).tocsr()
    K.sum_duplicates()
    pad_unknown = {pid: int(merged_of[nodes[0]]) for pid, nodes in pad_groups.items()}
    return K, merged_of, nm, sheet_g, elements, pad_unknown


# ---------------------------------------------------------------------------
# Solve
# ---------------------------------------------------------------------------

def _solve_spd(A: sp.csr_matrix, rhs: np.ndarray, s, progress: ProgressReporter, stats: dict):
    n = A.shape[0]
    if n <= DIRECT_SOLVE_MAX_UNKNOWNS:
        from scipy.sparse.linalg import spsolve
        progress.info(f"Direct solve ({n} unknowns)")
        progress.fraction(None)
        x = spsolve(A.tocsc(), rhs)
        stats["method"] = "direct"
        stats["iterations"] = 0
    else:
        import pyamg
        t = time.perf_counter()
        progress.info(f"AMG setup ({n} unknowns, {A.nnz} non-zeros)...")
        ml = pyamg.smoothed_aggregation_solver(A, symmetry="hermitian", max_coarse=500)
        progress.info(f"AMG hierarchy: {len(ml.levels)} levels, operator complexity "
                      f"{ml.operator_complexity():.2f} ({time.perf_counter() - t:.2f} s)")
        residuals: List[float] = []
        state = {"last": 0.0}
        log_target = math.log10(max(s.tol, 1e-16))

        def cb(_x):
            progress.check_cancel()
            if not residuals:
                return
            now = time.perf_counter()
            r = residuals[-1] / max(residuals[0], 1e-300)
            if now - state["last"] > 0.25:
                state["last"] = now
                progress.info(f"  CG iteration {len(residuals) - 1}: relative residual {r:.2e}")
                progress.fraction(min(1.0, math.log10(max(r, 1e-300)) / log_target))

        x = ml.solve(rhs, tol=s.tol, maxiter=s.max_iter, accel="cg", callback=cb,
                     residuals=residuals)
        rel = residuals[-1] / max(residuals[0], 1e-300) if residuals else 0.0
        stats["method"] = "amg-cg"
        stats["iterations"] = len(residuals) - 1
        stats["relative_residual"] = rel
        progress.info(f"CG converged in {len(residuals) - 1} iterations, "
                      f"relative residual {rel:.2e}")
        if rel > s.tol * 100:
            from scipy.sparse.linalg import spsolve
            progress.warn("Iterative solve did not converge; falling back to a direct solve")
            x = spsolve(A.tocsc(), rhs)
            stats["method"] = "direct (fallback)"
    return np.asarray(x)


def solve(snap: BoardSnapshot, mesh: NetMesh, problem: NetProblem,
          progress: ProgressReporter = NULL) -> Solution:
    if not problem.sources:
        raise SolveError(f"Net {problem.net} needs at least one voltage source")
    t0 = time.perf_counter()
    stats: Dict[str, float] = {}
    warnings: List[str] = []

    with progress.timed("Assembling"):
        K, merged_of, nm, sheet_g, elements, pad_unknown = assemble(snap, mesh, problem)
        progress.info(f"{nm} unknowns after merging pads and drill rings, {K.nnz} non-zeros, "
                      f"{len(elements)} barrel segments")
    stats.update(n_nodes=mesh.n_nodes, n_tris=mesh.n_tris, n_unknowns=nm, nnz=K.nnz)

    # Boundary conditions.
    fixed: Dict[int, float] = {}
    for src in problem.sources:
        for pid in src.pad_ids:
            u = pad_unknown[pid]
            if u in fixed and abs(fixed[u] - src.voltage) > 1e-12:
                raise SolveError(f"Pad of source {src.name} is shorted to another source "
                                 "at a different voltage")
            fixed[u] = src.voltage
    f = np.zeros(nm)
    for load in problem.loads:
        share = load.current / len(load.pad_ids)
        for pid in load.pad_ids:
            u = pad_unknown[pid]
            if u in fixed:
                warnings.append(f"Load {load.name} sits on a source pad; it draws no copper current")
            f[u] -= share

    # Copper not connected to any source cannot be solved (and carries no current).
    ncomp, comp = connected_components(K, directed=False)
    live_comps = {comp[u] for u in fixed}
    live = np.isin(comp, list(live_comps))
    for load in problem.loads:
        if not all(live[pad_unknown[pid]] for pid in load.pad_ids):
            srcs = ", ".join(s.name for s in problem.sources)
            raise SolveError(f"Load {load.name} has no copper path to the source ({srcs}) on "
                             f"net {problem.net}. If KiCad shows them connected, the link is "
                             "probably still unrouted (ratsnest) or goes through an unfilled "
                             "zone; refill zones and reload.")
    n_float = int((~live).sum())
    if n_float:
        floating_nodes = int((~live[merged_of]).sum())
        warnings.append(f"{ncomp - len(live_comps)} copper island(s) ({floating_nodes} mesh "
                        "nodes) are not connected to a source and were left out")

    # Solve for u = V - v_ref rather than V itself. Drops are ~1e-4 of the rail voltage,
    # so with absolute volts the source term would dominate the right-hand side and a
    # relative residual tolerance would say little about the drop. With the offset the
    # right-hand side is the load currents (plus any source-to-source differences).
    v_ref = max(fixed.values())
    is_fixed = np.zeros(nm, dtype=bool)
    u_fixed = np.zeros(nm)
    for u, v in fixed.items():
        is_fixed[u] = True
        u_fixed[u] = v - v_ref
    free = np.flatnonzero(live & ~is_fixed)
    fixed_idx = np.flatnonzero(is_fixed)
    A = K[free][:, free].tocsr()
    rhs = f[free] - K[free][:, fixed_idx] @ u_fixed[fixed_idx]

    with progress.timed("Solving"):
        x = _solve_spd(A, rhs, problem.settings, progress, stats) if len(free) else np.zeros(0)

    u_all = np.full(nm, np.nan)
    u_all[fixed_idx] = u_fixed[fixed_idx]
    u_all[free] = x
    v = u_all + v_ref
    node_v = v[merged_of]

    # Current each source delivers = net current flowing out of its fixed unknowns.
    # K annihilates constants on each connected piece, so using u avoids cancellation.
    flow = K @ np.nan_to_num(u_all) - f
    source_current = {}
    for src in problem.sources:
        us = {pad_unknown[pid] for pid in src.pad_ids}
        source_current[src.name] = float(sum(flow[u] for u in us))

    stats["solve_s"] = time.perf_counter() - t0
    for w in warnings:
        progress.warn(w)
    return Solution(problem=problem, mesh=mesh, node_v=node_v, merged_of=merged_of,
                    v_merged=v, sheet_g=sheet_g, barrel_elements=elements,
                    source_current=source_current, stats=stats, warnings=warnings)
