"""Make the ``.kicad_pcb`` reader's test boards. Needs KiCad 10 (kicad-cli and pcbnew).

    python tests/data/kicad_pcb/make_fixtures.py

1. Writes ``features.kicad_pcb``: a small 4-layer board with one of everything the reader
   handles (every pad shape, rotated and bottom-side footprints, a padstack, a pad that
   drops unused layers, arc tracks, blind and micro vias, zones, a rule area, copper
   drawings with and without a net, an outline with an arc and a cutout).
2. Has kicad-cli fill its zones and save it, so the file is exactly what KiCad writes.
3. Writes ``features_v9.kicad_pcb``: the same board with KiCad 9's numbered nets.
4. Exports the board as ODB++ to ``tests/data/odbpp``: ``features.zip`` at 6 decimals and
   ``features_p2.tgz`` at KiCad's default 2.

Then open each board in the PCB editor and save what the KiCad API reads as the
reference the tests compare with:

    set KIPI_HOST=plugin
    python -m kipi --dump tests/data/kicad_pcb/features.ref.kipi.json.gz
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
NETS = ["", "+5V", "SIG", "GND", "VOUT"]


def uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kipi-fixture/{name}"))


def net(name: str) -> str:
    return f'(net {NETS.index(name)} "{name}")'


def pad(fp: str, num: str, kind: str, shape: str, at: str, size: str, layers: str,
        net_name: str = "", extra: str = "") -> str:
    n = f"\n\t\t\t{net(net_name)}" if net_name else ""
    return (f'\t\t(pad "{num}" {kind} {shape}\n\t\t\t(at {at})\n\t\t\t(size {size})\n'
            f"\t\t\t(layers {layers}){extra}{n}\n"
            f'\t\t\t(uuid "{uid(f"{fp}/pad{num}@{at}")}")\n\t\t)\n')


def footprint(ref: str, layer: str, at: str, angle: float, pads: str, extra: str = "") -> str:
    silk = "F.SilkS" if layer == "F.Cu" else "B.SilkS"
    fields = "".join(
        f'\t\t(property "{name}" "{text}"\n\t\t\t(at 0 {y} {angle:g})\n\t\t\t(layer "{silk}")\n'
        f'\t\t\t(uuid "{uid(ref + name)}")\n\t\t\t(effects (font (size 1 1) (thickness 0.15)))\n'
        "\t\t)\n" for name, text, y in (("Reference", ref, -5), ("Value", "TEST", 5)))
    return (f'\t(footprint "kipi:{ref}"\n\t\t(layer "{layer}")\n\t\t(uuid "{uid(ref)}")\n'
            f"\t\t(at {at} {angle:g})\n{fields}{extra}{pads}\t)\n")


def u1() -> str:
    """Front side, turned 30 degrees: one SMD pad of every shape."""
    a = 30
    F = '"F.Cu" "F.Mask" "F.Paste"'
    pads = "".join([
        pad("U1", "1", "smd", "rect", f"-3 -2 {a}", "1 0.6", F, "+5V"),
        pad("U1", "2", "smd", "roundrect", f"-1 -2 {a}", "1.2 0.7", F, "+5V",
            "\n\t\t\t(roundrect_rratio 0.25)"),
        pad("U1", "3", "smd", "oval", f"1 -2 {a}", "1.6 0.8", F, "SIG"),
        pad("U1", "4", "smd", "circle", f"3 -2 {a}", "0.9 0.9", F, "SIG"),
        pad("U1", "5", "smd", "trapezoid", f"-3 1 {a}", "1.2 0.8", F, "GND",
            "\n\t\t\t(rect_delta 0 0.3)"),
        pad("U1", "6", "smd", "roundrect", f"-1 1 {a}", "1.2 1.2", F, "GND",
            "\n\t\t\t(roundrect_rratio 0.15)\n\t\t\t(chamfer_ratio 0.25)"
            "\n\t\t\t(chamfer top_left bottom_right)"),
        pad("U1", "7", "smd", "custom", f"1 1 {a}", "0.6 0.6", '"F.Cu" "F.Mask"', "VOUT",
            "\n\t\t\t(options (clearance outline) (anchor circle))"
            "\n\t\t\t(primitives\n\t\t\t\t(gr_poly (pts (xy 0 -0.25) (xy 1.5 -0.25) "
            "(xy 1.5 0.25) (xy 0 0.25)) (width 0) (fill yes))"
            "\n\t\t\t\t(gr_circle (center 1.5 0) (end 1.9 0) (width 0) (fill yes))\n\t\t\t)"),
        pad("U1", "8", "smd", "rect", f"3 1 {a + 45}", "0.8 0.8", F, "VOUT"),
    ])
    copper = (f'\t\t(fp_poly\n\t\t\t(pts (xy -4 3) (xy -2 3) (xy -2 4) (xy -4 4))\n'
              f"\t\t\t(stroke (width 0) (type solid))\n\t\t\t(fill yes)\n"
              f'\t\t\t(layer "F.Cu")\n\t\t\t(uuid "{uid("U1/logo")}")\n\t\t)\n')
    return footprint("U1", "F.Cu", "30 30", a, pads, copper)


def j1() -> str:
    """Back side, turned 90 degrees: plated and unplated holes."""
    a = 90
    T = '"*.Cu" "*.Mask"'
    pads = "".join([
        pad("J1", "1", "thru_hole", "circle", f"-3 0 {a}", "1.8 1.8", T, "+5V",
            "\n\t\t\t(drill 1)\n\t\t\t(remove_unused_layers no)"),
        pad("J1", "2", "thru_hole", "oval", f"3 0 {a}", "2.4 1.4", T, "GND",
            "\n\t\t\t(drill oval 1.6 0.8)\n\t\t\t(remove_unused_layers no)"),
        pad("J1", "3", "thru_hole", "rect", f"0 3 {a}", "1.5 1.5", T, "SIG",
            "\n\t\t\t(drill 0.8)\n\t\t\t(remove_unused_layers yes)\n\t\t\t(keep_end_layers yes)"),
        pad("J1", "4", "thru_hole", "circle", f"0 -3 {a}", "1.8 1.8", T, "GND",
            "\n\t\t\t(drill 1)\n\t\t\t(remove_unused_layers no)"
            '\n\t\t\t(padstack\n\t\t\t\t(mode front_inner_back)'
            '\n\t\t\t\t(layer "Inner" (shape circle) (size 1.3 1.3))'
            '\n\t\t\t\t(layer "B.Cu" (shape rect) (size 2 2))\n\t\t\t)'),
        pad("J1", "", "np_thru_hole", "circle", f"-6 3 {a}", "1.2 1.2", T, "",
            "\n\t\t\t(drill 1.2)"),
        pad("J1", "", "np_thru_hole", "circle", f"6 3 {a}", "2 2", T, "",
            "\n\t\t\t(drill 1)"),
        pad("J1", "5", "smd", "roundrect", f"6 -3 {a}", "1.5 1", '"B.Cu" "B.Mask" "B.Paste"',
            "VOUT", "\n\t\t\t(roundrect_rratio 0.25)"),
        # Not symmetric, on the back and turned off the grid: shows which way a mirrored
        # pad is turned.
        pad("J1", "6", "smd", "roundrect", f"-6 -3 {a + 20}", "1.6 0.9",
            '"B.Cu" "B.Mask" "B.Paste"', "GND",
            "\n\t\t\t(roundrect_rratio 0)\n\t\t\t(chamfer_ratio 0.3)\n\t\t\t(chamfer top_left)"),
        pad("J1", "7", "smd", "trapezoid", f"-6 0 {a + 20}", "1.4 1", '"B.Cu" "B.Mask"',
            "GND", "\n\t\t\t(rect_delta 0 0.4)"),
    ])
    return footprint("J1", "B.Cu", "70 30", a, pads)


def item(kind: str, body: str, name: str) -> str:
    return f"\t({kind}\n{body}\t\t(uuid \"{uid(name)}\")\n\t)\n"


def tracks() -> str:
    out = []
    segs = [("F.Cu", "+5V", "20 30", "27.4 28.5", 0.4), ("In1.Cu", "SIG", "20 20", "40 20", 0.25),
            ("B.Cu", "GND", "60 40", "70 33", 0.5), ("F.Cu", "VOUT", "33 31", "40 31", 0.3),
            ("In1.Cu", "SIG", "70 33", "70 20", 0.25)]
    for i, (layer, n, s, e, w) in enumerate(segs):
        out.append(item("segment", f"\t\t(start {s})\n\t\t(end {e})\n\t\t(width {w})\n"
                        f'\t\t(layer "{layer}")\n\t\t(net {NETS.index(n)})\n', f"seg{i}"))
    out.append(item("arc", "\t\t(start 40 40)\n\t\t(mid 45 37)\n\t\t(end 50 40)\n\t\t(width 0.3)\n"
                    f'\t\t(layer "F.Cu")\n\t\t(net {NETS.index("+5V")})\n', "arc0"))
    vias = [("", "20 20", 0.6, 0.3, '"F.Cu" "B.Cu"', "SIG"),
            ("blind ", "40 20", 0.45, 0.2, '"F.Cu" "In1.Cu"', "SIG"),
            ("micro ", "40 31", 0.3, 0.1, '"F.Cu" "In1.Cu"', "VOUT"),
            ("", "60 40", 0.6, 0.3, '"F.Cu" "B.Cu"', "GND")]
    for i, (kind, at, size, drill, layers, n) in enumerate(vias):
        out.append(f"\t(via {kind}\n\t\t(at {at})\n\t\t(size {size})\n\t\t(drill {drill})\n"
                   f"\t\t(layers {layers})\n\t\t(net {NETS.index(n)})\n"
                   f'\t\t(uuid "{uid(f"via{i}")}")\n\t)\n')
    return "".join(out)


def zone(name: str, net_name: str, layer: str, pts: str, priority: int = 0,
         keepout: str = "") -> str:
    n = NETS.index(net_name)
    fill = ("\t\t(fill\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n\t\t)\n"
            if keepout else
            "\t\t(fill yes\n\t\t\t(thermal_gap 0.3)\n\t\t\t(thermal_bridge_width 0.3)\n\t\t)\n")
    return (f'\t(zone\n\t\t(net {n})\n\t\t(net_name "{net_name}")\n\t\t(layer "{layer}")\n'
            f'\t\t(uuid "{uid(name)}")\n\t\t(name "{name}")\n\t\t(hatch edge 0.5)\n'
            f"\t\t(priority {priority})\n\t\t(connect_pads\n\t\t\t(clearance 0.2)\n\t\t)\n"
            f"\t\t(min_thickness 0.2)\n\t\t(filled_areas_thickness no)\n{keepout}{fill}"
            f"\t\t(polygon\n\t\t\t(pts {pts})\n\t\t)\n\t)\n")


def zones() -> str:
    box = lambda x1, y1, x2, y2: f"(xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2})"
    keepout = ("\t\t(keepout\n\t\t\t(tracks not_allowed)\n\t\t\t(vias not_allowed)\n"
               "\t\t\t(pads allowed)\n\t\t\t(copperpour not_allowed)\n"
               "\t\t\t(footprints allowed)\n\t\t)\n")
    return "".join([
        zone("ground", "GND", "In2.Cu", box(1, 1, 99, 59)),
        zone("five", "+5V", "B.Cu", box(45, 5, 95, 25), priority=1),
        zone("keep", "", "F.Cu", box(80, 5, 95, 15), keepout=keepout),
    ])


def drawings() -> str:
    out = []
    edges = [("line", "(start 0 0)\n\t\t(end 100 0)"), ("line", "(start 100 0)\n\t\t(end 100 55)"),
             ("arc", "(start 100 55)\n\t\t(mid 98.535534 58.535534)\n\t\t(end 95 60)"),
             ("line", "(start 95 60)\n\t\t(end 0 60)"), ("line", "(start 0 60)\n\t\t(end 0 0)"),
             ("circle", "(center 85 45)\n\t\t(end 87 45)")]
    for i, (kind, geo) in enumerate(edges):
        fill = "\t\t(fill no)\n" if kind == "circle" else ""
        out.append(item(f"gr_{kind}", f"\t\t{geo}\n\t\t(stroke (width 0.1) (type default))\n"
                        f'{fill}\t\t(layer "Edge.Cuts")\n', f"edge{i}"))
    out.append(item("gr_poly", "\t\t(pts (xy 10 40) (xy 20 40) (xy 20 50) (xy 10 50))\n"
                    "\t\t(stroke (width 0.2) (type solid))\n\t\t(fill yes)\n"
                    f'\t\t(layer "F.Cu")\n\t\t(net {NETS.index("+5V")})\n', "copper_poly"))
    out.append(item("gr_circle", "\t\t(center 30 50)\n\t\t(end 32 50)\n"
                    "\t\t(stroke (width 0.3) (type solid))\n\t\t(fill no)\n"
                    f'\t\t(layer "In1.Cu")\n\t\t(net {NETS.index("SIG")})\n', "copper_ring"))
    return "".join(out)


HEADER = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(generator_version "9.0")
\t(general
\t\t(thickness 1.5)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(4 "In1.Cu" signal)
\t\t(6 "In2.Cu" power "GND plane")
\t\t(2 "B.Cu" signal)
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(11 "B.Adhes" user "B.Adhesive")
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(25 "Edge.Cuts" user)
\t\t(27 "Margin" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.SilkS" (type "Top Silk Screen"))
\t\t\t(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4.2) (loss_tangent 0.02))
\t\t\t(layer "In1.Cu" (type "copper") (thickness 0.0175))
\t\t\t(layer "dielectric 2" (type "core") (thickness 0.6) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)
\t\t\t\taddsublayer (thickness 0.4) (material "FR408") (epsilon_r 3.7) (loss_tangent 0.01))
\t\t\t(layer "In2.Cu" (type "copper") (thickness 0.0175))
\t\t\t(layer "dielectric 3" (type "prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4.2) (loss_tangent 0.02))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
\t\t\t(layer "B.SilkS" (type "Bottom Silk Screen"))
\t\t\t(copper_finish "None")
\t\t\t(dielectric_constraints no)
\t\t)
\t\t(pad_to_mask_clearance 0)
\t\t(allow_soldermask_bridges_in_footprints no)
\t)
"""

