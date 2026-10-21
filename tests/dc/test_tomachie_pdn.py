# Portions Copyright (c) 2026 Tomachie LLC.
"""Importing a Tomachie power-tree file onto a board."""

import copy
import json

import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Stackup, Zone,
)
from kipi.dc.pi.importers.pdn_figures import dc_resistance
from kipi.dc.pi.importers.tomachie_pdn import (
    PdnImportError, TomachiePdnImporter, import_tomachie_pdn, path_to_rail,
)
from kipi.dc.pi.model import PISetup, validate
from kipi.dc.pi.tree import solve_tree
from tree_boards import B, F, T, board, by_name, pad, poly

PDN = {
    "schema": "tomachie-pdn", "schema_version": 1, "spec_revision": 2,
    "ground_nets": ["GND"],
    "rails": [
        {"name": "VIN", "voltage": {"nominal": 12.0, "min": "not_specified",
                                    "max": "not_specified", "basis": "net_name"},
         "source": {"role": "board_input", "refdes": "J1", "pins": ["J1_1"],
                    "output_net": "VIN", "output_path": []},
         "loads": [{"role": "regulator", "refdes": "U1", "pins": ["U1_1"],
                    "current": {"amps": 1.0, "basis": "rollup"}}],
         "offboard_pins": []},
        {"name": "+3V3", "voltage": {"nominal": 3.3, "min": 3.135, "max": 3.465,
                                     "basis": "user"},
         "source": {"role": "regulator", "refdes": "U1", "pins": ["U1_2"],
                    "output_net": "+3V3",
                    "output_path": [{"refdes": "R99", "class": "resistor", "value": "10k",
                                     "net_in": "+3V3", "net_out": "nRESET",
                                     "resistance_ohm": 10000.0, "datasheet_figures": []}]},
         "loads": [{"role": "ic", "refdes": "U2", "pins": ["U2_1"],
                    "current": {"amps": 1.0, "basis": "typ"},
                    "current_max": {"amps": 1.5, "basis": "max"},
                    "supply_min": {"volts": 3.0, "basis": "min", "spec": "vdd", "page": 4}}],
         "offboard_pins": []},
    ],
    "regulators": [{"refdes": "U1", "type": "linear", "efficiency": "unknown",
                    "input_rails": ["VIN"], "output_rails": ["+3V3"]}],
}


def run(pdn):
    return TomachiePdnImporter(copy.deepcopy(pdn), board()).run()


def test_sources_regulators_and_loads_map_onto_the_board():
    setup, notes = run(PDN)
    j1, u1 = by_name(setup.vrms, "J1 VIN"), by_name(setup.vrms, "U1 +3V3")
    assert (j1.kind, j1.voltage, j1.output.pins) == ("source", 12.0, ["1"])
    assert (u1.kind, u1.voltage, u1.input.net, u1.input.pins) == ("ldo", 3.3, "VIN", ["1"])
    u2 = by_name(setup.loads, "U2")
    # The typical is solved; the maximum is kept for a worst-case solve.
    assert (u2.current, u2.current_max) == (1.0, 1.5)
    assert any("kept for a worst-case solve" in n and "1 loads" in n for n in notes)
    # The datasheet minimum (3.0 V) is above the rail's (3.135 V)? No: the rail's is
    # tighter, so the rail's stated range is the tolerance, in % from nominal.
    assert (u2.tol_pct, u2.tol_min, u2.tol_max) == (True, pytest.approx(-5.0),
                                                     pytest.approx(5.0))
    # The regulator's own load entry is carried by its VRM input, not imported twice.
    assert [l.name for l in setup.loads] == ["U2"]
    assert validate(setup) == []
    assert any("R99" in n and "left out" in n for n in notes)


def test_imported_setup_solves():
    setup, _ = run(PDN)
    res = solve_tree(board(), setup)
    assert by_name(res.loads, "U2").passed is True


