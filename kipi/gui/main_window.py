"""KiPI main window: board browser, 3D copper view and live log."""

from __future__ import annotations

import html
import logging
import math
from pathlib import Path
from typing import Dict, List, Optional

from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QAction, QColor, QIcon, QPixmap, QTextCursor
from qtpy.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTextBrowser, QToolBar,
    QVBoxLayout, QWidget,
)

from kipi import __version__
from kipi.gui.viewer3d import Viewer3D
from kipi.gui.worker import QtProgress, Task
from kipi.model.board import BoardSnapshot
from kipi.model.nets import classify_nets, sorted_nets

log = logging.getLogger("kipi")

_KIND_TAG = {"power": "PWR", "ground": "GND", "signal": ""}
_KIND_COLOR = {"power": "#e07050", "ground": "#70a0e0", "signal": "#a0a0a0"}
MAX_AUTO_NETS = 4


class MainWindow(QMainWindow):
    def __init__(self, kicad=None, board=None, snapshot: Optional[BoardSnapshot] = None):
        super().__init__()
        self.setWindowTitle(f"KiPI {__version__} — Power Integrity")
        self.resize(1500, 950)
        self._kicad = kicad
        self._board = board
        self.snapshot: Optional[BoardSnapshot] = None
        self._copper_cache: Dict[str, Dict[int, object]] = {}
        self._task: Optional[Task] = None
        self._resync_pending = False   # net checkboxes changed while a task was running
        self._sync_timer = QTimer(self)  # coalesce rapid checkbox clicks into one build
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(150)
        self._sync_timer.timeout.connect(self.sync_nets)
        self._picked = None   # ("pad"|"via", id)

        self.viewer = Viewer3D(self)
        self.setCentralWidget(self.viewer)
        self.viewer.on_pick(self._on_pick)

        self._build_toolbar()
        self._build_left_panel()
        self._build_log_dock()
        self.statusBar().showMessage("Ready")

        if snapshot is not None:
            QTimer.singleShot(0, lambda: self._set_snapshot(snapshot))
        elif board is not None:
            QTimer.singleShot(0, self.load_from_kicad)

    # ================================================================== UI
    def _build_toolbar(self):
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, tip=""):
            a = QAction(text, self)
            a.setToolTip(tip or text)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        self.act_reload = act("Load from KiCad", self.load_from_kicad,
                              "Re-read the board from the running KiCad PCB editor")
        self.chk_refill = QCheckBox("Refill zones", self)
        self.chk_refill.setToolTip("Refill all zones in KiCad before reading (recommended)")
        tb.addWidget(self.chk_refill)
        tb.addSeparator()
        act("Open snapshot…", self.open_snapshot, "Load a saved board snapshot (.json / .json.gz)")
        self.act_save = act("Save snapshot…", self.save_snapshot,
                            "Save the extracted board for offline use")
        tb.addSeparator()
        act("Top", self.viewer.view_top)
        act("Bottom", self.viewer.view_bottom)
        act("Iso", self.viewer.view_iso)
        act("Screenshot…", self.save_screenshot)

    def _build_left_panel(self):
        panel = QWidget(self)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 6, 6, 6)

        # --- nets
        g_nets = QGroupBox("Nets", panel)
        ln = QVBoxLayout(g_nets)
        self.net_filter = QLineEdit(g_nets)
        self.net_filter.setPlaceholderText("Filter nets…")
        self.net_filter.textChanged.connect(self._apply_net_filter)
        self.chk_power_only = QCheckBox("Power && ground only", g_nets)
        self.chk_power_only.setChecked(True)
        self.chk_power_only.toggled.connect(self._apply_net_filter)
        self.net_list = QListWidget(g_nets)
        self.net_list.itemChanged.connect(self._on_net_item_changed)
        row = QHBoxLayout()
        btn_power = QPushButton("All power", g_nets)
        btn_power.setToolTip("Show every power and ground net")
        btn_power.clicked.connect(self._check_power_nets)
        btn_none = QPushButton("None", g_nets)
        btn_none.clicked.connect(lambda: self._set_checked(lambda it: False))
        row.addWidget(btn_power, 1)
        row.addWidget(btn_none, 1)
        ln.addWidget(self.net_filter)
        ln.addWidget(self.chk_power_only)
        ln.addWidget(self.net_list, 1)
        ln.addLayout(row)

        # --- layers
        g_layers = QGroupBox("Copper layers", panel)
        ll = QVBoxLayout(g_layers)
        self.layer_list = QListWidget(g_layers)
        self.layer_list.itemChanged.connect(self._on_layer_item_changed)
        ll.addWidget(self.layer_list)

        # --- display options
        g_view = QGroupBox("Display", panel)
        fv = QFormLayout(g_view)
        self.cmb_color = QComboBox(g_view)
        self.cmb_color.addItems(["By layer", "By net"])
        self.cmb_color.currentIndexChanged.connect(
            lambda i: self.viewer.set_color_mode("net" if i == 1 else "layer"))
        self.spin_z = QDoubleSpinBox(g_view)
        self.spin_z.setRange(1.0, 100.0)
        self.spin_z.setSingleStep(1.0)
        self.spin_z.setValue(5.0)
        self.spin_z.setSuffix(" ×")
        self.spin_z.setToolTip("Z exaggeration (stackup thickness only)")
        self.spin_z.valueChanged.connect(self.viewer.set_zscale)
        self.viewer.set_zscale(self.spin_z.value())
        feats = QHBoxLayout()
        for name, label in (("vias", "Vias"), ("pads", "Pads"), ("board", "Board")):
            cb = QCheckBox(label, g_view)
            cb.setChecked(True)
            cb.toggled.connect(lambda v, n=name: self.viewer.set_feature_visible(n, v))
            feats.addWidget(cb)
        fv.addRow("Colour", self.cmb_color)
        fv.addRow("Z scale", self.spin_z)
        fv.addRow(feats)

        # --- info
        g_info = QGroupBox("Info", panel)
        li = QVBoxLayout(g_info)
        self.info = QTextBrowser(g_info)
        self.info.setOpenExternalLinks(False)
        self.btn_select_kicad = QPushButton("Select in KiCad", g_info)
        self.btn_select_kicad.setEnabled(False)
        self.btn_select_kicad.clicked.connect(self._select_in_kicad)
        li.addWidget(self.info, 1)
        li.addWidget(self.btn_select_kicad)

        split = QSplitter(Qt.Vertical, panel)
        for w in (g_nets, g_layers, g_view, g_info):
            split.addWidget(w)
        split.setSizes([360, 170, 110, 220])
        lay.addWidget(split)

        dock = QDockWidget("Board", self)
        dock.setObjectName("board_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(panel)
        dock.setMinimumWidth(300)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def _build_log_dock(self):
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.lbl_stage = QLabel("Idle", w)
        self.progress_bar = QProgressBar(w)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(14)
        self.btn_cancel = QPushButton("Cancel", w)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_task)
        row.addWidget(self.lbl_stage, 2)
        row.addWidget(self.progress_bar, 3)
        row.addWidget(self.btn_cancel)
        self.log_view = QPlainTextEdit(w)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self.log_view.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        lay.addLayout(row)
        lay.addWidget(self.log_view)
        dock = QDockWidget("Log", self)
        dock.setObjectName("log_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(w)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [200], Qt.Vertical)

    # ============================================================ logging
    def log(self, text: str, level: int = logging.INFO):
        color = {logging.WARNING: "#e0b050", logging.ERROR: "#ff6060"}.get(level)
        if text.startswith("== "):
            color = "#80c0ff"
        esc = html.escape(text)
        self.log_view.appendHtml(f'<span style="color:{color}">{esc}</span>' if color else esc)
        self.log_view.moveCursor(QTextCursor.End)

    def _on_stage(self, name: str):
        self.lbl_stage.setText(name)
        self.statusBar().showMessage(name)

    def _on_fraction(self, f: float):
        if f < 0:
            self.progress_bar.setRange(0, 0)   # busy indicator
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(1000 * f))

    # ============================================================== tasks
    def _busy(self) -> bool:
        return self._task is not None and self._task.isRunning()

    def _run(self, fn, on_done, name: str):
        if self._busy():
            QMessageBox.information(self, "KiPI", "Another operation is still running.")
            return None
        prog = QtProgress()
        prog.sig_message.connect(self.log)
        prog.sig_stage.connect(self._on_stage)
        prog.sig_fraction.connect(self._on_fraction)
        task = Task(fn, prog, self)
        task.done.connect(on_done)
        task.failed.connect(lambda tb: self._on_task_failed(name, tb))
        task.finished.connect(self._on_task_finished)
        self._task = task
        self.btn_cancel.setEnabled(True)
        self.act_reload.setEnabled(False)
        task.start()
        return task

    def _on_task_finished(self):
        self.btn_cancel.setEnabled(False)
        self.act_reload.setEnabled(True)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self.lbl_stage.setText("Idle")
        self.statusBar().showMessage("Ready")
        if self._resync_pending:
            self._resync_pending = False
            self._sync_timer.start()

    def _on_task_failed(self, name: str, tb: str):
        log.error(tb)
        self.log(tb, logging.ERROR)
        QMessageBox.critical(self, "KiPI", f"{name} failed:\n\n{tb.strip().splitlines()[-1]}")

    def _cancel_task(self):
        if self._task is not None:
            self._task.progress.cancel()

    # ===================================================== board loading
    def load_from_kicad(self):
        refill = self.chk_refill.isChecked()

        def work(progress):
            from kipi.kicad_io.extract import connect, extract_board
            if self._board is None:
                self._kicad, self._board = connect(progress=progress)
            snap = extract_board(self._board, refill_zones=refill, progress=progress)
            try:
                snap.kicad_version = str(self._kicad.get_version())
            except Exception:  # noqa: BLE001
                pass
            return snap

        self._run(work, self._set_snapshot, "Loading board")

    def open_snapshot(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open board snapshot", "",
                                              "KiPI snapshot (*.json *.json.gz)")
        if not path:
            return
        from kipi.kicad_io.snapshot import load

        self._run(lambda progress: load(path), self._set_snapshot, "Opening snapshot")

    def save_snapshot(self):
        if self.snapshot is None:
            return
        default = str(Path(self.snapshot.project_dir or ".") /
                      f"{Path(self.snapshot.board_name).stem}.kipi.json.gz")
        path, _ = QFileDialog.getSaveFileName(self, "Save board snapshot", default,
                                              "KiPI snapshot (*.json.gz *.json)")
        if not path:
            return
        from kipi.kicad_io.snapshot import save
        save(self.snapshot, path)
        self.log(f"Snapshot saved to {path}")

    def save_screenshot(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", "kipi.png", "PNG (*.png)")
        if path:
            self.viewer.screenshot(path)
            self.log(f"Screenshot saved to {path}")

    def _set_snapshot(self, snap: BoardSnapshot):
        self.snapshot = snap
        self._copper_cache.clear()
        self._picked = None
        self.btn_select_kicad.setEnabled(False)
        self.setWindowTitle(f"KiPI {__version__} — {snap.board_name}")
        self._populate_layers()
        self._populate_nets()
        self._show_board_summary()
        from kipi.viz.meshes import board_body
        self.viewer.set_board(*board_body(snap))
        self.sync_nets()

    # ============================================================ lists
    def _populate_layers(self):
        from kipi.viz.meshes import layer_color
        st = self.snapshot.stackup
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for cu in st.copper:
            it = QListWidgetItem(f"{cu.name}   {cu.thickness_mm * 1000:.0f} µm   "
                                 f"z={cu.z_mid_mm:.3f} mm")
            r, g, b = layer_color(cu, len(st.copper))
            pm = QPixmap(12, 12)
            pm.fill(QColor.fromRgbF(r, g, b))
            it.setIcon(QIcon(pm))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, cu.id)
            self.layer_list.addItem(it)
        self.layer_list.blockSignals(False)

    def _populate_nets(self):
        snap = self.snapshot
        kinds = classify_nets(snap)
        zone_nets = {z.net for z in snap.zones}
        pad_count: Dict[str, int] = {}
        for p in snap.pads:
            pad_count[p.net] = pad_count.get(p.net, 0) + 1
        # Start with the main plane nets: a few power nets plus the main ground.
        by_pads = sorted(zone_nets, key=lambda n: -pad_count.get(n, 0))
        auto = set([n for n in by_pads if kinds.get(n) == "power"][:MAX_AUTO_NETS - 1] +
                   [n for n in by_pads if kinds.get(n) == "ground"][:1])

        self.net_list.blockSignals(True)
        self.net_list.clear()
        for name in sorted_nets(snap):
            kind = kinds[name]
            tag = _KIND_TAG[kind]
            extra = f"  [{tag}]" if tag else ""
            it = QListWidgetItem(f"{name}{extra}   ({pad_count.get(name, 0)} pads)")
            it.setData(Qt.UserRole, name)
            it.setData(Qt.UserRole + 1, kind)
            it.setForeground(QColor(_KIND_COLOR[kind]))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if name in auto else Qt.Unchecked)
            self.net_list.addItem(it)
        self.net_list.blockSignals(False)
        self._apply_net_filter()

    def _apply_net_filter(self):
        text = self.net_filter.text().strip().lower()
        power_only = self.chk_power_only.isChecked()
        for i in range(self.net_list.count()):
            it = self.net_list.item(i)
            name = it.data(Qt.UserRole).lower()
            kind = it.data(Qt.UserRole + 1)
            hide = (text and text not in name) or (power_only and kind == "signal"
                                                  and it.checkState() != Qt.Checked)
            it.setHidden(bool(hide))

    def _checked_nets(self) -> List[str]:
        return [self.net_list.item(i).data(Qt.UserRole) for i in range(self.net_list.count())
                if self.net_list.item(i).checkState() == Qt.Checked]

    def _set_checked(self, pred):
        """Check exactly the net items for which ``pred(item)`` is true, then sync once."""
        self.net_list.blockSignals(True)
        for i in range(self.net_list.count()):
            it = self.net_list.item(i)
            it.setCheckState(Qt.Checked if pred(it) else Qt.Unchecked)
        self.net_list.blockSignals(False)
        self._apply_net_filter()
        self._sync_timer.start()

    def _check_power_nets(self):
        self._set_checked(lambda it: it.checkState() == Qt.Checked
                          or it.data(Qt.UserRole + 1) != "signal")

    def _on_net_item_changed(self, item):
        self._sync_timer.start()

    def _on_layer_item_changed(self, item):
        self.viewer.set_layer_visible(item.data(Qt.UserRole), item.checkState() == Qt.Checked)

    # ============================================================ scene
    def sync_nets(self):
        """Make the 3D view match the checked nets.

        Unchecked nets are removed at once. Newly checked nets are built in the
        background and added as they finish; the nets already shown are left alone.
        """
        if self.snapshot is None:
            return
        if self._busy():
            self._resync_pending = True
            return
        wanted = self._checked_nets()
        shown = set(self.viewer.nets())
        for net in shown - set(wanted):
            self.viewer.remove_net(net)
        todo = [n for n in wanted if n not in shown]
        if not todo:
            return

        snap = self.snapshot
        cache = self._copper_cache

        def work(progress):
            from kipi.geometry.copper import net_copper
            from kipi.viz.meshes import build_net_scene
            scenes = []
            with progress.timed(f"Building {len(todo)} net(s)"):
                for i, net in enumerate(todo):
                    progress.check_cancel()
                    if net not in cache:
                        cache[net] = net_copper(snap, net, progress)
                    scenes.append(build_net_scene(snap, net, cache[net], progress))
                    progress.fraction((i + 1) / len(todo))
            return scenes

        def done(scenes):
            still_wanted = set(self._checked_nets())
            added = [ns for ns in scenes if ns.net in still_wanted]
            for ns in added:
                self.viewer.add_net(ns)
            for ns in scenes:
                if not ns.copper:
                    self.log(f"{ns.net}: no copper on any layer", logging.WARNING)
            n_cells = sum(lm.mesh.n_cells for ns in added for lm in ns.copper)
            self.log(f"Added {len(added)} net(s), {n_cells} cells; "
                     f"{len(self.viewer.nets())} net(s) shown")

        task = self._run(work, done, "Building view")
        if task is not None:
            # A cancelled build leaves its nets unchecked so the list matches the view.
            task.cancelled.connect(lambda: self._set_checked(
                lambda it: it.data(Qt.UserRole) in self.viewer.nets()))

    # ============================================================ info
    def _show_board_summary(self):
        s = self.snapshot
        st = s.stackup
        rows = "".join(
            f"<tr><td>{html.escape(c.name)}</td><td align=right>{c.thickness_mm * 1000:.1f} µm</td>"
            f"<td align=right>{c.z_mid_mm:.3f}</td></tr>" for c in st.copper)
        kinds = classify_nets(s)
        n_pwr = sum(1 for k in kinds.values() if k == "power")
        n_gnd = sum(1 for k in kinds.values() if k == "ground")
        self.info.setHtml(
            f"<b>{html.escape(s.board_name)}</b><br>"
            f"<small>{html.escape(s.source)} {html.escape(s.kicad_version)}</small><br><br>"
            f"Board thickness: {st.board_thickness_mm:.3f} mm"
            f"{'' if st.from_board else ' <i>(default stackup)</i>'}<br>"
            f"Nets: {len(s.nets)} ({n_pwr} power, {n_gnd} ground)<br>"
            f"Tracks: {len(s.tracks)} &nbsp; Vias: {len(s.vias)} &nbsp; Pads: {len(s.pads)}<br>"
            f"Zones: {len(s.zones)} &nbsp; Footprints: {len(s.footprints)}<br><br>"
            f"<table cellspacing=4><tr><th align=left>Layer</th><th>Cu</th><th>z mm</th></tr>"
            f"{rows}</table><br><i>Left-click copper in the 3D view to identify it.</i>")

    def _nearest_layer(self, z: float):
        st = self.snapshot.stackup
        return min(st.copper, key=lambda c: abs(c.z_mid_mm - z))

    def _on_pick(self, pt):
        if self.snapshot is None:
            return
        x, y, z = pt
        s = self.snapshot
        cu = self._nearest_layer(z)
        shown = set(self.viewer.nets())

        best_via, best_d = None, math.inf
        for v in s.vias:
            if v.net in shown:
                d = math.hypot(v.position[0] - x, v.position[1] - y)
                if d < best_d and d <= v.diameter_mm / 2 + 0.05:
                    best_via, best_d = v, d

        pad_hit = None
        from shapely.geometry import Point
        from kipi.geometry.copper import poly_to_shapely
        p0 = Point(x, y)
        for p in s.pads:
            if p.net not in shown or cu.id not in p.shapes:
                continue
            if math.hypot(p.position[0] - x, p.position[1] - y) > 10:
                continue
            for poly in p.shapes[cu.id]:
                g = poly_to_shapely(poly)
                if g is not None and g.buffer(0.01).contains(p0):
                    pad_hit = p
                    break
            if pad_hit:
                break

        lines = [f"<b>Picked</b> x={x:.3f} y={y:.3f} mm on <b>{html.escape(cu.name)}</b>"]
        self._picked = None
        if pad_hit is not None:
            p = pad_hit
            self._picked = ("pad", p.id)
            lines.append(f"<b>Pad {html.escape(p.label)}</b> ({p.pad_type})<br>"
                         f"Net: {html.escape(p.net)}<br>"
                         f"Layers: {', '.join(html.escape(s.stackup.layer(l).name) for l in p.layers)}")
            if p.drill_mm[0] > 0:
                lines.append(f"Drill: {p.drill_mm[0]:.3f} × {p.drill_mm[1]:.3f} mm")
        elif best_via is not None:
            v = best_via
            self._picked = ("via", v.id)
            names = [s.stackup.layer(l).name for l in v.layers if s.stackup.layer(l)]
            lines.append(f"<b>Via</b> ({v.via_type})<br>Net: {html.escape(v.net)}<br>"
                         f"Drill {v.drill_mm:.3f} mm / pad {v.diameter_mm:.3f} mm<br>"
                         f"Span: {html.escape(names[0])} → {html.escape(names[-1])}")
        else:
            # Which net's copper was hit on this layer?
            for net in shown:
                g = self._copper_cache.get(net, {}).get(cu.id)
                if g is not None and g.buffer(0.01).contains(p0):
                    lines.append(f"Copper of net <b>{html.escape(net)}</b>")
                    break
        self.info.setHtml("<br>".join(lines))
        self.btn_select_kicad.setEnabled(self._picked is not None and self._board is not None)
        self.statusBar().showMessage(f"x={x:.3f} y={y:.3f} mm  {cu.name}")

    def _select_in_kicad(self):
        if self._picked is None or self._board is None:
            return
        from kipy.proto.common.types import KIID

        class _Ref:  # add_to_selection only needs an object with an ``id``
            def __init__(self, value):
                self.id = KIID(value=value)

        try:
            self._board.clear_selection()
            self._board.add_to_selection([_Ref(self._picked[1])])
            self.log(f"Selected {self._picked[0]} in KiCad")
        except Exception as e:  # noqa: BLE001
            self.log(f"Could not select in KiCad: {e}", logging.WARNING)

    # ============================================================ close
    def closeEvent(self, event):
        if self._task is not None and self._task.isRunning():
            self._task.progress.cancel()
            self._task.wait(3000)
        self.viewer.close()
        super().closeEvent(event)