PROJECT = """{
  "board": {"design_settings": {"rules": {"max_error": 0.005}}},
  "meta": {"filename": "%s.kicad_pro", "version": 3},
  "net_settings": {
    "classes": [{"name": "Default"}, {"name": "Power"}],
    "meta": {"version": 4},
    "netclass_assignments": {"VOUT": "Power"},
    "netclass_patterns": [{"netclass": "Power", "pattern": "+*"}]
  }
}
"""


def board_text() -> str:
    table = "".join(f'\t(net {i} "{n}")\n' for i, n in enumerate(NETS))
    return HEADER + table + u1() + j1() + tracks() + zones() + drawings() + ")\n"


def with_numbered_nets(text: str) -> str:
    """A KiCad 10 board with KiCad 9's net table and numbered net references."""
    names = sorted(set(re.findall(r'\(net "((?:[^"\\]|\\.)*)"\)', text)) | {""})
    code = {n: i for i, n in enumerate(names)}
    lines = []
    top = ""                             # the top-level item being read
    for line in text.splitlines(keepends=True):
        t = re.match(r"\t\((\w+)", line)
        if t:
            top = t.group(1)
        m = re.match(r'(\s*)\(net "((?:[^"\\]|\\.)*)"\)\s*$', line)
        if m:
            indent, name = m.groups()
            if top == "footprint":       # pads: the number, then the name
                line = f'{indent}(net {code[name]} "{name}")\n'
            elif top == "zone":          # zones: the number, the name on its own line
                line = f'{indent}(net {code[name]})\n{indent}(net_name "{name}")\n'
            else:
                line = f"{indent}(net {code[name]})\n"
        lines.append(line)
    out = "".join(lines)
    out = re.sub(r"\(version \d+\)", "(version 20241229)", out, count=1)
    table = "".join(f'\t(net {i} "{n}")\n' for n, i in code.items())
    return out.replace("\t(footprint", table + "\t(footprint", 1)


