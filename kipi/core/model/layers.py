"""KiPI's copper layer ids.

Every board reader numbers copper layers the same way, so setups, snapshots and results
mean the same layer whatever file or tool the board came from. The values are KiCad's
``BoardLayer`` enum (F.Cu = 3, In1.Cu .. In30.Cu = 4 .. 33, B.Cu = 34), which every
snapshot and results file written so far already uses.
"""

from __future__ import annotations

import re
from typing import Optional

F_CU = 3
B_CU = 34
MAX_INNER = 30


def inner_cu(n: int) -> int:
    """The id of In<n>.Cu, ``n`` from 1 to 30."""
    if not 1 <= n <= MAX_INNER:
        raise ValueError(f"inner copper layer In{n}.Cu is out of range (1..{MAX_INNER})")
    return F_CU + n


def is_inner(layer_id: int) -> bool:
    return F_CU < layer_id < B_CU


def copper_id(index: int, n_layers: int) -> int:
    """The id of the copper layer at ``index`` (0 = top) in a stack of ``n_layers``, for
    readers whose files only give the stack order."""
    if not 0 <= index < n_layers:
        raise ValueError(f"layer index {index} is outside a {n_layers}-layer stack")
    if index == 0:
        return F_CU
    if index == n_layers - 1:
        return B_CU
    return inner_cu(index)


def canonical_name(layer_id: int) -> str:
    """KiCad's name for the layer ("F.Cu", "In2.Cu", "B.Cu")."""
    if layer_id == F_CU:
        return "F.Cu"
    if layer_id == B_CU:
        return "B.Cu"
    if is_inner(layer_id):
        return f"In{layer_id - F_CU}.Cu"
    return str(layer_id)


_INNER_NAME = re.compile(r"In(\d+)\.Cu$")


def id_from_name(name: str) -> Optional[int]:
    """The id of a canonical copper layer name, or None for any other name."""
    if name == "F.Cu":
        return F_CU
    if name == "B.Cu":
        return B_CU
    m = _INNER_NAME.match(name)
    if m and 1 <= int(m.group(1)) <= MAX_INNER:
        return inner_cu(int(m.group(1)))
    return None
