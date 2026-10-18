"""ODB++ connectivity (``steps/<step>/eda/data``) and components (``comp_+_top`` and
``comp_+_bot``).

The layer features carry no nets. ``eda/data`` lists each net's subnets: a component pin
(``TOP``), a via, a trace or a plane, and for each the features that make it up (``FID``:
which layer, which feature). Component files place each part and its pins ("toeprints").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from kipi.sources.odbpp.features import NM, Point, Units

FeatureRef = Tuple[str, int]            # (layer name, lower case; feature index)


@dataclass
class Subnet:
    kind: str                           # "TOP", "VIA", "TRC", "PLN"
    net: str
    side: str = ""                      # TOP: "T" or "B"
    component: int = -1                 # TOP: the part's index on its side
    toeprint: int = -1                  # TOP: the pin's index in the part
    copper: List[FeatureRef] = field(default_factory=list)   # FID C
    holes: List[FeatureRef] = field(default_factory=list)    # FID H


@dataclass
class Eda:
    nets: List[str]
    subnets: List[Subnet]


def parse_eda(text: str) -> Eda:
    layers: List[str] = []
    nets: List[str] = []
    subnets: List[Subnet] = []
    current: Optional[Subnet] = None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line[0] == "#":
            continue
        fields = line.split()
        tag = fields[0]
        if tag == "LYR":
            layers = [f.lower() for f in fields[1:]]
        elif tag == "NET":
            nets.append(fields[1] if len(fields) > 1 else "")
            current = None
        elif tag == "SNT" and nets and len(fields) > 1:
            net = nets[-1]
            current = Subnet(fields[1], "" if net == "$NONE$" else net)
            if current.kind == "TOP" and len(fields) >= 5:
                current.side = fields[2]
                current.component, current.toeprint = int(fields[3]), int(fields[4])
            subnets.append(current)
        elif tag == "FID" and current is not None and len(fields) >= 4:
            try:
                ref = (layers[int(fields[2])], int(fields[3]))
            except (IndexError, ValueError):
                continue
            if fields[1] == "C":
                current.copper.append(ref)
            elif fields[1] == "H":
                current.holes.append(ref)
        elif tag in ("PKG", "FGR"):
            current = None                  # packages and groups: nothing for KiPI
    return Eda(["" if n == "$NONE$" else n for n in nets], subnets)


@dataclass
class Pin:
    name: str
    at: Point
    angle_deg: float
    net_index: int


@dataclass
class Component:
    ref: str
    at: Point
    angle_deg: float
    side: str                           # "top" or "bottom"
    part: str = ""                      # the part (package) name the job gives
    value: str = ""
    pins: List[Pin] = field(default_factory=list)


def _pt(units: Units, x: str, y: str) -> Point:
    return (round(float(x) * units.coord, NM), round(-float(y) * units.coord, NM))


def _value(prp_rest: str) -> str:
    """The value of a ``PRP name 'value'`` line."""
    text = prp_rest.strip()
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    return text


def parse_components(text: str, side: str, default_units: Units) -> List[Component]:
    units = Units.of(text, default_units)
    comps: List[Component] = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line[0] == "#":
            continue
        fields = line.split()
        tag = fields[0]
        try:
            if tag == "CMP" and len(fields) >= 7:
                comps.append(Component(ref=fields[6], at=_pt(units, fields[2], fields[3]),
                                       angle_deg=(-float(fields[4])) % 360.0, side=side,
                                       part=fields[7] if len(fields) > 7 else ""))
            elif tag == "PRP" and comps and len(fields) >= 2:
                name, _, rest = line[4:].partition(" ")
                if name.lower() == "value":
                    comps[-1].value = _value(rest)
            elif tag == "TOP" and comps and len(fields) >= 9:
                comps[-1].pins.append(Pin(
                    name=" ".join(fields[8:]), at=_pt(units, fields[2], fields[3]),
                    angle_deg=(-float(fields[4])) % 360.0, net_index=int(fields[6])))
        except ValueError:
            continue
    return comps