def test_unknown_current_and_off_board_pin_are_not_known():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["current"] = "unknown"
    del pdn["rails"][1]["loads"][0]["current_max"]
    pdn["rails"][1]["offboard_pins"] = ["U2_1"]
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 0.0
    assert by_name(setup.loads, "U2_1 (off-board)").current == 0.0
    assert any(n.startswith("Current not known, imported as 0 A") and "U2, U2_1 (off-board)"
               in n for n in notes)
    assert validate(setup) == []


def test_load_that_draws_what_it_drives_is_not_known():
    """Its own current is solved (the least it draws), and it stays not known."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0].update(load_dependent=True, load_dependent_reason="LED driver")
    setup, notes = run(pdn)
    u2 = by_name(setup.loads, "U2")
    assert (u2.current, u2.current_known) == (1.0, False)
    assert any("LED driver" in n for n in notes)
    assert any(n.startswith("Draw what they drive") for n in notes)
    pdn["rails"][1]["loads"][0].update(current="unknown")
    del pdn["rails"][1]["loads"][0]["current_max"]
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 0.0
    assert any(n.startswith("Current not known") for n in notes)


def test_source_pin_missing_from_the_board_leaves_out_what_it_powered():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][0]["source"].update(refdes="J5", pins=["J5_1"])   # no J5 on the board
    setup, notes = run(pdn)
    assert setup.vrms == [] and setup.loads == []
    assert any("J5_1 not on the board" in n for n in notes)
    assert any("U1 +3V3" in n and "left out" in n for n in notes)
    assert validate(setup) == []


def test_wrong_schema_is_refused():
    with pytest.raises(PdnImportError):
        TomachiePdnImporter({"schema": "other"}, board())


def test_path_to_rail_follows_the_chain_only():
    parts = [{"refdes": "L1", "net_in": "SW", "net_out": "+5V"},
             {"refdes": "R40", "net_in": "SW", "net_out": "FB"}]
    assert [e["refdes"] for e in path_to_rail(parts, "SW", "+5V")] == ["L1"]


@pytest.mark.parametrize("figs,expect", [
    ([{"spec": "dcr", "max": "0.080", "unit": "ohm"},
      {"spec": "impedance_at_frequency", "typ": "330", "unit": "ohm"}], (None, 0.08)),
    ([{"spec": "rds_on", "typ": "9.2", "max": "11", "unit": "mΩ"},
      {"spec": "rds_on", "typ": "14.7", "max": "18.5", "unit": "mΩ"}], (0.0147, 0.0185)),
    ([{"spec": "dc_resistance", "typ": "64", "max": "68", "unit": "mOhm"}], (0.064, 0.068)),
])
def test_dc_resistance_never_takes_an_impedance(figs, expect):
    typ, mx, _ = dc_resistance({"resistance_ohm": "unknown", "datasheet_figures": figs})
    assert typ == (None if expect[0] is None else pytest.approx(expect[0]))
    assert mx == pytest.approx(expect[1])


def test_regulator_quiescent_dropout_and_input_range():
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(
        quiescent_current={"amps": 6.5e-6, "basis": "typ"},
        dropout_voltage={"volts": 0.29, "basis": "max"},
        input_voltage_min={"volts": 1.6, "basis": "min"},
        input_voltage_max={"volts": 6.0, "basis": "max"})
    setup, _ = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.quiescent_a, u1.dropout_v, u1.vin_min, u1.vin_max) == (6.5e-6, 0.29, 1.6, 6.0)


def test_source_pin_named_by_pin_name_uses_the_parts_pads_on_its_output_net():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][0]["source"]["pins"] = ["J1_VBUS"]     # a pin name, not a pad number
    setup, notes = run(pdn)
    j1 = by_name(setup.vrms, "J1 VIN")
    assert (j1.output.ref, j1.output.net, j1.output.pins) == ("J1", "VIN", ["1"])
    assert any("J1_VBUS not on the board" in n for n in notes)
    assert any("J1's pads on its output net VIN" in n for n in notes)


def test_rail_whose_source_is_not_on_the_board_is_noted():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["source"].update(pins=[], output_net="NOWHERE")
    _, notes = run(pdn)
    assert any(n.startswith("Rail +3V3: regulator U1 output pins not found") for n in notes)


def _load_switch(**reg):
    """U1 as a power switch passing VIN (12 V) to its output rail."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["voltage"] = {"nominal": 12.0, "min": "not_specified",
                                  "max": "not_specified", "basis": "net_name"}
    pdn["rails"][1]["source"]["output_path"] = []
    pdn["regulators"][0] = {"refdes": "U1", "type": "load_switch", "type_basis": "schematic",
                            "input_rails": ["VIN"], "output_rails": ["+3V3"], **reg}
    return pdn


