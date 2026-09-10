# ToolpathLab

A deliberately small toolpath planning base: one tool, one regular region, the two most basic
raster strategies, a 3D view and feed-rate playback. The point is that a newcomer can read the
whole thing in an afternoon and then start adding their own pieces.

![UI](docs/images/screenshot.png)

## What is in it (and what is deliberately missing)

- **Tool**: flat end mill (diameter, length). Ball and bull nose are listed as "to be extended"
  and cannot be selected.
- **Region**: square (side) and circle (diameter) on the XY plane.
- **Strategies**: zigzag and one-way raster toolpaths.
- **Parameters**: stepover, pass direction, feed rate. Everything else is a fixed constant
  (safe height 5 mm, rapid 5000 mm/min, the path is inset by the tool footprint radius, two
  points per pass).
- **Desktop window** (Electron): left drag rotates, middle wheel zooms, right drag pans
  (same as ROMP); standard views; live shadows / white background / grid toggles; playback.
- **Export**: a single button - NC (G-code).

Deliberately missing: surfaces and 3D regions, model import, concave presets, contour/helix
strategies, material removal simulation, post-processor dialogs, multiple export formats.
Those are exercises for whoever builds on this - see the ready-to-copy contour plugin in
[examples/plugins](examples/plugins).

## Quick start

Windows: double click `start.bat` (it finds Python, installs numpy into `.venv` when needed,
installs Electron once, then opens the window; falls back to the browser without Node.js).

```bash
pip install -r requirements.txt
npm install
npm start                          # desktop window (spawns the Python backend)
python -m toolpath_lab             # backend + browser only
python examples/headless_plan.py   # library use, no UI
```

## Layout

```
toolpath_lab/core/        domain: parameter specs, tool, region, move/toolpath model
toolpath_lab/planning/    strategies: Planner base + registry, scan-line geometry, raster
toolpath_lab/simulation/  feed-rate based time parameterisation
toolpath_lab/export/      G-code writer
toolpath_lab/server/      standard library HTTP API + static front-end
toolpath_lab/web/         plain ES modules + vendored three.js
electron/main.mjs         desktop shell: spawns the backend and hosts the window
```

The parameter panel is generated from `/api/catalog`, so a new strategy or region shape shows up
in the interface without touching JavaScript. See [docs/extending.md](docs/extending.md).

## Tests

```bash
python -m unittest discover -s tests
```

## License

MIT - see [LICENSE](LICENSE).
