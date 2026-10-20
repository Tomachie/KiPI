# Portions Copyright (c) 2026 Tomachie LLC.
"""Read a Tomachie power-tree file (``<design>.pdn.json``, schema ``tomachie-pdn`` v1) into
a :class:`~kipi.dc.pi.model.PISetup`.

The file comes from the schematic and the parts' datasheets: every rail, its voltage
and source, the parts in series between rails, every load with its typical and maximum
current. What maps where:

* a rail sourced by a connector, battery or module pin -> a ``source`` VRM;
* a rail sourced by a regulator -> an ``ldo`` / ``smps`` VRM whose input pins are the
  regulator's pins on its input rail (the file also lists those as a load of the input
  rail; that entry is not imported, because KiPI derives a VRM's input current itself).
  An isolated converter is a switcher: its input power is its output power over its
  efficiency, plus its no-load current;
* a part in series between rails, and a switcher's inductor -> a DC connection;
* a load switch (a regulator entry of type ``load_switch``: it passes its input to its
  output and regulates nothing) -> a DC connection from its input pins to its output
  pins with its on-resistance, and its quiescent current a load on its input pins;
* a load -> a load with its typical current, and a tolerance from its rail's stated
  minimum and maximum, tightened to the datasheet's minimum and maximum supply voltage
  where those are stricter; its pre-layout copper margin (``copper_margin``) is kept
  beside it;
* a regulator's output band (``regulator_band``: its datasheet accuracy with its
  feedback divider's tolerance, at the board's temperature grade where given) -> the
  VRM's DC accuracy, in volts; a worst-case solve runs the VRM at its low end;
* a series part's ESL -> its DC connection's ESL; each capacitor's part number, value
  and ESL -> one capacitor model per part number (AC), filling only what a model of
  that name does not already hold.

KiPI solves one value per item, so where the file gives a range only one end is used:
the typical current of a load and the typical resistance of a series part (the maximum
when no typical is printed). The minimum and maximum left out are listed in the notes. A
current the file does not know (unknown, a part that draws what it drives, power leaving
the board at a connector pin) is imported as 0 A and named in the notes, to be entered by
hand.

Nothing is guessed. Whatever cannot be placed on the board is listed in the notes.
"""

from __future__ import annotations

import dataclasses
import json
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.ac.model import CapModel
from kipi.core.model.parts import parse_capacitance, parse_resistance
from kipi.dc.pi.importers.pad_index import PadIndex
from kipi.dc.solver.setup import DIODE_MIN_OHMS
from kipi.dc.pi.importers.pdn_figures import (
    amps, curve_points, dc_resistance, forward_drop, number,
)
from kipi.dc.pi.model import (
    DcLink, Load, PinRef, PISetup, Vrm, group_name, net_groups, volts_to_pct,
)

# A part on a rail by a signal pin only (detect, enable, reference): 0 A, not a load.
# "signal_pin_only" since Tomachie 7710456a; "not_a_supply" in files written before.
NOT_A_LOAD = ("signal_pin_only", "not_a_supply")

SCHEMA = "tomachie-pdn"
SCHEMA_VERSION = 1
SOURCE_ROLES = ("board_input", "battery", "generator")
# A resistor joining two ground nets above this carries no DC to speak of (a shield's 1M).
GROUND_LINK_MAX_OHMS = 1e3
VRM_KIND = {"linear": "ldo", "switching": "smps", "isolated": "smps"}



def _return_part(load: dict) -> str:
    """The part a load's current returns to ground through: a resistor / LED chain's
    last part, the part a pull resistor pulls (pull_to, <refdes>_<pin>), else the load."""
    if load.get("role") == "pull_resistor" and "_" in (load.get("pull_to") or ""):
        return load["pull_to"].rsplit("_", 1)[0]
    return (load.get("path_parts") or [load["refdes"]])[-1]

class PdnImportError(ValueError):
    pass


def _volts(v) -> Optional[float]:
    """A voltage object's volts; None for "unknown" or a missing field."""
    return number(v.get("volts")) if isinstance(v, dict) else None


def path_to_rail(parts: List[dict], start: str, rail: str) -> List[dict]:
    """The parts of a regulator's ``output_path`` that lead from its output net to its
    rail, in order. Files written before Tomachie 5.137 also list parts that only hang
    off the output node (pull-ups, feedback-divider legs); those are not on the walk."""
    reached: Dict[str, Optional[dict]] = {start: None}
    queue = deque([start])
    while queue and rail not in reached:
        net = queue.popleft()
        for e in parts:
            nxt = (e.get("net_out") if e.get("net_in") == net else
                   e.get("net_in") if e.get("net_out") == net else None)
            if nxt and nxt not in reached:
                reached[nxt] = dict(e, net_in=net, net_out=nxt)
                queue.append(nxt)
    path: List[dict] = []
    net = rail
    while reached.get(net) is not None:
        path.insert(0, reached[net])
        net = reached[net]["net_in"]
    return path