def kicad_cli() -> str:
    found = shutil.which("kicad-cli")
    for p in (found, r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli", "/usr/bin/kicad-cli"):
        if p and Path(p).is_file():
            return p
    sys.exit("kicad-cli not found")


def main() -> int:
    board = HERE / "features.kicad_pcb"
    board.write_text(board_text(), encoding="utf-8", newline="\n")
    (HERE / "features.kicad_pro").write_text(PROJECT % "features", encoding="utf-8")
    subprocess.run([kicad_cli(), "pcb", "drc", "--refill-zones", "--save-board",
                    "--output", str(HERE / "drc.rpt"), str(board)], check=False)
    (HERE / "drc.rpt").unlink(missing_ok=True)
    v9 = HERE / "features_v9.kicad_pcb"
    v9.write_text(with_numbered_nets(board.read_text(encoding="utf-8")), encoding="utf-8",
                  newline="\n")
    (HERE / "features_v9.kicad_pro").write_text(PROJECT % "features_v9", encoding="utf-8")
    odb = HERE.parent / "odbpp"
    odb.mkdir(exist_ok=True)
    for name, extra in (("features.zip", ["--precision", "6"]),
                        ("features_p2.tgz", ["--compression", "tgz"])):
        subprocess.run([kicad_cli(), "pcb", "export", "odb", *extra, "-o", str(odb / name),
                        str(board)], check=True)
    print(f"Wrote {board}, {v9} and the ODB++ exports in {odb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
