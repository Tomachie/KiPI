# KiPI — DC power integrity for KiCad 10

[![CI](https://github.com/drewsum/KiPI/actions/workflows/ci.yml/badge.svg)](https://github.com/drewsum/KiPI/actions/workflows/ci.yml)

KiPI is a HyperLynx-PI-style DC drop analyser that runs as a KiCad 10 IPC plugin. The planned
workflow:

1. Define voltage regulators (VRMs), loads (current into pins) and DC series elements.
2. Simulate voltage drop net by net, across every copper layer.
3. Inspect voltage, current density, via currents and copper power dissipation in a rotatable
   3D view, with live solver progress.

**Status: Milestone 3.** Board extraction, the 3D copper viewer and a DC power-tree solver
work. Tick the nets you care about in the Board panel, then describe the power system in the
**PI setup** panel. Nothing is inferred from the board; every entry is yours:

* **VRMs:** an ideal *source*, an *LDO* (I_in = I_out + I_q) or a *switcher*
  (P_in = P_out / efficiency), with output pins and set voltage. LDOs and switchers also need
  input pins, which is how the power tree is defined. Sense pins (regulate the voltage there)
  and ground pins are optional.
* **Loads:** supply pins, current, optional ground pins and an optional maximum drop (%)
  for PASS/FAIL.
* **DC connections:** parts that pass DC between nets (sense resistors, ferrites, fuses,
  load switches), with their resistance. Nets joined this way are simulated as one group,
  and each group may have only one VRM.

**Solve all** solves every group, loads first and then back toward the source,
including ground return if ground pins are set. It lists each load's drop and PASS/FAIL,
each VRM's input and output, and each DC connection's current and loss. Pick a group to
see its voltage, current density, power density or via current in 3D, and click to probe.
The setup is saved as `<board>.kipi-setup.json` next to the board.
See [docs/research.md](docs/research.md) for design notes.

## Install (development)

1. In KiCad, enable **Preferences → Plugins → Enable KiCad API**.
2. Link this repo into your KiCad plugins folder. KiCad uses your *Documents* folder, which may
   be redirected to OneDrive:

   ```powershell
   $plug = "$([Environment]::GetFolderPath('MyDocuments'))\KiCad\10.0\plugins"
   New-Item -ItemType Junction -Path "$plug\KiPI" -Target "<path to this repo>"
   ```

3. Restart the PCB editor. KiCad builds the plugin venv in the background; the first time this
   downloads PySide6 and VTK. The **KiPI** toolbar button appears once the venv is ready. If it
   never appears, right-click the action in *Preferences → Plugins* and choose
   *Recreate Plugin Environment*.

## Standalone use

```powershell
python -m venv --system-site-packages .venv   # use KiCad's bundled python.exe
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m kipi                         # connect to the running PCB editor
.venv\Scripts\python -m kipi --dump board.kipi.json.gz   # extract only
.venv\Scripts\python -m kipi --snapshot tests\data\synthetic.kipi.json
.venv\Scripts\python -m pytest tests
```

Each session writes its own log to `%TEMP%\kipi\kipi-<date>-<time>-<pid>.log`, and the last 15
sessions are kept. A matching `.crash.log` collects native crash tracebacks, plus stack dumps
of every thread if a background task runs longer than a minute (useful when something hangs).

## Layout

| Path | Purpose |
|---|---|
| `kipi_entry.py`, `plugin.json` | KiCad IPC plugin action |
| `kipi/kicad_io/` | kipy → `BoardSnapshot` extraction, JSON snapshot save/load |
| `kipi/model/` | Toolkit-independent board model, power-net heuristics |
| `kipi/geometry/` | Per-net, per-layer copper union (shapely) |
| `kipi/mesh/` | Conforming per-layer triangle mesh (Triangle), graded around plated holes |
| `kipi/solver/` | 2.5D FEM: sheet conductance, via/PTH barrels, series parts, sources, loads; LU / AMG-CG |
| `kipi/pi/` | User power-tree setup (VRMs, loads, DC connections), JSON, validation, tree solver |
| `kipi/viz/` | PyVista mesh building and batching, result meshes (no Qt) |
| `kipi/gui/` | PySide6 main window, embedded 3D viewer, worker threads |
| `kipi/progress.py` | Progress/log reporting used by every pipeline stage |
