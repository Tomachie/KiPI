# Copyright (c) 2026 Tomachie LLC.
"""A diode in series (#161): a DC connection with a forward voltage, solved conducting as
its forward drop plus the current times its slope resistance."""

import sys
from pathlib import Path

import pytest

from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm
from kipi.dc.pi.tree import solve_tree

sys.path.insert(0, str(Path(__file__).parent))
from test_tomachie_pdn import filtered_board  # noqa: E402


def _setup(forward_v=None, forward_v_max=None):
    """12 V on VIN, a diode FB1 from VIN to VIN_F (50 mOhm slope), 1 A drawn on VIN_F."""
    s = PISetup()
    s.vrms = [Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source",
                  ground=PinRef("J1", "GND", ["2"]))]
    s.links = [DcLink("FB1", PinRef("FB1", "VIN", ["1"]), PinRef("FB1", "VIN_F", ["2"]), 0.05,
                      forward_v=forward_v, forward_v_max=forward_v_max)]
    s.loads = [Load("U1", PinRef("U1", "VIN_F", ["1"]), 1.0,
                    ground=PinRef("U1", "GND", ["3"]))]
    return s


def _load(setup):
    return next(l for l in solve_tree(filtered_board(), setup).loads if l.name == "U1")


def test_the_load_sits_one_forward_drop_lower():
    plain, diode = _load(_setup()), _load(_setup(forward_v=0.45))
    assert diode.v_supply == pytest.approx(plain.v_supply - 0.45, abs=1e-6)


def test_the_diode_current_and_loss():
    tree = solve_tree(filtered_board(), _setup(forward_v=0.45))
    link = next(s for g in tree.groups.values() for s in g.result.fields.series
                if s.name == "FB1")
    assert link.current == pytest.approx(1.0, rel=1e-6)
    assert link.power == pytest.approx(1.0 * (0.45 + 1.0 * 0.05), rel=1e-6)


def test_worst_case_takes_the_maximum_forward_voltage():
    s = _setup(forward_v=0.45, forward_v_max=0.55)
    typ = _load(s)
    s.corner = "max"
    assert _load(s).v_supply == pytest.approx(typ.v_supply - 0.10, abs=1e-6)


def test_a_diode_round_trips_through_the_setup_file():
    s = PISetup.from_dict(_setup(forward_v=0.45, forward_v_max=0.55).to_dict())
    assert (s.links[0].forward_v, s.links[0].forward_v_max) == (0.45, 0.55)
