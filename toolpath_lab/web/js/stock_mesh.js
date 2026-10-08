// Clip a triangulated region to height-field cells once. Playback only updates Z.
// Top, side walls and bottom reuse exactly the same XY seam (including grid cuts).
function clip(points, axis, limit, keepGreater) {
  const output = [];
  let a = points[points.length - 1];
  if (!a) return output;
  let aInside = keepGreater ? a[axis] >= limit : a[axis] <= limit;
  for (const b of points) {
    const bInside = keepGreater ? b[axis] >= limit : b[axis] <= limit;
    if (aInside !== bInside) {
      const t = (limit - a[axis]) / (b[axis] - a[axis]);
      const p = [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])];
      p[axis] = limit;
      output.push(p);
    }
    if (bInside) output.push(b);
    a = b;
    aInside = bInside;
  }
  return output;
}

export class StockMesh {
  constructor(stock, triangulate) {
    this.stock = stock;
    this.xy = [];
    this.topIndices = [];
    const vertices = new Map();
    const scale = Math.max(1, ...stock.xs.map(Math.abs), ...stock.ys.map(Math.abs));
    const tolerance = scale * 1e-10;
    const vertex = p => {
      const key = p.map(v => Math.round(v / tolerance)).join(",");
      if (!vertices.has(key)) {
        vertices.set(key, this.xy.length);
        this.xy.push(p);
      }
      return vertices.get(key);
    };
    const boundary = stock.spec.boundary;
    const faces = triangulate(boundary);
    for (const face of faces) {
      const triangle = face.map(i => boundary[i]);
      const minX = Math.min(...triangle.map(p => p[0]));
      const maxX = Math.max(...triangle.map(p => p[0]));
      const minY = Math.min(...triangle.map(p => p[1]));
      const maxY = Math.max(...triangle.map(p => p[1]));
      for (let row = 0; row < stock.ny - 1; row += 1) {
        if (stock.ys[row + 1] <= minY || stock.ys[row] >= maxY) continue;
        for (let col = 0; col < stock.nx - 1; col += 1) {
          if (stock.xs[col + 1] <= minX || stock.xs[col] >= maxX) continue;
          let polygon = clip(triangle, 0, stock.xs[col], true);
          polygon = clip(polygon, 0, stock.xs[col + 1], false);
          polygon = clip(polygon, 1, stock.ys[row], true);
          polygon = clip(polygon, 1, stock.ys[row + 1], false);
          const ids = polygon.map(vertex).filter((id, i, all) => id !== all[(i + all.length - 1) % all.length]);
          for (let i = 1; i + 1 < ids.length; i += 1) {
            const [a, b, c] = [ids[0], ids[i], ids[i + 1]];
            const [p, q, r] = [this.xy[a], this.xy[b], this.xy[c]];
            const area = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
            if (Math.abs(area) <= tolerance * tolerance) continue;
            this.topIndices.push(...(area > 0 ? [a, b, c] : [a, c, b]));
          }
        }
      }
    }
    const edges = new Map();
    for (let i = 0; i < this.topIndices.length; i += 3) {
      const face = this.topIndices.slice(i, i + 3);
      for (let j = 0; j < 3; j += 1) {
        const a = face[j], b = face[(j + 1) % 3];
        const key = a < b ? `${a},${b}` : `${b},${a}`;
        if (edges.has(key)) edges.delete(key);
        else edges.set(key, [a, b]);
      }
    }
    this.edges = [...edges.values()];
    // Cache active-only interpolation; exterior grid nodes must never drag down the rim.
    this.samples = this.xy.map(p => stock.heightWeights(...p));
    this.topPositions = new Float32Array(this.xy.length * 3);
    this.topColors = new Float32Array(this.xy.length * 3);
    // Bottom duplicates the top tessellation with reverse winding. Walls share XY/Z.
    this.shellPositions = new Float32Array((this.xy.length + this.edges.length * 4) * 3);
    this.shellIndices = [];
    for (let i = 0; i < this.topIndices.length; i += 3) {
      this.shellIndices.push(this.topIndices[i], this.topIndices[i + 2], this.topIndices[i + 1]);
    }
    this.edges.forEach((_, i) => {
      const a = this.xy.length + i * 4;
      this.shellIndices.push(a, a + 2, a + 1, a + 2, a + 3, a + 1);
    });
    this.update();
  }

  update() {
    const stock = this.stock;
    const bottom = stock.spec.bottom_z_mm;
    const span = Math.max(stock.spec.initial_top_z_mm - bottom, 1e-9);
    this.xy.forEach((p, i) => {
      const sample = this.samples[i];
      const z = sample.length ? sample.reduce((sum, [index, weight]) => sum + stock.heights[index] * weight, 0)
        : stock.spec.initial_top_z_mm;
      this.topPositions.set([p[0], p[1], z], i * 3);
      this.shellPositions.set([p[0], p[1], bottom], i * 3);
      const removed = 1 - Math.max(0, Math.min(1, (z - bottom) / span));
      this.topColors.set([0.12 + removed * 0.78, 0.54 - removed * 0.20, 0.62 - removed * 0.56], i * 3);
    });
    this.edges.forEach(([a, b], i) => {
      const offset = (this.xy.length + i * 4) * 3;
      this.shellPositions.set(this.topPositions.subarray(a * 3, a * 3 + 3), offset);
      this.shellPositions.set([this.xy[a][0], this.xy[a][1], bottom], offset + 3);
      this.shellPositions.set(this.topPositions.subarray(b * 3, b * 3 + 3), offset + 6);
      this.shellPositions.set([this.xy[b][0], this.xy[b][1], bottom], offset + 9);
    });
  }
}
