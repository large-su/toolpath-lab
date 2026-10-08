<p align="center">
  <img src="toolpath_lab/web/icon.png" width="128" alt="ToolpathLab">
</p>

# ToolpathLab

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

ToolpathLab is a toolpath planning base: given a cutting tool and a machining region, it plans
raster, contour and adaptive-contour toolpaths, shows the workpiece, the toolpath and the cutter in a
3D window, plays the process back at the programmed feed rates, measures how much of the region the
toolpath actually machines, and exports the result as G-code or a CSV point table.

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
- **Regions**: square, rectangle, circle, ellipse, U shape, dumbbell and triangle, all centred at the
  origin and machined on the XY plane. Each one reduces to a single counter-clockwise boundary
  polygon, which is what the toolpath planners clip against and what the 3D workpiece is extruded
  from - so a new shape needs no change to any strategy or to the front-end.
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
  segments further split by feed so slowed corners are visible), cutter solid, traversed path and live
  shadows; the workpiece turns translucent when a layered toolpath cuts below the top face.
- **Playback**: time is parameterised by each move's own feed rate, with play / pause, scrubbing, and
  cutting length plus estimated machining time.
- **Analysis**: **coverage** - a grid compares the area the tool swept against the area of the region
  and reports the coverage ratio, the uncut area and where the uncut patches are, drawn as a warning
  coloured overlay in the 3D view; a response also carries a warning when a noticeable part is left
  (more than 2 % by default).
- **Notes**: every plan explains its own choices in a collapsible "刀路说明" list (per-round stepover,
  coverage, ring count, cutting length and time for adaptive contouring; the corner slowdown and
  layer counts; the safe height and rapid feed actually used).
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

- **View toolbar** (top centre): fit / front / back / left / right / top / bottom; clicking the active
  direction again flips to the opposite side.
- **Appearance toggles** (top left): live shadows, white background, grid floor.
- **Display toggles** (left panel): workpiece, toolpath, rapids, traversed path, uncut material, cutter.
- **Playback bar** (bottom): play / pause (space bar works too), rewind, scrub, current time.
- **Statistics** (top right): region size, pass count, point count, cutting length, machining time,
  coverage and uncut area; the collapsible notes sit underneath.

![Top view](docs/images/screenshot-top.png)

The parameter panel is generated from `/api/catalog`: adding a region shape or a toolpath strategy
makes its controls appear in the interface without touching the front-end.

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
| Boundary handling | `boundary_mode` | inset by the tool radius | inset / none | raster (`none` puts the tool centre on the contour) |
| Stock allowance | `stock_allowance_mm` | 0 mm | 0-20 | raster (leave a ring inside the contour) |
| Ring direction | `ring_direction` | alternate | alternate / climb / conventional | contour |
| Coverage target | `coverage_target` | 99.5 % | 50-100 | adaptive contour (below it the stepover is tightened) |
| Time budget | `max_time_ratio` | 2 × | 1-10 | adaptive contour (a round costing more is rejected) |
| Maximum rounds | `max_rounds` | 3 | 0-8 | adaptive contour |
| Tightening factor | `stepover_factor` | 0.7 | 0.3-0.95 | adaptive contour |
| Stepover floor | `min_stepover_mm` | 1 mm | 0.2-20 | adaptive contour |

The first six are declared once in `toolpath_lab/planning/base.py` as `MOTION_PARAMETERS` and merged
into every strategy's `ParameterSet`, so a new strategy gets them by writing `+ MOTION_PARAMETERS`;
the boundary pair is raster specific, the ring direction contour specific, and the last five sit on
top of the contour parameters in the adaptive strategy.

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
