"""The AC values of a Tomachie power-tree file (spec revision 9)."""

import copy

import pytest

from kipi.ac.model import CapModel
from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.model import PISetup
from test_tomachie_pdn import PDN, run
from test_tomachie_pdn_budget import _cap_board, _caps_pdn
from tree_boards import by_name


def _ac_pdn():
    pdn = _caps_pdn()
    pdn["spec_revision"] = 9
    ac22 = {"capacitance_f": 22e-6, "rated_v": 6.3, "dielectric": "X5R", "tolerance_pct": 20,
            "esr": {"ohm": 0.004, "basis": "estimated", "from": "typical ceramic ESR"}}
    for c in pdn["rails"][1]["decoupling"]:
        c["ac"] = ac22
    pdn["rails"][1]["loads"][0]["local_caps"][0]["ac"] = {
        "capacitance_f": 1e-7, "rated_v": "unknown", "dielectric": "C0G(NP0)",
        "tolerance_pct": "unknown", "esr": "unknown"}
    pdn["rails"][1]["loads"][0]["ac"] = {"i_step": {"amps": 0.5, "basis": "estimated"}}
    pdn["regulators"][0]["ac"] = {"f_sw": {"hz": 1e6, "basis": "datasheet", "page": 5},
                                  "bandwidth": {"hz": 1e5, "basis": "estimated",
                                                "from": "switching frequency / 10"}}
    return pdn


def test_capacitor_models_take_esr_rating_dielectric_and_tolerance():
    setup, notes = TomachiePdnImporter(_ac_pdn(), _cap_board()).run()
    caps = {m.name: m for m in setup.caps}
    m = caps["PN22U"]
    assert (m.esr_ohm, m.rated_v, m.dielectric, m.mfg_tolerance_pct) == (0.004, 6.3, "X5R", 20)
    assert m.aging_pct_per_decade == pytest.approx(3.0)
    # "unknown" stays unknown; the decoder's C0G(NP0) is KiPI's C0G.
    n = caps["PN100N"]
    assert (n.esr_ohm, n.rated_v, n.dielectric, n.mfg_tolerance_pct) == (0.0, None, "C0G", None)
    assert any("ESR from the file for 1 (1 of them estimates)" in x and "1 have no ESR" in x
               for x in notes)


def test_the_users_capacitor_values_are_kept():
    base = PISetup(caps=[CapModel("PN22U", 20e-6, 0.0, 0.003, ["C1"], rated_v=10.0)])
    setup, _ = TomachiePdnImporter(_ac_pdn(), _cap_board(), base).run()
    m = {x.name: x for x in setup.caps}["PN22U"]
    assert (m.esr_ohm, m.rated_v, m.dielectric) == (0.003, 10.0, "X5R")


def test_vrm_takes_its_bandwidth_and_says_when_estimated():
    setup, notes = run(_ac_pdn())
    assert by_name(setup.vrms, "U1 +3V3").bandwidth_khz == pytest.approx(100.0)
    assert any("VRM bandwidth (AC): estimated for U1 (switching frequency / 10)" in n
               for n in notes)


def test_ic_load_gets_a_probe_without_a_target():
    setup, notes = run(_ac_pdn())
    p = by_name(setup.probes, "U2")
    assert (p.rail.net, p.ground.net, p.target_ohm, p.target_hz) == ("+3V3", "GND", None, None)
    assert any("Probes (AC): 1 added" in n and "U2 (step 500 mA, estimated)" in n
               for n in notes)


def test_a_file_without_ac_values_imports_as_before():
    setup, notes = run(PDN)
    assert setup.probes == [] and by_name(setup.vrms, "U1 +3V3").bandwidth_khz is None
    assert not any("(AC): estimated" in n or "Probes (AC)" in n for n in notes)


def test_an_existing_probe_is_kept():
    from kipi.ac.model import Probe
    from kipi.dc.pi.model import PinRef
    mine = Probe("U2", PinRef("U2", "+3V3", ["1"]), PinRef("U2", "GND", ["2"]), 0.05, 1e6)
    pdn = copy.deepcopy(_ac_pdn())
    setup, _ = TomachiePdnImporter(pdn, _cap_board(), PISetup(probes=[mine])).run()
    assert [p.target_ohm for p in setup.probes] == [0.05]


def test_capacitors_without_a_part_number_group_by_value_and_esl():
    pdn = _ac_pdn()
    for c in pdn["rails"][1]["decoupling"]:
        c.pop("part_number")
    setup, _ = TomachiePdnImporter(pdn, _cap_board()).run()
    m = {x.name: x for x in setup.caps}["22uF/6.3V, 0.4 nH (no part number)"]
    assert (m.refs, m.esr_ohm, m.capacitance_f) == (["C1", "C2"], 0.004, pytest.approx(22e-6))


def test_without_ac_values_a_capacitor_needs_its_part_number():
    pdn = copy.deepcopy(_caps_pdn())
    pdn["rails"][1]["decoupling"][0].pop("part_number")
    setup, _ = TomachiePdnImporter(pdn, _cap_board()).run()
    assert {x.name: x.refs for x in setup.caps}["PN22U"] == ["C2"]