def test_load_switch_is_a_dc_connection_with_its_on_resistance():
    figs = [{"spec": "rds_on", "typ": "89", "max": "120", "unit": "mΩ", "page": 4}]
    setup, notes = run(_load_switch(datasheet_resistances=figs,
                                    quiescent_current={"amps": 9e-5, "basis": "typ"}))
    # Not a VRM: an LDO making 12 V from 12 V would be out of regulation.
    assert [v.name for v in setup.vrms] == ["J1 VIN"]
    u1 = by_name(setup.links, "U1")
    assert (u1.side_a.net, u1.side_a.pins, u1.side_b.net, u1.side_b.pins) == \
        ("VIN", ["1"], "+3V3", ["2"])
    assert u1.resistance == pytest.approx(0.089)
    assert u1.resistance_max == pytest.approx(0.120)          # kept for a worst-case solve
    # Its quiescent current is drawn at its input pins.
    iq = by_name(setup.loads, "U1 quiescent")
    assert (iq.supply.net, iq.current) == ("VIN", 9e-5)
    assert validate(setup) == []
    assert any("U1 (+3V3): load switch, imported as a DC connection" in n for n in notes)
    res = solve_tree(board(), setup)
    assert by_name(res.loads, "U2").passed is True


def test_load_switch_without_an_on_resistance_is_a_short_and_said():
    setup, notes = run(_load_switch())
    u1 = by_name(setup.links, "U1")
    assert u1.resistance == 0.0
    assert [l.name for l in setup.loads] == ["U2"]       # no quiescent current in the file
    assert any("U1: on-resistance not known; modeled as a short" in n for n in notes)


def filtered_board():
    """J1 -> VIN -> FB1 -> VIN_F -> U1 (switch) -> SWOUT -> FB2 -> +3V3 -> U2."""
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    spans = {"VIN": (0, 7), "VIN_F": (8, 10), "SWOUT": (12, 14), "+3V3": (15, 22)}
    pads = [pad("j1_1", "VIN", "J1", "1", box(0, 0, 1, 1)),
            pad("fb1_1", "VIN", "FB1", "1", box(6, 0, 7, 1)),
            pad("fb1_2", "VIN_F", "FB1", "2", box(8, 0, 9, 1)),
            pad("u1_1", "VIN_F", "U1", "1", box(9, 0, 10, 1)),
            pad("u1_2", "SWOUT", "U1", "2", box(12, 0, 13, 1)),
            pad("fb2_1", "SWOUT", "FB2", "1", box(13, 0, 14, 1)),
            pad("fb2_2", "+3V3", "FB2", "2", box(15, 0, 16, 1)),
            pad("u2_1", "+3V3", "U2", "1", box(21, 0, 22, 1)),
            pad("j1_2", "GND", "J1", "2", box(0, 0, 1, 1), B),
            pad("u1_3", "GND", "U1", "3", box(10, 0, 11, 1), B),
            pad("u2_2", "GND", "U2", "2", box(21, 0, 22, 1), B)]
    zones = [Zone(n, n, "", 0, {F: [poly(box(a, 0, b, 1))]}) for n, (a, b) in spans.items()]
    zones.append(Zone("gnd", "GND", "", 0, {B: [poly(box(0, 0, 22, 1))]}))
    nets = [Net(i + 1, n) for i, n in enumerate([*spans, "GND"])]
    return BoardSnapshot("filtered", "", st, nets, [], [], pads, [], zones, [])


