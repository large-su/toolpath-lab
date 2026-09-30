"""ToolpathLab - a small, extensible 2.5-axis toolpath planning workbench.

The package is organised in strictly inward-pointing layers:

- core        domain types: parameter declarations, tool, region, surface
              height field and the toolpath/move model
- planning    toolpath strategies built on top of core
- simulation  feed-rate based time parameterisation of a toolpath
- export      G-code / JSON / CSV writers
- server      HTTP adapter (Python standard library) exposing the layers above
              to the web front-end
- web         static three.js front-end served by the server

"core" imports nothing from the other layers, and no layer imports from
"server" or "web".  Everything is expressed in millimetres, seconds and degrees
unless a name says otherwise.
"""

__version__ = "0.0.1"
__all__ = ["__version__"]
