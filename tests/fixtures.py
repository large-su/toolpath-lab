"""STEP 测试夹具：程序化生成小型但"真"的 AP214 文件。

解析器不能只靠手写字符串测试——所以这里直接生成合法的 B-rep：
``MANIFOLD_SOLID_BREP → CLOSED_SHELL → ADVANCED_FACE → FACE_BOUND → EDGE_LOOP → ORIENTED_EDGE → EDGE_CURVE``，
平面用 PLANE + AXIS2_PLACEMENT_3D，边界用直线（LINE + VECTOR）。

三个夹具：

``simple_box``
    40 × 30 × 20 的方块，6 个平面面。
``plate_with_pocket``
    100 × 80 × 40 的板，顶面有一个 60 × 40 × 15 的矩形型腔（12 个面，含带孔顶面）。
``plate_with_cylinder``
    100 × 80 × 40 的板，顶面有一个 Ø30 的通孔（圆柱面 + 两个带孔平面）。
``cylinder_boss``
    Ø60 × 50 的圆柱（圆柱侧面 + 上下平面）。

这些文件同样用来做"示例模型"：``examples/sample_plate.step`` 就是 plate_with_pocket。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, pi, sin

Point = tuple[float, float, float]


def _f(value: float) -> str:
    """STEP 实数：必须带小数点。"""

    text = f"{value:.6f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


class DerivedArg:
    """占位类型：写成 STEP 的**裸** ``*``（派生属性），而不是带引号的字符串。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "*"


#: 单例：派生属性标记。见 :meth:`StepWriter._render` 的说明。
DERIVED = DerivedArg()


