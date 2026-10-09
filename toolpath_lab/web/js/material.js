const MAX_GRID_CELLS = 30000;

// 刀轴方向：与后端 HeightField 使用的公式完全一致，视口也用同一个函数给刀具网格定向。
export function toolAxis(rotaryAxes) {
  const a = rotaryAxes[0] * Math.PI / 180;
  const b = rotaryAxes[1] * Math.PI / 180;
  return [Math.sin(b) * Math.cos(a), -Math.sin(a), Math.cos(b) * Math.cos(a)];
}

function cutterProfileSegments(toolKind, radius, cornerRadius, length, resolution) {
  if (toolKind === "flat") return [[0, length, radius]];

  const segments = [];
  if (toolKind === "ball") {
    const transitionLength = Math.min(radius, length);
    const count = Math.max(4, Math.ceil(transitionLength / Math.max(resolution * 0.5, 0.05)));
    for (let index = 0; index < count; index += 1) {
      const start = transitionLength * index / count;
      const end = transitionLength * (index + 1) / count;
      const sliceRadius = Math.sqrt(Math.max(0, 2 * radius * end - end ** 2));
      if (sliceRadius > 1e-9) segments.push([start, end, sliceRadius]);
    }
    if (length > radius) segments.push([radius, length, radius]);
    return segments;
  }

  if (toolKind === "bull") {
    if (!Number.isFinite(cornerRadius) || cornerRadius <= 0 || cornerRadius > radius) {
      throw new Error("Bull-nose corner radius must be positive and no larger than the tool radius.");
    }
    const transitionLength = Math.min(cornerRadius, length);
    const count = Math.max(4, Math.ceil(transitionLength / Math.max(resolution * 0.25, 0.025)));
    const flatRadius = radius - cornerRadius;
    for (let index = 0; index < count; index += 1) {
      const start = transitionLength * index / count;
      const end = transitionLength * (index + 1) / count;
      const sliceRadius = flatRadius + Math.sqrt(
        Math.max(0, 2 * cornerRadius * end - end ** 2)
      );
      if (sliceRadius > 1e-9) segments.push([start, end, sliceRadius]);
    }
    if (length > cornerRadius) segments.push([cornerRadius, length, radius]);
    return segments;
  }

  throw new Error(`Unsupported cutter type: ${toolKind}`);
}

function verticalCylinderInterval(x, y, position, axis, radius, length) {
  const dx = x - position[0];
  const dy = y - position[1];
  const projection = axis[0] * dx + axis[1] * dy;
  const a = Math.max(0, 1 - axis[2] ** 2);
  const b = -2 * projection * axis[2];
  const c = dx ** 2 + dy ** 2 - projection ** 2 - radius ** 2;
  let radialLow = -Infinity;
  let radialHigh = Infinity;
  if (a <= 1e-12) {
    if (c > 1e-9) return null;
  } else {
    const discriminant = b ** 2 - 4 * a * c;
    if (discriminant < 0) return null;
    const root = Math.sqrt(Math.max(0, discriminant));
    radialLow = (-b - root) / (2 * a);
    radialHigh = (-b + root) / (2 * a);
  }

  let axialLow = -Infinity;
  let axialHigh = Infinity;
  if (Math.abs(axis[2]) <= 1e-12) {
    if (projection < 0 || projection > length) return null;
  } else {
    const axialStart = -projection / axis[2];
    const axialEnd = (length - projection) / axis[2];
    axialLow = Math.min(axialStart, axialEnd);
    axialHigh = Math.max(axialStart, axialEnd);
  }

  const low = Math.max(radialLow, axialLow);
  const high = Math.min(radialHigh, axialHigh);
  return low <= high ? [position[2] + low, position[2] + high] : null;
}

