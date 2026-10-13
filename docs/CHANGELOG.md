# Changelog

All notable changes to KiPI, newest first. Each minor version is one development milestone.
The code's version number went from 0.1.0 straight to 0.5.0, so 0.2.0 to 0.4.0 name
milestones rather than versions that were ever set.

## 0.6.0 (2026-10-12): Quality of life

### Added
- **Multi-phase VRMs.** A VRM can have N parallel phases, each with its own output, input
  and ground pins and one shared pair of sense pins. Each phase carries 1/N of the output
  current and settles at whatever voltage its own copper gives it. The VRM regulates its
  sense pins or, without them, the average of its phase outputs. Each phase draws its own
  input current and returns its own ground current.
  - The VRMs results tab, the HTML report and a new `<board>_vrm_phases.csv` export list
    every phase's voltages, currents and loss.
  - In the thermal co-simulation, each phase's conversion loss is placed at its own output
    pins.
  - Validation checks that phases share rails and grounds but not pins.
  - Setups and results files saved by older versions still load, as single-phase VRMs.
- **Negative remote sense.** Optional ground sense pins: the VRM holds its set voltage
  relative to them instead of 0 V.
- **Per-layer copper resistivity.** The Copper tab lists every layer with its own resistivity,
  overriding the board default, for example for electroplated or rolled-annealed foil.
- **Copper thickness unit.** Layer thicknesses can be shown in µm, mil or oz. This only
  changes the display, and the choice is remembered.
- **Example board.** `examples/multiphase_buck` is a 4-phase 12 V to 0.9 V, 40 A core rail,
  with its snapshot, PI setup and the script that generates them.
- **Linting.** ruff runs in CI. It is a development dependency only and is never installed
  into KiCad's plugin venv.
- **Tests.** The tests solve the example board and check that its committed setup still
  matches its generator.
- **Changelog.** This file.

### Changed
- American spelling throughout the user interface, messages and documentation (color,
  analyze, center, gray, and so on).
- The README explains what the 3D voltages are measured against, and why a plane can read
  above its set point when ground returns are solved.

### Fixed
- Ticking or unticking nets in the Board panel could overwrite the VRM or load being
  edited with a different net and voltage. For example, a source VRM could end up driving
  another VRM's output.
- Startup could crash on Windows (0xC000041D) when the crash handler tried to dump the
  stacks of threads that were still running.
- Unused imports, an unhelpful exception chain in the mesher and other issues found by ruff.

## 0.5.0 (2026-10-11): Electro-thermal co-simulation and HTML report

### Added
- **Electro-thermal co-simulation** (Thermal tab, off by default).
  - A board-level heat model: in-plane conduction through every layer's copper and the
    laminate, and convection from both faces with an airflow setting.
  - Each iteration solves the power tree with temperature-dependent copper resistivity,
    then solves the board temperature from all the heat: copper, vias, DC connections and
    VRM conversion loss.
  - A Temperature result view, with the peak temperature and the iteration history.
- **Laminate conductivity from the stackup.** Dielectric material names are matched to
  typical thermal conductivities, covering FR4 glass styles and resin content, high-Tg FR4,
  polyimide, PTFE, Rogers, ceramics and metal cores. Unrecognized names fall back to FR4 with
  a warning, and **Laminate k** overrides the result.
- **HTML report.** A single offline file containing the summary and every results table, an
  interactive 3D view per net group, per-layer statistics with top-down voltage and
  current-density maps, and the thermal map.
- Package metadata in `pyproject.toml`, a `kipi` command-line entry point, the GPL-3.0-or-later
  license, the README user guide with screenshots, and the KiPI logo.

### Changed
- Repeated solves warm-start the AMG solver and reuse cached meshes.

## 0.4.0 (2026-10-11): Results tools

### Added
- **Current-flow arrows** in the current-density view.
- **Cut planes** along X, Y or Z, with a position slider and a flip option.
- **Vias tab.** Every via and PTH barrel ranked by current, with an optional current limit
  for PASS/FAIL. Double-click a row to fly to the via.
- **Losses tab.** Copper loss per group and layer, plus vias, DC connections and VRM
  conversion.
- **Exports.**
  - CSV tables.
  - ParaView `.vtm` files per group.
  - Saving and reopening `.kipi-results` files without solving again.
- **Load pin groups.** A load can split its current unevenly across groups of pins.
  Results report the worst pin, and PASS/FAIL judges it.

## 0.3.0 (2026-10-11): Power tree

### Added
- **PI setup.** Define VRMs, loads and DC connections, each referenced by part and pad.
  - VRMs can be sources, LDOs or switchers, with efficiency and quiescent current.
  - VRMs can have remote sense pins and ground pins.
  - Loads have supply and ground pins, a current and a drop tolerance.
  - The setup is saved next to the board as `<board>.kipi-setup.json` and reloaded with it.
  - Live validation keeps **Solve all** disabled until the setup is valid.
- **Power-tree solver.** Rails are solved downstream first, and each VRM's input current
  becomes a load on the rail that feeds it. Switcher input current is iterated until it
  converges. Ground returns are solved too.
- **DC connections.** Series parts such as resistors, ferrites and 0 Ω links join nets.
- **Results tables.**
  - Loads: drop, %, PASS/FAIL.
  - VRMs: voltage, current and power in and out, and loss.
  - DC connections: current and dissipation.
- **Searchable pickers.** Type part of a net, reference or value to filter the net and part
  pickers.

### Changed
- The single-net solve panel is replaced by the PI setup panel.
- The linear system is factorized once and reused across power-tree iterations.

## 0.2.0 (2026-10-11): DC solver

### Added
- **Mesher.** A conforming triangle mesh per net and layer, with pad outlines and plated-hole
  rings, refined towards holes.
- **2.5D FEM DC solver.** Sheet conductance per layer plus via and PTH barrel resistance. A
  direct solve is used for small boards and AMG-preconditioned CG for large ones. Results
  are checked against closed-form cases.
- **Result views.**
  - Voltage, current density, power density and via current.
  - Click any copper point or via to probe it.
  - Vias are picked by on-screen distance.
- **Logs.** Per-session logs, with crash tracebacks and a watchdog that dumps every thread's
  stack during long tasks.

### Fixed
- Hangs when KiCad stopped reading the plugin's output, near-endless mesh refinement on pad
  slivers, and a Triangle crash on unnoded geometry.

## 0.1.0 (2026-10-11): Board extraction and 3D viewer

### Added
- **KiCad 10 IPC plugin** (`plugin.json`) that extracts the stackup, nets, tracks, arcs,
  vias, pads, footprints, zone fills and board outline.
- **Snapshots.** JSON and gzip board snapshots for working without KiCad. Open one with
  `python -m kipi --snapshot`.
- **3D copper viewer.**
  - Copper is drawn at its true heights, with via barrels, pads and the board body.
  - Layer and net toggles, Z exaggeration, and picking with select-in-KiCad.
  - Nets render as they are ticked.
  - Meshes are batched so boards with hundreds of nets stay responsive.
- **Development.** CI runs the tests on Windows and Linux with Python 3.11, and research
  notes are in `docs/research.md`.