class StepWriter:
    """按 ``#id=KEYWORD(args);`` 顺序输出实例的极简写入器。"""

    def __init__(self, name: str = "toolpath-lab fixture") -> None:
        self.lines: list[str] = []
        self.next_id = 1
        self._points: dict[tuple[float, float, float], int] = {}
        self._directions: dict[tuple[float, float, float], int] = {}
        self.name = name

    def add(self, keyword: str, *arguments: object) -> int:
        entity_id = self.next_id
        self.next_id += 1
        rendered = ",".join(self._render(argument) for argument in arguments)
        self.lines.append(f"#{entity_id}={keyword}({rendered});")
        return entity_id

    @staticmethod
    def _render(argument: object) -> str:
        if isinstance(argument, bool):
            # 必须放在 int 之前：Python 里 bool 是 int 的子类。
            return ".T." if argument else ".F."
        if argument is DERIVED:
            # 派生属性写成**裸的** `*`。早先这里写成了带引号的 '*'（一个普通字符串），
            # 于是整个测试集都没覆盖过真实的派生属性语法，解析器漏读 `*` 也没人发现。
            return "*"
        if isinstance(argument, int):
            return f"#{argument}"
        if isinstance(argument, str) and argument.startswith("."):
            return argument
        if isinstance(argument, str):
            return "'" + argument.replace("'", "''") + "'"
        if isinstance(argument, float):
            return _f(argument)
        if isinstance(argument, (list, tuple)):
            return "(" + ",".join(StepWriter._render(item) for item in argument) + ")"
        raise TypeError(f"无法写入的参数类型：{type(argument)!r}")

    # -- 常用实体 ----------------------------------------------------------
    def point(self, coordinates: Point) -> int:
        key = tuple(round(float(value), 9) for value in coordinates)
        cached = self._points.get(key)
        if cached is not None:
            return cached
        entity = self.add("CARTESIAN_POINT", "", [float(value) for value in coordinates])
        self._points[key] = entity
        return entity

    def direction(self, vector: Point) -> int:
        key = tuple(round(float(value), 9) for value in vector)
        cached = self._directions.get(key)
        if cached is not None:
            return cached
        entity = self.add("DIRECTION", "", [float(value) for value in vector])
        self._directions[key] = entity
        return entity

    def axis_placement(self, origin: Point, axis: Point = (0.0, 0.0, 1.0),
                       reference: Point = (1.0, 0.0, 0.0)) -> int:
        return self.add("AXIS2_PLACEMENT_3D", "", self.point(origin),
                        self.direction(axis), self.direction(reference))

    def plane(self, origin: Point, axis: Point = (0.0, 0.0, 1.0),
              reference: Point = (1.0, 0.0, 0.0)) -> int:
        return self.add("PLANE", "", self.axis_placement(origin, axis, reference))

    def cylinder_surface(self, origin: Point, axis: Point, radius: float,
                         reference: Point) -> int:
        return self.add("CYLINDRICAL_SURFACE", "",
                        self.axis_placement(origin, axis, reference), float(radius))

    def line(self, start: Point, end: Point) -> int:
        point = self.point(start)
        delta = (end[0] - start[0], end[1] - start[1], end[2] - start[2])
        vector = self.add("VECTOR", "", self.direction(delta), 1.0)
        return self.add("LINE", "", point, vector)

    def circle(self, center: Point, axis: Point, reference: Point, radius: float) -> int:
        placement = self.axis_placement(center, axis, reference)
        return self.add("CIRCLE", "", placement, float(radius))

    def edge_curve(self, start: Point, end: Point, curve: int | None = None) -> int:
        curve_id = curve if curve is not None else self.line(start, end)
        return self.add("EDGE_CURVE", "", self.point(start), self.point(end), curve_id, True)

    def oriented_edge(self, start: Point, end: Point, curve: int | None = None,
                      orientation: bool = True) -> int:
        edge = self.edge_curve(start, end, curve)
        return self.add("ORIENTED_EDGE", "", DERIVED, DERIVED, edge, orientation)

    def edge_loop(self, loop: list[Point], curves: list[int] | None = None,
                  closed: bool = True, mixed_orientation: bool = False) -> int:
        """一条 EDGE_LOOP。

        ``mixed_orientation=True`` 时按**真实导出器（SolidWorks / SwSTEP）的写法**生成：
        边的几何方向按 ``T,F,F,T`` 的规律翻转，再用 ``ORIENTED_EDGE(..., .F.)`` 掰回环的走向。

        这个规律是从真实文件里测出来的，不能随便取：如果只是"隔一条翻一条"（T,F,T,F），
        错误折线里那些反向段会成对抵消，面积**照样是对的**，测试也就抓不到 bug。
        真实的 T,F,F,T 才会让面积少一半。
        """

        edges = []
        for index in range(len(loop)):
            if not closed and index == len(loop) - 1:
                break
            start = loop[index]
            end = loop[(index + 1) % len(loop)]
            curve = curves[index] if curves is not None else None
            if mixed_orientation and index % 4 in (1, 2):
                edges.append(self.oriented_edge(end, start, curve, orientation=False))
            else:
                edges.append(self.oriented_edge(start, end, curve, orientation=True))
        return self.add("EDGE_LOOP", "", edges)

    def bound(self, loop_id: int, orientation: bool = True) -> int:
        return self.add("FACE_OUTER_BOUND", "", loop_id, orientation)

    def face(self, loops: list[int], surface: int, same_sense: bool = True) -> int:
        # ADVANCED_FACE(name, bounds, face_geometry, same_sense) —— name 按惯例留空
        bounds = [self.bound(loop) for loop in loops]
        return self.add("ADVANCED_FACE", "", bounds, surface, same_sense)

    def closed_shell(self, faces: list[int]) -> int:
        return self.add("CLOSED_SHELL", "", faces)

    def solid(self, shell: int) -> int:
        return self.add("MANIFOLD_SOLID_BREP", self.name, shell)

    # -- 输出 --------------------------------------------------------------
    def render(self, schema: str = "AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 }") -> str:
        header = [
            "ISO-10303-21;",
            "HEADER;",
            f"FILE_DESCRIPTION(('{self.name}'),'2;1');",
            f"FILE_NAME('{self.name}.step','2024-01-01T00:00:00',('toolpath-lab'),('toolpath-lab'),"
            "'toolpath-lab 0.0.1','toolpath-lab','');",
            f"FILE_SCHEMA(('{schema}'));",
            "ENDSEC;",
            "DATA;",
        ]
        return "\n".join(header + self.lines + ["ENDSEC;", "END-ISO-10303-21;", ""])


