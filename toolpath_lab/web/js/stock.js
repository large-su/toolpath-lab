// 教学用高度场毛坯仿真：只有勾选“材料切除仿真”时才创建实例。
import { poseSamples } from "./collision.js";
import { slerpAxis } from "./orientation.js";

function pointInPolygon(x, y, boundary) {
  let inside = false;
  let previous = boundary[boundary.length - 1];
  for (const current of boundary) {
    const dx = current[0] - previous[0], dy = current[1] - previous[1];
    const cross = (x - previous[0]) * dy - (y - previous[1]) * dx;
    if (Math.abs(cross) <= 1e-8 * Math.max(1, Math.hypot(dx, dy))
        && x >= Math.min(previous[0], current[0]) - 1e-8
        && x <= Math.max(previous[0], current[0]) + 1e-8
        && y >= Math.min(previous[1], current[1]) - 1e-8
        && y <= Math.max(previous[1], current[1]) + 1e-8) return true;
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
    this.initialVolume = 0;
    for (let row = 0; row < ny; row += 1) {
      for (let col = 0; col < nx; col += 1) {
        const index = row * nx + col;
        this.active[index] = pointInPolygon(this.xs[col], this.ys[row], spec.boundary) ? 1 : 0;
        this.heights[index] = this.active[index] ? spec.initial_top_z_mm : spec.bottom_z_mm;
      }
    }
    this.initialVolume = this._volume();
    this.boundaryGridIndices = (spec.boundary || []).map((point) => {
      let best = 0;
      let bestDistance = Infinity;
      for (let row = 0; row < this.ny; row += 1) {
        for (let col = 0; col < this.nx; col += 1) {
          if (!this.active[row * this.nx + col]) continue;
          const dx = this.xs[col] - point[0];
          const dy = this.ys[row] - point[1];
          const distance = dx * dx + dy * dy;
          if (distance < bestDistance) {
            bestDistance = distance;
            best = row * this.nx + col;
          }
        }
      }
      return best;
    });
    this.kinds = decodeRuns(timeline.kind_runs || [], timeline.positions.length);
    this.cursor = -1;
    this.queryTime = -1;
    this.partialPose = null;
    this.firstCollision = null;
    this.currentCollision = null;
  }

  reset() {
    for (let index = 0; index < this.heights.length; index += 1) {
      this.heights[index] = this.active[index]
        ? this.spec.initial_top_z_mm : this.spec.bottom_z_mm;
    }
    this.cursor = -1;
    this.queryTime = -1;
    this.partialPose = null;
    this.firstCollision = null;
    this.currentCollision = null;
  }

  setIndex(targetIndex, inspector = null) {
    const target = Math.max(0, Math.min(targetIndex, this.timeline.positions.length - 1));
    if (target < this.cursor) this.reset();
    this.firstCollision = null;
    for (let index = this.cursor + 1; index <= target; index += 1) {
      const previous = Math.max(0, index - 1);
      const partial = this.partialPose && this.partialPose.index === previous ? this.partialPose : null;
      this._advanceSegment(
        partial ? partial.position : this.timeline.positions[previous], this.timeline.positions[index],
        partial ? partial.axis : (this.timeline.tool_axes?.[previous] || [0, 0, 1]),
        this.timeline.tool_axes?.[index] || [0, 0, 1],
        partial ? partial.time : this.timeline.times[previous], this.timeline.times[index],
        this.kinds[index] === 0, inspector
      );
      this.partialPose = null;
    }
    this.cursor = target;
    return this.heights;
  }

  updateAt(time, inspector = null) {
    const times = this.timeline.times;
    const query = Math.max(times[0], Math.min(Number(time), times[times.length - 1]));
    if (query < this.queryTime - 1e-9) this.reset();
    const unchanged = query === this.queryTime;
    let low = 0;
    let high = times.length - 1;
    while (low < high) {
      const mid = (low + high + 1) >> 1;
      if (times[mid] <= query) low = mid;
      else high = mid - 1;
    }
    this.setIndex(low, inspector);
    const next = Math.min(low + 1, times.length - 1);
    if (!unchanged && query > times[low] && times[next] > times[low]) {
      const ratio = (query - times[low]) / (times[next] - times[low]);
      const a = this.timeline.positions[low];
      const b = this.timeline.positions[next];
      const axisA = this.timeline.tool_axes?.[low] || [0, 0, 1];
      const axisB = this.timeline.tool_axes?.[next] || [0, 0, 1];
      const point = a.map((value, i) => value + (b[i] - value) * ratio);
      const axis = slerpAxis(axisA, axisB, ratio);
      const partial = this.partialPose;
      this._advanceSegment(partial ? partial.position : a, point,
        partial ? partial.axis : axisA, axis, partial ? partial.time : times[low], query,
        this.kinds[next] === 0, inspector);
      this.partialPose = { index: low, position: point, axis, time: query };
    }
    // 暂停中切换检测开关时，也检查当前位置；不必重新切除或播放整条刀路。
    if (unchanged) {
      const pose = this.partialPose || {
        position: this.timeline.positions[low], axis: this.timeline.tool_axes?.[low] || [0, 0, 1],
      };
      const hit = inspector ? inspector(pose.position, pose.axis) : null;
      this.currentCollision = hit ? { ...hit, time_s: query } : null;
      this.firstCollision = this.currentCollision;
    }
    this.queryTime = query;
    return { first: this.firstCollision, current: this.currentCollision };
  }

  _advanceSegment(start, end, axisStart, axisEnd, timeStart, timeEnd, cutting, inspector) {
    if (!inspector) {
      if (cutting) this._removeSegment(start, end);
      this.currentCollision = null;
      return;
    }
    let previous = start;
    for (const pose of poseSamples(start, end, axisStart, axisEnd, this.tool, this.spec.resolution_mm)) {
      // 检查非切削刀身后才更新材料，防止刀身干涉被圆形削料操作掩盖。
      const hit = inspector(pose.position, pose.axis);
      this.currentCollision = hit ? {
        ...hit, time_s: timeStart + (timeEnd - timeStart) * pose.ratio,
      } : null;
      if (!this.firstCollision && this.currentCollision) this.firstCollision = this.currentCollision;
      if (cutting) this._removeSegment(previous, pose.position);
      previous = pose.position;
    }
  }

  heightAt(x, y) {
    if (!pointInPolygon(x, y, this.spec.boundary)) return null;
    return this.heightWeights(x, y).reduce((sum, [index, weight]) => sum + this.heights[index] * weight, 0);
  }

  heightWeights(x, y) {
    const xStep = (this.xs[this.nx - 1] - this.xs[0]) / (this.nx - 1);
    const yStep = (this.ys[this.ny - 1] - this.ys[0]) / (this.ny - 1);
    const fx = Math.max(0, Math.min(this.nx - 1, (x - this.xs[0]) / xStep));
    const fy = Math.max(0, Math.min(this.ny - 1, (y - this.ys[0]) / yStep));
    const col = Math.min(this.nx - 2, Math.floor(fx));
    const row = Math.min(this.ny - 2, Math.floor(fy));
    const tx = fx - col;
    const ty = fy - row;
    const a = row * this.nx + col;
    const samples = [[a, (1 - tx) * (1 - ty)], [a + 1, tx * (1 - ty)],
      [a + this.nx, (1 - tx) * ty], [a + this.nx + 1, tx * ty]]
      .filter(([index, weight]) => this.active[index] && weight > 0);
    const sum = samples.reduce((total, sample) => total + sample[1], 0);
    if (sum > 1e-12) return samples.map(([index, weight]) => [index, weight / sum]);
    // A very thin boundary cell can contain no active corner: use nearest interior sample.
    let best = -1, distance = Infinity;
    for (let i = 0; i < this.active.length; i += 1) {
      if (!this.active[i]) continue;
      const d = (this.xs[i % this.nx] - x) ** 2 + (this.ys[Math.floor(i / this.nx)] - y) ** 2;
      if (d < distance) { best = i; distance = d; }
    }
    return best >= 0 ? [[best, 1]] : [];
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

  _removeSegment(start, end) {
    const radius = Number(this.tool.radius_mm || this.spec.tool_radius_mm || 0);
    if (!(radius > 0)) return;
    const x0 = Number(start[0]);
    const y0 = Number(start[1]);
    const z0 = Number(start[2]);
    const dx = Number(end[0]) - x0;
    const dy = Number(end[1]) - y0;
    const dz = Number(end[2]) - z0;
    const lengthSquared = dx * dx + dy * dy;
    if (lengthSquared <= 1e-12) {
      this._removePoint(end);
      return;
    }
    const radiusSquared = radius * radius;
    const xMin = Math.min(x0, x0 + dx) - radius;
    const xMax = Math.max(x0, x0 + dx) + radius;
    const yMin = Math.min(y0, y0 + dy) - radius;
    const yMax = Math.max(y0, y0 + dy) + radius;
    let colStart = 0;
    while (colStart < this.nx && this.xs[colStart] < xMin) colStart += 1;
    let colEnd = this.nx - 1;
    while (colEnd >= 0 && this.xs[colEnd] > xMax) colEnd -= 1;
    let rowStart = 0;
    while (rowStart < this.ny && this.ys[rowStart] < yMin) rowStart += 1;
    let rowEnd = this.ny - 1;
    while (rowEnd >= 0 && this.ys[rowEnd] > yMax) rowEnd -= 1;
    for (let row = rowStart; row <= rowEnd; row += 1) {
      const y = this.ys[row];
      for (let col = colStart; col <= colEnd; col += 1) {
        const index = row * this.nx + col;
        if (!this.active[index]) continue;
        const x = this.xs[col];
        const t = Math.max(0, Math.min(1, ((x - x0) * dx + (y - y0) * dy) / lengthSquared));
        const closestX = x0 + t * dx;
        const closestY = y0 + t * dy;
        const distanceSquared = (x - closestX) ** 2 + (y - closestY) ** 2;
        if (distanceSquared > radiusSquared) continue;
        const cutHeight = Math.max(z0 + t * dz, this.spec.bottom_z_mm);
        if (cutHeight < this.heights[index]) this.heights[index] = cutHeight;
      }
    }
  }

  _volume() {
    const cellArea = Number(this.spec.resolution_mm || 1) ** 2;
    let volume = 0;
    for (let index = 0; index < this.heights.length; index += 1) {
      if (this.active[index]) {
        volume += Math.max(this.heights[index] - this.spec.bottom_z_mm, 0) * cellArea;
      }
    }
    return volume;
  }

  stats() {
    const remaining = this._volume();
    const removed = Math.max(0, this.initialVolume - remaining);
    return {
      initial_volume_mm3: this.initialVolume,
      remaining_volume_mm3: remaining,
      removed_volume_mm3: removed,
      removed_percent: this.initialVolume > 1e-9 ? removed / this.initialVolume * 100 : 0,
    };
  }

  colors() {
    const colors = new Float32Array(this.heights.length * 3);
    const span = Math.max(this.spec.initial_top_z_mm - this.spec.bottom_z_mm, 1e-9);
    for (let index = 0; index < this.heights.length; index += 1) {
      const ratio = this.active[index]
        ? Math.max(0, Math.min(1, (this.heights[index] - this.spec.bottom_z_mm) / span))
        : 0;
      const removed = 1 - ratio;
      // 未切削为蓝色，切削越多逐渐转为琥珀色，便于观察材料去除范围。
      colors[index * 3] = 0.12 + removed * 0.78;
      colors[index * 3 + 1] = 0.34 + (1 - removed) * 0.20;
      colors[index * 3 + 2] = 0.62 - removed * 0.56;
    }
    return colors;
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
    for (let index = 0; index < boundary.length; index += 1) {
      const point = boundary[index];
      const gridIndex = this.boundaryGridIndices[index];
      const height = gridIndex === undefined
        ? this.boundaryHeight(point[0], point[1]) : this.heights[gridIndex];
      points.push(point[0], point[1], height);
      points.push(point[0], point[1], this.spec.bottom_z_mm);
    }
    return points;
  }

  boundaryHeight(x, y) {
    const points = this.spec.boundary || [];
    const boundaryIndex = points.findIndex((point) => point[0] === x && point[1] === y);
    if (boundaryIndex >= 0 && this.boundaryGridIndices[boundaryIndex] !== undefined) {
      return this.heights[this.boundaryGridIndices[boundaryIndex]];
    }
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