def test_load_switch_behind_an_input_filter_with_a_part_on_its_output():
    ferrite = {"class": "ferrite", "resistance_ohm": "unknown",
               "datasheet_figures": [{"spec": "dcr", "max": "0.080", "unit": "ohm"},
                                     {"spec": "impedance_at_frequency", "typ": "330",
                                      "unit": "ohm"}]}
    pdn = _load_switch(datasheet_resistances=[{"spec": "rds_on", "typ": "0.089",
                                               "unit": "ohm"}])
    pdn["rails"][0]["loads"][0].update(
        input_net="VIN_F", input_filter=[dict(ferrite, refdes="FB1", net_in="VIN",
                                              net_out="VIN_F")])
    pdn["rails"][1]["source"].update(output_net="SWOUT", output_path=[
        dict(ferrite, refdes="FB2", net_in="SWOUT", net_out="+3V3")])
    setup, _ = TomachiePdnImporter(pdn, filtered_board()).run()
    links = {k.name: (k.side_a.net, k.side_b.net, k.resistance) for k in setup.links}
    assert links == {"U1": ("VIN_F", "SWOUT", pytest.approx(0.089)),
                     "FB1": ("VIN", "VIN_F", pytest.approx(0.08)),
                     "FB2": ("SWOUT", "+3V3", pytest.approx(0.08))}
    assert [l.name for l in setup.loads] == ["U2"]       # powered through all three
    assert by_name(solve_tree(filtered_board(), setup).loads, "U2").passed is True


def test_a_switchers_output_inductor_is_inside_its_regulation_loop():
    """A switcher regulates its rail after its output inductor (the feedback divider is on
    the rail), so the inductor's DCR lowers nothing: a short, said once. The same part
    after a load switch, which regulates nothing, is a real drop (test above)."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="switching", efficiency=0.9)
    pdn["rails"][0]["loads"][0].update(
        input_net="VIN_F", input_filter=[{"refdes": "FB1", "class": "ferrite",
                                          "net_in": "VIN", "net_out": "VIN_F",
                                          "resistance_ohm": 0.08, "datasheet_figures": []}])
    pdn["rails"][1]["source"].update(output_net="SWOUT", output_path=[
        {"refdes": "FB2", "class": "inductor", "net_in": "SWOUT", "net_out": "+3V3",
         "resistance_ohm": "unknown",
         "datasheet_figures": [{"spec": "dcr", "typ": "27", "max": "35", "unit": "mOhm"}]}])
    setup, notes = TomachiePdnImporter(pdn, filtered_board()).run()
    fb2 = by_name(setup.links, "FB2")
    assert (fb2.resistance, fb2.resistance_max) == (0.0, None)
    assert any(n.startswith("FB2: inside U1's regulation loop") for n in notes)
    assert by_name(setup.links, "FB1").resistance == pytest.approx(0.08)   # before U1: a drop


def test_datasheet_minimum_supply_tightens_the_rail_minimum():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["supply_min"]["volts"] = 3.2      # above the rail's 3.135 V
    setup, _ = run(pdn)
    u2 = by_name(setup.loads, "U2")
    assert (u2.tol_pct, u2.tol_min, u2.tol_max) == (False, 3.2, 3.465)


def test_minimum_current_and_only_a_maximum():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["current_min"] = {"amps": 0.2, "basis": "min"}
    setup, notes = run(pdn)
    assert any("minimum currents are not used" in n and "U2 (min 200 mA)" in n for n in notes)
    assert by_name(setup.loads, "U2").current_max == 1.5
    pdn["rails"][1]["loads"][0]["current"] = "unknown"
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 1.5
    assert any("U2: only a maximum current is known; 1.5 A is used" in n for n in notes)


def test_import_keeps_the_rest_of_the_setup(tmp_path):
    base = PISetup(html_report=True)
    base.settings.max_edge_mm = 0.7
    path = tmp_path / "board.pdn.json"
    path.write_text(json.dumps(PDN), encoding="utf-8")
    setup, _ = import_tomachie_pdn(path, board(), base)
    assert setup.html_report and setup.settings.max_edge_mm == 0.7
    assert [l.name for l in setup.loads] == ["U2"] and base.loads == []


def test_isolated_converter_is_a_switcher():
    """An isolated DC/DC converter (an RFM-0505S on a supply board) draws its output power over its
    efficiency from its input rail, like a switcher."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="isolated", efficiency=0.8)
    setup, notes = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.kind, u1.efficiency, u1.input.net) == ("smps", 0.8, "VIN")
    assert not any("not known whether linear" in n for n in notes)


