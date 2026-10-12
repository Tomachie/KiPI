"""Power-integrity setup panel: VRMs, loads and DC connections, all user-defined.

Each tab is a list of items with an editor form. Edits go straight into the
:class:`~kipi.pi.model.PISetup`, which is validated after every change.
"""

from __future__ import annotations

import html
import math
import re
from typing import Callable, Dict, List, Optional

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QColor
from qtpy.QtWidgets import (
    QAbstractItemView, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from kipi.model.board import BoardSnapshot
from kipi.model.nets import guess_voltage, sorted_nets
from kipi.pi.model import DcLink, Load, PinRef, PISetup, Vrm, VRM_KINDS, validate
from kipi.viz.results import FIELDS

NONE = "(none)"


def _spin(lo, hi, value, decimals, suffix, step=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setValue(value)
    s.setSuffix(suffix)
    if step:
        s.setSingleStep(step)
    s.setKeyboardTracking(False)
    return s


def make_searchable(combo: QComboBox):
    """Let the user type to filter a combo box (substring, case-insensitive).

    Text that matches no item is discarded when editing finishes, so the combo always
    holds a real choice.
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    comp = combo.completer()
    comp.setCompletionMode(QCompleter.PopupCompletion)
    comp.setFilterMode(Qt.MatchContains)
    comp.setCaseSensitivity(Qt.CaseInsensitive)
    combo.lineEdit().setPlaceholderText("type to search…")

    def settle():
        text = combo.lineEdit().text()
        i = combo.findText(text, Qt.MatchFixedString)
        if i < 0:
            # Accept a unique partial match (e.g. "u402"); otherwise restore the choice.
            hits = [k for k in range(combo.count())
                    if text and text.lower() in combo.itemText(k).lower()]
            i = hits[0] if len(hits) == 1 else combo.currentIndex()
        if i >= 0:
            if i != combo.currentIndex():
                combo.setCurrentIndex(i)
            combo.lineEdit().setText(combo.itemText(i))
    combo.lineEdit().editingFinished.connect(settle)


def parts_on_net(snap: BoardSnapshot, net: str):
    """[(ref, value, [pad numbers])] for parts with pads on ``net``, busiest first."""
    _, _, pads, _ = snap.items_on_net(net)
    by_ref: Dict[str, List[str]] = {}
    for p in pads:
        if p.footprint:
            by_ref.setdefault(p.footprint, []).append(p.number)
    values = {f.reference: f.value for f in snap.footprints}
    return sorted(((r, values.get(r, ""), sorted(set(n))) for r, n in by_ref.items()),
                  key=lambda t: (-len(t[2]), t[0]))


# ---------------------------------------------------------------------------
# Pin picker: net + part + pin numbers
# ---------------------------------------------------------------------------

class PinPicker(QWidget):
    changed = Signal()

    def __init__(self, panel: "PIPanel", optional: bool = False, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.optional = optional
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.cmb_net = QComboBox(self)
        make_searchable(self.cmb_net)
        self.cmb_net.setMinimumContentsLength(10)
        self.cmb_part = QComboBox(self)
        make_searchable(self.cmb_part)
        self.cmb_part.setMinimumContentsLength(8)
        self.txt_pins = QLineEdit(self)
        self.txt_pins.setPlaceholderText("all pins")
        self.txt_pins.setMaximumWidth(90)
        lay.addWidget(self.cmb_net, 3)
        lay.addWidget(self.cmb_part, 3)
        lay.addWidget(self.txt_pins, 2)
        self.cmb_net.currentIndexChanged.connect(self._net_changed)
        self.cmb_part.currentIndexChanged.connect(self._part_changed)
        self.txt_pins.editingFinished.connect(self.changed.emit)
        self._loading = False

    def net(self) -> str:
        """The selected net (not the half-typed search text)."""
        i = self.cmb_net.currentIndex()
        return self.cmb_net.itemText(i) if i >= 0 else ""

    def _select_net(self, name: str):
        i = self.cmb_net.findText(name, Qt.MatchFixedString)
        if i >= 0:
            self.cmb_net.setCurrentIndex(i)

    def refresh_nets(self, nets: List[str]):
        cur = self.net()
        self._loading = True
        self.cmb_net.clear()
        if self.optional:
            self.cmb_net.addItem(NONE)
        self.cmb_net.addItems(nets)
        if cur:
            self._select_net(cur)
        self._loading = False
        self._fill_parts()

    def _fill_parts(self, keep: Optional[str] = None):
        net = self.net()
        keep = keep or self.cmb_part.currentData()
        self.cmb_part.blockSignals(True)
        self.cmb_part.clear()
        if net and net != NONE and self.panel.snapshot is not None:
            for ref, value, pins in parts_on_net(self.panel.snapshot, net):
                self.cmb_part.addItem(f"{ref} {value} ({len(pins)})", ref)
        i = self.cmb_part.findData(keep) if keep else -1
        if i >= 0:
            self.cmb_part.setCurrentIndex(i)
        self.cmb_part.blockSignals(False)
        self.cmb_part.setEnabled(net != NONE)
        self.txt_pins.setEnabled(net != NONE)
        self._update_hint()

    def _update_hint(self):
        net, ref = self.net(), self.cmb_part.currentData()
        if self.panel.snapshot is None or not ref:
            return
        pins = next((p for r, _, p in parts_on_net(self.panel.snapshot, net) if r == ref), [])
        self.txt_pins.setPlaceholderText(f"all: {','.join(pins[:8])}{'…' if len(pins) > 8 else ''}")

    def _net_changed(self, _):
        if self._loading:
            return
        self._fill_parts()
        self.changed.emit()

    def _part_changed(self, _):
        self._update_hint()
        if not self._loading:
            self.changed.emit()

    def set_value(self, ref: Optional[PinRef]):
        self._loading = True
        if ref is None:
            if self.optional:
                self._select_net(NONE)
            self.txt_pins.clear()
        else:
            if self.cmb_net.findText(ref.net) < 0:
                self.cmb_net.addItem(ref.net)     # keep nets used by the setup selectable
            self._select_net(ref.net)
            self._fill_parts(keep=ref.ref)
            self.txt_pins.setText(",".join(ref.pins))
        self._loading = False
        self._fill_parts(keep=ref.ref if ref else None)

    def value(self) -> Optional[PinRef]:
        net = self.net()
        ref = self.cmb_part.currentData()
        if not net or net == NONE or not ref:
            return None
        pins = [t for t in re.split(r"[,\s]+", self.txt_pins.text().strip()) if t]
        return PinRef(ref, net, pins)


# ---------------------------------------------------------------------------
# Editors
# ---------------------------------------------------------------------------

class _Editor(QWidget):
    changed = Signal()

    def __init__(self, panel: "PIPanel", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.form = QFormLayout(self)
        self.obj = None
        self._loading = False
        self.pickers: List[PinPicker] = []

    def _picker(self, optional=False) -> PinPicker:
        p = PinPicker(self.panel, optional, self)
        p.changed.connect(self._store)
        self.pickers.append(p)
        return p

    def _watch(self, *widgets):
        for w in widgets:
            sig = getattr(w, "valueChanged", None) or getattr(w, "currentIndexChanged", None)
            if isinstance(w, QLineEdit):
                sig = w.editingFinished
            sig.connect(self._store)

    def refresh_nets(self, nets):
        for p in self.pickers:
            p.refresh_nets(nets)

    def load(self, obj):
        self._loading = True
        self.obj = obj
        self._load(obj)
        self._loading = False

    def _store(self, *_):
        if self._loading or self.obj is None:
            return
        self._save(self.obj)
        self.changed.emit()


class VrmEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.kind = QComboBox()
        self.kind.addItem("Source (ideal supply, no input)", "source")
        self.kind.addItem("LDO (I_in = I_out + I_q)", "ldo")
        self.kind.addItem("Switcher (P_in = P_out / η)", "smps")
        self.output = self._picker()
        self.voltage = _spin(-1000, 1000, 3.3, 4, " V", 0.1)
        self.eff = _spin(1, 100, 90, 1, " %", 1)
        self.iq = _spin(0, 10000, 0, 3, " mA", 0.1)
        self.input = self._picker(optional=True)
        self.sense = self._picker(optional=True)
        self.ground = self._picker(optional=True)
        f.addRow("Name", self.name)
        f.addRow("Type", self.kind)
        f.addRow("Output pins", self.output)
        f.addRow("Set voltage", self.voltage)
        f.addRow("Efficiency", self.eff)
        f.addRow("Quiescent", self.iq)
        f.addRow("Input pins", self.input)
        f.addRow("Sense pins", self.sense)
        f.addRow("Ground pins", self.ground)
        self._watch(self.name, self.kind, self.voltage, self.eff, self.iq)
        self.kind.currentIndexChanged.connect(self._kind_rows)
        self.output.cmb_net.currentIndexChanged.connect(
            lambda _i: self._guess_voltage(self.output.net()))

    def _kind_rows(self, *_):
        kind = self.kind.currentData()
        self.form.setRowVisible(self.eff, kind == "smps")
        self.form.setRowVisible(self.iq, kind != "source")
        self.form.setRowVisible(self.input, kind != "source")

    def _guess_voltage(self, net):
        if not self._loading and net:
            self.voltage.setValue(guess_voltage(net))

    def _load(self, v: Vrm):
        self.name.setText(v.name)
        self.kind.setCurrentIndex(max(0, self.kind.findData(v.kind)))
        self.output.set_value(v.output)
        self.voltage.setValue(v.voltage)
        self.eff.setValue(v.efficiency * 100)
        self.iq.setValue(v.quiescent_a * 1e3)
        self.input.set_value(v.input)
        self.sense.set_value(v.sense)
        self.ground.set_value(v.ground)
        self._kind_rows()

    def _save(self, v: Vrm):
        v.name = self.name.text().strip() or v.name
        v.kind = self.kind.currentData()
        v.output = self.output.value() or v.output
        v.voltage = self.voltage.value()
        v.efficiency = self.eff.value() / 100
        v.quiescent_a = self.iq.value() / 1e3
        v.input = self.input.value() if v.kind != "source" else None
        v.sense = self.sense.value()
        v.ground = self.ground.value()


class LoadEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.supply = self._picker()
        self.current = _spin(-1000, 1000, 0.1, 4, " A", 0.01)
        self.ground = self._picker(optional=True)
        self.tol = _spin(0, 100, 0, 2, " %", 0.5)
        self.tol.setSpecialValueText("no limit")
        f.addRow("Name", self.name)
        f.addRow("Supply pins", self.supply)
        f.addRow("Current", self.current)
        f.addRow("Ground pins", self.ground)
        f.addRow("Max drop", self.tol)
        self._watch(self.name, self.current, self.tol)

    def _load(self, l: Load):
        self.name.setText(l.name)
        self.supply.set_value(l.supply)
        self.current.setValue(l.current)
        self.ground.set_value(l.ground)
        self.tol.setValue(l.tolerance_pct or 0.0)

    def _save(self, l: Load):
        new_supply = self.supply.value() or l.supply
        typed = self.name.text().strip() or l.name
        # A name still equal to the old part's reference was the automatic default:
        # follow the newly picked part.
        if typed == l.supply.ref and new_supply.ref != l.supply.ref:
            typed = new_supply.ref
            self.name.setText(typed)
        l.name = typed
        l.supply = new_supply
        l.current = self.current.value()
        l.ground = self.ground.value()
        l.tolerance_pct = self.tol.value() or None


class LinkEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.a = self._picker()
        self.b = self._picker()
        self.r = _spin(0, 1e9, 0, 3, " mΩ", 1)
        self.r.setSpecialValueText("0 (short)")
        f.addRow("Name", self.name)
        f.addRow("Side A pins", self.a)
        f.addRow("Side B pins", self.b)
        f.addRow("Resistance", self.r)
        hint = QLabel("A part that passes DC between two nets (sense resistor, ferrite, "
                      "fuse, load switch…). The nets it joins are simulated together.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        f.addRow(hint)
        self._watch(self.name, self.r)

    def _load(self, k: DcLink):
        self.name.setText(k.name)
        self.a.set_value(k.side_a)
        self.b.set_value(k.side_b)
        self.r.setValue(k.resistance * 1e3)

    def _save(self, k: DcLink):
        new_a = self.a.value() or k.side_a
        typed = self.name.text().strip() or k.name
        if typed == k.side_a.ref and new_a.ref != k.side_a.ref:
            typed = new_a.ref
            self.name.setText(typed)
        k.name = typed
        k.side_a = new_a
        k.side_b = self.b.value() or k.side_b
        k.resistance = self.r.value() / 1e3


class ItemList(QWidget):
    """A list of setup items plus the editor for the selected one."""
    changed = Signal()

    def __init__(self, items: Callable[[], list], editor: _Editor, make_new: Callable[[], object],
                 describe: Callable[[object], str], parent=None):
        super().__init__(parent)
        self.items, self.editor, self.make_new, self.describe = items, editor, make_new, describe
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.lst = QListWidget(self)
        self.lst.currentRowChanged.connect(self._select)
        row = QHBoxLayout()
        for text, slot in (("Add", self._add), ("Duplicate", self._dup), ("Remove", self._remove)):
            b = QPushButton(text, self)
            b.clicked.connect(slot)
            row.addWidget(b)
        split = QSplitter(Qt.Vertical, self)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.lst)
        tl.addLayout(row)
        split.addWidget(top)
        split.addWidget(editor)
        split.setSizes([120, 280])
        lay.addWidget(split)
        editor.changed.connect(self._edited)
        editor.setEnabled(False)

    def reload(self):
        row = self.lst.currentRow()
        self.lst.blockSignals(True)
        self.lst.clear()
        for obj in self.items():
            self.lst.addItem(self.describe(obj))
        self.lst.blockSignals(False)
        n = self.lst.count()
        self.lst.setCurrentRow(min(max(row, 0), n - 1) if n else -1)
        self._select(self.lst.currentRow())

    def _select(self, row):
        items = self.items()
        if 0 <= row < len(items):
            self.editor.setEnabled(True)
            self.editor.load(items[row])
        else:
            self.editor.setEnabled(False)
            self.editor.obj = None

    def _edited(self):
        row = self.lst.currentRow()
        if 0 <= row < len(self.items()):
            self.lst.item(row).setText(self.describe(self.items()[row]))
        self.changed.emit()

    def _add(self):
        obj = self.make_new()
        if obj is None:
            return
        self.items().append(obj)
        self.reload()
        self.lst.setCurrentRow(self.lst.count() - 1)
        self.changed.emit()

    def _dup(self):
        import copy
        row = self.lst.currentRow()
        if row < 0:
            return
        obj = copy.deepcopy(self.items()[row])
        obj.name = _unique(obj.name, [x.name for x in self.items()])
        self.items().append(obj)
        self.reload()
        self.lst.setCurrentRow(self.lst.count() - 1)
        self.changed.emit()

    def _remove(self):
        row = self.lst.currentRow()
        if row < 0:
            return
        del self.items()[row]
        self.reload()
        self.changed.emit()


def _unique(base: str, taken: List[str]) -> str:
    if base not in taken:
        return base
    m = re.match(r"(.*?)(\d+)$", base)
    stem, k = (m.group(1), int(m.group(2))) if m else (base + " ", 1)
    while f"{stem}{k}" in taken:
        k += 1
    return f"{stem}{k}"


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class PIPanel(QWidget):
    solve_requested = Signal(object)      # PISetup
    field_changed = Signal(str)
    group_changed = Signal(str)
    show_geometry = Signal()
    save_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot: Optional[BoardSnapshot] = None
        self.setup = PISetup()
        self._nets: List[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        row = QHBoxLayout()
        self.lbl_file = QLabel("No board", self)
        self.lbl_file.setStyleSheet("color: #999;")
        self.lbl_file.setWordWrap(True)
        b_save = QPushButton("Save", self)
        b_save.setToolTip("Save the setup next to the board")
        b_save.clicked.connect(self.save_requested.emit)
        row.addWidget(self.lbl_file, 1)
        row.addWidget(b_save)
        lay.addLayout(row)

        self.tabs = QTabWidget(self)
        self.vrm_list = ItemList(lambda: self.setup.vrms, VrmEditor(self), self._new_vrm,
                                 self._describe_vrm)
        self.load_list = ItemList(lambda: self.setup.loads, LoadEditor(self), self._new_load,
                                  self._describe_load)
        self.link_list = ItemList(lambda: self.setup.links, LinkEditor(self), self._new_link,
                                  self._describe_link)
        self.tabs.addTab(self.vrm_list, "VRMs")
        self.tabs.addTab(self.load_list, "Loads")
        self.tabs.addTab(self.link_list, "DC connections")
        self.tabs.addTab(self._settings_tab(), "Mesh")
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.changed.connect(self._on_changed)
        lay.addWidget(self.tabs, 3)

        self.lbl_errors = QLabel(self)
        self.lbl_errors.setWordWrap(True)
        self.lbl_errors.setStyleSheet("color: #ff8080;")
        lay.addWidget(self.lbl_errors)
        self.btn_solve = QPushButton("Solve all", self)
        self.btn_solve.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_solve.clicked.connect(lambda: self.solve_requested.emit(self.setup))
        lay.addWidget(self.btn_solve)

        g_res = QGroupBox("Results", self)
        lr = QVBoxLayout(g_res)
        row = QHBoxLayout()
        self.cmb_group = QComboBox(g_res)
        self.cmb_group.setToolTip("Net group shown in 3D")
        self.cmb_group.currentTextChanged.connect(
            lambda t: t and self.group_changed.emit(t))
        self.cmb_field = QComboBox(g_res)
        for key, (_, _, label, unit) in FIELDS.items():
            self.cmb_field.addItem(f"{label} ({unit})", key)
        self.cmb_field.currentIndexChanged.connect(
            lambda i: self.field_changed.emit(self.cmb_field.itemData(i)))
        b_geom = QPushButton("Show copper", g_res)
        b_geom.clicked.connect(self.show_geometry.emit)
        row.addWidget(self.cmb_group, 2)
        row.addWidget(self.cmb_field, 2)
        row.addWidget(b_geom)
        lr.addLayout(row)
        self.res_tabs = QTabWidget(g_res)
        self.tbl_loads = self._table(["Load", "Group", "I (A)", "V eff (V)", "Drop (mV)",
                                      "Drop %", "Limit %", ""])
        self.tbl_vrms = self._table(["VRM", "Type", "V out", "I out (A)", "V in", "I in (A)",
                                     "P in (W)", "Loss (W)"])
        self.tbl_links = self._table(["Part", "Group", "R (mΩ)", "I (A)", "P (mW)"])
        self.txt_notes = QTextBrowser(g_res)
        self.res_tabs.addTab(self.tbl_loads, "Loads")
        self.res_tabs.addTab(self.tbl_vrms, "VRMs")
        self.res_tabs.addTab(self.tbl_links, "DC connections")
        self.res_tabs.addTab(self.txt_notes, "Notes")
        self.tbl_loads.cellDoubleClicked.connect(self._row_to_group)
        lr.addWidget(self.res_tabs)
        lay.addWidget(g_res, 2)
        self.setEnabled(False)

    # ------------------------------------------------------------------ widgets
    def _table(self, headers) -> QTableWidget:
        t = QTableWidget(0, len(headers), self)
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        return t

    def _settings_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.setup.settings
        self.spin_edge = _spin(0.05, 5.0, s.max_edge_mm, 2, " mm", 0.05)
        self.spin_grading = _spin(0.0, 2.0, s.grading, 2, "", 0.05)
        self.spin_plating = _spin(1.0, 100.0, s.plating_mm * 1000, 1, " µm", 1.0)
        f.addRow("Max edge", self.spin_edge)
        f.addRow("Via grading", self.spin_grading)
        f.addRow("Barrel plating", self.spin_plating)
        for sp in (self.spin_edge, self.spin_grading, self.spin_plating):
            sp.valueChanged.connect(self._settings_changed)
        return w

    def _settings_changed(self, *_):
        s = self.setup.settings
        s.max_edge_mm = self.spin_edge.value()
        s.grading = self.spin_grading.value()
        s.plating_mm = self.spin_plating.value() / 1000
        self._on_changed()

    # ------------------------------------------------------------------ data
    def set_board(self, snap: BoardSnapshot, setup: PISetup, file_label: str):
        self.snapshot = snap
        self.setup = setup
        self.lbl_file.setText(file_label)
        for sp, v in ((self.spin_edge, setup.settings.max_edge_mm),
                      (self.spin_grading, setup.settings.grading),
                      (self.spin_plating, setup.settings.plating_mm * 1000)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
        self.clear_results()
        self.setEnabled(True)
        self._refresh_pickers()
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.reload()
        self._on_changed()

    def set_nets(self, nets: List[str]):
        """Nets offered in the pin pickers (the ones ticked in the Board panel)."""
        if self.snapshot is None:
            return
        order = {n: i for i, n in enumerate(sorted_nets(self.snapshot))}
        nets = sorted(set(nets), key=lambda n: order.get(n, len(order)))
        if nets == self._nets:
            return
        self._nets = nets
        self._refresh_pickers()
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.reload()

    def _refresh_pickers(self):
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.editor.refresh_nets(self._nets)

    def _on_changed(self):
        errors = validate(self.setup) if (self.setup.vrms or self.setup.loads) else []
        if not self.setup.vrms:
            errors.insert(0, "Add a VRM (the source of a net) to start")
        self.lbl_errors.setText("\n".join(f"• {e}" for e in errors))
        self.btn_solve.setEnabled(not errors)

    # ------------------------------------------------------------------ new items
    def _first_net(self) -> Optional[str]:
        if not self._nets:
            self.lbl_errors.setText("• Tick nets in the Board panel first")
            return None
        return self._nets[0]

    def _first_part(self, net) -> str:
        parts = parts_on_net(self.snapshot, net)
        return parts[0][0] if parts else ""

    def _new_vrm(self):
        net = self._first_net()
        if net is None:
            return None
        name = _unique("VRM1", [x.name for x in self.setup.vrms])
        return Vrm(name, PinRef(self._first_part(net), net), guess_voltage(net), "source")

    def _new_load(self):
        net = self._first_net()
        if net is None:
            return None
        ref = self._first_part(net)
        return Load(_unique(ref or "LOAD1", [x.name for x in self.setup.loads]),
                    PinRef(ref, net), 0.1)

    def _new_link(self):
        net = self._first_net()
        if net is None:
            return None
        other = self._nets[1] if len(self._nets) > 1 else net
        ref = self._first_part(net)
        return DcLink(_unique(ref or "R1", [x.name for x in self.setup.links]),
                      PinRef(ref, net), PinRef(self._first_part(other), other), 0.0)

    @staticmethod
    def _describe_vrm(v: Vrm) -> str:
        src = "" if v.kind == "source" else f"  ← {v.input.net if v.input else '?'}"
        return f"{v.name}: {v.output.net} {v.voltage:g} V ({v.kind}){src}"

    @staticmethod
    def _describe_load(l: Load) -> str:
        return f"{l.name}: {l.current:g} A from {l.supply.net} ({l.supply.label()})"

    @staticmethod
    def _describe_link(k: DcLink) -> str:
        r = "short" if k.resistance <= 0 else f"{k.resistance * 1e3:g} mΩ"
        return f"{k.name}: {k.side_a.net} ↔ {k.side_b.net} ({r})"

    # ------------------------------------------------------------------ results
    def set_busy(self, busy: bool, queued: bool = False):
        self.btn_solve.setEnabled(not busy and not self.lbl_errors.text())
        self.btn_solve.setText(("Queued…" if queued else "Solving…") if busy else "Solve all")

    def current_field(self) -> str:
        return self.cmb_field.currentData()

    def clear_results(self):
        for t in (self.tbl_loads, self.tbl_vrms, self.tbl_links):
            t.setRowCount(0)
        self.txt_notes.clear()
        self.cmb_group.blockSignals(True)
        self.cmb_group.clear()
        self.cmb_group.blockSignals(False)

    def show_error(self, message: str):
        self.res_tabs.setCurrentWidget(self.txt_notes)
        self.txt_notes.setHtml(f'<span style="color:#ff7070"><b>Cannot solve:</b><br>'
                               f'{html.escape(message).replace(chr(10), "<br>")}</span>')

    def show_results(self, tree, initial_group: str):
        def put(t, r, c, text, color=None):
            it = QTableWidgetItem(text)
            if color:
                it.setForeground(QColor(color))
            t.setItem(r, c, it)

        self.tbl_loads.setRowCount(len(tree.loads))
        for r, l in enumerate(tree.loads):
            status = "" if l.passed is None else ("PASS" if l.passed else "FAIL")
            color = None if l.passed is None else ("#60d060" if l.passed else "#ff6060")
            for c, text in enumerate([l.name, l.group, f"{l.current:.4g}", f"{l.v_effective:.5f}",
                                      f"{l.drop * 1e3:.3f}", f"{l.drop_pct:.3f}",
                                      "" if l.tolerance_pct is None else f"{l.tolerance_pct:g}",
                                      status]):
                put(self.tbl_loads, r, c, text, color if c == 7 else None)
        self.tbl_vrms.setRowCount(len(tree.vrms))
        f = lambda x, fmt: "" if x is None else format(x, fmt)
        for r, v in enumerate(tree.vrms):
            for c, text in enumerate([v.name, v.kind, f"{v.v_out:.5f}", f"{v.i_out:.4g}",
                                      f(v.v_in, ".5f"), f(v.i_in, ".4g"), f(v.p_in, ".4g"),
                                      f(v.p_loss, ".4g")]):
                put(self.tbl_vrms, r, c, text)
        rows = [(g.name, s) for g in tree.groups.values() for s in g.result.fields.series]
        self.tbl_links.setRowCount(len(rows))
        for r, (gname, s) in enumerate(rows):
            cur = "" if math.isnan(s.current) else f"{s.current:.4g}"
            for c, text in enumerate([s.name, gname, f"{s.resistance * 1e3:g}", cur,
                                      f"{s.power * 1e3:.3f}"]):
                put(self.tbl_links, r, c, text)
        notes = [f"Solved {len(tree.groups)} net group(s) in {tree.solve_s:.2f} s:"]
        for g in tree.groups.values():
            role = f"driven by {g.driver}" if g.kind == "rail" else f"ground, referenced at {g.driver}"
            notes.append(f"&nbsp;&nbsp;<b>{html.escape(g.name)}</b> ({role}): "
                         f"{html.escape(', '.join(g.nets))}")
        notes += [f"⚠ {html.escape(w)}" for w in tree.warnings]
        self.txt_notes.setHtml("<br>".join(notes))
        self.cmb_group.blockSignals(True)
        self.cmb_group.clear()
        self.cmb_group.addItems(list(tree.groups))
        self.cmb_group.setCurrentText(initial_group)
        self.cmb_group.blockSignals(False)
        self.res_tabs.setCurrentWidget(self.tbl_loads)

    def _row_to_group(self, row, _col):
        it = self.tbl_loads.item(row, 1)
        if it:
            self.cmb_group.setCurrentText(it.text())
