const MAX_GRID_CELLS = 30000;

export class MaterialSimulation {
  constructor({
    boundary, bounds, radiusMm, bottomZMm, timeline,
    resolutionMm = 0.5, topZMm = 0.0,
  }) {
    if (!Array.isArray(boundary) || boundary.length < 3) {
      throw new Error("Material simulation needs a polygon boundary.");
    }
    if (!Number.isFinite(radiusMm) || radiusMm <= 0) {
      throw new Error("Material simulation requires a positive flat-tool radius.");
    }
    if (!Number.isFinite(resolutionMm) || resolutionMm <= 0) {
      throw new Error("Material simulation grid resolution must be positive.");
    }
    if (!Number.isFinite(topZMm) || !Number.isFinite(bottomZMm) || bottomZMm >= topZMm) {
      throw new Error("Material simulation stock bottom must be below its top.");
    }

    this.boundary = boundary.map((point) => [Number(point[0]), Number(point[1])]);
    this.radiusMm = radiusMm;
    this.topZMm = topZMm;
    this.bottomZMm = bottomZMm;
    this.timeline = timeline;
    this.positions = timeline.positions;
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
      if (index === 0) {
        changed = this._cutAt(current) || changed;
      } else {
        changed = this._cutSegment(this.positions[index - 1], current) || changed;
      }
    }
    this.cursor = target;
    return changed;
  }

  _cutSegment(start, end) {
    const dx = end[0] - start[0];
    const dy = end[1] - start[1];
    const lengthSquared = dx * dx + dy * dy;
    const columnStart = Math.max(
      0,
      Math.floor((Math.min(start[0], end[0]) - this.radiusMm - this.xMin) / this.cellX)
    );
    const columnEnd = Math.min(
      this.columns - 1,
      Math.ceil((Math.max(start[0], end[0]) + this.radiusMm - this.xMin) / this.cellX)
    );
    const rowStart = Math.max(
      0,
      Math.floor((Math.min(start[1], end[1]) - this.radiusMm - this.yMin) / this.cellY)
    );
    const rowEnd = Math.min(
      this.rows - 1,
      Math.ceil((Math.max(start[1], end[1]) + this.radiusMm - this.yMin) / this.cellY)
    );
    const radiusSquared = this.radiusMm ** 2;
    let changed = false;
    for (let row = rowStart; row <= rowEnd; row += 1) {
      const y = this.yMin + row * this.cellY;
      for (let column = columnStart; column <= columnEnd; column += 1) {
        const index = row * this.columns + column;
        if (!this.inside[index]) continue;
        const x = this.xMin + column * this.cellX;
        const ratio = lengthSquared > 1e-12
          ? Math.max(0, Math.min(1, ((x - start[0]) * dx + (y - start[1]) * dy) / lengthSquared))
          : 1;
        const nearestX = start[0] + ratio * dx;
        const nearestY = start[1] + ratio * dy;
        if ((x - nearestX) ** 2 + (y - nearestY) ** 2 > radiusSquared + 1e-8) continue;
        const cutZ = Math.max(
          start[2] + ratio * (end[2] - start[2]),
          this.bottomZMm
        );
        if (cutZ >= this.heights[index]) continue;
        this.heights[index] = cutZ;
        changed = true;
      }
    }
    return changed;
  }

  _cutAt(position) {
    const cutZ = Math.max(position[2], this.bottomZMm);
    if (cutZ >= this.topZMm) return false;
    const columnStart = Math.max(0, Math.floor((position[0] - this.radiusMm - this.xMin) / this.cellX));
    const columnEnd = Math.min(
      this.columns - 1,
      Math.ceil((position[0] + this.radiusMm - this.xMin) / this.cellX)
    );
    const rowStart = Math.max(0, Math.floor((position[1] - this.radiusMm - this.yMin) / this.cellY));
    const rowEnd = Math.min(
      this.rows - 1,
      Math.ceil((position[1] + this.radiusMm - this.yMin) / this.cellY)
    );
    let changed = false;
    for (let row = rowStart; row <= rowEnd; row += 1) {
      const y = this.yMin + row * this.cellY;
      for (let column = columnStart; column <= columnEnd; column += 1) {
        const index = row * this.columns + column;
        if (!this.inside[index]) continue;
        const x = this.xMin + column * this.cellX;
        if ((x - position[0]) ** 2 + (y - position[1]) ** 2 > this.radiusMm ** 2) continue;
        if (this.heights[index] <= cutZ) continue;
        this.heights[index] = cutZ;
        changed = true;
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