def test_source_pin_on_another_net_leaves_the_rail_out():
    """A file naming one regulator pin as the source of two rails (one LDO pin for
    both its 1V8 output and a 0V9 rail) is wrong about one of them: that rail is left out and said, and the
    rest still solves."""
    pdn = copy.deepcopy(PDN)
    rail = copy.deepcopy(pdn["rails"][1])
    rail["name"] = "VREF"
    rail["source"]["output_net"] = "VREF"
    rail["loads"] = []
    pdn["rails"].append(rail)
    setup, notes = run(pdn)
    assert [v.name for v in setup.vrms if v.name.startswith("U1")] == ["U1 +3V3"]
    assert any(n.startswith("Rail VREF: the file names") and "already drive" in n
               for n in notes)
    assert validate(setup) == []


def test_a_new_setup_from_the_file_heats_the_board_with_its_loads():
    setup, notes = run(PDN)
    assert setup.thermal.load_power
    assert any(n.startswith("Thermal: load power is included") for n in notes)
    base = PISetup()
    base.thermal.load_power = False                     # the user's own choice stays
    kept, _ = TomachiePdnImporter(copy.deepcopy(PDN), board(), base).run()
    assert not kept.thermal.load_power


def test_worst_case_solves_the_maximum_current():
    """The worst-case corner solves each load at its maximum current (U2: 1.5 A, against
    1.0 A typical), so its drop grows by the same factor on this one-load rail."""
    setup, _ = run(PDN)
    typ = by_name(solve_tree(board(), setup).loads, "U2")
    setup.corner = "max"
    worst = by_name(solve_tree(board(), setup).loads, "U2")
    assert (typ.basis, worst.basis) == ("typ", "max")
    assert worst.current == pytest.approx(1.5)
    assert worst.drop > typ.drop