export class MaterialSimulation {
  constructor({
    boundary, bounds, radiusMm, fluteLengthMm, toolKind = "flat", cornerRadiusMm = null,
    bottomZMm, timeline,
    resolutionMm = 0.5, topZMm = 0.0,
  }) {
    if (!Array.isArray(boundary) || boundary.length < 3) {
      throw new Error("Material simulation needs a polygon boundary.");
    }
    if (!Number.isFinite(radiusMm) || radiusMm <= 0) {
      throw new Error("Material simulation requires a positive flat-tool radius.");
    }
    if (!Number.isFinite(fluteLengthMm) || fluteLengthMm <= 0) {
      throw new Error("Material simulation requires a positive flute length.");
    }
    if (!["flat", "ball", "bull"].includes(toolKind)) {
      throw new Error(`Unsupported cutter type: ${toolKind}`);
    }
    if (!Number.isFinite(resolutionMm) || resolutionMm <= 0) {
      throw new Error("Material simulation grid resolution must be positive.");
    }
    if (!Number.isFinite(topZMm) || !Number.isFinite(bottomZMm) || bottomZMm >= topZMm) {
      throw new Error("Material simulation stock bottom must be below its top.");
    }

    this.boundary = boundary.map((point) => [Number(point[0]), Number(point[1])]);
    this.radiusMm = radiusMm;
    this.fluteLengthMm = fluteLengthMm;
    this.toolKind = toolKind;
    this.cornerRadiusMm = cornerRadiusMm;
    this.topZMm = topZMm;
    this.bottomZMm = bottomZMm;
    this.timeline = timeline;
    this.positions = timeline.positions;
    this.rotaryAxes = timeline.rotary_axes || timeline.positions.map(() => [0, 0]);
    this.kinds = this._decodeKinds(timeline);
    this.cursor = -1;

    const [xMin, xMax] = bounds[0];
    const [yMin, yMax] = bounds[1];
    const spanX = xMax - xMin;
    const spanY = yMax - yMin;
    let spacing = resolutionMm;
    while (true) {
      this.columns = Math.max(2, Math.ceil(spanX / spacing) + 1);
      this.rows = Math.max(2, Math.ceil(spanY / spacing) + 1);
      if (this.columns * this.rows <= MAX_GRID_CELLS) break;
      spacing *= Math.sqrt((this.columns * this.rows) / MAX_GRID_CELLS) * 1.01;
    }
    this.xMin = xMin;
    this.yMin = yMin;
    this.cellX = spanX / (this.columns - 1);
    this.cellY = spanY / (this.rows - 1);
    this.resolutionMm = Math.max(this.cellX, this.cellY);
    this.profileSegments = cutterProfileSegments(
      this.toolKind, this.radiusMm, this.cornerRadiusMm,
      this.fluteLengthMm, this.resolutionMm
    );
    this.heights = new Float32Array(this.columns * this.rows);
    this.inside = new Uint8Array(this.columns * this.rows);
    this._createGrid();
  }

  _decodeKinds(timeline) {
    const kinds = new Uint8Array(timeline.positions.length);
    const rapidCode = Object.keys(timeline.kind_codes || {})
      .find((code) => timeline.kind_codes[code] === "rapid");
    if (rapidCode === undefined) {
      throw new Error("Timeline is missing the rapid movement code.");
    }
    const runs = timeline.kind_runs || [];
    for (let runIndex = 0; runIndex < runs.length; runIndex += 1) {
      const [start, code] = runs[runIndex];
      const end = runIndex + 1 < runs.length ? runs[runIndex + 1][0] : kinds.length;
      kinds.fill(Number(code), Number(start), Number(end));
    }
    this.rapidCode = Number(rapidCode);
    return kinds;
  }

  _createGrid() {
    for (let row = 0; row < this.rows; row += 1) {
      const y = this.yMin + row * this.cellY;
      for (let column = 0; column < this.columns; column += 1) {
        const x = this.xMin + column * this.cellX;
        const index = row * this.columns + column;
        if (this._contains(x, y)) {
          this.inside[index] = 1;
          this.heights[index] = this.topZMm;
        } else {
          this.heights[index] = this.bottomZMm;
        }
      }
    }
  }

