<p align="center">
  <img src="toolpath_lab/web/icon.png" width="128" alt="ToolpathLab">
</p>

# ToolpathLab

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

ToolpathLab is a 2.5-axis toolpath planning base: given a cutting tool and a machining region (seven
regular shapes, or an outline imported from a DXF drawing), it plans raster, contour and
adaptive-contour toolpaths, shows the workpiece, the toolpath, the cutter and the depth-coloured
machined floor in a 3D window, plays the process back at the programmed feed rates, and reports
coverage, 2.5D material removal, holder-collision checks and self-describing NC / CSV exports.

The backend is plain Python (numpy is the only dependency), the front-end is native ES modules with a
vendored three.js, and the desktop window is provided by Electron. Tools, regions and strategies are
described by parameter declarations, and the parameter panel is generated from them.

> The interface text and the documentation are Chinese, and so is the primary document
> [README.md](README.md); this file is its English counterpart and is kept in sync by hand.

![UI](docs/images/screenshot.png)

## Features

- **Tools**: flat end mill, ball nose and bull nose (diameter, length, and a corner radius for the
  bull nose - all of them parameters). The offset from the region contour uses how far the cutter
  reaches sideways over the whole cut, so a shaped tool keeps its full radius away from the wall at
  the deepest layer instead of gouging it. Coverage sweeps the flat contact on the floor: the full
  radius for a flat mill, `R - Rc` for a bull nose, and a single point for a ball nose, whose real
  surface is a scalloped envelope this model does not simulate.
- **Tool library**: the top of the tool section offers a preset selector with six common tools (flat
  D6 and D10, ball D6, bull D10 Rc2, a 15 degree taper, a small D3); picking one copies its values into
  the same parameter fields below. A preset is a shortcut, not a second configuration system: editing
  any value returns the selector to "custom", and the request that reaches the API is identical either
  way (a test asserts that no library field travels with a plan).
- **Tapered tools** (`taper_angle_deg`, half-angle, 0 = straight by default): the flanks open by
  `tan(angle)` of radius per millimetre of height, and the diameter parameter is then the diameter **at
  the tip**. The taper feeds into the wall clearance, which grows with the cut depth -- so a tapered
  tool in a deep pocket is not a contradiction: the whole path is pushed out to
  `R + (depth - Rc) * tan(angle)` and the flank itself never gouges the wall (a test pins the measured
  path-to-wall distance to that analytic value). The 3D view draws the same cone.
- **Holder collision**: the tool is two cylinders - the cutting head (radius R up to
  `flute_length_mm`, a cone for a tapered tool) and the shank above it (`shank_diameter_mm`), which is
  exactly what the 3D view draws: the automatic defaults `min(0.65 L, 6 R)` and `1.25 R` are the shapes
  it has always shown, now supplied by the backend, so what you see is what gets checked. Once a cut is
  deeper than the flutes the shank is inside the pocket and needs `shank radius` of room to the wall,
  while the planner only guarantees the *cutter* its own clearance - so a shank wider than the cutter
  is reported as a collision, with the closest approach, the shortfall and three ways out, and a cut
  deeper than the tool is reported as a holder that would enter the part. The measurement travels with
  the response (`holder`, including the widest radius in the pocket and the tightest margin); the
  statistics grow a clearance row while the shank is engaged. Two deliberate exclusions: entries are
  not checked (a ramp or helix may leave the region on purpose, which the notes report) and points
  outside the region do not count (they are not in the pocket).
- **Regions**: square, rectangle, circle, ellipse, U shape, dumbbell and triangle, all centred at the
  origin and machined on the XY plane. Each one reduces to a single counter-clockwise boundary
  polygon, which is what the toolpath planners clip against and what the 3D workpiece is extruded
  from - so a new shape needs no change to any strategy or to the front-end. A region can also come
  from a drawing, see the next bullet.
- **Drawing import**: pick a DXF file in the panel and the backend reads its closed outlines
  (`LWPOLYLINE`, `POLYLINE` and end-to-end `LINE` loops; arcs and circles are reported as skipped
  rather than guessed), then the outline you choose becomes the region. An imported outline is
  deliberately *not* registered in the catalogue: the shape selector only grows an "imported outline"
  entry once a file has actually been read, and the point list travels with the planning request. The
  exported NC header states `imported - N points` instead of printing thousands of coordinates.