def test_current_not_known_gives_its_rail_a_headroom():
    """A load the file gives no current is solved at 0 A and marked not known. Its rail is
    judged at the known currents, and the headroom says how much such loads may add
    together before a load leaves its tolerance: margin / (volts lost per amp)."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["current"] = "unknown"
    pdn["rails"][1]["loads"][0].pop("current_max", None)
    setup, notes = run(pdn)
    u2 = by_name(setup.loads, "U2")
    assert (u2.current, u2.current_known) == (0.0, False)
    tree = solve_tree(board(), setup)
    res = by_name(tree.loads, "U2")
    assert (res.status, res.passed) == ("", True)          # judged at 0 A, not INCOMPLETE
    [h] = [h for h in tree.headroom if "U2" in h.unknown]
    assert h.tightest == "U2" and h.room_a > 0 and h.costliest == "U2"
    assert h.room_a == pytest.approx(h.margin_v / h.per_amp_v)
    assert res.unknown_room_a == pytest.approx(h.room_a)
    # Linear copper: drawing the room at U2 brings its lowest pin to its minimum.
    u2.current, u2.current_known = h.room_a, True
    at_room = by_name(solve_tree(board(), setup).loads, "U2")
    assert at_room.margin_low == pytest.approx(0.0, abs=1e-6)
    assert any("must stay under" in w and "U2" in w for w in tree.warnings)
    assert any("marked not known" in n for n in notes)


def test_off_board_load_is_imported_at_0_A_not_known():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["offboard_pins"] = ["U2_1"]
    setup, _ = run(pdn)
    off = by_name(setup.loads, "U2_1 (off-board)")
    assert (off.current, off.current_known) == (0.0, False)


@pytest.mark.parametrize("basis", ["signal_pin_only", "not_a_supply"])
def test_a_part_on_a_rail_by_no_supply_pin_is_not_a_load(basis):
    """A part on the rail only by a detect, enable or reference pin (basis signal_pin_only,
    not_a_supply in older files; 0 A) is left out of the loads and named once in the
    notes."""
    pdn = copy.deepcopy(PDN)
    detect = copy.deepcopy(pdn["rails"][1]["loads"][0])
    detect.update(refdes="U9", current={"amps": 0.0, "basis": basis})
    pdn["rails"][1]["loads"].append(detect)
    setup, notes = run(pdn)
    assert "U9" not in [l.name for l in setup.loads]
    assert any(n.startswith("On a rail by a detect") and "U9" in n for n in notes)


def test_loads_and_regulators_return_through_their_ground_pads():
    """Each load and VRM gets its pads on the file's ground net as its ground pins, so the
    return current flows in the ground copper rather than an ideal ground."""
    setup, _ = run(PDN)
    grounds = {x.name: x.ground for x in [*setup.vrms, *setup.loads]}
    assert all(g is not None and g.net == "GND" for g in grounds.values()), grounds


def test_a_ground_with_no_source_returns_to_an_ideal_ground():
    """A load returning on a ground net no source VRM references (an isolated converter's
    output ground) goes to an ideal ground, named once, so the setup still solves."""
    from kipi.dc.pi.model import validate
    pdn = copy.deepcopy(PDN)
    pdn["ground_nets"] = ["GND_ISO", "GND"]          # U2's own pads are on GND only
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").ground.net == "GND"
    snap = board()
    for p in snap.pads:
        if p.footprint == "U2" and p.net == "GND":
            p.net = "GND_ISO"
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), snap).run()
    assert by_name(setup.loads, "U2").ground is None
    assert any(n.startswith("No supply returns through GND_ISO") for n in notes)
    assert not validate(setup)


def test_a_regulator_standing_in_as_a_source_is_not_a_ground_reference():
    """A regulator of unknown type is modeled as an ideal source; its ground pins would
    pin the ground copper to 0 V there as well as at the board input, hiding the ground
    drop between them, so it gets none - the board input stays the one reference."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0]["type"] = "unknown"
    setup, notes = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.kind, u1.ground) == ("source", None)
    assert by_name(setup.vrms, "J1 VIN").ground.net == "GND"
    assert by_name(setup.loads, "U2").ground.net == "GND"


