"""Build a standalone Blender 5.1 scene from versioned ToolpathLab motion data."""
import argparse
import bisect
import json
from math import ceil, cos, pi, sin, sqrt
from pathlib import Path
import sys
import bpy
from mathutils import Vector

MM = 0.001


def material(name, color, metallic=0, roughness=0.35, emission=0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Metallic"].default_value = metallic
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Emission Color"].default_value = (*color, 1)
    shader.inputs["Emission Strength"].default_value = emission
    return mat


def finish(obj, name, mat, bevel=0, smooth=False):
    obj.name = name
    obj.data.materials.append(mat)
    if bevel:
        modifier = obj.modifiers.new("Edge highlights", "BEVEL")
        modifier.width, modifier.segments = bevel, 3
        obj.modifiers.new("Weighted normals", "WEIGHTED_NORMAL")
    if smooth:
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
    return obj


def cylinder(name, radius, depth, center, mat):
    bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=radius, depth=depth, location=center)
    return finish(bpy.context.object, name, mat, min(depth / 15, radius / 12), True)


def curve(name, points, mat, width):
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    data.resolution_u = 1
    data.bevel_depth = width
    data.bevel_resolution = 2
    spline = data.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for point, xyz in zip(spline.points, points):
        point.co = (*xyz, 1)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    data.materials.append(mat)
    return obj


def camera(name, position, target, scale):
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = position
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    data.type = "ORTHO"
    data.ortho_scale = scale
    data.clip_start, data.clip_end = 0.0001, 10
    return obj


def label(cam, text, y, size, mat):
    data = bpy.data.curves.new("Annotation", "FONT")
    data.body, data.size = text, size
    obj = bpy.data.objects.new("Annotation", data)
    bpy.context.collection.objects.link(obj)
    obj.parent = cam
    obj.location = (-cam.data.ortho_scale * 0.45, y, -0.01)
    data.materials.append(mat)