- **Strategies**:
  - **raster** - parallel passes, `zigzag` (every other pass reversed, consecutive passes linked) or
    `one_way` (all passes in the same direction, retracting in between);
  - **contour** - equidistant inward offsets of the region outline; a concave region splits into
    several loops per layer, nested loops are joined by link moves while sibling loops retract and
    rapid across, and the ring direction can be `alternate`, `climb` or `conventional`;
  - **adaptive_contour** - contouring plus a coverage loop: plan with the requested stepover, measure
    the coverage, tighten the stepover and replan (up to `max_rounds`) until `coverage_target` is
    met, then return the best round that also fits `max_time_ratio`. If the target cannot be reached
    it says so - either "this would cost N times the time" or "tightening further does not improve
    anything", the latter usually being a limit of the tool geometry (a round tool cannot reach a
    sharp corner).
  - **spiral** - the same offset chain, walked as **one continuous cut per level**: every revolution
    blends into the next ring, so the tool steps in by one stepover per turn and never lifts or links
    inside a level. The outermost ring is still cut in full first (a blend only touches it where it
    starts), and sibling rings after a concave split still retract. It suits shapes machined from a
    **single front**; a thin wall, a U-shaped bar or a dumbbell is cut from two fronts at once, where
    ring-by-ring contouring advances both while one spiral only reaches the second front later in its
    revolution - so the planner **measures both coverages** and falls back to contouring (with a note
    saying why) as soon as the spiral would lose more than two points of coverage.