def test_a_series_diode_is_imported_with_its_forward_drop():
    """A diode ahead of a regulator (an input filter here) is a DC connection with its
    datasheet forward voltage, not a short; with none printed it stays a short, said."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="switching", efficiency=0.9)
    diode = {"refdes": "FB1", "class": "diode", "net_in": "VIN", "net_out": "VIN_F",
             "resistance_ohm": "unknown",
             "datasheet_figures": [{"spec": "v_forward", "max": "0.85", "unit": "V",
                                    "if": "5A", "page": 2}]}
    pdn["rails"][0]["loads"][0].update(input_net="VIN_F", input_filter=[diode])
    pdn["rails"][1]["source"].update(output_net="SWOUT", output_path=[])
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), filtered_board()).run()
    fb1 = by_name(setup.links, "FB1")
    assert (fb1.diode_vf, fb1.side_a.net, fb1.side_b.net) == (0.85, "VIN", "VIN_F")
    assert any(n.startswith("FB1: diode, forward drop 0.85 V") for n in notes)
    diode["datasheet_figures"] = []
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), filtered_board()).run()
    assert by_name(setup.links, "FB1").diode_vf == 0.0
    assert any("no forward voltage in the file" in n for n in notes)


def isolated_board():
    """board() with its ground split: GND under the input side (x 0..11), GND_ISO under
    the output side (x 12..22); U1 has a pad on each, U2 returns on GND_ISO."""
    from kipi.core.model.board import Net, Zone
    from tree_boards import B, pad, poly
    snap = board()
    snap.pads = [p for p in snap.pads if p.id != "u2_2"] + [
        pad("u1_4", "GND_ISO", "U1", "4", box(12, 0, 13, 1), B),
        pad("u2_2", "GND_ISO", "U2", "2", box(21, 0, 22, 1), B)]
    snap.zones = [z for z in snap.zones if z.net != "GND"] + [
        Zone("g1", "GND", "", 0, {B: [poly(box(0, 0, 11, 1))]}),
        Zone("g2", "GND_ISO", "", 0, {B: [poly(box(12, 0, 22, 1))]})]
    snap.nets = list(snap.nets) + [Net(len(snap.nets) + 1, "GND_ISO")]
    return snap


def test_an_isolated_converter_holds_its_output_ground_at_its_own_pins():
    """An isolated converter returns its input on its input rail's ground and holds its
    output rail's ground (isolated from it) at 0 V at its own pins there, so the loads
    behind it return through real copper, not an ideal ground."""
    from kipi.dc.pi.model import validate
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="isolated", efficiency=0.8)
    pdn["ground_nets"] = ["GND", "GND_ISO"]
    pdn["rails"][0]["ground"] = "GND"
    pdn["rails"][1]["ground"] = "GND_ISO"
    snap = isolated_board()
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), snap).run()
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.ground.net, u1.output_ground.net) == ("GND", "GND_ISO")
    assert by_name(setup.loads, "U2").ground.net == "GND_ISO"
    assert not validate(setup)
    assert not any("ideal ground" in n for n in notes)
    u2 = by_name(solve_tree(snap, setup).loads, "U2")
    assert u2.v_ground > 0                        # the return drop in GND_ISO's copper


def _behind_ferrite_pdn(load):
    return {"schema": "tomachie-pdn", "schema_version": 1, "spec_revision": 7,
            "ground_nets": ["GND"],
            "rails": [{"name": "VIN", "voltage": {"nominal": 12.0, "min": 11.0, "max": 13.0,
                                                  "basis": "user"},
                       "source": {"role": "board_input", "refdes": "J1", "pins": ["J1_1"],
                                  "output_net": "VIN", "output_path": []},
                       "loads": [load], "offboard_pins": []}],
            "regulators": []}


def test_a_load_behind_a_ferrite_is_reached_through_it():
    """U1's supply pin sits behind FB1 (input_net / input_filter): FB1 is a DC connection,
    so U1 is powered from VIN through it rather than disconnected."""
    fb1 = {"refdes": "FB1", "class": "ferrite", "net_in": "VIN", "net_out": "VIN_F",
           "resistance_ohm": "unknown",
           "datasheet_figures": [{"spec": "dcr", "max": "0.080", "unit": "ohm"}]}
    pdn = _behind_ferrite_pdn({"role": "ic", "refdes": "U1", "pins": ["U1_1"],
                               "current": {"amps": 0.2, "basis": "typ"},
                               "input_net": "VIN_F", "input_filter": [fb1]})
    setup, _ = TomachiePdnImporter(pdn, filtered_board()).run()
    assert {k.name: (k.side_a.net, k.side_b.net) for k in setup.links} == \
        {"FB1": ("VIN", "VIN_F")}
    assert by_name(solve_tree(filtered_board(), setup).loads, "U1").passed is True


def test_a_pull_resistor_returns_through_the_part_it_pulls():
    """FB1 standing in for a pull resistor from VIN to U1_1: its return is U1's ground
    pins, not the resistor's (it has none)."""
    pdn = _behind_ferrite_pdn({"role": "pull_resistor", "refdes": "FB1", "pins": ["FB1_1"],
                               "pull_to": "U1_1", "current": {"amps": 0.001, "basis": "v_over_r"}})
    setup, _ = TomachiePdnImporter(pdn, filtered_board()).run()
    load = by_name(setup.loads, "FB1")
    assert load.ground is not None and (load.ground.ref, load.ground.net) == ("U1", "GND")


