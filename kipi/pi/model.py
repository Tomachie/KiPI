"""The user's power-integrity setup: VRMs, loads and DC connections.

Everything here is entered by the user; nothing is inferred from the board:

* **DC connections** (series resistors, ferrites, fuses, load switches...) join nets.
  Nets joined this way form one *group* that is simulated as a single problem.
* **VRMs** drive one group from their output pins. A group may have only one VRM.
  A VRM's input pins (LDO / switcher) make it a load on the group feeding it; that is
  how the power tree is defined.
* **Loads** draw a current from their supply pins, optionally returning it through
  ground pins.

Pins are referenced by footprint reference + pad numbers on a net, so a setup
survives re-extracting the board from KiCad (pad ids are not stable across sessions).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from kipi.solver.setup import SolveSettings

FORMAT_VERSION = 1
VRM_KINDS = ("source", "ldo", "smps")


@dataclass
class PinRef:
    """Pads of footprint ``ref`` on ``net``; an empty ``pins`` list means all of them."""
    ref: str
    net: str
    pins: List[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.ref}[{','.join(self.pins)}]" if self.pins else self.ref


@dataclass
class Vrm:
    name: str
    output: PinRef
    voltage: float
    kind: str = "source"                 # "source" (ideal supply), "ldo", "smps"
    efficiency: float = 0.9              # switchers: P_in = P_out / efficiency
    quiescent_a: float = 0.0             # extra input current (LDO / switcher)
    input: Optional[PinRef] = None       # required for "ldo" and "smps"
    sense: Optional[PinRef] = None       # remote sense: regulate the voltage here
    ground: Optional[PinRef] = None      # ground pins (enables ground-net simulation)


@dataclass
class Load:
    name: str
    supply: PinRef
    current: float
    ground: Optional[PinRef] = None
    tolerance_pct: Optional[float] = None   # pass if the drop is within this % of nominal


@dataclass
class DcLink:
    name: str
    side_a: PinRef
    side_b: PinRef
    resistance: float                       # ohms; 0 = short


@dataclass
class PISetup:
    vrms: List[Vrm] = field(default_factory=list)
    loads: List[Load] = field(default_factory=list)
    links: List[DcLink] = field(default_factory=list)
    settings: SolveSettings = field(default_factory=SolveSettings)

    # ------------------------------------------------------------------ JSON
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["format_version"] = FORMAT_VERSION
        return d

    @staticmethod
    def from_dict(d: dict) -> "PISetup":
        if d.get("format_version", 1) > FORMAT_VERSION:
            raise ValueError("KiPI setup file is from a newer version of KiPI")

        def pin(x):
            return PinRef(x["ref"], x["net"], list(x.get("pins", []))) if x else None

        known = {f.name for f in dataclasses.fields(SolveSettings)}
        return PISetup(
            vrms=[Vrm(name=v["name"], output=pin(v["output"]), voltage=v["voltage"],
                      kind=v.get("kind", "source"), efficiency=v.get("efficiency", 0.9),
                      quiescent_a=v.get("quiescent_a", 0.0), input=pin(v.get("input")),
                      sense=pin(v.get("sense")), ground=pin(v.get("ground")))
                  for v in d.get("vrms", [])],
            loads=[Load(name=l["name"], supply=pin(l["supply"]), current=l["current"],
                        ground=pin(l.get("ground")), tolerance_pct=l.get("tolerance_pct"))
                   for l in d.get("loads", [])],
            links=[DcLink(name=k["name"], side_a=pin(k["side_a"]), side_b=pin(k["side_b"]),
                          resistance=k["resistance"]) for k in d.get("links", [])],
            settings=SolveSettings(**{k: v for k, v in d.get("settings", {}).items()
                                      if k in known}),
        )

    def save(self, path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(path) -> "PISetup":
        return PISetup.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def setup_path(board_file_or_name: str, project_dir: str) -> Path:
    """Where a board's setup lives: ``<project dir>/<board stem>.kipi-setup.json``."""
    stem = Path(board_file_or_name).stem
    return Path(project_dir or ".") / f"{stem}.kipi-setup.json"


