# KiPI research notes (2026-10)

## Reference: KiPIDA (kbralten/KiPIDA, AGPL-3.0)

| Area | KiPIDA approach | Limitation |
|---|---|---|
| KiCad access | kipy IPC, hand-built socket path, `kicad_version: 9.99` | Hoop-jumping on KiCad 10; kipy already reads `KICAD_API_SOCKET`/`KICAD_API_TOKEN` |
| Mesh | Uniform raster grid (default 0.5 mm) via matplotlib `Path.contains_points` | A 0.5 mm grid cannot resolve 0.15–0.3 mm traces; refining blows up node count |
| Conductance | Edge g = t/ρ (sheet conductance); via = single-node annular barrel resistor | Vias/pads are point contacts → current crowding is not captured |
| Assembly | COO stamps, but a per-edge Python loop | Slow on large boards |
| Sources | Row overwrite for Dirichlet nodes | Breaks symmetry → can't use CG/Cholesky |
| Solve | `pypardiso` or `scipy.sparse.linalg.spsolve`, a single blocking call | No progress or feedback during the solve |
| Power tree | Leaf-to-root; LDO passes current 1:1, SMPS by efficiency | Good model, keep |
| Output | Matplotlib heatmaps per layer | Not true 3D, can't rotate/zoom/probe |

## Chosen engine: 2.5D P1 triangular FEM

The same approach as atx/padne (GPL-3.0). Its FEM core is about 40 lines on SciPy.

* **Geometry.** Per net and layer, take the shapely union of buffered tracks and arcs, pad polygons
  (`GetPadShapeAsPolygon`), via rings and zone `filled_polygons`, minus drill holes.
  This is implemented in `kipi/geometry/copper.py`.
* **Mesh.** Use `triangle` (Shewchuk; win/cp311 wheels exist) with quality and max-area constraints,
  refined near pads, vias and narrow necks. Conforming triangles resolve thin traces cheaply
  and stay coarse in planes.
* **Element.** Sheet conductance σt. The P1 stiffness is σt·(bbᵀ+ccᵀ)/(4A), vectorised in numpy.
* **Vias / PTH.** Punch the drill hole in each layer mesh and tie the hole-edge nodes to a barrel
  node with the barrel resistance ρh / (π(d·tp − tp²)).
* **Pads.** Each pad is an equipotential terminal region (star coupling, as in padne).
* **Sources.** Eliminate Dirichlet nodes properly to keep the system SPD. Loads become current
  injections. DC series parts (R, ferrite, fuse) become lumped conductances. Nets joined by
  series parts are solved as one "extended net", as HyperLynx does.
* **Solver.** `pyamg` smoothed-aggregation preconditioned CG with a per-iteration callback
  that reports the residual live. SuperLU (or optional pypardiso) is the fallback.
* **Post-processing.** Per triangle, J = −σ∇V and P = |J|²/σ · t · A. Via current = g·ΔV. Also
  per-pin currents and load drop vs. spec.
* **Electro-thermal (later).** Use ρ(T) = ρ₀(1 + α(T−T₀)) with fixed-point iteration.

## KiCad 10 IPC API facts (verified against KiCad 10.0.7 + kicad-python 0.8.0)

* Plugins live in `${KICAD_DOCUMENTS_HOME}/10.0/plugins/<dir>/plugin.json`.
  **On this machine Documents is redirected to OneDrive**, so the folder is
  `C:\Users\drewm\OneDrive\Documents\KiCad\10.0\plugins`.
* `runtime.type = python`. KiCad creates `%LOCALAPPDATA%\KiCad\10.0\python-environments\<identifier>`
  with `--system-site-packages` on its bundled Python 3.11 and pip-installs `requirements.txt`.
  numpy, shapely and wx already exist in the base interpreter.
* The schema is checked with `python -m kipy.packaging validate .`.
* All coordinates are in nm. Pad positions from `FootprintInstance.definition.pads` are absolute
  board coordinates (verified: 0 mismatches against pad-shape centroids on a 355-pad board).
