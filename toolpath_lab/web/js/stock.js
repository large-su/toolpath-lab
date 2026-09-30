// 毛坯切削仿真：用 2.5D 高度场（Z-map）表示毛坯"现在长什么样"。
//
// 网格铺满毛坯的 XY 投影，每个节点记"这里的顶面现在多高"：初始就是毛坯顶面（贴紧区域、
// 顶面留余量）；刀具经过时，把落在刀盘里的节点压到**刀面**在该半径处的高度。于是动画里
// 毛坯被一点点削掉，走完刀路剩下的是刀具真正切出来的形状——球头刀/圆鼻刀会留下应有的
// 残留曲面，平底刀则如实体现它在斜面上的过切（模型不美化这一点，和文档里的说明一致）。
//
// 网格分辨率按区域大小自适应（目标 STOCK_CELL_MM，节点数封顶 STOCK_MAX_NODES），
// 切削只改高度数组，顶点位置每帧只写变化过的部分。

import * as THREE from "three";

import { blankSize } from "./blank.js";

export const STOCK_CELL_MM = 1.2;
export const STOCK_MAX_NODES = 192;
export const STOCK_WALL_SEGMENTS = 96;

const EPS = 1e-9;

/** 刀面在"离刀轴 radius"处的高度。
 *
 * 平底刀：圆角为 0，整个刀盘一样高；
 * 球头刀：圆角 = 半径，刀面是球面（只有刀尖最低）；
 * 圆鼻刀：半径 R − Rc 之内是平面，之外是 Rc 的圆角。
 */
export function toolFaceHeight(tool, radius, tipZ) {
  const kind = String((tool && tool.kind) || "flat");
  const toolRadius = Math.max(Number((tool && tool.diameter_mm) || 0), 0) / 2;
  const corner = kind === "ball"
    ? toolRadius
    : Math.max(Number((tool && tool.corner_radius_mm) || 0), 0);
  if (corner <= EPS || toolRadius <= EPS) return tipZ;
  const flat = Math.max(toolRadius - corner, 0);
  const r = Math.min(Math.max(Number(radius) || 0, 0), toolRadius);
  if (r <= flat) return tipZ;
  const offset = r - flat;
  return tipZ + corner - Math.sqrt(Math.max(corner * corner - offset * offset, 0));
}

/** 时间轴里逐采样的运动类型（cut / link / rapid）；快移不切料。 */
export function decodeKinds(timeline) {
  const count = timeline && timeline.positions ? timeline.positions.length : 0;
  const kinds = new Array(count).fill("cut");
  if (!timeline || !count) return kinds;
  const names = Object.assign({ 0: "cut", 1: "link", 2: "rapid" }, timeline.kind_codes || {});
  const runs = timeline.kind_runs || [];
  for (let index = 0; index < runs.length; index += 1) {
    const start = runs[index][0];
    const end = index + 1 < runs.length ? runs[index + 1][0] : count;
    const name = names[runs[index][1]] || "cut";
    for (let i = Math.max(start, 0); i < Math.min(end, count); i += 1) kinds[i] = name;
  }
  return kinds;
}

export class StockSimulation {
  constructor(region, tool, topMarginMm = 2, options = {}) {
    this.region = region;
    this.tool = tool || {};
    this.size = blankSize(region, topMarginMm);
    const [xRange, yRange] = region.bounds_mm;
    const cell = Math.max(Number(options.cellMm) || STOCK_CELL_MM, 1e-3);
    const cap = Math.max(Number(options.maxNodes) || STOCK_MAX_NODES, 2);
    this.nx = Math.max(2, Math.min(cap, Math.ceil((xRange[1] - xRange[0]) / cell)));
    this.ny = Math.max(2, Math.min(cap, Math.ceil((yRange[1] - yRange[0]) / cell)));
    this.dx = (xRange[1] - xRange[0]) / this.nx;
    this.dy = (yRange[1] - yRange[0]) / this.ny;
    this.x0 = xRange[0];
    this.y0 = yRange[0];
    this.nx1 = this.nx + 1;
    this.ny1 = this.ny + 1;
    this.count = this.nx1 * this.ny1;
    this.cylindrical = String(region.id || "") === "circle";
    this.heights = new Float32Array(this.count);
    this.active = new Uint8Array(this.count);
    this.points = (options.timeline && options.timeline.positions) || [];
    this.kinds = options.kinds || decodeKinds(options.timeline);
    this.applied = -1;
    this.dirty = true;
    this._markActive();
    this.reset();
    this.object3d = this._buildObject(options.colour == null ? 0xb07cf0 : options.colour);
  }

