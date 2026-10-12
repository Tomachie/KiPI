"""Power-tree solves against closed-form results."""

import math

import pytest
from shapely.geometry import box

from kipi.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Zone,
)
from kipi.pi.model import DcLink, Load, PinRef, PISetup, Vrm, net_groups, validate
from kipi.pi.tree import solve_tree
from kipi.solver.fem import SolveError
from kipi.solver.setup import RHO_CU_OHM_MM as RHO, SolveSettings

F, B = 3, 34
T = 0.035


def poly(g):
    return Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])


def pad(pid, net, ref, num, g, layer=F):
    return Pad(pid, net, ref, num, (g.centroid.x, g.centroid.y), "smd", shapes={layer: [poly(g)]})


def R(length):
    """Resistance of a 1 mm wide strip of this length."""
    return RHO * length / (1.0 * T)


def board():
    """VIN: J1.1 -> U1.1 on F (x 0..10).  +3V3: U1.2 -> U2.1 on F (x 12..22).
    GND on B (x 0..22): J1.2 at 0..1, U1.3 at 10..11, U2.2 at 21..22."""
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [pad("j1_1", "VIN", "J1", "1", box(0, 0, 1, 1)),
            pad("u1_1", "VIN", "U1", "1", box(9, 0, 10, 1)),
            pad("u1_2", "+3V3", "U1", "2", box(12, 0, 13, 1)),
            pad("u2_1", "+3V3", "U2", "1", box(21, 0, 22, 1)),
            pad("j1_2", "GND", "J1", "2", box(0, 0, 1, 1), B),
            pad("u1_3", "GND", "U1", "3", box(10, 0, 11, 1), B),
            pad("u2_2", "GND", "U2", "2", box(21, 0, 22, 1), B)]
    zones = [Zone("a", "VIN", "", 0, {F: [poly(box(0, 0, 10, 1))]}),
             Zone("b", "+3V3", "", 0, {F: [poly(box(12, 0, 22, 1))]}),
             Zone("c", "GND", "", 0, {B: [poly(box(0, 0, 22, 1))]})]
    nets = [Net(1, "VIN"), Net(2, "+3V3"), Net(3, "GND")]
    return BoardSnapshot("tree", "", st, nets, [], [], pads, [], zones, [])


def setup(kind="ldo", eff=0.9, iq=0.0, sense=False, ground=False, gsense=False):
    src = Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source",
              ground=PinRef("J1", "GND", ["2"]) if ground else None)
    reg = Vrm("U1", PinRef("U1", "+3V3", ["2"]), 3.3, kind, efficiency=eff, quiescent_a=iq,
              input=PinRef("U1", "VIN", ["1"]),
              sense=PinRef("U2", "+3V3", ["1"]) if sense else None,
              ground=PinRef("U1", "GND", ["3"]) if ground else None,
              ground_sense=PinRef("U2", "GND", ["2"]) if gsense else None)
    load = Load("U2", PinRef("U2", "+3V3", ["1"]), 1.0,
                ground=PinRef("U2", "GND", ["2"]) if ground else None, tolerance_pct=1.0)
    return PISetup([src, reg], [load], [], SolveSettings(max_edge_mm=0.3))


def by_name(items, name):
    return next(x for x in items if x.name == name)


def test_ldo_chain():
    res = solve_tree(board(), setup("ldo", iq=0.01))
    u1, j1 = by_name(res.vrms, "U1"), by_name(res.vrms, "J1")
    assert u1.i_out == pytest.approx(1.0, rel=1e-9)
    assert u1.i_in == pytest.approx(1.01, rel=1e-9)
    assert j1.i_out == pytest.approx(1.01, rel=1e-9)
    assert u1.v_in == pytest.approx(12.0 - 1.01 * R(8), rel=1e-9)
    ld = by_name(res.loads, "U2")
    assert ld.v_supply == pytest.approx(3.3 - 1.0 * R(8), rel=1e-9)
    assert ld.passed is True