class TomachiePdnImporter:
    def __init__(self, pdn: dict, snap: BoardSnapshot, base: Optional[PISetup] = None):
        if pdn.get("schema") != SCHEMA or pdn.get("schema_version") != SCHEMA_VERSION:
            raise PdnImportError(f"Not a {SCHEMA} version {SCHEMA_VERSION} file "
                                 f"(schema {pdn.get('schema')!r}, "
                                 f"version {pdn.get('schema_version')!r})")
        self.pdn = pdn
        self.idx = PadIndex(snap)
        self.notes: List[str] = []
        # Only the power tree is replaced: solve settings, thermal, the report option, the
        # stackup and the AC setup stay as the user set them.
        self.setup = (dataclasses.replace(base, vrms=[], loads=[], links=[]) if base
                      else PISetup())
        if base is None:
            # The file gives every load's current, and what a load that passes power on or
            # sends it off the board dissipates itself: a new setup from it heats the
            # board with its loads. A setup imported onto keeps its own choice.
            self.setup.thermal.load_power = True
            self.notes.append("Thermal: load power is included (Thermal settings tab), "
                              "from the loads' currents and stated heat")
        # One value per item is solved; the other ends of a range, summed up at the end.
        self.unused_current: List[Tuple[str, str]] = []     # (load, what is left out)
        # The maxima, solved in a worst-case solve (kipi.dc.pi.corner).
        self.max_current: Dict[str, float] = {}
        self.max_resistance: List[str] = []
        self.unknown_current: List[str] = []
        self.static_current: Dict[str, float] = {}   # solved at a static figure, more not known
        self.not_supplied: List[str] = []    # on a rail by no supply pin: not a load
        self.rails: Dict[str, dict] = {r["name"]: r for r in pdn.get("rails", [])}
        self.regulators: Dict[str, dict] = {g["refdes"]: g for g in pdn.get("regulators", [])}
        self.skipped_rails: Set[str] = set()
        self.linked: Set[str] = set()
        self.accuracies: List[str] = []          # VRM output bands taken from the file
        self.out_before_copper: List[Tuple[str, str]] = []   # (load, by how much)

    # ------------------------------------------------------------ helpers
    def _unique(self, name: str) -> str:
        taken = {x.name for x in [*self.setup.vrms, *self.setup.loads, *self.setup.links]}
        out, i = name, 2
        while out in taken:
            out, i = f"{name} ({i})", i + 1
        return out

    def _learn_nets(self):
        """Tie each schematic net in the file to its board net through pins on it."""
        for name, r in self.rails.items():
            src = r["source"]
            if src.get("output_net"):
                self.idx.learn(src["output_net"], src.get("pins", []))
            self.idx.learn(name, [p for l in r.get("loads", []) if not l.get("input_net")
                                  for p in l.get("pins", [])])
            for l in r.get("loads", []):
                if l.get("input_net"):
                    self.idx.learn(l["input_net"], l.get("pins", []))

    def _voltage(self, rail: dict) -> Optional[float]:
        v = number(rail.get("voltage", {}).get("nominal"))
        if v is None:
            self.notes.append(f"Rail {rail['name']}: voltage not stated, so it has no source; "
                              "its loads are not imported")
            self.skipped_rails.add(rail["name"])
        return v

    # ------------------------------------------------------------ sources
    def _source_pins(self, src: dict, what: str):
        """The source's output pins. A pin the file names by its pin name (``J9_VBUS``)
        rather than its pad number is not found among the pads; the file also names the
        part and the net it drives, so its pads on that board net are used, and said so."""
        out = self.idx.pin_ref(src.get("pins", []), self.notes, what)
        ref, net = src.get("refdes"), src.get("output_net")
        if out is not None or not ref or not net:
            return out
        board_net = self.idx.board_net(net)
        out = self.idx.part_on_net(ref, board_net) if board_net else None
        if out is not None:
            self.notes.append(f"{what}: using {ref}'s pads on its output net {board_net} "
                              f"({', '.join(out.pins)})")
        return out

    def _off_its_net(self, name: str, src: dict, out) -> bool:
        """True (and said) when the file is wrong about this rail's source: the source pins
        it names are on another board net than the source's output net, or they already
        drive a rail placed before. Placing it would drive another rail."""
        if out is None:
            return False
        pins = ", ".join(src.get("pins", [])) or src.get("refdes", "?")
        net = src.get("output_net")
        board_net = self.idx.board_net(net) if net else None
        if board_net is not None and out.net != board_net:
            why = f"on the board that pin is on {out.net}, not {board_net}"
        elif any(v.output and v.output.net == out.net for v in self.setup.vrms):
            why = f"those pins already drive {out.net}"
        else:
            return False
        self.notes.append(f"Rail {name}: the file names {pins} as its source, but {why}; "
                          "the rail and its loads are not imported")
        self.skipped_rails.add(name)
        return True

    def _board_sources(self):
        for name, r in self.rails.items():
            src = r["source"]
            if src["role"] == "unresolved":
                self.notes.append(f"Rail {name}: no source traced; its loads are not imported")
                self.skipped_rails.add(name)
            elif src["role"] in SOURCE_ROLES:
                v = self._voltage(r)
                out = self._source_pins(src, f"Source of {name}")
                if v is None or self._off_its_net(name, src, out):
                    continue
                if out is None:
                    self.notes.append(f"Rail {name}: source {src.get('refdes') or '?'} not "
                                      "found on the board; its loads are not imported")
                    self.skipped_rails.add(name)
                    continue
                ret = src.get("return") if isinstance(src.get("return"), dict) else {}
                self.setup.vrms.append(Vrm(self._unique(f"{src.get('refdes') or name} {name}"),
                                           out, v, "source",
                                           ground=self._ground(src.get("refdes"),
                                                               ret.get("net") or r.get("ground"))))

    def _ground(self, ref: Optional[str], net: Optional[str] = None) -> Optional[PinRef]:
        """The part's pads on its ground net - ``net`` when the file names it (the ground
        its rail returns on), else the first of the file's ground nets it has pads on - so
        its return current flows in the ground copper too (None: it has none there, and
        returns through an ideal ground)."""
        if ref and net and net not in ("unknown", "not_specified"):
            pins = self.idx.part_on_net(ref, self.idx.board_net(net) or net)
            if pins is not None:
                return pins
        for name in self.pdn.get("ground_nets", []):
            pins = self.idx.part_on_net(ref, self.idx.board_net(name) or name) if ref else None
            if pins is not None:
                return pins
        return None

    def _regulator_input(self, reg: dict):
        """The regulator's pins on its input rail, from its load entry there."""
        l = self._regulator_entry(reg)
        return self.idx.pin_ref(l["pins"], self.notes, f"{reg['refdes']} input") if l else None

    def _regulator_kind(self, reg: dict, rail: str) -> Tuple[str, float]:
        kind = VRM_KIND.get(reg.get("type"))
        eff = number(reg.get("efficiency"))
        if kind is None:
            self.notes.append(f"{reg['refdes']} ({rail}): not known whether linear or "
                              "switching; modeled as an ideal source, drawing nothing from "
                              "its input rail")
            return "source", 1.0
        if kind == "smps" and eff is None:
            self.notes.append(f"{reg['refdes']} ({rail}): efficiency not read from its "
                              "datasheet; solved lossless, so its input current and the rail "
                              "feeding it are understated")
            return kind, 1.0
        return kind, eff if eff is not None else 1.0

    def _regulated_rails(self):
        for name, r in self.rails.items():
            src = r["source"]
            if src["role"] != "regulator":
                continue
            reg = self.regulators.get(src.get("refdes", ""))
            if reg is not None and reg.get("type") == "load_switch":
                self._load_switch(reg, src, name)
                continue
            v = self._voltage(r)
            out = self._source_pins(src, f"{src.get('refdes')} output")
            if self._off_its_net(name, src, out):
                continue
            if v is None or out is None or reg is None:
                if v is not None:
                    why = ("not listed among the regulators" if reg is None else
                           "output pins not found on the board")
                    self.notes.append(f"Rail {name}: regulator {src.get('refdes') or '?'} "
                                      f"{why}; its loads are not imported")
                self.skipped_rails.add(name)
                continue
            kind, eff = self._regulator_kind(reg, name)
            vin = self._regulator_input(reg) if kind != "source" else None
            if kind != "source" and vin is None:
                self.notes.append(f"{reg['refdes']} ({name}): input pins not found; modeled "
                                  "as an ideal source")
                kind = "source"
            ground, out_ground = self._regulator_grounds(reg, r, kind)
            self.setup.vrms.append(Vrm(self._unique(f"{reg['refdes']} {name}"), out, v, kind,
                                       efficiency=eff, input=vin,
                                       quiescent_a=amps(reg.get("quiescent_current")) or 0.0,
                                       dropout_v=_volts(reg.get("dropout_voltage")) or 0.0,
                                       vin_min=_volts(reg.get("input_voltage_min")),
                                       vin_max=_volts(reg.get("input_voltage_max")),
                                       ground=ground, output_ground=out_ground,
                                       **self._accuracy(r, reg["refdes"])))
            if vin is not None:
                self._input_filter(reg)
            self._output_path(reg["refdes"], src, name, in_loop=kind == "smps")

    def _accuracy(self, rail: dict, ref: str) -> dict:
        """The VRM's DC accuracy in volts from the rail's ``regulator_band``: the band at
        the board's temperature grade, else at 25 C. Nothing when the file gives none."""
        band = rail.get("regulator_band")
        if not isinstance(band, dict):
            return {}
        for key in ("at_grade", "at_25c"):
            b = band.get(key)
            if not isinstance(b, dict):
                continue
            lo, hi = number(b.get("min_v")), number(b.get("max_v"))
            if lo is None and hi is None:
                continue
            grade = number(band.get("grade_c"))
            where = f"at {grade:g} C" if key == "at_grade" and grade is not None else "at 25 C"
            self.accuracies.append(f"{ref} {rail['name']} {_volts_range(lo, hi)} {where}")
            return {"acc_min": lo, "acc_max": hi, "acc_pct": False, "acc_dv": False}
        why = band.get("not_known_because")
        if why:
            self.notes.append(f"{ref} ({rail['name']}): output accuracy not known: "
                              + "; ".join(map(str, why)))
        return {}

    def _regulator_grounds(self, reg: dict, rail: dict, kind: str):
        """(ground, output ground) pins of a regulator. A source VRM's ground pins are a 0 V
        reference; a regulator only standing in as one is not the board's reference, so
        its ground floats with the copper (one reference, at the board input). An isolated
        converter returns its input on its input rail's ground and holds its output rail's
        ground - isolated from it - at 0 V at its own pins there."""
        if kind == "source":
            return None, None
        ref = reg["refdes"]
        out_net = rail.get("ground")
        in_rail = next(iter(reg.get("input_rails") or []), None)
        in_net = self.rails.get(in_rail, {}).get("ground") if in_rail else None
        if reg.get("type") != "isolated":
            return self._ground(ref, out_net), None
        ground = self._ground(ref, in_net)
        out_ground = self._ground(ref, out_net)
        if out_ground is None or ground is None or out_ground.net == ground.net:
            self.notes.append(f"{ref} ({rail['name']}): isolated converter whose output "
                              "ground pins were not found; its output returns as if on its "
                              "input's ground")
            return ground, None
        return ground, out_ground

    def _output_path(self, ref: str, src: dict, rail: str, in_loop: bool = False):
        """The parts between a regulator's output pins and its rail, as DC connections.
        A switcher regulates its rail AFTER them (its feedback divider sits on the rail),
        so with ``in_loop`` they lower nothing: shorts, their loss in the efficiency."""
        parts = src.get("output_path", [])
        path = path_to_rail(parts, src.get("output_net", ""), rail)
        off = sorted({e["refdes"] for e in parts} - {e["refdes"] for e in path})
        if off:
            self.notes.append(f"{ref} ({rail}): {', '.join(off)} listed on its "
                              "output path but not between its output and the rail; left out")
        for e in path:
            self._link(e, loop_of=ref if in_loop else None)

    def _input_filter(self, reg: dict):
        """The parts between a regulator's input rail and its input pins (a ferrite or an
        inductor ahead of it), as DC connections: a real drop, before any regulation."""
        for e in (self._regulator_entry(reg) or {}).get("input_filter") or []:
            self._link(e)

    def _regulator_entry(self, reg: dict) -> Optional[dict]:
        """The regulator's load entry on its input rail."""
        for rail_name in reg.get("input_rails", []):
            for l in self.rails.get(rail_name, {}).get("loads", []):
                if l["role"] == "regulator" and l["refdes"] == reg["refdes"]:
                    return l
        return None

    def _load_switch(self, reg: dict, src: dict, rail: str):
        """A power or load switch regulates nothing: its output is its input less I times
        its on-resistance, so it is a DC connection from its input pins to its output
        pins, not an LDO (which would make 5 V from 5 V and be out of regulation). The
        parts between its input rail and its input pins (an input filter) and between its
        output pins and its rail are DC connections too. Its quiescent current is drawn
        at its input pins, as an LDO VRM's is."""
        ref = reg["refdes"]
        vin = self._regulator_input(reg)
        out = self._source_pins(src, f"{ref} output")
        if vin is None or out is None:
            self.notes.append(f"Rail {rail}: load switch {ref} "
                              f"{'input' if vin is None else 'output'} pins not found on the "
                              "board; its loads are not imported")
            self.skipped_rails.add(rail)
            return
        self.linked.add(ref)
        typ, mx, fig_notes = dc_resistance({"datasheet_figures":
                                            reg.get("datasheet_resistances") or []})
        self._add_link(ref, vin, out, typ, mx, fig_notes, "on-resistance")
        self.notes.append(f"{ref} ({rail}): load switch, imported as a DC connection from "
                          f"{vin.net} to {out.net}, not a regulator")
        self._input_filter(reg)
        self._output_path(ref, src, rail)
        iq = amps(reg.get("quiescent_current"))
        if iq is not None:
            self.setup.loads.append(Load(self._unique(f"{ref} quiescent"), vin, iq,
                                         ground=self._ground(ref)))

    # ------------------------------------------------------------ series parts
    def _link(self, e: dict, loop_of: Optional[str] = None):
        ref = e["refdes"]
        if ref in self.linked:
            return
        self.linked.add(ref)
        net_in = self.idx.board_net(e.get("net_in", ""))
        a = self.idx.part_on_net(ref, net_in) if net_in else None
        net_out = self.idx.board_net(e.get("net_out", ""))
        b = (self.idx.part_on_net(ref, net_out) if net_out else None) or (
            self.idx.other_side(ref, net_in) if net_in else None)
        if a is None or b is None:
            self.notes.append(f"{ref}: series part between {e.get('net_in')} and "
                              f"{e.get('net_out')} not found on the board")
            return
        typ, mx, fig_notes = dc_resistance(e)
        esl = _henries(e.get("esl"))
        if e.get("class") == "diode":
            vf, vf_max, slope, vf_notes = forward_drop(e)
            if vf is None:
                self.notes.append(f"{ref}: a diode in series with no forward voltage in the "
                                  "file; modeled as a short, so the rail after it reads high")
                typ, mx = 0.0, None
            else:
                # Anode on the side the power comes from (net_in).
                self.notes.extend(f"{ref}: diode, {n}" for n in vf_notes)
                # A diode needs a series resistance; the solver's floor when the file
                # gives one figure (no slope).
                self._add_link(ref, a, b, max(slope, DIODE_MIN_OHMS), None, [],
                               forward_v=vf, forward_v_max=vf_max,
                               esl_h=esl, vf_curve=curve_points(e))
                return
        if loop_of:
            self.notes.append(f"{ref}: inside {loop_of}'s regulation loop, so its DC "
                              "resistance lowers nothing (its loss is in the efficiency); "
                              "modeled as a short")
            typ, mx, fig_notes = 0.0, None, []
        self._add_link(ref, a, b, typ, mx, fig_notes, esl_h=esl)

    def _add_link(self, ref: str, a, b, typ: Optional[float], mx: Optional[float],
                  fig_notes: List[str], what: str = "DC resistance",
                  forward_v: Optional[float] = None, forward_v_max: Optional[float] = None,
                  esl_h: Optional[float] = None, vf_curve: Optional[list] = None):
        self.notes.extend(f"{ref}: {n}" for n in fig_notes)
        if typ is None and mx is None:
            self.notes.append(f"{ref}: {what} not known; modeled as a short")
            typ = 0.0
        elif typ is None:
            self.notes.append(f"{ref}: only a maximum {what} is printed; it is used")
            typ = mx
        elif mx is not None and mx != typ:
            self.max_resistance.append(f"{ref} {_ohms(mx)}")
        self.setup.links.append(DcLink(self._unique(ref), a, b, typ,
                                       resistance_max=mx if mx is not None and mx != typ
                                       else None,
                                       diode_vf=forward_v or 0.0, diode_vf_max=forward_v_max,
                                       esl_h=esl_h, vf_curve=vf_curve))

    def _ground_links(self):
        """The parts joining one ground net to another (a common-mode choke's return
        winding, a 0R, a net-tie) as DC connections, so a return reaches the reference
        through them. Capacitors, TVS diodes and resistors above 1 kOhm carry no DC to
        speak of; shield nets carry no load current."""
        for g in self.pdn.get("grounds", []):
            if g.get("kind") == "shield":
                continue
            for c in g.get("connections", []):
                cls = str(c.get("class", "")).lower()
                if cls in ("capacitor", "tvs", "diode", "esd"):
                    continue
                ohms = parse_resistance(str(c.get("value", ""))) if cls == "resistor" else None
                if ohms is not None and ohms > GROUND_LINK_MAX_OHMS:
                    continue
                self._link({"refdes": c["refdes"], "class": cls, "net_in": g["net"],
                            "net_out": c.get("to"), "datasheet_figures": [],
                            "resistance_ohm": ohms if ohms is not None else "unknown"})

    def _series_parts(self):
        for r in self.rails.values():
            for e in r.get("series_from_parent", []):
                self._link(e)

    # ------------------------------------------------------------ loads
    def _tolerance(self, rail: dict, l: dict) -> dict:
        """The load's PASS / FAIL range: the rail's stated minimum and maximum, in % from
        nominal, or in volts when the datasheet's minimum supply voltage (``supply_min``)
        is higher than the rail's minimum and takes its place."""
        v = rail.get("voltage", {})
        nom, lo, hi = number(v.get("nominal")), number(v.get("min")), number(v.get("max"))
        smin, smax = (number(x.get("volts")) if isinstance(x, dict) else None
                      for x in (l.get("supply_min"), l.get("supply_max")))
        if not nom:
            return {}
        if smin is not None or smax is not None:
            low = smin if smin is not None and (lo is None or smin > lo) else lo
            high = smax if smax is not None and (hi is None or smax < hi) else hi
            if nom < 0 or (smin or 0) < 0 or (smax or 0) < 0:
                self.notes.append(f"{l['refdes']} on {rail['name']}: supply range "
                                  f"{_volts_range(smin, smax)} not applied on a negative "
                                  "rail; set its tolerance by hand")
            elif (low, high) != (lo, hi):
                return {"tol_min": low, "tol_max": high, "tol_pct": False}
        if lo is None and hi is None:
            return {}
        tmin, tmax = volts_to_pct(nom, lo, hi)
        return {"tol_min": None if tmin is None else round(tmin, 6),
                "tol_max": None if tmax is None else round(tmax, 6), "tol_pct": True}

    def _current(self, l: dict, rail: str, label: str) -> float:
        """The one current KiPI solves for a load: its typical, else its maximum, else
        0 A (named in the notes)."""
        typ, mx, mn = (amps(l.get(k)) for k in ("current", "current_max", "current_min"))
        if l["role"] == "ic_with_regulator":
            rin, rin_max, rin_min = (amps(l.get(k)) for k in (
                "regulator_input", "regulator_input_max", "regulator_input_min"))
            typ = None if typ is None or rin is None else typ + rin
            mx = None if mx is None or rin_max is None else mx + rin_max
            mn = None if mn is None or rin_min is None else mn + rin_min
        if l.get("load_dependent"):
            # What the datasheet gives is the part's own (static) current: the least it
            # draws, with what it drives on top. That is solved, never 0 A.
            self.notes.append(f"{l['refdes']} on {rail}: draws what it drives "
                              f"({l.get('load_dependent_reason', 'no reason given')})")
            static = typ if typ is not None else mx
            if static is None:
                self.unknown_current.append(label)
                return 0.0
            self.static_current[label] = static
            return static
        if typ is None and mx is not None:
            self.notes.append(f"{label}: only a maximum current is known; {_amps(mx)} is "
                              "used")
            return mx
        if typ is None:
            self.unknown_current.append(label)
            return 0.0
        if mn is not None and mn != typ:
            self.unused_current.append((label, f"min {_amps(mn)}"))
        if mx is not None and mx != typ:
            self.max_current[label] = mx
        return typ

    def _rail_loads(self, rail: dict):
        name = rail["name"]
        for l in rail.get("loads", []):
            if l["role"] == "regulator":
                continue                   # the regulator's VRM input carries this current
            current = l.get("current")
            if isinstance(current, dict) and current.get("basis") in NOT_A_LOAD:
                # On the rail by a detect, enable or reference pin only: it draws nothing.
                self.not_supplied.append(f"{l['refdes']} ({name})")
                continue
            supply = self.idx.pin_ref(l["pins"], self.notes, f"Load {l['refdes']} on {name}")
            if supply is None:
                continue
            taken = {x.name for x in self.setup.loads}
            label = self._unique(l["refdes"] if l["refdes"] not in taken
                                 else f"{l['refdes']} ({name})")
            current = self._current(l, name, label)
            # The parts between the rail and pins behind a ferrite (input_net): DC
            # connections, so the load is reached through them.
            for e in l.get("input_filter") or []:
                self._link(e)
            returns = _return_part(l)
            self.setup.loads.append(Load(label, supply, current, **self._tolerance(rail, l),
                                         ground=self._ground(returns, rail.get("ground")),
                                         current_max=self.max_current.get(label),
                                         current_known=label not in self.unknown_current
                                         and label not in self.static_current,
                                         copper_margin_v=self._copper_margin(l, label)))
        for pin in rail.get("offboard_pins", []):
            supply = self.idx.pin_ref([pin], self.notes, f"Off-board pin {pin} on {name}")
            if supply is not None:
                label = self._unique(f"{pin} (off-board)")
                self.unknown_current.append(label)
                # Its current is not in the file: 0 A, not known (so it heats nothing).
                self.setup.loads.append(Load(label, supply, 0.0,
                                             **self._tolerance(rail, {"refdes": pin}),
                                             ground=self._ground(supply.ref,
                                                                 rail.get("ground")),
                                             current_known=False))

    def _copper_margin(self, l: dict, label: str) -> Optional[float]:
        """The load's pre-layout copper margin, volts: what its supply copper and ground
        return may drop together before it is below its minimum supply, from its rail's
        lowest output less the series parts. Below 0 it is out before any copper; so is
        a load whose maximum supply its rail's highest output is above."""
        cm = l.get("copper_margin")
        if not isinstance(cm, dict):
            return None
        v, high = number(cm.get("v")), number(cm.get("high_room_v"))
        if v is not None and v < 0:
            self.out_before_copper.append((label, f"{-v * 1e3:.4g} mV below its minimum"))
        if high is not None and high < 0:
            self.out_before_copper.append((label, f"{-high * 1e3:.4g} mV above its maximum "
                                                  "at its rail's highest output"))
        return v

    def _loads(self):
        for name, r in self.rails.items():
            if name not in self.skipped_rails:
                self._rail_loads(r)

    # ------------------------------------------------------------ capacitors (AC)
    def _file_caps(self) -> List[dict]:
        out = []
        for r in self.rails.values():
            out += r.get("decoupling") or []
            for l in r.get("loads", []):
                out += l.get("local_caps") or []
        return out

    def _caps(self):
        """One capacitor model per part number, from the capacitors' values and ESL. A
        model of that name already in the setup keeps what it holds and only gets what it
        lacks; a capacitor already in another model stays there."""
        by_pn: Dict[str, List[dict]] = {}
        seen = set()
        for c in self._file_caps():
            ref = c.get("refdes")
            if ref and ref not in seen and c.get("part_number") and ref in self.idx.by_part:
                seen.add(ref)
                by_pn.setdefault(str(c["part_number"]), []).append(c)
        if not by_pn:
            return
        models = {m.name: m for m in self.setup.caps}
        assigned = {ref for m in self.setup.caps for ref in m.refs}
        added = filled = 0
        for pn, caps in sorted(by_pn.items()):
            farads = next((f for f in (parse_capacitance(str(c.get("value", "")).split("/")[0])
                                       for c in caps) if f), 0.0)
            esl = next((h for h in (_henries(c.get("esl")) for c in caps) if h), 0.0)
            m = models.get(pn)
            if m is None:
                m = models[pn] = CapModel(pn, farads, esl, 0.0)
                self.setup.caps.append(m)
                added += 1
            else:
                before = (m.capacitance_f, m.esl_h)
                m.capacitance_f, m.esl_h = m.capacitance_f or farads, m.esl_h or esl
                filled += before != (m.capacitance_f, m.esl_h)
            m.refs += [c["refdes"] for c in caps if c["refdes"] not in assigned]
            assigned.update(m.refs)
        no_esr = sum(1 for pn in by_pn if not models[pn].esr_ohm)
        self.notes.append(f"Capacitor models (AC): {added} added and {filled} filled in by "
                          f"part number, with their ESL; {no_esr} have no ESR in the file, "
                          "to be entered before an AC sweep")

    # ------------------------------------------------------------ what nothing powers
    def _drop_unpowered(self):
        """Leave out VRMs whose input nothing in the file powers, and loads on nets nothing
        drives, until every item left is powered; each one left out is named."""
        while True:
            groups = net_groups(self.setup)
            driven = {groups[v.output.net] for v in self.setup.vrms}
            cut = [v for v in self.setup.vrms if v.input and groups[v.input.net] not in driven]
            for v in cut:
                self.notes.append(f"VRM {v.name}: nothing in the file powers its input "
                                  f"({group_name(groups[v.input.net])}); left out")
            self.setup.vrms = [v for v in self.setup.vrms if v not in cut]
            if not cut:
                break
        groups = net_groups(self.setup)
        driven = {groups[v.output.net] for v in self.setup.vrms}
        unpowered: Dict[str, List[str]] = {}
        for l in self.setup.loads:
            g = groups[l.supply.net]
            if g not in driven:
                unpowered.setdefault(group_name(g), []).append(l.name)
        for net, names in unpowered.items():
            self.notes.append(f"Nothing in the file powers {net}; its loads are left out: "
                              + ", ".join(names))
        dropped = {n for names in unpowered.values() for n in names}
        self.setup.loads = [l for l in self.setup.loads if l.name not in dropped]

    # ------------------------------------------------------------ run
    def run(self) -> Tuple[PISetup, List[str]]:
        self._learn_nets()
        self._board_sources()
        self._regulated_rails()
        self._series_parts()
        self._ground_links()
        self._loads()
        self._drop_unpowered()
        self._unreferenced_grounds()
        self._caps()
        self._range_notes()
        return self.setup, self.notes

    def _unreferenced_grounds(self):
        """A ground net carries return current only from a reference: a source VRM's ground
        pins, or an isolated converter's output ground pins. A ground with neither (an
        isolated converter whose output ground pins were not found) sends what returns
        there to an ideal ground instead, named once."""
        groups = net_groups(self.setup)
        referenced = {groups[v.ground.net] for v in self.setup.vrms
                      if v.kind == "source" and v.ground}
        referenced |= {groups[v.output_ground.net] for v in self.setup.vrms if v.output_ground}
        ideal = set()
        for x in [*self.setup.vrms, *self.setup.loads]:
            if x.ground and groups[x.ground.net] not in referenced:
                ideal.add(x.ground.net)
                x.ground = None
        if ideal:
            self.notes.append("No supply returns through " + ", ".join(sorted(ideal))
                              + " (an isolated converter's output side), so what returns "
                              "there goes to an ideal ground")

    def _range_notes(self):
        """What a single value per item left out, one note each, for the loads kept."""
        kept = {x.name for x in self.setup.loads}
        if self.not_supplied:
            self.notes.append("On a rail by a detect, enable or reference pin, not a supply "
                              "pin, so not a load: " + ", ".join(self.not_supplied))
        static = [f"{n} ({_amps(a)})" for n, a in self.static_current.items() if n in kept]
        if static:
            self.notes.append("Draw what they drive: their own (static) current from the "
                              "datasheet is solved, what they drive is on top and not known, "
                              "so the results they touch are INCOMPLETE: " + ", ".join(static))
        unknown = [n for n in self.unknown_current if n in kept]
        if unknown:
            self.notes.append("Current not known, imported as 0 A and marked not known (the "
                              "results they touch are INCOMPLETE); enter it on the Loads tab: "
                              + ", ".join(unknown))
        unused = [f"{n} ({r})" for n, r in self.unused_current if n in kept]
        if unused:
            self.notes.append("The typical current is solved; these minimum currents are "
                              "not used: " + "; ".join(unused))
        if self.accuracies:
            self.notes.append("Output accuracy from the datasheets (a worst-case solve runs "
                              "each VRM at its low end): " + "; ".join(self.accuracies))
        out = [f"{n} ({why})" for n, why in self.out_before_copper if n in kept]
        if out:
            self.notes.append("Out of their supply range before any copper (the regulator's "
                              "output band against the part's datasheet): " + "; ".join(out))
        maxed = [f"{n} ({_amps(a)})" for n, a in self.max_current.items() if n in kept]
        if maxed or self.max_resistance:
            self.notes.append("Maximum currents and resistances are kept for a worst-case "
                              f"solve (beside Solve): {len(maxed)} loads, "
                              f"{len(self.max_resistance)} DC connections")


def _amps(a: float) -> str:
    if abs(a) < 1e-3:
        return f"{a * 1e6:.4g} µA"
    return f"{a * 1e3:.4g} mA" if abs(a) < 1 else f"{a:.4g} A"


def _volts_range(lo: Optional[float], hi: Optional[float]) -> str:
    return " to ".join("?" if x is None else f"{x:.4g}" for x in (lo, hi)) + " V"


def _henries(esl) -> Optional[float]:
    """An ``esl`` field's henries; None for "unknown" or none."""
    return number(esl.get("h")) if isinstance(esl, dict) else None


def _ohms(r: float) -> str:
    return f"{r * 1e3:.4g} mΩ" if abs(r) < 1 else f"{r:.4g} Ω"


def import_tomachie_pdn(path, snap: BoardSnapshot,
                        base: Optional[PISetup] = None) -> Tuple[PISetup, List[str]]:
    """The setup described by a Tomachie ``.pdn.json`` on this board, and notes on what
    could not be placed."""
    pdn = json.loads(Path(path).read_text(encoding="utf-8"))
    return TomachiePdnImporter(pdn, snap, base).run()