  get nodeCount() {
    return this.count;
  }

  /** 毛坯投影之内才算有料：圆形区域是个圆柱，方形/斜坡是整块。 */
  _markActive() {
    const radius = this.size.diameter / 2;
    const cx = this.size.centreX;
    const cy = this.size.centreY;
    for (let j = 0; j < this.ny1; j += 1) {
      for (let i = 0; i < this.nx1; i += 1) {
        const node = j * this.nx1 + i;
        if (!this.cylindrical) {
          this.active[node] = 1;
          continue;
        }
        const x = this.x0 + i * this.dx - cx;
        const y = this.y0 + j * this.dy - cy;
        this.active[node] = Math.hypot(x, y) <= radius + EPS ? 1 : 0;
      }
    }
  }

  /** 回到未切削的毛坯。 */
  reset() {
    this.heights.fill(this.size.bottom);
    for (let node = 0; node < this.count; node += 1) {
      if (this.active[node]) this.heights[node] = this.size.top;
    }
    this.applied = -1;
    this.dirty = true;
    return this;
  }

  /** 把一次直线走刀扫过的料削掉：沿线段密集取刀位，每个刀位压一遍刀盘。 */
  applySegment(a, b) {
    if (!a || !b) return this;
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const dz = b[2] - a[2];
    const length = Math.hypot(dx, dy);
    const step = Math.max(0.35 * Math.min(this.dx, this.dy), 1e-3);
    const steps = Math.max(1, Math.ceil(length / step));
    for (let s = 0; s <= steps; s += 1) {
      const t = s / steps;
      this.cutAt(a[0] + dx * t, a[1] + dy * t, a[2] + dz * t);
    }
    return this;
  }

  /** 刀尖在 (x, y, tipZ) 时，把刀盘范围内的节点压到刀面高度。 */
  cutAt(x, y, tipZ) {
    const radius = Math.max(Number(this.tool.diameter_mm) || 0, 0) / 2;
    if (radius <= EPS) return this;
    const i0 = Math.max(0, Math.floor((x - radius - this.x0) / this.dx));
    const i1 = Math.min(this.nx, Math.ceil((x + radius - this.x0) / this.dx));
    const j0 = Math.max(0, Math.floor((y - radius - this.y0) / this.dy));
    const j1 = Math.min(this.ny, Math.ceil((y + radius - this.y0) / this.dy));
    for (let j = j0; j <= j1; j += 1) {
      const ny = this.y0 + j * this.dy;
      for (let i = i0; i <= i1; i += 1) {
        const node = j * this.nx1 + i;
        if (!this.active[node]) continue;
        const nx = this.x0 + i * this.dx;
        const distance = Math.hypot(nx - x, ny - y);
        if (distance > radius) continue;
        const face = toolFaceHeight(this.tool, distance, tipZ);
        if (face < this.heights[node] - 1e-9) {
          this.heights[node] = face;
          this.dirty = true;
        }
      }
    }
    return this;
  }

  kindAt(index) {
    if (!this.kinds.length) return "cut";
    const clamped = Math.min(Math.max(index, 0), this.kinds.length - 1);
    return this.kinds[clamped] || "cut";
  }