def test_switcher_input_current_follows_input_voltage():
    eff = 0.8
    res = solve_tree(board(), setup("smps", eff=eff))
    u1 = by_name(res.vrms, "U1")
    p_out = u1.v_out * u1.i_out
    r = R(8)
    # I_in (12 - r I_in) = P_out / eff  ->  r I^2 - 12 I + P/eff = 0
    i_in = (12 - math.sqrt(144 - 4 * r * p_out / eff)) / (2 * r)
    assert u1.i_in == pytest.approx(i_in, rel=1e-8)
    assert u1.p_in == pytest.approx(p_out / eff, rel=1e-8)
    assert by_name(res.vrms, "J1").i_out == pytest.approx(i_in, rel=1e-8)


def test_remote_sense_regulates_at_the_sense_pins():
    res = solve_tree(board(), setup("ldo", sense=True))
    u1 = by_name(res.vrms, "U1")
    assert u1.v_sense == pytest.approx(3.3, abs=1e-9)
    assert u1.v_out == pytest.approx(3.3 + 1.0 * R(8), rel=1e-9)
    assert by_name(res.loads, "U2").v_supply == pytest.approx(3.3, abs=1e-9)


def test_ground_return_drop():
    res = solve_tree(board(), setup("ldo", iq=0.01, ground=True))
    ld = by_name(res.loads, "U2")
    # Ground: 1.01 A from U1's pad back to J1 over 9 mm, then 1 A from U2 to U1 over 10 mm.
    v_u1_gnd = 1.01 * R(9)
    assert ld.v_ground == pytest.approx(v_u1_gnd + 1.0 * R(10), rel=1e-9)
    assert ld.v_effective == pytest.approx(ld.v_supply - ld.v_ground, abs=1e-15)
    assert "GND" in res.groups and res.groups["GND"].kind == "ground"


def test_ground_sense_regulates_against_the_load_ground():
    res = solve_tree(board(), setup("ldo", iq=0.01, sense=True, ground=True, gsense=True))
    u1, ld = by_name(res.vrms, "U1"), by_name(res.loads, "U2")
    v_gnd = 1.01 * R(9) + 1.0 * R(10)
    assert u1.v_ground_sense == pytest.approx(v_gnd, rel=1e-9)
    assert u1.v_out == pytest.approx(3.3 + 1.0 * R(8) + v_gnd, rel=1e-9)
    assert ld.v_effective == pytest.approx(3.3, abs=1e-9)
    assert ld.drop == pytest.approx(0.0, abs=1e-9)


def test_ground_sense_without_positive_sense():
    res = solve_tree(board(), setup("ldo", iq=0.01, ground=True, gsense=True))
    u1 = by_name(res.vrms, "U1")
    assert u1.v_sense is None
    assert u1.v_out == pytest.approx(3.3 + 1.01 * R(9) + 1.0 * R(10), rel=1e-9)


def test_ground_sense_with_switcher_converges():
    # A switcher's ground current depends on its output voltage, which depends on the
    # ground sense voltage: the rails and grounds must be iterated together.
    res = solve_tree(board(), setup("smps", eff=0.8, sense=True, ground=True, gsense=True))
    assert not any("converge" in w for w in res.warnings)
    assert by_name(res.loads, "U2").v_effective == pytest.approx(3.3, abs=1e-8)


def test_validation_rules():
    s = setup()
    assert validate(s) == []
    # Joining VIN and +3V3 with a DC connection puts two VRMs on one group.
    s.links.append(DcLink("R9", PinRef("U1", "VIN", ["1"]), PinRef("U1", "+3V3", ["2"]), 0.1))
    errs = validate(s)
    assert any("Only one VRM" in e for e in errs)
    assert net_groups(s)["VIN"] == net_groups(s)["+3V3"]
    s = setup()
    s.vrms[1].input = None
    assert any("needs its input pins" in e for e in validate(s))
    s = setup()
    s.vrms[0].kind = "ldo"
    s.vrms[0].input = PinRef("U2", "+3V3", ["1"])          # J1 fed from U1's output: a loop
    assert any("loop" in e for e in validate(s))
    s = setup(ground=True)
    s.vrms[0].ground = None                                # ground without a reference
    assert any("needs a reference" in e for e in validate(s))
    with pytest.raises(SolveError, match="Setup problems"):
        solve_tree(board(), s)
    s = setup(ground=True, gsense=True)
    assert validate(s) == []
    s.vrms[1].ground = None
    assert any("ground sense needs" in e for e in validate(s))
    s = setup(ground=True, gsense=True)
    s.vrms[1].ground_sense = PinRef("U2", "+3V3", ["1"])
    assert any("ground sense pins must be on" in e for e in validate(s))