  _contains(x, y) {
    let inside = false;
    for (let i = 0, j = this.boundary.length - 1; i < this.boundary.length; j = i, i += 1) {
      const [xi, yi] = this.boundary[i];
      const [xj, yj] = this.boundary[j];
      const cross = (x - xi) * (yj - yi) - (y - yi) * (xj - xi);
      const onSegment = Math.abs(cross) < 1e-7
        && x >= Math.min(xi, xj) - 1e-7 && x <= Math.max(xi, xj) + 1e-7
        && y >= Math.min(yi, yj) - 1e-7 && y <= Math.max(yi, yj) + 1e-7;
      if (onSegment) return true;
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  reset() {
    for (let index = 0; index < this.heights.length; index += 1) {
      this.heights[index] = this.inside[index] ? this.topZMm : this.bottomZMm;
    }
    this.cursor = -1;
  }

  update(sampleIndex) {
    const target = Math.max(0, Math.min(Math.trunc(sampleIndex), this.positions.length - 1));
    let changed = false;
    if (target < this.cursor) {
      this.reset();
      changed = true;
    }
    if (target === this.cursor) return false;

    for (let index = this.cursor + 1; index <= target; index += 1) {
      if (this.kinds[index] === this.rapidCode) continue;
      const current = this.positions[index];
      const currentAxes = this.rotaryAxes[index];
      if (index === 0) {
        changed = this._cutAt(current, currentAxes) || changed;
      } else {
        changed = this._cutSegment(
          this.positions[index - 1], current,
          this.rotaryAxes[index - 1], currentAxes
        ) || changed;
      }
    }
    this.cursor = target;
    return changed;
  }

  _cutSegment(start, end, startAxes, endAxes) {
    const distance = Math.hypot(end[0] - start[0], end[1] - start[1], end[2] - start[2]);
    const angleChange = Math.max(
      Math.abs(endAxes[0] - startAxes[0]), Math.abs(endAxes[1] - startAxes[1])
    );
    const steps = Math.max(
      1,
      Math.ceil(distance / Math.max(this.resolutionMm * 0.5, 1e-6)),
      Math.ceil(angleChange)
    );
    let changed = false;
    for (let index = 1; index <= steps; index += 1) {
      const ratio = index / steps;
      const position = start.map((value, axis) => value + (end[axis] - value) * ratio);
      const rotaryAxes = startAxes.map(
        (value, axis) => value + (endAxes[axis] - value) * ratio
      );
      changed = this._cutAt(position, rotaryAxes) || changed;
    }
    return changed;
  }

  _cutAt(position, rotaryAxes) {
    const axis = toolAxis(rotaryAxes);
    const endX = position[0] + axis[0] * this.fluteLengthMm;
    const endY = position[1] + axis[1] * this.fluteLengthMm;
    const columnStart = Math.max(
      0,
      Math.floor((Math.min(position[0], endX) - this.radiusMm - this.xMin) / this.cellX)
    );
    const columnEnd = Math.min(
      this.columns - 1,
      Math.ceil((Math.max(position[0], endX) + this.radiusMm - this.xMin) / this.cellX)
    );
    const rowStart = Math.max(
      0,
      Math.floor((Math.min(position[1], endY) - this.radiusMm - this.yMin) / this.cellY)
    );
    const rowEnd = Math.min(
      this.rows - 1,
      Math.ceil((Math.max(position[1], endY) + this.radiusMm - this.yMin) / this.cellY)
    );
    let changed = false;
    for (let row = rowStart; row <= rowEnd; row += 1) {
      const y = this.yMin + row * this.cellY;
      for (let column = columnStart; column <= columnEnd; column += 1) {
        const index = row * this.columns + column;
        if (!this.inside[index]) continue;
        const x = this.xMin + column * this.cellX;
        for (let segmentIndex = this.profileSegments.length - 1; segmentIndex >= 0; segmentIndex -= 1) {
          const [axialStart, axialEnd, segmentRadius] = this.profileSegments[segmentIndex];
          const segmentPosition = [
            position[0] + axis[0] * axialStart,
            position[1] + axis[1] * axialStart,
            position[2] + axis[2] * axialStart,
          ];
          const interval = verticalCylinderInterval(
            x, y, segmentPosition, axis, segmentRadius, axialEnd - axialStart
          );
          if (!interval) continue;
          // interval 是这一列上"刀体实体"占据的 Z 区间，toolLow 即刀体在该列的最低点。
          const [toolLow] = interval;
          const height = this.heights[index];
          // 只要刀体最低点低于当前料面，这一列毛坯就和刀体体积重叠，必须切掉。
          // 这里不能加"刀体最高点也低于料面就跳过"的短路：那正是摆轴刀具的侧壁
          // 已经埋进毛坯、而料面却纹丝不动的原因——渲染时料面会横切过刀体，
          // 看起来就是刀具与工件互相干涉。后端 HeightField 用的是同一条规则
          // （只看接触面是否低于料面，不看上方是否还有刀体）。
          if (toolLow >= height) continue;
          this.heights[index] = Math.max(toolLow, this.bottomZMm);
          changed = true;
        }
      }
    }
    return changed;
  }

  surfaceData() {
    const positions = new Float32Array(this.heights.length * 3);
    for (let row = 0; row < this.rows; row += 1) {
      for (let column = 0; column < this.columns; column += 1) {
        const index = row * this.columns + column;
        const offset = index * 3;
        positions[offset] = this.xMin + column * this.cellX;
        positions[offset + 1] = this.yMin + row * this.cellY;
        positions[offset + 2] = this.heights[index];
      }
    }

    const indices = [];
    for (let row = 0; row + 1 < this.rows; row += 1) {
      for (let column = 0; column + 1 < this.columns; column += 1) {
        const lowerLeft = row * this.columns + column;
        const lowerRight = lowerLeft + 1;
        const upperLeft = lowerLeft + this.columns;
        const upperRight = upperLeft + 1;
        if (this.inside[lowerLeft] && this.inside[lowerRight] && this.inside[upperRight]) {
          indices.push(lowerLeft, lowerRight, upperRight);
        }
        if (this.inside[lowerLeft] && this.inside[upperRight] && this.inside[upperLeft]) {
          indices.push(lowerLeft, upperRight, upperLeft);
        }
      }
    }
    return { positions, indices: new Uint32Array(indices) };
  }

  writeHeightsTo(positionArray) {
    for (let index = 0; index < this.heights.length; index += 1) {
      positionArray[index * 3 + 2] = this.heights[index];
    }
  }
}
