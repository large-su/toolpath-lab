<p align="center">
  <img src="toolpath_lab/web/icon.png" width="128" alt="ToolpathLab">
</p>

# ToolpathLab

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

ToolpathLab is a toolpath planning base: given a cutting tool, a regular machining region and a
machining surface, it generates raster or surface-finishing toolpaths, shows the workpiece, the
toolpath and the cutter in a 3D window, and plays the whole process back at the programmed feed rate.

The backend is plain Python (numpy is the only dependency), the front-end is native ES modules with
a vendored three.js, and the desktop window is provided by Electron. Tools and regions are described
by parameters, and the parameter panel is generated from the backend's parameter declarations.

![UI](docs/images/screenshot.png)

## Features

- **Tool**: three tip shapes - **flat**, **ball nose** and **bull nose** (bull nose takes a corner
  radius Rc). The footprint radius defines how far the toolpath is offset from the region contour,
  and the tip arc radius also drives how small a scallop height surface finishing can reach.
- **Region**: square (side) and circle (diameter), centred at the origin.
- **Machining surface**: a height field `z = height(x, y)` - **flat**, **incline**, **cylinder** and
  **dome**. Passes are sampled onto the surface with the "surface sampling step" and the safe height
  is lifted relative to it; a surface that does not cover the region is reported explicitly.
- **Toolpaths**:
  - **raster** - parallel scan lines in two modes: **zigzag** (every other pass runs in the opposite
    direction, consecutive passes are linked) and **one-way** (all passes run the same way, retracting
    to the safe plane in between);
  - **surface finishing** - replaces the stepover with a target **scallop height h** and derives the
    stepover from the tip arc radius rho, `ae = 2*sqrt(2*rho*h - h^2)`; a flat end mill cannot invert
    that expression, so it falls back to the maximum stepover and warns.
- **Parameters**: stepover (or scallop height and maximum stepover), pass direction, surface sampling
  step and feed rate. Safe height, rapid feed and boundary handling are constants (see
  "Configuration constants").
- **3D view**: workpiece solid (flat or a curved stock with a skirt), region contour, toolpath
  (cut / link / rapid colour coded), the cutter revolved from its tip profile, traversed path and
  live shadows.
- **Playback**: time is parameterised by each move's own feed rate; play / pause, scrubbing, cutting
  length and estimated machining time.
- **Export**: NC program (G-code, G21 / G90 / G17 with G0 / G1 and F).
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
- **Playback bar** (bottom): play / pause (space bar works too), rewind, scrub, current time.
- **Statistics** (top right): region size, pass count, point count, cutting length, machining time.

![Top view](docs/images/screenshot-top.png)

The parameter panel is generated from `/api/catalog`: adding a region shape, a machining surface or a
toolpath strategy makes its controls appear in the interface without touching the front-end.

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

The package has to be importable first - `pip install -e .`, or put the repository root on `PYTHONPATH`:

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
| `GET /api/catalog` | Capabilities: region shapes, surfaces, strategies, parameter declarations, defaults |
| `POST /api/plan` | Plan a toolpath; returns moves, statistics, the playback timeline and a surface mesh |
| `POST /api/export/gcode` | Export the NC program |

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
toolpath_lab/core/        domain: parameter specs, tool, region, surface height field, move model
toolpath_lab/planning/    strategies: Planner base + registry, planar geometry, raster + finishing
toolpath_lab/simulation/  feed-rate based time parameterisation
toolpath_lab/export/      G-code writer
toolpath_lab/server/      standard library HTTP API, request validation, static files
toolpath_lab/web/         front-end: native ES modules + vendored three.js, no build step
electron/                 desktop shell that spawns the backend and hosts the window
```

Dependencies point in one direction: `core` depends on nothing, `planning` / `simulation` /
`export` depend only on `core`, `server` assembles them, `web` talks HTTP and `electron` only owns
the window - so the planning code runs headless. See [docs/architecture.md](docs/architecture.md).

## Configuration constants

| Constant | Value | Location |
| --- | --- | --- |
| Safe height | 5 mm | `toolpath_lab/planning/base.py` |
| Rapid feed | 5000 mm/min | `toolpath_lab/planning/base.py` |
| Boundary handling | inset the contour by the tool footprint radius | `toolpath_lab/planning/raster.py` |
| Pass sampling | two end points on a flat surface; sampled along the pass on a curved one | `toolpath_lab/planning/raster.py` |

To expose them as adjustable parameters, see [docs/extending.md](docs/extending.md).

## Extending

- **A new strategy**: subclass `Planner`, declare its parameters, implement `plan()` and register it.
  [examples/plugins/contour_planner.py](examples/plugins/contour_planner.py) is a working contour
  (constant offset) strategy: copy it into `toolpath_lab/planning/` and import it once.
- **A new region shape**: implement `boundary()` returning a counter-clockwise polygon; clipping and
  the 3D view adapt automatically.
- **A new machining surface**: subclass `Surface` and implement `height(x, y)` plus its domain check;
  the backend and the front-end both pick it up automatically.
- **A new export format**: add a pure function under `export/` and a branch in the HTTP router.

Full details: [docs/extending.md](docs/extending.md); conventions: [CONTRIBUTING.md](CONTRIBUTING.md).

## Tests

```bash
python -m unittest discover -s tests
```

## Design notes

The toolpath model uses the usual parallel scan-line form, the time axis accumulates each move's
own feed rate, and the 3D interaction follows the common CAD conventions (left drag orbits, middle
wheel zooms, right drag pans).

## License

[MIT](LICENSE)
