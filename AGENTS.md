# ToolpathLab course project

- Keep core independent; planning, simulation and export depend on core. Server assembles features.
- Declare user parameters with ParameterSpec. User-facing text is Chinese; code documentation is English.
- Units are mm and seconds, right handed coordinates, Z up. Tool position means tool tip.
- Preserve original polyline corners and feed-derived durations in animation.
- Run `python -m unittest discover -s tests`, `python examples/headless_plan.py` and `node --check` on changed JS.
- Run Blender integration verification before claiming Blender compatibility. Record actual outcomes in docs/ai-development.md.
- Do not describe the static workpiece animation as material-removal or machine-dynamics simulation.
- Write the course Word report only after feature verification. Do not change the completed UG assignment.