- **Parameters**: stepover, pass direction, feed rate, safe height, rapid feed, corner feed
  reduction, total depth and depth of cut, plus per-strategy extras (boundary handling and stock
  allowance for raster, ring direction for contour, the coverage target and time budget for
  adaptive). Controls are generated from the backend declarations, see
  [Parameters and constants](#parameters-and-constants).
- **Corner feed reduction**: cutting segments whose turn angle exceeds `corner_angle_deg` ramp
  linearly down to `corner_feed_ratio` of the programmed feed at a full 180° reversal. A move carries
  a single feed rate, so the polyline is split into runs of equal feed: geometry and pass count are
  unchanged, only the feeds, the estimated time and the playback speed move with it. Off by default.
- **Step-down**: with a total depth, the same planar path is repeated at successive depths (the last
  layer takes the remainder, never overshooting), which is how a flat-bottomed pocket is machined.
  Cutting and link moves follow their layer while retract points keep the absolute safe height, so
  `safe_height_mm` keeps meaning "above the top surface". Off by default.
- **3D view**: workpiece solid, region contour, toolpath (cut / link / rapid colour coded, cutting
  segments further split by feed so slowed corners are visible), cutter solid, traversed path, the
  machined floor coloured by depth (see Analysis) and live shadows; the workpiece turns translucent
  when a layered toolpath cuts below the top face.
- **Playback**: time is parameterised by each move's own feed rate, with play / pause, scrubbing, and
  cutting length plus estimated machining time.
- **Analysis**: **coverage** - a grid compares the area the tool swept against the area of the region
  and reports the coverage ratio, the uncut area and where the uncut patches are, drawn as a warning
  coloured overlay in the 3D view; a response also carries a warning when a noticeable part is left
  (more than 2 % by default). **Material removal (2.5D height map)** - every cell records the Z it was
  cut down to, which yields the floor ratio (the share of the region that reached the floor, i.e.
  coverage in depth), the removed and remaining volumes and the area never touched at all; below 90 %
  the response warns again and the statistics grow a row per figure. The same map is **sent with the
  response in reduced form** (at most 4096 cells; each cell takes the deepest cut in its block, i.e.
  how deep the tool reached there, and cells outside the region stay empty), and the 3D view paints it
  as the machined floor, coloured from teal to orange by depth and placed at its real Z, so the
  terraces of a stepped plan are visible ("切深" in the display toggles). Only that overlay is
  reduced: coverage, floor ratio and the volumes always come from the 0.5 mm measurement grid.
- **Notes**: every plan explains its own choices in a collapsible "刀路说明" list (per-round stepover,
  coverage, ring count, cutting length and time for adaptive contouring; the corner slowdown and
  layer counts; the safe height and rapid feed actually used; the length of every ramp or helix entry,
  which is a cutting move -- 2 mm down at 1° is 114.6 mm of travel before the first pass starts; the
  holder clearance when the shank enters the pocket and still clears the wall).
- **Export**: NC program (G-code, G21 / G90 / G17 with G0 / G1 and F) and a **CSV point table** (one
  tool point per row). Both explain where they came from: the NC header and a CSV comment block carry
  the echoed request, the toolpath summary (strategy, passes, points, cutting and rapid length,
  estimated time), the coverage and the strategy's own notes. The CSV keeps its data rows pure ASCII,
  so `pandas.read_csv(path, comment="#")` reads it directly.
- **HTTP API**: catalog, planning and export endpoints for scripting and integration.

## Interface

The left side is the parameter panel; the right side holds the 3D view, statistics and the playback bar.

| Mouse | Action |
| --- | --- |
| Left drag | Orbit |
| Middle wheel | Zoom |
| Right drag | Pan |

- **Tool** (top of the parameter panel): a preset selector plus the parameters; picking a preset fills
  them, and editing any value returns the selector to "custom".
- **View toolbar** (top centre): fit / front / back / left / right / top / bottom; clicking the active
  direction again flips to the opposite side.
- **Appearance toggles** (top left): live shadows, white background, grid floor.
- **Display toggles** (left panel): workpiece, toolpath, rapids, traversed path, uncut material, depth,
  cutter. "Uncut" is the warning coloured overlay of what coverage found missing; "depth" is the
  machined floor, drawn inside the workpiece at its real Z and coloured by how deep the tool reached
  (the little gradient dot in front of it is that colour ramp).
- **Playback bar** (bottom): play / pause (space bar works too), rewind, scrub, current time.
- **Statistics** (top right): region size, pass count, point count, cutting length, machining time,
  coverage and uncut area, plus a holder clearance row while the shank is inside the pocket; the
  collapsible notes sit underneath.

![Top view](docs/images/screenshot-top.png)

The parameter panel is generated from `/api/catalog`: adding a region shape or a toolpath strategy
makes its controls appear in the interface without touching the front-end. The single exception is
the imported outline: its geometry is a point list rather than parameters, so it is deliberately kept
out of the catalogue and the panel only adds that entry once a drawing has actually been read.

## Requirements

- Python 3.10+ and numpy (`pip install -r requirements.txt`);
- Node.js 18+ and Electron for the desktop window (`npm install`, about 200 MB);
- without Node.js the backend can still run in browser mode with the same features.

## Getting started

**Windows**: double click `start.bat`. It locates a usable Python (creating `.venv` and installing
numpy when needed), makes sure Electron is available (running `npm install` on first use) and opens
the desktop window.

**Manual**:

```bash
git clone https://github.com/large-su/toolpath-lab.git
cd toolpath-lab

pip install -r requirements.txt
npm install

npm start                 # desktop window (spawns the Python backend)
python -m toolpath_lab    # backend + browser only: http://127.0.0.1:8770/
```

Command line options: `--host`, `--port`, `--no-browser`.

## Usage

### As a library

```bash
python examples/headless_plan.py
```

The example plans a toolpath without any UI, prints its statistics and writes an NC file:

```python
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan

outcome = run_plan(
    planner_id="raster",
    tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
    region=build_region("square", {"side_mm": 80.0}),
    parameters={"mode": "zigzag", "stepover_mm": 6.0, "feed_mm_per_min": 800.0},
)
print(outcome.toolpath.statistics())
```

### HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Health check and version |
| `GET /api/catalog` | Capabilities: region shapes, strategies, parameter declarations, defaults |
| `POST /api/plan` | Plan a toolpath; returns moves, statistics, coverage and the playback timeline |
| `POST /api/export/gcode` | Export the NC program |
| `POST /api/export/csv` | Export the CSV point table |
| `POST /api/import/dxf` | Read the 2D outlines of a DXF drawing (raw DXF text or `{"text": "..."}`) |

`/api/plan` also takes an imported outline:
`{"region": {"shape": "imported", "points": [[x, y], ...]}}`, where the points come from
`/api/import/dxf`. Fewer than three points, a point that is not `[x, y]`, or a non-finite coordinate
is a `400`. Such a region is absent from `/api/catalog`: it is the one region whose geometry is not
built through the parameter system.

```bash
curl http://127.0.0.1:8770/api/catalog

curl -X POST http://127.0.0.1:8770/api/plan \
  -H "Content-Type: application/json" \
  -d '{"tool":{"diameter_mm":6,"length_mm":30},
       "region":{"shape":"circle","parameters":{"diameter_mm":80}},
       "planner":{"id":"raster","parameters":{"mode":"one_way","stepover_mm":6}}}'
```

Invalid parameters return `400`; valid parameters that cannot be machined (for example a tool larger
than the region) return `422`, with the reason in the `error` field.

## Layout

```
toolpath_lab/core/        domain: parameter specs, tool, region, move/toolpath model
toolpath_lab/planning/    strategies, planar geometry, offset geometry, coverage, feeds, step-down
toolpath_lab/simulation/  feed-rate based time parameterisation
toolpath_lab/export/      G-code and CSV writers plus the shared export summary
toolpath_lab/importers/   input formats: a minimal DXF 2D outline reader
toolpath_lab/server/      standard library HTTP API, request validation, static files
toolpath_lab/web/         front-end: native ES modules + vendored three.js, no build step
electron/                 desktop shell that spawns the backend and hosts the window
examples/                 headless example and a plugin template
tests/                    unit tests, including the convention checks
```

Dependencies point in one direction: `core` depends on nothing, `planning` / `simulation` /
`export` / `importers` depend only on `core`, `server` assembles them, `web` talks HTTP and
`electron` only owns the window - so the planning code runs headless. See [docs/architecture.md](docs/architecture.md).

## Parameters and constants

**Adjustable parameters** (the same names in the HTTP request):

| Parameter | Key | Default | Range | Applies to |
| --- | --- | --- | --- | --- |
| Safe height | `safe_height_mm` | 5 mm | 0-200 | every strategy (how far above Z = 0 the tool retracts; 0 = no retract) |
| Rapid feed | `rapid_feed_mm_per_min` | 5000 mm/min | 100-50000 | every strategy (retract / traverse / plunge, counted in the estimate) |
| Corner slowdown start | `corner_angle_deg` | 0° (off) | 0-180 | every strategy (turn angle above which cutting feeds ramp down) |
| Corner minimum feed | `corner_feed_ratio` | 0.35 × | 0.05-1 | every strategy (feed factor at a 180° reversal) |
| Total depth | `depth_mm` | 0 mm (off) | 0-200 | every strategy (0 = a single layer on the machining plane) |
| Depth of cut | `stepdown_mm` | 2 mm | 0.1-50 | every strategy (per layer; the last layer takes the remainder) |
| Entry mode | `entry_mode` | plunge | plunge / ramp / helix | every strategy (how the tool gets down to a layer; ramps and helixes use the cutting feed) |
| Ramp angle | `ramp_angle_deg` | 10° | 1-45 | every strategy (descent angle of a ramp or helix; the shallower, the longer the entry) |
| Helix radius | `helix_radius_mm` | 1.5 mm | 0.2-20 | every strategy (clamped to the cutter's wall clearance, a wider helix would cut the wall) |
| Boundary handling | `boundary_mode` | inset by the tool radius | inset / none | raster (`none` puts the tool centre on the contour) |
| Stock allowance | `stock_allowance_mm` | 0 mm | 0-20 | raster (leave a ring inside the contour) |
| Ring direction | `ring_direction` | alternate | alternate / climb / conventional | contour and spiral (a spiral cannot alternate, so it follows the outermost ring) |
| Coverage target | `coverage_target` | 99.5 % | 50-100 | adaptive contour (below it the stepover is tightened) |
| Time budget | `max_time_ratio` | 2 × | 1-10 | adaptive contour (a round costing more is rejected) |
| Maximum rounds | `max_rounds` | 3 | 0-8 | adaptive contour |
| Tightening factor | `stepover_factor` | 0.7 | 0.3-0.95 | adaptive contour |
| Stepover floor | `min_stepover_mm` | 1 mm | 0.2-20 | adaptive contour |
| Flute length | `flute_length_mm` | 0 = auto (`0.65 × length` or `6 × diameter`, whichever is smaller) | 0-300 | the tool (the shank starts at the top of the flutes; a cut deeper than them puts the shank in the pocket) |
| Taper half-angle | `taper_angle_deg` | 0° (straight flanks) | 0-45 | the tool (how much the flanks open per side; the diameter is the one at the tip, and the cone feeds into the wall clearance) |
| Shank diameter | `shank_diameter_mm` | 0 = auto (`1.25 × diameter`) | 0-200 | the tool (a shank wider than the cutter needs the pocket to leave that much room, or it is a collision) |

The first six are declared once in `toolpath_lab/planning/base.py` as `MOTION_PARAMETERS` and merged
into every strategy's `ParameterSet`, so a new strategy gets them by writing `+ MOTION_PARAMETERS`;
the boundary pair is raster specific, the ring direction contour specific, and the five adaptive rows
sit on top of the contour parameters. The last three rows are the tool's own parameters
(`toolpath_lab/core/tool.py`, independent of any strategy; `0` means "use the rule in the default
column").

**Climb vs conventional**: this project labels them by **geometric winding**. Contouring walks from
the outside in, so unmachined material is always on the **inner** side of a loop; with an M03 spindle
(clockwise seen from above) and a right-hand tool, **counter-clockwise = climb** and clockwise =
conventional. Switch to M04, or machine an outer contour (material outside the path), and the two
swap. The raster strategy has no such parameter: one pass is climb on one side and conventional on
the other, and zigzag alternates by itself.

**Still fixed design choices**:

| Item | Value | Location |
| --- | --- | --- |
| Pass sampling | two end points (the machining plane is flat) | `toolpath_lab/planning/raster.py` |
| Curve discretisation | 180 segment polygon for circles and ellipses (`CURVE_SEGMENTS`) | `toolpath_lab/core/region.py` |
| Workpiece model | extruded from the region boundary, top face at Z = 0 | `toolpath_lab/web/js/viewport.js` |
| Contour boundary | the first ring is always inset by one footprint radius (offsets are inward only) | `toolpath_lab/planning/contour.py` |
| Layers | each layer is the same planar path moved down (vertical walls, flat floor, no islands) | `toolpath_lab/planning/stepdown.py` |
| Path display | the machining plane is lifted 0.05 mm to avoid z-fighting; layered paths below it keep their real depth and the workpiece turns translucent | `toolpath_lab/web/js/viewport.js` |
| Coverage / removal grid | 0.5 mm cells, at most 400 000 of them (grown for large regions); both share the one grid | `toolpath_lab/planning/coverage.py` |
| Height-map display grid | reduced to at most 4096 cells (each takes the deepest cut in its block); only the 3D colouring reads it, no volume or ratio does | `toolpath_lab/planning/removal.py` |
| Uncut output caps | at most 8 patches and 800 rectangles (the rest only count towards the total) | `toolpath_lab/planning/coverage.py` |
| Playback sampling cap | at most 4000 timeline samples | `toolpath_lab/server/service.py` |
| Collision tolerance | 0.01 mm: offset-polygon rounding is not a collision (an exact fit reports a margin of 0) | `toolpath_lab/planning/collision.py` |
| Entries are not clipped | a ramp or helix runs its geometric length, may leave the region, and only reports its length and reach | `toolpath_lab/planning/entry.py` |
| Spiral suitability | coverage is compared against contouring on the same shape; more than **2 points** of loss falls back to contouring (a single-fronted shape only loses its seam) | `toolpath_lab/planning/spiral.py` |

The full recipe for adding a parameter (declare, read, test, changelog) is in
[docs/extending.md](docs/extending.md) section 3.

## Extending

- **A new strategy**: subclass `Planner`, declare its parameters, implement `plan()` and register it
  by importing it in `planning/__init__.py` (the import order is the order in the UI).
  [examples/plugins/contour_planner.py](examples/plugins/contour_planner.py) is a working
  single-loop contour strategy: copy it into `toolpath_lab/planning/` under a new id and import it
  once. Corner slowdown and step-down are post-processes of the planning service, so a plugin gets
  them for free.
- **A new region shape**: implement `boundary()` returning a counter-clockwise polygon; clipping,
  contouring, coverage and the 3D view adapt automatically.
- **A new export format**: add a pure function under `export/` and a branch in the HTTP router; take a
  `provenance` argument and use `export/summary.py` so the file explains itself.

Full details: [docs/extending.md](docs/extending.md); conventions: [CONTRIBUTING.md](CONTRIBUTING.md).
Comments and docstrings are written in English while everything the user sees (labels, help texts,
notes, warnings, error messages) is written in Chinese - both are machine checked by
`tests/test_conventions.py`.

## Tests

```bash
python selfcheck.py                        # everything below in one command, plus an HTTP smoke test
python -m unittest discover -s tests       # unit tests
python examples/headless_plan.py           # the library path, headless
node --check electron/main.mjs             # desktop shell syntax
```

`selfcheck.py` also runs `node --check` over every module in `toolpath_lab/web/js` and starts a
throwaway server to walk the whole API (every region shape, every strategy, both exports, the 400 /
404 / 422 paths). GitHub Actions runs the same command on every push and pull request, see
`.github/workflows/selfcheck.yml`.

## Design notes

The toolpath model uses the usual parallel scan-line form, the time axis accumulates each move's own
feed rate, and the 3D interaction follows the common CAD conventions (left drag orbits, middle wheel
zooms, right drag pans). Coverage is a planar measurement (grid cells whose centre is inside the
region and within the tool footprint radius of a material removing move), which is why it reports the
same number for a layered toolpath: depth-wise coverage would need a real material removal
simulation.

## License

[MIT](LICENSE)
