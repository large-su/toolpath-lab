// 教学用高度场毛坯仿真：只有勾选“材料切除仿真”时才创建实例。

function pointInPolygon(x, y, boundary) {
  let inside = false;
  let previous = boundary[boundary.length - 1];
  for (const current of boundary) {
    const crosses = (previous[1] > y) !== (current[1] > y);
    const denominator = current[1] - previous[1] || 1e-12;
    const crossingX = (current[0] - previous[0]) * (y - previous[1]) / denominator + previous[0];
    if (crosses && x < crossingX) inside = !inside;
    previous = current;
  }
  return inside;
}

function decodeRuns(runs, count) {
  const values = new Uint8Array(count);
  for (let index = 0; index < runs.length; index += 1) {
    const start = runs[index][0];
    const end = index + 1 < runs.length ? runs[index + 1][0] : count;
    values.fill(runs[index][1], start, end);
  }
  return values;
}

export class StockSimulation {
  constructor(spec, timeline, tool) {
    this.spec = spec;
    this.timeline = timeline;
    this.tool = tool;
    const [xMin, xMax] = spec.bounds_mm[0];
    const [yMin, yMax] = spec.bounds_mm[1];
    const [nx, ny] = spec.grid_shape;
    this.xs = Array.from({ length: nx }, (_, index) => xMin + (xMax - xMin) * index / (nx - 1));
    this.ys = Array.from({ length: ny }, (_, index) => yMin + (yMax - yMin) * index / (ny - 1));
    this.nx = nx;
    this.ny = ny;
    this.heights = new Float32Array(nx * ny);
    this.active = new Uint8Array(nx * ny);
    for (let row = 0; row < ny; row += 1) {
      for (let col = 0; col < nx; col += 1) {
        const index = row * nx + col;
        this.active[index] = pointInPolygon(this.xs[col], this.ys[row], spec.boundary) ? 1 : 0;
        this.heights[index] = this.active[index] ? spec.initial_top_z_mm : spec.bottom_z_mm;
      }
    }
    this.kinds = decodeRuns(timeline.kind_runs || [], timeline.positions.length);
    this.cursor = -1;
  }

  reset() {
    for (let index = 0; index < this.heights.length; index += 1) {
      this.heights[index] = this.active[index]
        ? this.spec.initial_top_z_mm : this.spec.bottom_z_mm;
    }
    this.cursor = -1;
  }

  setIndex(targetIndex) {
    const target = Math.max(0, Math.min(targetIndex, this.timeline.positions.length - 1));
    if (target < this.cursor) this.reset();
    for (let index = this.cursor + 1; index <= target; index += 1) {
      if (this.kinds[index] === 0) this._removePoint(this.timeline.positions[index]);
    }
    this.cursor = target;
    return this.heights;
  }

  _removePoint(point) {
    const radius = Number(this.tool.radius_mm || this.spec.tool_radius_mm || 0);
    if (!(radius > 0)) return;
    const radiusSquared = radius * radius;
    for (let row = 0; row < this.ny; row += 1) {
      const dy = this.ys[row] - point[1];
      if (Math.abs(dy) > radius) continue;
      for (let col = 0; col < this.nx; col += 1) {
        const index = row * this.nx + col;
        if (!this.active[index]) continue;
        const dx = this.xs[col] - point[0];
        if (dx * dx + dy * dy <= radiusSquared && point[2] < this.heights[index]) {
          this.heights[index] = Math.max(point[2], this.spec.bottom_z_mm);
        }
      }
    }
  }

  positions() {
    const points = [];
    for (let row = 0; row < this.ny; row += 1) {
      for (let col = 0; col < this.nx; col += 1) {
        points.push(this.xs[col], this.ys[row], this.heights[row * this.nx + col]);
      }
    }
    return points;
  }

  boundaryPositions() {
    const points = [];
    const boundary = this.spec.boundary || [];
    for (const point of boundary) {
      points.push(point[0], point[1], this.boundaryHeight(point[0], point[1]));
      points.push(point[0], point[1], this.spec.bottom_z_mm);
    }
    return points;
  }

  boundaryHeight(x, y) {
    let bestDistance = Infinity;
    let bestHeight = this.spec.initial_top_z_mm;
    for (let row = 0; row < this.ny; row += 1) {
      for (let col = 0; col < this.nx; col += 1) {
        const index = row * this.nx + col;
        if (!this.active[index]) continue;
        const dx = this.xs[col] - x;
        const dy = this.ys[row] - y;
        const distance = dx * dx + dy * dy;
        if (distance < bestDistance) {
          bestDistance = distance;
          bestHeight = this.heights[index];
        }
      }
    }
    return bestHeight;
  }

  indices() {
    const indices = [];
    for (let row = 0; row + 1 < this.ny; row += 1) {
      for (let col = 0; col + 1 < this.nx; col += 1) {
        const a = row * this.nx + col;
        const b = a + 1;
        const c = a + this.nx;
        const d = c + 1;
        if (!this.active[a] || !this.active[b] || !this.active[c] || !this.active[d]) continue;
        indices.push(a, b, c, b, d, c);
      }
    }
    return indices;
  }

  boundaryIndices() {
    const indices = [];
    const count = (this.spec.boundary || []).length;
    for (let index = 0; index < count; index += 1) {
      const next = (index + 1) % count;
      const top = index * 2;
      const bottom = top + 1;
      const nextTop = next * 2;
      const nextBottom = nextTop + 1;
      indices.push(top, nextTop, bottom, nextTop, nextBottom, bottom);
    }
    return indices;
  }
}