def test_a_switched_path_is_a_source_series_parts_and_a_load():
    """switched_paths (spec revision 7): power carried from J1 through FB1 to U1's pin, at
    the designer's voltage and load current."""
    pdn = _behind_ferrite_pdn({"role": "ic", "refdes": "U2", "pins": ["U2_1"],
                               "current": {"amps": 0.1, "basis": "typ"}})
    pdn["rails"] = []
    pdn["switched_paths"] = [{
        "net": "VIN", "nets": ["VIN", "VIN_F"], "terminals": ["J1", "U1"],
        "in": {"net": "VIN", "terminals": ["J1"]}, "out": {"net": "VIN_F", "terminals": ["U1"]},
        "return": {"net": "GND", "terminals": ["J1", "U1"]},
        "switched_by": "FB1", "element": "relay", "contact_only": False, "parts": ["FB1"],
        "series": [{"refdes": "FB1", "class": "relay", "net_in": "VIN", "net_out": "VIN_F",
                    "resistance_ohm": "unknown",
                    "datasheet_figures": [{"spec": "contact_resistance", "max": "100",
                                           "unit": "m\u03a9"}]}],
        "voltage": {"nominal": 10.0, "min": 0.0, "max": 25.0, "basis": "user"},
        "load_current_max": {"amps": 2.5, "basis": "user"}}]
    setup, notes = TomachiePdnImporter(pdn, filtered_board()).run()
    assert [(v.output.net, v.voltage, v.kind) for v in setup.vrms] == [("VIN", 10.0, "source")]
    assert {k.name: (k.side_a.net, k.side_b.net, k.resistance) for k in setup.links} == \
        {"FB1": ("VIN", "VIN_F", pytest.approx(0.1))}
    load = setup.loads[0]
    assert (load.supply.net, load.current) == ("VIN_F", 2.5)
    # Both return on the path's return terminals: the source on the first, the load the last.
    assert (setup.vrms[0].ground.ref, load.ground.ref) == ("J1", "U1")
    assert any("Switched path VIN (FB1)" in n for n in notes)


def test_a_switched_path_with_no_stated_voltage_is_left_out():
    pdn = _behind_ferrite_pdn({"role": "ic", "refdes": "U2", "pins": ["U2_1"],
                               "current": {"amps": 0.1, "basis": "typ"}})
    pdn["switched_paths"] = [{"net": "VIN", "contact_only": True}]
    _, notes = TomachiePdnImporter(pdn, filtered_board()).run()
    assert any("contacts only" in n for n in notes)


def test_a_figure_named_for_another_pin_is_not_the_parts_resistance():
    """A load switch's FLAG FET (flg_on_resistance, 30 ohm) is not its switch (rds_on)."""
    from kipi.dc.pi.importers.pdn_figures import dc_resistance
    e = {"resistance_ohm": "unknown", "datasheet_figures": [
        {"spec": "flg_on_resistance", "typ": "30", "max": "60", "unit": "\u03a9"},
        {"spec": "rds_on", "typ": "75", "max": "95", "unit": "m\u03a9"}]}
    typ, mx, _ = dc_resistance(e)
    assert (typ, mx) == (pytest.approx(0.075), pytest.approx(0.095))


def test_a_regulators_rated_current_bounds_the_headroom():
    """max_output_current is the VRM's output current limit; the room for currents not
    known is the smaller of the copper's and what that rating leaves."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0]["max_output_current"] = {"amps": 0.3, "basis": "max"}
    pdn["rails"][1]["loads"][0]["current"] = "unknown"
    pdn["rails"][1]["loads"][0].pop("current_max", None)
    setup, _ = run(pdn)
    assert by_name(setup.vrms, "U1 +3V3").current_limit_a == pytest.approx(0.3)
    tree = solve_tree(board(), setup)
    [h] = [h for h in tree.headroom if "U2" in h.unknown]
    assert h.vrm_binds and h.binding_a == pytest.approx(0.3 - h.known_a)
    assert h.limited_by.startswith("U1 +3V3 rated")
    assert "is rated 300 mA" in h.sentence()
    assert by_name(tree.loads, "U2").unknown_room_a == pytest.approx(h.binding_a)
