"""ToolpathLab - a small, extensible CAM workbench.

Two capabilities share one backend and one 3D viewer:

- **bench** - a deliberately small toolpath planning base: flat end mill + square /
  circle region + zigzag/one-way raster toolpaths, with feed-rate playback;
- **CAM** - import a STEP part, create stock, build an operation tree, generate
  face/pocket milling toolpaths, simulate stock removal, and export NC programs.

The package is organised in strictly inward-pointing layers:

- core        domain types: parameters, tool, region, part, stock, operation,
              the toolpath/move model
- step        ISO 10303-21 (STEP) reader: parser, geometry, B-spline evaluation,
              face tessellation
- cam         machining: region/raster geometry, face & pocket milling, orchestration
- planning    toolpath strategies built on top of core
- simulation  feed-rate based time parameterisation + Z-map stock removal
- export      G-code / CAM program writers
- storage     project persistence (JSON + npz)
- server      HTTP adapter (Python standard library) exposing the layers above
              to the web front-end
- web         static three.js front-end served by the server

"core" imports nothing from the other layers, and no layer imports from
"server" or "web".  Everything is expressed in millimetres, seconds and degrees
unless a name says otherwise.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