# ---------------------------------------------------------------------------
# Groups of nets joined by DC connections
# ---------------------------------------------------------------------------

def net_groups(setup: PISetup, extra_nets=()) -> Dict[str, frozenset]:
    """Map every net mentioned in the setup to its group (nets joined by DC links)."""
    parent: Dict[str, str] = {}

    def find(n):
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a, b):
        parent[find(a)] = find(b)

    nets = set(extra_nets)
    for v in setup.vrms:
        for p in (v.output, v.input, v.sense, v.ground):
            if p:
                nets.add(p.net)
    for l in setup.loads:
        nets.add(l.supply.net)
        if l.ground:
            nets.add(l.ground.net)
    for k in setup.links:
        nets.update((k.side_a.net, k.side_b.net))
        union(k.side_a.net, k.side_b.net)
    groups: Dict[str, set] = {}
    for n in nets:
        groups.setdefault(find(n), set()).add(n)
    return {n: frozenset(groups[find(n)]) for n in nets}


def group_name(group) -> str:
    return " + ".join(sorted(group))


def validate(setup: PISetup) -> List[str]:
    """Problems that make the setup unsolvable (empty list = OK)."""
    errors: List[str] = []
    names = [x.name for x in [*setup.vrms, *setup.loads, *setup.links]]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"Names must be unique: {', '.join(dupes)}")
    groups = net_groups(setup)

    by_group: Dict[frozenset, List[str]] = {}
    ground_groups = set()
    for v in setup.vrms:
        by_group.setdefault(groups[v.output.net], []).append(v.name)
        if v.kind not in VRM_KINDS:
            errors.append(f"VRM {v.name}: unknown type {v.kind!r}")
        if v.kind in ("ldo", "smps") and v.input is None:
            errors.append(f"VRM {v.name}: an {v.kind.upper()} needs its input pins set "
                          "(that is what connects it into the power tree)")
        if v.kind == "smps" and not (0 < v.efficiency <= 1):
            errors.append(f"VRM {v.name}: efficiency must be between 0 and 1")
        if v.sense and groups[v.sense.net] != groups[v.output.net]:
            errors.append(f"VRM {v.name}: sense pins must be on the output net (or nets "
                          "DC-connected to it)")
        if v.input and groups[v.input.net] == groups[v.output.net]:
            errors.append(f"VRM {v.name}: input and output are on the same net group")
        if v.ground:
            ground_groups.add(groups[v.ground.net])
    for g, vs in by_group.items():
        if len(vs) > 1:
            errors.append(f"Only one VRM may drive a net group; {group_name(g)} has "
                          f"{', '.join(vs)}")

    rail_groups = set(by_group)
    for l in setup.loads:
        if groups[l.supply.net] not in rail_groups:
            errors.append(f"Load {l.name}: no VRM drives {group_name(groups[l.supply.net])}")
        if l.ground:
            ground_groups.add(groups[l.ground.net])
    for v in setup.vrms:
        if v.input and groups[v.input.net] not in rail_groups:
            errors.append(f"VRM {v.name}: no VRM drives its input "
                          f"({group_name(groups[v.input.net])})")
    for g in ground_groups:
        if g in rail_groups:
            errors.append(f"{group_name(g)} is used both as a supply and as ground")
        if not any(v.kind == "source" and v.ground and groups[v.ground.net] == g
                   for v in setup.vrms):
            errors.append(f"Ground {group_name(g)} needs a reference: set the ground pins of "
                          "the source VRM that returns current through it")

    # The power tree must not loop (A feeds B feeds A).
    feeds = {groups[v.output.net]: set() for v in setup.vrms}
    for v in setup.vrms:
        if v.input:
            feeds.setdefault(groups[v.input.net], set()).add(groups[v.output.net])
    state: Dict[frozenset, int] = {}

    def visit(g):
        if state.get(g) == 1:
            return True
        if state.get(g) == 2:
            return False
        state[g] = 1
        cyc = any(visit(h) for h in feeds.get(g, ()))
        state[g] = 2
        return cyc
    if any(visit(g) for g in list(feeds)):
        errors.append("The power tree has a loop (a VRM feeds, directly or indirectly, its "
                      "own input)")
    return errors