  _cutsBetween(first, second) {
    // 只有整段都在快移里才不切：进出刀与连接本来就会碰到料，切了才与实机一致。
    return !(this.kindAt(first) === "rapid" && this.kindAt(second) === "rapid");
  }

  /** 推进到时间轴的某个采样下标（position 是插值出来的当前位置，用它补上"半段"的切削）。 */
  syncTo(index, position) {
    if (!this.points.length) return this;
    const target = Math.min(Math.max(Number(index) || 0, 0), this.points.length - 1);
    if (target < this.applied) this.reset();
    for (let i = this.applied + 1; i <= target; i += 1) {
      if (this._cutsBetween(i - 1, i)) this.applySegment(this.points[i - 1], this.points[i]);
    }
    if (target > this.applied) this.applied = target;
    if (position && this.applied >= 0 && this._cutsBetween(target, target + 1)) {
      this.applySegment(this.points[target], position);
    }
    return this;
  }

  // -- 网格 ---------------------------------------------------------------
  _buildObject(colour) {
    const positions = [];
    const indices = [];
    const top = this.size.top;
    const bottom = this.size.bottom;
    this.nodeVertex = new Int32Array(this.count).fill(-1);

    // 顶面：每个活跃节点一个顶点，四角都活跃的格子拆成两个三角形。
    for (let j = 0; j < this.ny1; j += 1) {
      for (let i = 0; i < this.nx1; i += 1) {
        const node = j * this.nx1 + i;
        if (!this.active[node]) continue;
        this.nodeVertex[node] = positions.length / 3;
        positions.push(this.x0 + i * this.dx, this.y0 + j * this.dy, top);
      }
    }
    for (let j = 0; j < this.ny; j += 1) {
      for (let i = 0; i < this.nx; i += 1) {
        const a = j * this.nx1 + i;
        const b = a + 1;
        const c = a + this.nx1;
        const d = c + 1;
        if (!(this.active[a] && this.active[b] && this.active[c] && this.active[d])) continue;
        indices.push(this.nodeVertex[a], this.nodeVertex[c], this.nodeVertex[b],
                     this.nodeVertex[b], this.nodeVertex[c], this.nodeVertex[d]);
      }
    }

    // 侧壁：顶边跟着高度场走，底边固定在地面；顶边顶点要么直接对应某个节点（方形），
    // 要么按圆周采样再双线性取值（圆形）。
    this.wallTops = [];
    if (this.cylindrical) {
      const radius = this.size.diameter / 2;
      const ring = [];
      for (let s = 0; s <= STOCK_WALL_SEGMENTS; s += 1) {
        const angle = (s / STOCK_WALL_SEGMENTS) * Math.PI * 2;
        const x = this.size.centreX + radius * Math.cos(angle);
        const y = this.size.centreY + radius * Math.sin(angle);
        const topVertex = positions.length / 3;
        positions.push(x, y, top);
        const bottomVertex = positions.length / 3;
        positions.push(x, y, bottom);
        ring.push({ topVertex, bottomVertex, x, y });
      }
      for (let s = 0; s < STOCK_WALL_SEGMENTS; s += 1) {
        const a = ring[s];
        const b = ring[s + 1];
        indices.push(a.topVertex, a.bottomVertex, b.topVertex,
                     b.topVertex, a.bottomVertex, b.bottomVertex);
      }
      for (const item of ring.slice(0, -1)) {
        this.wallTops.push({ vertex: item.topVertex, x: item.x, y: item.y });
      }
      const centre = positions.length / 3;
      positions.push(this.size.centreX, this.size.centreY, bottom);
      for (let s = 0; s < STOCK_WALL_SEGMENTS; s += 1) {
        indices.push(centre, ring[s + 1].bottomVertex, ring[s].bottomVertex);
      }
    } else {
      const loop = [];
      for (let i = 0; i < this.nx1; i += 1) loop.push(i);                                  // y = y0
      for (let j = 1; j < this.ny1; j += 1) loop.push(j * this.nx1 + this.nx);             // x = x1
      for (let i = this.nx - 1; i >= 0; i -= 1) loop.push(this.ny * this.nx1 + i);         // y = y1
      for (let j = this.ny - 1; j >= 1; j -= 1) loop.push(j * this.nx1);                   // x = x0
      for (let s = 0; s < loop.length; s += 1) {
        const nodeA = loop[s];
        const nodeB = loop[(s + 1) % loop.length];
        const ax = this.x0 + (nodeA % this.nx1) * this.dx;
        const ay = this.y0 + Math.floor(nodeA / this.nx1) * this.dy;
        const bx = this.x0 + (nodeB % this.nx1) * this.dx;
        const by = this.y0 + Math.floor(nodeB / this.nx1) * this.dy;
        const aTop = positions.length / 3;
        positions.push(ax, ay, top);
        const aBottom = positions.length / 3;
        positions.push(ax, ay, bottom);
        const bTop = positions.length / 3;
        positions.push(bx, by, top);
        const bBottom = positions.length / 3;
        positions.push(bx, by, bottom);
        indices.push(aTop, aBottom, bTop, bTop, aBottom, bBottom);
        this.wallTops.push({ vertex: aTop, node: nodeA });
      }
      const corners = [[this.x0, this.y0], [this.x0 + this.nx * this.dx, this.y0],
                       [this.x0 + this.nx * this.dx, this.y0 + this.ny * this.dy],
                       [this.x0, this.y0 + this.ny * this.dy]];
      const base = positions.length / 3;
      for (const [x, y] of corners) positions.push(x, y, bottom);
      indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices);
    geometry.computeBoundingSphere();
    this.geometry = geometry;
    const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
      color: colour,
      metalness: 0.08,
      roughness: 0.85,
      transparent: true,
      opacity: 0.62,
      side: THREE.DoubleSide,
      flatShading: true,
    }));
    mesh.name = "stock";
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    return mesh;
  }

  /** 刀位不在节点上时按双线性取当前高度（圆形毛坯的侧壁顶点用得上）。 */
  heightAt(x, y) {
    const fx = Math.min(Math.max((x - this.x0) / this.dx, 0), this.nx);
    const fy = Math.min(Math.max((y - this.y0) / this.dy, 0), this.ny);
    const i = Math.min(Math.floor(fx), this.nx - 1);
    const j = Math.min(Math.floor(fy), this.ny - 1);
    const tx = fx - i;
    const ty = fy - j;
    const h00 = this.heights[j * this.nx1 + i];
    const h10 = this.heights[j * this.nx1 + i + 1];
    const h01 = this.heights[(j + 1) * this.nx1 + i];
    const h11 = this.heights[(j + 1) * this.nx1 + i + 1];
    return (h00 * (1 - tx) + h10 * tx) * (1 - ty) + (h01 * (1 - tx) + h11 * tx) * ty;
  }

  /** 把高度写进顶点；只有真的削到料时才调用（切削是单调的，脏标记够用）。 */
  updateGeometry() {
    const array = this.geometry.attributes.position.array;
    for (let node = 0; node < this.count; node += 1) {
      const vertex = this.nodeVertex[node];
      if (vertex < 0) continue;
      array[vertex * 3 + 2] = this.heights[node];
    }
    for (const item of this.wallTops) {
      array[item.vertex * 3 + 2] = item.node != null
        ? this.heights[item.node]
        : this.heightAt(item.x, item.y);
    }
    this.geometry.attributes.position.needsUpdate = true;
    this.dirty = false;
    return this;
  }

  /** 顶面高度的极值，用于自检与"最后是不是区域形状"的判断。 */
  heightRange() {
    let low = Infinity;
    let high = -Infinity;
    for (let node = 0; node < this.count; node += 1) {
      if (!this.active[node]) continue;
      if (this.heights[node] < low) low = this.heights[node];
      if (this.heights[node] > high) high = this.heights[node];
    }
    return { low, high };
  }
}
