# KiPI — DC power integrity for KiCad 10

KiPI is a HyperLynx-PI-style DC drop analyser that runs as a KiCad 10 IPC plugin. The planned
workflow:

1. Define voltage regulators (VRMs), loads (current into pins) and DC series elements.
2. Simulate voltage drop net by net, across every copper layer.
3. Inspect voltage, current density, via currents and copper power dissipation in a rotatable
   3D view, with live solver progress.

**Status: Milestone 1.** Board extraction, offline snapshots and the interactive 3D copper viewer
work. The solver is next. See [docs/research.md](docs/research.md) for design notes.

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
.venv\Scripts\pip install -r requirements.txt pytest
.venv\Scripts\python -m kipi                         # connect to the running PCB editor
.venv\Scripts\python -m kipi --dump board.kipi.json.gz   # extract only
.venv\Scripts\python -m kipi --snapshot tests\data\synthetic.kipi.json
.venv\Scripts\python -m pytest tests
```

The log is written to `%TEMP%\kipi.log`.

## Layout

| Path | Purpose |
|---|---|
| `kipi_entry.py`, `plugin.json` | KiCad IPC plugin action |
| `kipi/kicad_io/` | kipy → `BoardSnapshot` extraction, JSON snapshot save/load |
| `kipi/model/` | Toolkit-independent board model, power-net heuristics |
| `kipi/geometry/` | Per-net, per-layer copper union (shapely) |
| `kipi/viz/` | PyVista mesh building (no Qt) |
| `kipi/gui/` | PySide6 main window, embedded 3D viewer, worker threads |
| `kipi/progress.py` | Progress/log reporting used by every pipeline stage |