# ------------------------------------------------------------------ 面构造
def _plane_normal(points: list[Point]) -> tuple[float, float, float]:
    (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = points[0], points[1], points[2]
    ux, uy, uz = x1 - x0, y1 - y0, z1 - z0
    vx, vy, vz = x2 - x0, y2 - y0, z2 - z0
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


def _align(points: list[Point], desired: tuple[float, float, float]) -> list[Point]:
    """让环的绕向与期望法向一致（右手定则）。"""

    normal = _plane_normal(points)
    if sum(a * b for a, b in zip(normal, desired)) < 0:
        return list(reversed(points))
    return list(points)


@dataclass(slots=True)
class _SolidBuilder:
    """把"平面矩形环 + 期望法向"累积成 B-rep 的小工具。"""

    writer: StepWriter
    faces: list[int] = field(default_factory=list)
    #: 是否采用真实导出器（SolidWorks / SwSTEP）的写法：曲线反向 + ORIENTED_EDGE(.F.) 掰回来
    mixed_orientation: bool = False

    def add_planar_face(self, outer: list[Point], desired_normal: tuple[float, float, float],
                        holes: list[list[Point]] | None = None,
                        mixed_orientation: bool | None = None) -> None:
        flag = self.mixed_orientation if mixed_orientation is None else mixed_orientation
        outer_aligned = _align(outer, desired_normal)
        plane_origin = outer_aligned[0]
        plane = self.writer.plane(plane_origin, desired_normal, _axis_reference(desired_normal))
        loops = [self.writer.edge_loop(outer_aligned, mixed_orientation=flag)]
        for hole in holes or []:
            # 孔环与外环反向，离散层才能正确配对
            loops.append(self.writer.edge_loop(_align(hole, _negate(desired_normal)),
                                               mixed_orientation=flag))
        self.faces.append(self.writer.face(loops, plane, True))

    def add_cylindrical_face(self, center: Point, axis: tuple[float, float, float], radius: float,
                             height: float, segments: int = 24) -> None:
        """侧面：沿圆周离散成若干条母线边，用一条闭合 EDGE_LOOP 表达一圈。"""

        reference = _axis_reference(axis)
        surface = self.writer.cylinder_surface(center, axis, radius, reference)
        u_axis, v_axis = _cylinder_basis(axis, reference)
        angles = [2.0 * pi * index / segments for index in range(segments)]

        def ring_point(angle: float, base: Point) -> Point:
            return (
                base[0] + radius * (cos(angle) * u_axis[0] + sin(angle) * v_axis[0]),
                base[1] + radius * (cos(angle) * u_axis[1] + sin(angle) * v_axis[1]),
                base[2] + radius * (cos(angle) * u_axis[2] + sin(angle) * v_axis[2]),
            )

        top_base = (center[0] + axis[0] * height, center[1] + axis[1] * height, center[2] + axis[2] * height)
        edges: list[int] = []
        for index in range(segments):
            lower = ring_point(angles[index], center)
            upper = ring_point(angles[index], top_base)
            if self.mixed_orientation and index % 4 in (1, 2):
                edges.append(self.writer.oriented_edge(upper, lower, orientation=False))
            else:
                edges.append(self.writer.oriented_edge(lower, upper))
        loop = self.writer.add("EDGE_LOOP", "", edges)
        self.faces.append(self.writer.add("ADVANCED_FACE", "", [self.writer.bound(loop)], surface, True))


def _negate(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-vector[0], -vector[1], -vector[2])


def _axis_reference(axis: tuple[float, float, float]) -> tuple[float, float, float]:
    """给出一个与 axis 不平行的参考方向。"""

    if abs(axis[2]) < 0.9:
        return (0.0, 0.0, 1.0)
    return (1.0, 0.0, 0.0)


def _cylinder_basis(axis: tuple[float, float, float], reference: tuple[float, float, float]
                    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    length = sum(component * component for component in axis) ** 0.5
    z = tuple(component / length for component in axis)
    dot = sum(a * b for a, b in zip(reference, z))
    x = tuple(reference[index] - dot * z[index] for index in range(3))
    norm = sum(component * component for component in x) ** 0.5
    x = tuple(component / norm for component in x)
    y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
    return x, y


# ------------------------------------------------------------------- 夹具
def simple_box(width: float = 40.0, depth: float = 30.0, height: float = 20.0,
               name: str = "box", mixed_orientation: bool = False) -> str:
    """长方体：6 个平面面。

    ``mixed_orientation=True`` 生成的是**同一个实体**，但环按真实导出器的写法表达
    （每隔一条边的几何反向，再用 ``ORIENTED_EDGE(..., .F.)`` 掰回来）。
    两者解析出来的体积、面积必须完全一致。
    """

    writer = StepWriter(name)
    builder = _SolidBuilder(writer, mixed_orientation=mixed_orientation)
    x0, x1 = -width / 2.0, width / 2.0
    y0, y1 = -depth / 2.0, depth / 2.0
    z0, z1 = 0.0, height

    builder.add_planar_face([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0, 0, 1))
    builder.add_planar_face([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], (0, 0, -1))
    builder.add_planar_face([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0))
    builder.add_planar_face([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0, 1, 0))
    builder.add_planar_face([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], (-1, 0, 0))
    builder.add_planar_face([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1, 0, 0))
    shell = writer.closed_shell(builder.faces)
    writer.solid(shell)
    return writer.render()


def plate_with_pocket(width: float = 100.0, depth: float = 80.0, height: float = 40.0,
                      pocket_x: float = 60.0, pocket_y: float = 40.0, pocket_depth: float = 15.0,
                      name: str = "plate") -> str:
    """带型腔的板：外轮廓 6 个平面 + 型腔 5 个面（共 11 个面，顶面为方框形）。

    型腔从底面贯通到 z = height - pocket_depth，因此体积应当等于
    ``width * depth * height - pocket_x * pocket_y * (height - pocket_depth)``。
    """

    writer = StepWriter(name)
    builder = _SolidBuilder(writer)
    x0, x1 = -width / 2.0, width / 2.0
    y0, y1 = -depth / 2.0, depth / 2.0
    z0, z1 = 0.0, height
    px0, px1 = -pocket_x / 2.0, pocket_x / 2.0
    py0, py1 = -pocket_y / 2.0, pocket_y / 2.0
    pz = height - pocket_depth  # 型腔底面高度

    top_outer = [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    top_hole = [(px0, py0, z1), (px1, py0, z1), (px1, py1, z1), (px0, py1, z1)]
    builder.add_planar_face(top_outer, (0, 0, 1), holes=[top_hole])

    builder.add_planar_face([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], (0, 0, -1))
    builder.add_planar_face([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0))
    builder.add_planar_face([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0, 1, 0))
    builder.add_planar_face([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], (-1, 0, 0))
    builder.add_planar_face([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1, 0, 0))

    # 型腔 5 个面：底面朝上（材料在下方），四个侧壁法向指向型腔内部
    builder.add_planar_face([(px0, py0, pz), (px1, py0, pz), (px1, py1, pz), (px0, py1, pz)], (0, 0, 1))
    builder.add_planar_face([(px0, py0, z0), (px1, py0, z0), (px1, py0, pz), (px0, py0, pz)], (0, 1, 0))
    builder.add_planar_face([(px0, py1, z0), (px1, py1, z0), (px1, py1, pz), (px0, py1, pz)], (0, -1, 0))
    builder.add_planar_face([(px0, py0, z0), (px0, py1, z0), (px0, py1, pz), (px0, py0, pz)], (1, 0, 0))
    builder.add_planar_face([(px1, py0, z0), (px1, py1, z0), (px1, py1, pz), (px1, py0, pz)], (-1, 0, 0))

    shell = writer.closed_shell(builder.faces)
    writer.solid(shell)
    return writer.render()


def plate_with_cylinder(width: float = 100.0, depth: float = 80.0, height: float = 40.0,
                        hole_diameter: float = 30.0, segments: int = 24,
                        name: str = "plate_hole") -> str:
    """带通孔的板：上下两个带孔平面 + 圆柱侧面。"""

    writer = StepWriter(name)
    builder = _SolidBuilder(writer)
    x0, x1 = -width / 2.0, width / 2.0
    y0, y1 = -depth / 2.0, depth / 2.0
    z0, z1 = 0.0, height
    radius = hole_diameter / 2.0
    reference = (1.0, 0.0, 0.0)
    axis = (0.0, 0.0, 1.0)
    u_axis, v_axis = _cylinder_basis(axis, reference)

    def ring(z: float) -> list[Point]:
        points = []
        for index in range(segments):
            angle = 2.0 * pi * index / segments
            points.append((radius * cos(angle), radius * sin(angle), z))
        return points

    top_hole = ring(z1)
    bottom_hole = ring(z0)
    builder.add_planar_face([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0, 0, 1),
                            holes=[top_hole])
    builder.add_planar_face([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], (0, 0, -1),
                            holes=[bottom_hole])
    builder.add_planar_face([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0))
    builder.add_planar_face([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0, 1, 0))
    builder.add_planar_face([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], (-1, 0, 0))
    builder.add_planar_face([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1, 0, 0))

    # 圆柱孔壁：一整圈闭合环（底圆 → 母线 → 顶圆 → 母线），与真实 CAD 导出一致
    surface = writer.cylinder_surface((0.0, 0.0, z0), axis, radius, reference)
    edges: list[int] = []
    low_circle = writer.circle((0.0, 0.0, z0), axis, reference, radius)
    high_circle = writer.circle((0.0, 0.0, z1), axis, reference, radius)
    for index in range(segments):
        start = bottom_hole[index]
        end = bottom_hole[(index + 1) % segments]
        edges.append(writer.oriented_edge(start, end, low_circle))
    for index in range(segments):
        start = bottom_hole[(index + 1) % segments]
        end = top_hole[(index + 1) % segments]
        edges.append(writer.oriented_edge(start, end))
    for index in range(segments):
        start = top_hole[(index + 1) % segments]
        end = top_hole[index]
        edges.append(writer.oriented_edge(start, end, high_circle))
    for index in range(segments):
        start = top_hole[index]
        end = bottom_hole[index]
        edges.append(writer.oriented_edge(start, end))
    loop = writer.add("EDGE_LOOP", "", edges)
    builder.faces.append(writer.add("ADVANCED_FACE", "", [writer.bound(loop)], surface, True))

    shell = writer.closed_shell(builder.faces)
    writer.solid(shell)
    return writer.render()


def disc_face_with_closed_circular_edge(radius: float = 12.5,
                                        name: str = "disc") -> str:
    """一张由**单条闭合圆边**围成的圆面（圆柱端盖 / 圆孔底的真实写法）。

    SolidWorks 把圆柱端盖写成：一个 ``EDGE_LOOP`` 里只有**一条** ``ORIENTED_EDGE``，
    它的 ``EDGE_CURVE`` 是一整圈 ``CIRCLE``，而且两个顶点是**同一个点**（接缝点）。

    早先按"起止角之间的那一段圆弧"采样：两个角度相同 → 跨度为 0 → 采样出一堆重合点，
    整张圆面退化成零面积并被丢弃，模型上凭空多一个破洞。
    """

    writer = StepWriter(name)
    surface = writer.plane((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0))
    circle = writer.circle((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0), radius)
    seam = (radius, 0.0, 0.0)
    # 闭合边：起点与终点引用同一个顶点
    edge = writer.add("EDGE_CURVE", "", writer.point(seam), writer.point(seam), circle, True)
    oriented = writer.add("ORIENTED_EDGE", "", DERIVED, DERIVED, edge, True)
    loop = writer.add("EDGE_LOOP", "", [oriented])
    writer.solid(writer.closed_shell([writer.face([loop], surface, True)]))
    return writer.render()


def cylinder_boss(diameter: float = 60.0, height: float = 50.0, segments: int = 32,
                  name: str = "boss") -> str:
    """圆柱体：圆柱侧面 + 上下平面。"""

    writer = StepWriter(name)
    builder = _SolidBuilder(writer)
    radius = diameter / 2.0
    axis = (0.0, 0.0, 1.0)
    reference = (1.0, 0.0, 0.0)

    def ring(z: float) -> list[Point]:
        return [(radius * cos(2.0 * pi * index / segments),
                 radius * sin(2.0 * pi * index / segments), z) for index in range(segments)]

    top = ring(height)
    bottom = ring(0.0)
    builder.add_planar_face(top, (0, 0, 1))
    builder.add_planar_face(bottom, (0, 0, -1))

    surface = writer.cylinder_surface((0.0, 0.0, 0.0), axis, radius, reference)
    edges: list[int] = []
    low_circle = writer.circle((0.0, 0.0, 0.0), axis, reference, radius)
    high_circle = writer.circle((0.0, 0.0, height), axis, reference, radius)
    for index in range(segments):
        edges.append(writer.oriented_edge(bottom[index], bottom[(index + 1) % segments], low_circle))
    for index in range(segments):
        edges.append(writer.oriented_edge(bottom[(index + 1) % segments], top[(index + 1) % segments]))
    for index in range(segments):
        edges.append(writer.oriented_edge(top[(index + 1) % segments], top[index], high_circle))
    for index in range(segments):
        edges.append(writer.oriented_edge(top[index], bottom[index]))
    loop = writer.add("EDGE_LOOP", "", edges)
    builder.faces.append(writer.add("ADVANCED_FACE", "", [writer.bound(loop)], surface, True))

    shell = writer.closed_shell(builder.faces)
    writer.solid(shell)
    return writer.render()


FIXTURES = {
    "box": simple_box,
    "plate": plate_with_pocket,
    "plate_hole": plate_with_cylinder,
    "boss": cylinder_boss,
}

__all__ = [
    "FIXTURES",
    "StepWriter",
    "cylinder_boss",
    "plate_with_cylinder",
    "plate_with_pocket",
    "simple_box",
]