def flute_mesh(radius, length, mat):
    # Cosmetic helical flutes; the planning footprint remains the nominal disk.
    n, layers = 64, 32
    vertices, faces = [], []
    for layer in range(layers + 1):
        z = length * layer / layers
        for index in range(n):
            angle = 2 * pi * index / n
            shape = 0.84 + 0.16 * (0.5 + 0.5 * cos(4 * angle - 5 * z / length)) ** 0.3
            vertices.append((radius * shape * cos(angle), radius * shape * sin(angle), z))
    for layer in range(layers):
        for index in range(n):
            a = layer * n + index
            b = layer * n + (index + 1) % n
            faces.append((a, b, b + n, a + n))
    faces.extend((tuple(reversed(range(n))), tuple(range(layers * n, (layers + 1) * n))))
    mesh = bpy.data.meshes.new("Helical flute mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("Tool_Flutes", mesh)
    bpy.context.collection.objects.link(obj)
    return finish(obj, "Tool_Flutes", mat, smooth=True)


def expected_position(timeline, time):
    times, points = timeline["times"], timeline["positions"]
    index = max(0, min(len(times) - 1, bisect.bisect_right(times, time) - 1))
    nxt = min(index + 1, len(times) - 1)
    ratio = 0 if times[nxt] <= times[index] else (time - times[index]) / (times[nxt] - times[index])
    return [a + ratio * (b - a) for a, b in zip(points[index], points[nxt])]


def verify(scene, manifest, root):
    timeline, settings = manifest["timeline"], manifest["settings"]
    tool = bpy.data.objects["Tool_Tip"]
    probes = set(timeline["times"][::max(1, len(timeline["times"]) // 200)])
    probes.update(timeline["duration_s"] * i / 100 for i in range(101))
    probes.add(timeline["duration_s"])
    maximum = 0
    worst = None
    for time in sorted(probes):
        frame = 1 + time * settings["fps"] / settings["playback_speed"]
        base = int(frame)
        scene.frame_set(base, subframe=frame - base)
        actual = tool.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation
        target = expected_position(timeline, time)
        error = sqrt(sum((actual[i] / MM - target[i]) ** 2 for i in range(3)))
        if error > maximum:
            maximum = error
            worst = {"time_s": time, "frame": frame, "actual_mm": [v / MM for v in actual], "expected_mm": target}
    result = {"blender_version": bpy.app.version_string, "probe_count": len(probes),
              "max_position_error_mm": maximum, "tolerance_mm": 0.01,
              "passed": maximum <= 0.01,
              "source_duration_s": timeline["duration_s"],
              "estimated_time_s": manifest["toolpath"]["statistics"]["estimated_time_s"],
              "timeline_samples": len(timeline["times"])}
    result["duration_error_s"] = abs(result["source_duration_s"] - result["estimated_time_s"])
    result["passed"] = result["passed"] and result["duration_error_s"] <= 1e-6
    result["worst_probe"] = worst
    curves = tool.animation_data.action.layers[0].strips[0].channelbags[0].fcurves
    result["keyframe_count"] = len(curves[0].keyframe_points)
    if worst:
        result["nearby_keys"] = [list(key.co) for key in sorted(curves[0].keyframe_points,
            key=lambda key: abs(key.co.x - worst["frame"]))[:3]]
    (root / "verify.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not result["passed"]:
        raise RuntimeError(f"Animation verification failed: {result}")
    print("BLENDER_VERIFY", json.dumps(result))
    scene.frame_set(1)


def build(manifest):
    if manifest.get("schema") != "toolpath-lab.blender" or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported ToolpathLab Blender schema")
    if manifest.get("units") != "mm" or manifest.get("axis") != "Z_UP":
        raise ValueError("Unsupported units or coordinate system")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    if hasattr(bpy.context.preferences.filepaths, "use_save_preview_images"):
        bpy.context.preferences.filepaths.use_save_preview_images = False
    bpy.context.preferences.filepaths.save_version = 0
    settings = manifest["settings"]
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1
    scene.unit_settings.length_unit = "MILLIMETERS"
    scene.render.engine = settings["engine"]
    scene.render.resolution_x, scene.render.resolution_y = settings["width"], settings["height"]
    scene.render.resolution_percentage = 100
    scene.render.fps = settings["fps"]
    scene.frame_start = 1
    scene.frame_end = ceil(1 + manifest["timeline"]["duration_s"] * settings["fps"] / settings["playback_speed"])
    scene.cycles.samples = settings["samples"]
    scene.cycles.use_denoising = True
    scene.view_settings.view_transform = "AgX"
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.035, 0.055, 0.075, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.25
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    metal = material("Brushed aluminum", (0.42, 0.52, 0.61), 0.8, 0.29)
    gold = material("Coated cutting tool", (0.64, 0.37, 0.07), 0.8, 0.23)
    steel = material("Steel shank", (0.3, 0.38, 0.45), 0.9, 0.21)
    dark = material("Graphite base", (0.02, 0.035, 0.047), 0.35, 0.4)
    colors = {"cut": (1, 0.32, 0.045), "link": (0.95, 0.7, 0.06), "rapid": (0.1, 0.55, 0.95)}
    path_mats = {kind: material(kind, value, 0.1, 0.35, 0.3) for kind, value in colors.items()}
    trace_mat = material("Traversed", (0.05, 0.9, 0.65), 0.1, 0.3, 0.4)
    text_mat = material("Labels", (0.75, 0.88, 0.92), emission=1)
    region = manifest["region"]
    span = max(pair[1] - pair[0] for pair in region["bounds_mm"]) * MM
    thickness = region["thickness_mm"] * MM
    if region["id"] == "circle":
        cylinder("Workpiece", region["parameters"]["diameter_mm"] * MM / 2,
                 thickness, (0, 0, -thickness / 2), metal)
    elif region["id"] == "square":
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, -thickness / 2))
        obj = bpy.context.object
        side = region["parameters"]["side_mm"] * MM
        obj.dimensions = (side, side, thickness)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        finish(obj, "Workpiece", metal, min(thickness / 12, span / 120))
    else:
        raise ValueError("Unsupported workpiece shape")
    cylinder("Fixture", span * 0.67, thickness * 0.35,
             (0, 0, -thickness * 1.175), dark)
    bpy.ops.mesh.primitive_plane_add(size=span * 200, location=(0, 0, -thickness * 1.35))
    finish(bpy.context.object, "Studio ground", dark)
    tip = bpy.data.objects.new("Tool_Tip", None)
    bpy.context.collection.objects.link(tip)
    tool = manifest["tool"]
    radius, length = tool["radius_mm"] * MM, tool["length_mm"] * MM
    flute = min(length * 0.65, radius * 6)
    flute_mesh(radius, flute, gold).parent = tip
    shank = cylinder("Tool_Shank", radius * 1.25, length - flute,
                     (0, 0, (flute + length) / 2), steel)
    shank.parent = tip
    # keyframe_insert merges nearby subframes. Bulk F-curves retain every sample.
    tip.animation_data_create()
    action = bpy.data.actions.new("Toolpath motion")
    tip.animation_data.action = action
    slot = action.slots.new(id_type="OBJECT", name="Tool_Tip")
    tip.animation_data.action_slot = slot
    strip = action.layers.new("Motion").strips.new(type="KEYFRAME")
    channels = strip.channelbags.new(slot)
    timeline = manifest["timeline"]
    for axis in range(3):
        fcurve = channels.fcurves.new(data_path="location", index=axis)
        fcurve.keyframe_points.add(len(timeline["times"]))
        coordinates = []
        for time, position in zip(timeline["times"], timeline["positions"]):
            coordinates.extend((1 + time * settings["fps"] / settings["playback_speed"], position[axis] * MM))
        fcurve.keyframe_points.foreach_set("co", coordinates)
        for key in fcurve.keyframe_points:
            key.interpolation = "LINEAR"
        # Coordinates are already sorted and interpolation is LINEAR. update()
        # would deduplicate keys within Blender's 0.01-frame merge threshold.
    tip["reference"] = "tool_tip_mm_to_m"
    scene["source_duration_s"] = manifest["timeline"]["duration_s"]
    scene["playback_speed"] = settings["playback_speed"]
    clock = 0.0
    for index, move in enumerate(manifest["toolpath"]["moves"]):
        points = move["points"]
        distance = sum(sqrt(sum((a - b) ** 2 for a, b in zip(p, q))) for p, q in zip(points, points[1:]))
        duration = distance * 60 / move["feed_mm_per_min"]
        xyz = [(p[0] * MM, p[1] * MM, (p[2] + (0.06 if move["kind"] != "rapid" else 0)) * MM) for p in points]
        visible = settings["show_path"] and (move["kind"] != "rapid" or settings["show_rapid"])
        if visible:
            curve(f"Path_{index}_{move['kind']}", xyz, path_mats[move["kind"]], span * 0.001)
            if move["kind"] != "rapid" and duration > 0:
                trace = curve(f"Trace_{index}", [(x, y, z + 0.00003) for x, y, z in xyz], trace_mat, span * 0.00115)
                trace.data.bevel_factor_mapping_end = "SPLINE"
                trace.data.bevel_factor_end = 0
                trace.data.keyframe_insert("bevel_factor_end", frame=1 + clock * settings["fps"] / settings["playback_speed"])
                trace.data.bevel_factor_end = 1
                trace.data.keyframe_insert("bevel_factor_end", frame=1 + (clock + duration) * settings["fps"] / settings["playback_speed"])
        clock += duration
    target = (0, 0, length * 0.12)
    oblique = camera("Camera_Oblique", (span * 1.15, -span * 1.6, span * 1.35), target, span * 1.75)
    top = camera("Camera_Top", (0, 0, span * 2.5), (0, 0, 0), span * 1.95)
    scene.camera = top if settings["camera"] == "top" else oblique
    for cam in (oblique, top):
        label(cam, "TOOLPATH LAB / " + manifest["toolpath"]["planner"].upper(), span * 0.4, span * 0.022, text_mat)
        label(cam, f"{settings['playback_speed']:g}x | F {tool_feed(manifest):g} mm/min | KINEMATIC DEMO", -span * 0.42, span * 0.016, text_mat)
    for name, position, energy, size in (("Key", (0.8, -0.9, 1.9), 24, 1.5),
                                         ("Fill", (-1.6, -0.1, 0.9), 12, 1.2),
                                         ("Rim", (0.2, 1.3, 1.5), 30, 1.0)):
        data = bpy.data.lights.new(name, "AREA")
        light = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(light)
        light.location = [value * span for value in position]
        light.rotation_euler = (Vector(target) - light.location).to_track_quat("-Z", "Y").to_euler()
        data.energy = energy * 0.015 * (span / 0.08) ** 2
        data.shape, data.size = "DISK", size * span
    embedded = bpy.data.texts.get("ToolpathLab_Data.json") or bpy.data.texts.new("ToolpathLab_Data.json")
    embedded.clear()
    embedded.write(json.dumps(manifest, ensure_ascii=False))
    scene.frame_set(1)
    return scene


def tool_feed(manifest):
    return next(move["feed_mm_per_min"] for move in manifest["toolpath"]["moves"] if move["kind"] == "cut")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data")
    parser.add_argument("--output", default="machining.blend")
    parser.add_argument("--engine", choices=("BLENDER_EEVEE", "CYCLES"))
    parser.add_argument("--render", choices=("stills", "video"))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.verify_only:
        manifest = json.loads(bpy.data.texts["ToolpathLab_Data.json"].as_string())
        verify(bpy.context.scene, manifest, output.parent)
        return
    data = Path(args.data).resolve() if args.data else Path(__file__).with_name("scene.json")
    manifest = json.loads(data.read_text(encoding="utf-8"))
    if args.engine:
        manifest["settings"]["engine"] = args.engine
    scene = build(manifest)
    verify(scene, manifest, output.parent)
    if hasattr(scene.render.image_settings, "media_type"):
        scene.render.image_settings.media_type = "VIDEO"
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format, scene.render.ffmpeg.codec = "MPEG4", "H264"
    scene.render.ffmpeg.constant_rate_factor = "HIGH"
    scene.render.filepath = str(output.parent / ("animation_" + scene.render.engine.lower() + ".mp4"))
    bpy.ops.wm.save_as_mainfile(filepath=str(output))
    if args.render == "stills":
        if hasattr(scene.render.image_settings, "media_type"):
            scene.render.image_settings.media_type = "IMAGE"
        scene.render.image_settings.file_format = "PNG"
        for name, progress, cam in (("overview", 0.0, "Camera_Oblique"),
                                     ("machining", 0.52, "Camera_Oblique"),
                                     ("top", 0.75, "Camera_Top")):
            scene.camera = bpy.data.objects[cam]
            scene.frame_set(round(1 + progress * (scene.frame_end - 1)))
            scene.render.filepath = str(output.parent / (name + ".png"))
            bpy.ops.render.render(write_still=True)
    elif args.render == "video":
        bpy.ops.render.render(animation=True)
    print("BLENDER_OUTPUT", output)


if __name__ == "__main__":
    main()
