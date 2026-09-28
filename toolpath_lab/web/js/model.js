// 浏览器端轻量模型导入：支持 OBJ 与 ASCII/Binary STL。
// 导入模型用于显示，并将其 XY 投影转换成后端可规划的 polygon 区域。

const MAX_VERTICES = 250000;

function number(value, label) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label} 坐标不是有限数字`);
  return parsed;
}

function addVertex(vertices, lookup, point) {
  const key = point.map((value) => value.toFixed(6)).join(",");
  let index = lookup.get(key);
  if (index === undefined) {
    if (vertices.length >= MAX_VERTICES) throw new Error("模型顶点数超过 250000，无法在浏览器中预览");
    index = vertices.length;
    vertices.push(point);
    lookup.set(key, index);
  }
  return index;
}

function convexHull(points) {
  const sorted = [...new Map(points.map((point) => [point.join(","), point])).values()]
    .sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  if (sorted.length < 3) throw new Error("模型的 XY 投影不足以形成加工区域");
  const cross = (origin, a, b) => (
    (a[0] - origin[0]) * (b[1] - origin[1])
      - (a[1] - origin[1]) * (b[0] - origin[0])
  );
  const lower = [];
  for (const point of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower.at(-1), point) <= 1e-9) {
      lower.pop();
    }
    lower.push(point);
  }
  const upper = [];
  for (const point of [...sorted].reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper.at(-1), point) <= 1e-9) {
      upper.pop();
    }
    upper.push(point);
  }
  lower.pop();
  upper.pop();
  const hull = lower.concat(upper);
  if (hull.length < 3) throw new Error("模型的 XY 投影面积必须大于 0");
  return hull;
}

function boundsPolygon(vertices) {
  let xMin = Infinity;
  let xMax = -Infinity;
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const point of vertices) {
    xMin = Math.min(xMin, point[0]);
    xMax = Math.max(xMax, point[0]);
    yMin = Math.min(yMin, point[1]);
    yMax = Math.max(yMax, point[1]);
  }
  return [[xMin, yMin], [xMax, yMin], [xMax, yMax], [xMin, yMax]];
}

function finishModel(name, format, vertices, indices) {
  if (vertices.length < 3 || indices.length < 3) throw new Error("模型没有可显示的三角面");
  const hull = convexHull(vertices.map((point) => [point[0], point[1]]));
  return {
    name,
    format,
    mesh: { vertices, indices },
    boundaries: { hull, bounds: boundsPolygon(vertices) },
    vertex_count: vertices.length,
    triangle_count: Math.floor(indices.length / 3),
  };
}

function parseObj(text, name) {
  const vertices = [];
  const indices = [];
  const lines = text.split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const fields = line.split(/\s+/);
    if (fields[0] === "v" && fields.length >= 4) {
      vertices.push([
        number(fields[1], "OBJ X"), number(fields[2], "OBJ Y"), number(fields[3], "OBJ Z"),
      ]);
      if (vertices.length > MAX_VERTICES) throw new Error("模型顶点数超过 250000，无法在浏览器中预览");
    } else if (fields[0] === "f" && fields.length >= 4) {
      const face = fields.slice(1).map((token) => {
        const value = Number.parseInt(token.split("/")[0], 10);
        if (!Number.isInteger(value) || value === 0) throw new Error("OBJ 面索引格式无效");
        const index = value < 0 ? vertices.length + value : value - 1;
        if (index < 0 || index >= vertices.length) throw new Error("OBJ 面索引超出顶点范围");
        return index;
      });
      for (let index = 1; index + 1 < face.length; index += 1) {
        indices.push(face[0], face[index], face[index + 1]);
      }
    }
  }
  return finishModel(name, "OBJ", vertices, indices);
}

function parseBinaryStl(buffer, name) {
  if (buffer.byteLength < 84) throw new Error("STL 文件长度不足");
  const view = new DataView(buffer);
  const count = view.getUint32(80, true);
  if (84 + count * 50 > buffer.byteLength || count > MAX_VERTICES / 3) {
    throw new Error("STL 三角面数量超出浏览器预览上限");
  }
  const vertices = [];
  const lookup = new Map();
  const indices = [];
  for (let face = 0; face < count; face += 1) {
    const offset = 84 + face * 50 + 12;
    const triangle = [];
    for (let corner = 0; corner < 3; corner += 1) {
      const base = offset + corner * 12;
      triangle.push(addVertex(vertices, lookup, [
        view.getFloat32(base, true), view.getFloat32(base + 4, true), view.getFloat32(base + 8, true),
      ]));
    }
    indices.push(...triangle);
  }
  return finishModel(name, "STL", vertices, indices);
}

function parseAsciiStl(text, name) {
  const vertices = [];
  const lookup = new Map();
  const indices = [];
  const matches = text.matchAll(/vertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)/gi);
  let triangle = [];
  for (const match of matches) {
    triangle.push(addVertex(vertices, lookup, [
      number(match[1], "STL X"), number(match[2], "STL Y"), number(match[3], "STL Z"),
    ]));
    if (triangle.length === 3) {
      indices.push(...triangle);
      triangle = [];
    }
  }
  return finishModel(name, "STL", vertices, indices);
}

export async function loadModelFile(file) {
  const name = file.name || "imported-model";
  const extension = name.toLowerCase().split(".").pop();
  if (!["obj", "stl"].includes(extension)) {
    throw new Error("当前支持 OBJ 和 STL 模型；STEP/STP 需要先转换为其中一种格式");
  }
  if (extension === "obj") return parseObj(await file.text(), name);
  const buffer = await file.arrayBuffer();
  const header = new TextDecoder().decode(buffer.slice(0, Math.min(buffer.byteLength, 512)));
  const binaryLooksValid = buffer.byteLength >= 84
    && 84 + new DataView(buffer).getUint32(80, true) * 50 <= buffer.byteLength;
  return binaryLooksValid ? parseBinaryStl(buffer, name) : parseAsciiStl(header + new TextDecoder().decode(buffer.slice(512)), name);
}
