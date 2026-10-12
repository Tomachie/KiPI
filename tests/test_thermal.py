"""Electro-thermal: resistivity vs temperature and the board heat model."""

import math

import numpy as np
import pytest
from shapely.geometry import box

from kipi.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Zone,
)
from kipi.solver import CurrentLoad, NetProblem, SolveSettings, VoltageSource, run
from kipi.solver.setup import RHO_CU_OHM_MM as RHO
from kipi.thermal.cosim import solve_electrothermal
from kipi.thermal.model import BoardThermalModel, ThermalSettings, K_CU_W_MM_K
from test_tree import F, B, T, board, setup, by_name, R

TCR = 0.00393


def test_hot_copper_is_more_resistive():
    """A uniformly 120 degC strip drops 1 + 0.00393 * 100 times more than at 20 degC."""
    snap = board()
    from test_tree import pad
    prob = lambda temp: NetProblem("VIN", [VoltageSource("J1", ["j1_1"], 12.0)],
                                   [CurrentLoad("U1", ["u1_1"], 1.0)], SolveSettings(max_edge_mm=0.3),
                                   temperature=temp)
    cold = run(snap, prob(None))
    hot = run(snap, prob(lambda xy: np.full(len(xy), 120.0)))
    drop = lambda r: 12.0 - r.fields.terminals[1].v_mean
    assert drop(cold) == pytest.approx(R(8), rel=1e-9)
    assert drop(hot) == pytest.approx(R(8) * (1 + TCR * 100), rel=1e-9)
    # Current density is the same (same current, same copper); loss scales with rho.
    assert hot.fields.summary["power_loss"] == pytest.approx(
        cold.fields.summary["power_loss"] * (1 + TCR * 100), rel=1e-9)


def plate(width=40.0, cu_t=0.035):
    """A bare square board fully covered by one copper layer (for the heat model)."""
    copper = [CopperLayer(F, "F.Cu", 0, cu_t, 1.5 + cu_t), CopperLayer(B, "B.Cu", 1, cu_t, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * cu_t)
    sq = box(0, 0, width, width)
    zone = Zone("z", "N", "", 0, {F: [Poly(list(sq.exterior.coords)[:-1])]})
    return BoardSnapshot("plate", "", st, [Net(1, "N")], [], [], [], [], [zone],
                         [Poly(list(sq.exterior.coords)[:-1])])


def test_uniform_heating_matches_convection_balance():
    """Spread heat uniformly: every cell sits at T_amb + q / (2 h)."""
    s = ThermalSettings(enabled=True, ambient_c=25.0, airflow_m_s=2.0, grid_mm=1.0)
    m = BoardThermalModel(plate(), s)
    heat = m.empty_heat()
    q = 1e-4                                         # W per mm^2 of board
    heat[m.inside] = q * m.d * m.d
    t = m.solve(heat)
    expected = 25.0 + q / (2 * s.h_w_mm2_k())
    assert np.nanmax(np.abs(t - expected)) < 1e-6
    assert s.h_w_mm2_k() == pytest.approx((10 + 3.8 * 2) * 1e-6)


def test_point_heat_spreads_and_total_balances():
    s = ThermalSettings(enabled=True, ambient_c=20.0, grid_mm=1.0)
    m = BoardThermalModel(plate(), s)
    heat = m.empty_heat()
    m.deposit(heat, np.array([[20.0, 20.0]]), np.array([0.5]))
    t = m.solve(heat)
    # Energy balance: all 0.5 W leaves by convection.
    out = np.nansum((t - 20.0) * m.g_conv)
    assert out == pytest.approx(0.5, rel=1e-9)
    # Hottest at the source, falling off with distance; copper spreads it widely.
    x, y = np.meshgrid(m.x, m.y)
    r = np.hypot(x - 20, y - 20)
    assert t[np.unravel_index(np.nanargmax(t), t.shape)] == pytest.approx(np.nanmax(t))
    assert np.nanmax(t[r < 2]) > np.nanmax(t[(r > 10) & (r < 12)]) > np.nanmax(t[r > 18]) > 20


def test_more_airflow_runs_cooler():
    temps = []
    for v in (0.0, 1.0, 3.0):
        m = BoardThermalModel(plate(), ThermalSettings(enabled=True, airflow_m_s=v, grid_mm=1.0))
        heat = m.empty_heat()
        m.deposit(heat, np.array([[20.0, 20.0]]), np.array([0.5]))
        temps.append(np.nanmax(m.solve(heat)))
    assert temps[0] > temps[1] > temps[2]


def test_electrothermal_cosim_heats_and_raises_drop():
    snap = board()
    s = setup("ldo", iq=0.01)
    s.loads[0].current = 5.0                       # enough loss to warm the strips
    cold = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1, grid_mm=0.5))
    hot = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=4, grid_mm=0.5))
    assert hot.thermal.t_max > 25.0
    assert len(hot.thermal.history) == 4
    # Iteration 1 is at ambient; later iterations see warmer, more resistive copper.
    assert by_name(hot.loads, "U2").drop > by_name(cold.loads, "U2").drop
    dts = [h["dt_max"] for h in hot.thermal.history]
    assert dts[-1] < dts[0]                         # settling