* `Board.get_pad_shapes_as_polygons(list)` **drops pads with no shape**, which misaligns its
  result with the input list. KiPI sends `GetPadShapeAsPolygon` directly and matches by pad id.
* `check_padstack_presence_on_layers` handles unconnected-layer removal on inner layers.
* `Zone.filled_polygons` gives `{layer: [PolygonWithHoles]}`. `Zone.is_rule_area()` is a method.
* Zone fills come back **fractured**. Every antipad or clearance hole is joined to the outer ring
  by a zero-width slit, so `holes` is empty and shapely reports a "Ring Self-intersection".
  `shapely.make_valid` restores real interiors (647 on the Audio_Visualiser_DSP board) and
  leaves the slits as `LineString`s, which `geometry/copper.py` drops. The solver mesher must
  run on the repaired polygons, not the raw rings.
* Footprints can contain unnumbered, net-less pads with no copper (paste or mask apertures,
  e.g. 12 on an INA231 WCSP). KiCad reports no copper shape for them, which is correct.
* `get_stackup()` gives copper and dielectric thicknesses. Dielectric layers can have sub-layers.
* Requests go through KiCad's UI thread, so expect `AS_BUSY` and retry. Use one connection per process.
* From 10.0.1, plugin stdout/stderr shows in the KiCad status bar.

## GUI / 3D

* KiCad 10 plugins are separate processes, so the GUI toolkit is not tied to wx.
* PySide6 (`PySide6-Essentials` keeps the venv smaller) + `pyvistaqt.QtInteractor` embeds VTK
  with full rotate/zoom/pick. Other plugins on this machine (fanout-tool, OrthoRoute,
  KiCad-Parasitics) already install PySide6/PyQt6/pyvista into their KiCad venvs.
* Copper is drawn as slabs at true stackup Z with a Z-exaggeration scale. Vias are drawn as barrels.
* Later additions: scalar fields (V, ΔV, |J|, P), J arrow glyphs, clipping planes, `.vtu` export.

## Solver validation and accuracy findings (Milestone 2)

* **Straight trace.** Matches rho*L/(W*t) to 1e-9. P1 elements are exact for a linear field.
* **Radial spreading into a via.** With a uniform mesh the error was -1.7% at h = 0.4 mm. A
  P1 model with equipotential ends over-estimates conductance, so the error was in the
  expected direction, but it converged only as O(h). The cause is the 1/r field near the
  drill. A size field that grows linearly with distance from each plated hole,
  h = clamp(h_min + k*d, h_min, h_max), brings it to -0.02% for about the same node count.
  The defaults are k = 0.2 and h_min = 0.02 mm.
* **Drill polygons.** Approximating a drill as an 8-gon makes it look about 0.8% more
  resistive than a circle, because an octagon's effective radius is smaller. The default
  is 16 segments.
* **Solve for the offset from the source voltage.** A rail drop is about 1e-4 of the rail
  voltage. Solving in absolute volts lets the source term dominate the right-hand side, so a
  relative residual of 1e-10 said little about the drop: AMG and direct solves disagreed by
  0.4%, and the energy balance error was about 1e-6. Solving for u = V - V_ref gives energy
  balance and KCL errors of about 1e-10, with AMG matching direct to five digits.
* **Plated holes.** A plated hole ties its ring together even when it has copper on only one
  layer, because the barrel wall shorts the hole edge. Dropping single-layer barrels left
  1 mm PTH holes in a plane acting as insulating cut-outs and shifted load drops by 0.4%.
* **Dead rings.** Annular rings that touch nothing but their own barrel carry no current. On
  the DSP board, skipping them removes 29% of the nodes with an identical answer.
* **Performance.** On the DSP board, +3.3V meshes to 174k nodes on 3 layers. Smoothed-
  aggregation AMG-CG then converges in about 60 iterations, and the whole run takes 2–3 s.