def test_setup_json_round_trip(tmp_path):
    s = setup(kind="smps", eff=0.85, sense=True, ground=True, gsense=True)
    s.links.append(DcLink("FB1", PinRef("U2", "+3V3", ["1"]), PinRef("U2", "+3V3", ["1"]), 0.05))
    from kipi.pi.model import PinGroup
    s.loads[0].groups = [PinGroup(["1"], 0.4, "a")]
    s.settings.rho_by_layer = {"In1.Cu": 1.9e-5}
    s.settings.tcr_per_c = 0.0038
    back = PISetup.load(s.save(tmp_path / "x.kipi-setup.json"))
    assert back == s


def test_resistivity_must_be_positive():
    s = setup()
    s.settings.rho_by_layer = {"F.Cu": 0.0}
    assert any("positive on F.Cu" in e for e in validate(s))
    s.settings.rho_by_layer = {}
    s.settings.rho_ohm_mm = -1.0
    assert any("resistivity must be positive" in e for e in validate(s))


def board_two_pin():
    """board() plus a second +3V3 pin on U2 halfway along the strip (x 16..17)."""
    b = board()
    b.pads.append(pad("u2_3", "+3V3", "U2", "3", box(16, 0, 17, 1)))
    b.invalidate_index()
    return b


def test_per_pin_group_currents():
    from kipi.pi.model import PinGroup
    s = setup("ldo")
    s.loads[0] = Load("U2", PinRef("U2", "+3V3", []), 0.0, tolerance_pct=0.05,
                      groups=[PinGroup(["3"], 0.3, "mid"), PinGroup(["1"], 0.7, "end")])
    res = solve_tree(board_two_pin(), s)
    ld = by_name(res.loads, "U2")
    assert ld.current == pytest.approx(1.0)
    v_mid = 3.3 - 1.0 * R(3)              # all 1 A flows from U1.2 (x=13) to the mid pin (x=16)
    v_end = v_mid - 0.7 * R(4)            # 0.7 A continues to the end pin (x=21)
    groups = {g[0]: g for g in ld.pin_groups}
    assert groups["mid"][2] == pytest.approx(v_mid, rel=1e-9)
    assert groups["end"][2] == pytest.approx(v_end, rel=1e-9)
    assert ld.v_worst == pytest.approx(v_end, rel=1e-9)
    assert ld.drop_worst > ld.drop                      # mean hides the worst pin
    assert ld.passed == (100 * (3.3 - v_end) / 3.3 <= 0.05)
    assert by_name(res.vrms, "U1").i_out == pytest.approx(1.0, rel=1e-9)

    # The same total split equally gives a different (and wrong for this part) answer.
    s2 = setup("ldo")
    s2.loads[0] = Load("U2", PinRef("U2", "+3V3", []), 1.0)
    eq = by_name(solve_tree(board_two_pin(), s2).loads, "U2")
    assert eq.v_worst == pytest.approx(v_mid - 0.5 * R(4), rel=1e-9)


def test_pin_group_validation():
    from kipi.pi.model import PinGroup
    s = setup()
    s.loads[0].groups = [PinGroup(["1"], 0.5), PinGroup(["1"], 0.5)]
    assert any("more than one group" in e for e in validate(s))
    s.loads[0].groups = [PinGroup([], 0.5)]
    assert any("has no pins" in e for e in validate(s))
    s.loads[0].supply = PinRef("U2", "+3V3", ["1"])
    s.loads[0].groups = [PinGroup(["3"], 0.5)]
    assert any("not among its supply pins" in e for e in validate(s))
