"""Single-net DC solve setup: one voltage source, any number of current loads."""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget, QTextBrowser, QVBoxLayout,
    QWidget,
)

from kipi.model.board import BoardSnapshot
from kipi.model.nets import guess_voltage, sorted_nets
from kipi.solver import NetProblem, SolveSettings
from kipi.solver.fem import SolveError
from kipi.solver.setup import CurrentLoad, VoltageSource
from kipi.viz.results import FIELDS

def _spin(lo, hi, value, decimals, suffix, step=None):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setValue(value)
    s.setSuffix(suffix)
    if step:
        s.setSingleStep(step)
    return s


class SolvePanel(QWidget):
    solve_requested = Signal(object)        # NetProblem
    field_changed = Signal(str)             # key of kipi.viz.results.FIELDS
    show_geometry = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot: Optional[BoardSnapshot] = None
        self._saved: Dict[str, dict] = {}    # per-net setup kept for the session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        g_net = QGroupBox("Net", self)
        fn = QFormLayout(g_net)
        self.cmb_net = QComboBox(g_net)
        self.cmb_net.currentTextChanged.connect(self._on_net_changed)
        fn.addRow("Net", self.cmb_net)
        lay.addWidget(g_net)

        g_src = QGroupBox("Source (VRM output)", self)
        fs = QFormLayout(g_src)
        self.cmb_src = QComboBox(g_src)
        self.cmb_src.currentIndexChanged.connect(self._update_src_pins_hint)
        self.txt_src_pins = QLineEdit(g_src)
        self.txt_src_pins.setPlaceholderText("all pins on this net")
        self.spin_v = _spin(-1000, 1000, 3.3, 4, " V", 0.1)
        fs.addRow("Part", self.cmb_src)
        fs.addRow("Pins", self.txt_src_pins)
        fs.addRow("Voltage", self.spin_v)
        lay.addWidget(g_src)

        g_ld = QGroupBox("Loads", self)
        ll = QVBoxLayout(g_ld)
        self.tbl = QTableWidget(0, 3, g_ld)
        self.tbl.setHorizontalHeaderLabels(["Part", "Pins", "Current"])
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        row = QHBoxLayout()
        b_add = QPushButton("Add load", g_ld)
        b_add.clicked.connect(lambda: self._add_load_row())
        b_del = QPushButton("Remove", g_ld)
        b_del.clicked.connect(self._remove_load_rows)
        row.addWidget(b_add)
        row.addWidget(b_del)
        ll.addWidget(self.tbl)
        ll.addLayout(row)
        lay.addWidget(g_ld, 1)

        g_set = QGroupBox("Mesh && material", self)
        fm = QFormLayout(g_set)
        d = SolveSettings()
        self.spin_edge = _spin(0.05, 5.0, d.max_edge_mm, 2, " mm", 0.05)
        self.spin_edge.setToolTip("Largest triangle edge. Smaller = more accurate, slower.")
        self.spin_grading = _spin(0.0, 2.0, d.grading, 2, "", 0.05)
        self.spin_grading.setToolTip("Mesh size growth per mm away from vias (0 = no via "
                                     "refinement). Smaller = finer around vias.")
        self.spin_plating = _spin(1.0, 100.0, d.plating_mm * 1000, 1, " µm", 1.0)
        fm.addRow("Max edge", self.spin_edge)
        fm.addRow("Via grading", self.spin_grading)
        fm.addRow("Barrel plating", self.spin_plating)
        lay.addWidget(g_set)

        self.btn_solve = QPushButton("Solve", self)
        self.btn_solve.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_solve.clicked.connect(self._emit_solve)
        lay.addWidget(self.btn_solve)

        g_res = QGroupBox("Results", self)
        lr = QVBoxLayout(g_res)
        row = QHBoxLayout()
        self.cmb_field = QComboBox(g_res)
        for key, (_, _, label, unit) in FIELDS.items():
            self.cmb_field.addItem(f"{label} ({unit})", key)
        self.cmb_field.currentIndexChanged.connect(
            lambda i: self.field_changed.emit(self.cmb_field.itemData(i)))
        self.btn_geom = QPushButton("Show copper", g_res)
        self.btn_geom.clicked.connect(self.show_geometry.emit)
        row.addWidget(self.cmb_field, 1)
        row.addWidget(self.btn_geom)
        self.txt_results = QTextBrowser(g_res)
        lr.addLayout(row)
        lr.addWidget(self.txt_results)
        lay.addWidget(g_res, 2)
        self.setEnabled(False)

    # ------------------------------------------------------------------ data
    def set_snapshot(self, snap: BoardSnapshot):
        self.snapshot = snap
        self._saved.clear()
        self._net = None
        self.txt_results.clear()
        self.set_nets([])

    def set_nets(self, nets: List[str]):
        """Offer only ``nets`` (the nets ticked in the Board panel), keeping the current
        choice when it is still available."""
        if self.snapshot is None:
            return
        current = self.cmb_net.currentText()
        order = {n: i for i, n in enumerate(sorted_nets(self.snapshot))}
        nets = sorted(set(nets), key=lambda n: order.get(n, len(order)))
        if nets == [self.cmb_net.itemText(i) for i in range(self.cmb_net.count())]:
            return
        self.cmb_net.blockSignals(True)
        self.cmb_net.clear()
        self.cmb_net.addItems(nets)
        if current in nets:
            self.cmb_net.setCurrentText(current)
        self.cmb_net.blockSignals(False)
        self.setEnabled(bool(nets))
        if not nets:
            self.txt_results.setHtml("<i>Tick a net in the Board panel to set up a solve.</i>")
        if self.cmb_net.currentText() != current or self._net is None:
            self._on_net_changed(self.cmb_net.currentText())

    def current_net(self) -> str:
        return self.cmb_net.currentText()

    def _parts_on_net(self, net: str) -> List[tuple]:
        _, _, pads, _ = self.snapshot.items_on_net(net)
        by_ref: Dict[str, List[str]] = {}
        for p in pads:
            if p.footprint:
                by_ref.setdefault(p.footprint, []).append(p.number)
        values = {f.reference: f.value for f in self.snapshot.footprints}
        return sorted(((r, values.get(r, ""), sorted(set(n))) for r, n in by_ref.items()),
                      key=lambda t: (-len(t[2]), t[0]))

    def _part_combo(self) -> QComboBox:
        cmb = QComboBox()
        for ref, value, pins in self._parts:
            cmb.addItem(f"{ref}  {value}  ({len(pins)} pin{'s' if len(pins) > 1 else ''})", ref)
        return cmb

    def _on_net_changed(self, net: str):
        if not net or self.snapshot is None:
            if getattr(self, "_net", None):
                self._saved[self._net] = self._capture()
            self._net = None
            return
        prev = getattr(self, "_net", None)
        if prev:
            self._saved[prev] = self._capture()
        self._net = net
        self._parts = self._parts_on_net(net)
        self.cmb_src.clear()
        for ref, value, pins in self._parts:
            self.cmb_src.addItem(f"{ref}  {value}  ({len(pins)} pin{'s' if len(pins) > 1 else ''})", ref)
        self.tbl.setRowCount(0)
        saved = self._saved.get(net)
        if saved:
            self._restore(saved)
        else:
            self.spin_v.setValue(guess_voltage(net))
            self.txt_src_pins.clear()
            # Default load: the busiest part that is not the default source.
            src = self.cmb_src.currentData()
            others = [ref for ref, _, _ in self._parts if ref != src]
            if others:
                self._add_load_row(others[0])

    def _update_src_pins_hint(self):
        i = self.cmb_src.currentIndex()
        if 0 <= i < len(self._parts):
            pins = self._parts[i][2]
            self.txt_src_pins.setPlaceholderText(f"all: {', '.join(pins[:12])}"
                                                 f"{' …' if len(pins) > 12 else ''}")

    def _add_load_row(self, ref: Optional[str] = None, pins: str = "", current: float = 0.1):
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        cmb = self._part_combo()
        if ref:
            idx = cmb.findData(ref)
            if idx >= 0:
                cmb.setCurrentIndex(idx)
        self.tbl.setCellWidget(r, 0, cmb)
        pins_edit = QLineEdit(pins)
        pins_edit.setPlaceholderText("all")
        self.tbl.setCellWidget(r, 1, pins_edit)
        self.tbl.setCellWidget(r, 2, _spin(-1000, 1000, current, 4, " A", 0.01))

    def _remove_load_rows(self):
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True) or \
            [self.tbl.rowCount() - 1]
        for r in rows:
            if r >= 0:
                self.tbl.removeRow(r)

    def _capture(self) -> dict:
        loads = []
        for r in range(self.tbl.rowCount()):
            loads.append((self.tbl.cellWidget(r, 0).currentData(),
                          self.tbl.cellWidget(r, 1).text(), self.tbl.cellWidget(r, 2).value()))
        return {"src": self.cmb_src.currentData(), "src_pins": self.txt_src_pins.text(),
                "v": self.spin_v.value(), "loads": loads}

    def _restore(self, d: dict):
        idx = self.cmb_src.findData(d["src"])
        if idx >= 0:
            self.cmb_src.setCurrentIndex(idx)
        self.txt_src_pins.setText(d["src_pins"])
        self.spin_v.setValue(d["v"])
        for ref, pins, cur in d["loads"]:
            self._add_load_row(ref, pins, cur)

    # ------------------------------------------------------------------ solve
    def _pads(self, net: str, ref: str, pins_text: str) -> List[str]:
        from kipi.solver import pads_for
        numbers = [t for t in re.split(r"[,\s]+", pins_text.strip()) if t] or None
        return [p.id for p in pads_for(self.snapshot, net, ref, numbers)]

    def build_problem(self) -> NetProblem:
        net = self.current_net()
        src_ref = self.cmb_src.currentData()
        if not src_ref:
            raise SolveError("Choose the part that sources this net")
        sources = [VoltageSource(f"{src_ref}", self._pads(net, src_ref, self.txt_src_pins.text()),
                                 self.spin_v.value())]
        loads = []
        for r in range(self.tbl.rowCount()):
            ref = self.tbl.cellWidget(r, 0).currentData()
            pins = self.tbl.cellWidget(r, 1).text()
            cur = self.tbl.cellWidget(r, 2).value()
            if ref == src_ref and not pins.strip() and not self.txt_src_pins.text().strip():
                raise SolveError(f"{ref} is both the source and a load; give pin numbers")
            name = f"{ref}" + (f" [{pins.strip()}]" if pins.strip() else "")
            loads.append(CurrentLoad(name, self._pads(net, ref, pins), cur))
        if not loads:
            raise SolveError("Add at least one load")
        settings = SolveSettings(max_edge_mm=self.spin_edge.value(),
                                 grading=self.spin_grading.value(),
                                 plating_mm=self.spin_plating.value() / 1000)
        return NetProblem(net, sources, loads, settings)

    def show_error(self, message: str):
        self.txt_results.setHtml(f'<span style="color:#ff7070"><b>Cannot solve:</b> '
                                 f'{html.escape(message)}</span>')

    def _emit_solve(self):
        try:
            problem = self.build_problem()
        except SolveError as e:
            self.show_error(str(e))
            return
        self._saved[problem.net] = self._capture()
        self.solve_requested.emit(problem)

    def set_busy(self, busy: bool, queued: bool = False):
        self.btn_solve.setEnabled(not busy)
        self.btn_solve.setText(("Queued…" if queued else "Solving…") if busy else "Solve")

    def current_field(self) -> str:
        return self.cmb_field.currentData()

    # ------------------------------------------------------------------ report
    def show_results(self, result):
        f = result.fields
        sm = f.summary
        st = result.solution.stats
        rows = []
        for t in f.terminals:
            drop = (f.v_ref - t.v_mean) * 1e3
            pct = 100 * drop / 1e3 / f.v_ref if f.v_ref else 0.0
            spread = (t.v_max - t.v_min) * 1e3
            rows.append(
                f"<tr><td>{'⏻' if t.kind == 'source' else '↓'} {html.escape(t.name)}</td>"
                f"<td align=right>{t.current * 1e3:.1f} mA</td>"
                f"<td align=right>{t.v_mean:.5f} V</td>"
                f"<td align=right><b>{drop:.3f}</b> mV</td><td align=right>{pct:.3f}%</td>"
                f"<td align=right>{spread:.3f}</td></tr>")
        worst_via = max(f.vias, key=lambda v: v.current, default=None)
        warn = "".join(f'<br><span style="color:#e0b050">⚠ {html.escape(w)}</span>'
                       for w in result.solution.warnings)
        self.txt_results.setHtml(
            f"<b>{html.escape(result.mesh.net)}</b> — reference {f.v_ref:g} V<br>"
            "<table cellspacing=3><tr><th align=left>Terminal</th><th>I</th><th>V</th>"
            "<th>Drop</th><th></th><th>Pin spread mV</th></tr>" + "".join(rows) + "</table><br>"
            f"Max drop anywhere: <b>{sm['max_drop'] * 1e3:.3f} mV</b><br>"
            f"Peak current density: {sm['j_max']:.2f} A/mm² "
            f"<small>(pad corners are singular; see map)</small><br>"
            f"Copper loss: {sm['power_loss'] * 1e3:.3f} mW "
            f"(planes/traces {sm['power_copper'] * 1e3:.3f}, barrels {sm['power_barrels'] * 1e3:.3f})<br>"
            + (f"Highest via current: {worst_via.current * 1e3:.1f} mA "
               f"({'via' if worst_via.kind == 'via' else 'PTH'} Ø{worst_via.drill_mm:.2f} mm)<br>"
               if worst_via else "") +
            f"<small>Mesh {st['n_nodes']} nodes / {st['n_tris']} triangles, {st['n_unknowns']} "
            f"unknowns, {st['method']}"
            f"{', ' + str(int(st['iterations'])) + ' CG iterations' if st.get('iterations') else ''}, "
            f"{st['solve_s']:.2f} s. Checks: energy {sm['energy_balance_error']:.1e}, "
            f"KCL {sm['kcl_error']:.1e}</small>" + warn)
