"""KiPI's copper layer ids, which every board reader shares."""

import pytest

from kipi.core.model import layers


def test_ids_follow_the_stack():
    assert layers.copper_id(0, 4) == layers.F_CU
    assert layers.copper_id(1, 4) == layers.inner_cu(1)
    assert layers.copper_id(2, 4) == layers.inner_cu(2)
    assert layers.copper_id(3, 4) == layers.B_CU
    assert layers.copper_id(0, 1) == layers.F_CU
    with pytest.raises(ValueError):
        layers.copper_id(4, 4)


def test_names_round_trip():
    for lid in [layers.F_CU, layers.B_CU] + [layers.inner_cu(n) for n in range(1, 31)]:
        assert layers.id_from_name(layers.canonical_name(lid)) == lid
    assert layers.canonical_name(layers.inner_cu(7)) == "In7.Cu"
    assert layers.id_from_name("GND_PLANE") is None
    assert layers.id_from_name("In31.Cu") is None


def test_inner_range():
    assert layers.is_inner(layers.inner_cu(1)) and layers.is_inner(layers.inner_cu(30))
    assert not layers.is_inner(layers.F_CU) and not layers.is_inner(layers.B_CU)
    with pytest.raises(ValueError):
        layers.inner_cu(0)


def test_ids_are_kicads_board_layers():
    """Snapshots and results already saved use KiCad's ids, so KiPI's must stay equal."""
    pytest.importorskip("kipy")
    from kipy.proto.board.board_types_pb2 import BoardLayer
    assert layers.F_CU == BoardLayer.BL_F_Cu
    assert layers.B_CU == BoardLayer.BL_B_Cu
    for n in range(1, 31):
        assert layers.inner_cu(n) == getattr(BoardLayer, f"BL_In{n}_Cu")
