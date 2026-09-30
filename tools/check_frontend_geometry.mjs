// 前端几何自检：只查"Python 测试覆盖不到、又要肉眼才看得出"的几条不变量。
//
//   node tools/check_frontend_geometry.mjs
//
// 覆盖三类曾经悄悄出过错的东西：
//   1. 刀路抬升必须**保留真实 Z**（曾把 Z 换成固定抬升量，斜面上刀路整个横在基准平面）；
//   2. 三种刀具的**刀尖都落在 Z = 0**、最高点到刀具长度；
//   3. 斜面工件的三角化**法向朝外**（体积/朝向错了，视图里会看到黑面）。
//
// 脚本自己保证 node_modules/three 指向仓库自带的 vendor（浏览器用 importmap，Node 用这个转发文件），
// 所以不需要 npm install，也不联网。

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const vendor = path.join(root, "toolpath_lab", "web", "vendor", "three.module.js");
const shimDir = path.join(root, "node_modules", "three");
if (!fs.existsSync(vendor)) {
  console.error(`找不到自带的 three：${vendor}`);
  process.exit(2);
}
fs.mkdirSync(shimDir, { recursive: true });
fs.writeFileSync(
  path.join(shimDir, "package.json"),
  JSON.stringify({ name: "three", version: "0.0.0-local-shim", type: "module",
    main: "index.js", exports: { ".": "./index.js" } }, null, 2)
);
fs.writeFileSync(
  path.join(shimDir, "index.js"),
  `export * from ${JSON.stringify(pathToFileURL(vendor).href)};\n`
);

const viewport = await import(
  pathToFileURL(path.join(root, "toolpath_lab", "web", "js", "viewport.js")).href
);
const THREE = await import("three");

let failed = 0;
const check = (label, ok, detail = "") => {
  if (!ok) failed += 1;
  console.log(`  ${ok ? "OK  " : "FAIL"} ${label}${detail ? `（${detail}）` : ""}`);
};

// 1. 刀路抬升保留真实 Z -------------------------------------------------
console.log("刀路抬升：");
const lifted = viewport.liftPaths([[[0, 0, 0], [10, 0, 37], [20, 20, 80]]])[0];
check("Z 保留并只加抬升量", Math.abs(lifted[0][2] - 0.05) < 1e-9
  && Math.abs(lifted[1][2] - 37.05) < 1e-9 && Math.abs(lifted[2][2] - 80.05) < 1e-9,
  `${lifted.map((point) => point[2].toFixed(2)).join(", ")}`);
check("XY 不动", lifted[1][0] === 10 && lifted[2][1] === 20);

// 2. 三种刀具的刀尖都在 Z = 0 -------------------------------------------
console.log("刀具实体：");
for (const [kind, corner] of [["flat", 0], ["ball", 0], ["bull", 1.5]]) {
  const parts = viewport.buildToolParts({
    kind, radius_mm: 5, length_mm: 40, corner_radius_mm: corner,
  });
  const box = new THREE.Box3();
  const vertex = new THREE.Vector3();
  for (const mesh of parts) {
    const position = mesh.geometry.attributes.position;
    mesh.updateMatrixWorld(true);
    for (let index = 0; index < position.count; index += 1) {
      box.expandByPoint(vertex.fromBufferAttribute(position, index).applyMatrix4(mesh.matrixWorld));
    }
  }
  check(`${kind} 刀尖贴 Z = 0`, Math.abs(box.min.z) < 1e-3, `最低 ${box.min.z.toFixed(4)}`);
  check(`${kind} 顶端到刀具长度`, Math.abs(box.max.z - 40) < 1e-3, `最高 ${box.max.z.toFixed(4)}`);
}

// 3. 斜面工件法向朝外 ---------------------------------------------------
console.log("斜面工件：");
const region = {
  boundary: [[-40, -40, 0], [40, -40, 0], [40, 40, 0], [-40, 40, 0]],
  top_patches: [
    [[-40, -40, 80], [-6, -40, 80], [-6, 40, 80], [-40, 40, 80]],
    [[-6, -40, 80], [40, -40, 0], [40, 40, 0], [-6, 40, 80]],
  ],
};
const geometry = viewport.buildSurfaceGeometry(region, 7.2);
const position = geometry.attributes.position;
const centre = new THREE.Box3().setFromBufferAttribute(position).getCenter(new THREE.Vector3());
const a = new THREE.Vector3();
const b = new THREE.Vector3();
const c = new THREE.Vector3();
let outward = 0;
let inward = 0;
for (let index = 0; index < position.count; index += 3) {
  a.fromBufferAttribute(position, index);
  b.fromBufferAttribute(position, index + 1);
  c.fromBufferAttribute(position, index + 2);
  const normal = b.clone().sub(a).cross(c.clone().sub(a));
  if (normal.length() < 1e-9) continue;
  const away = a.clone().add(b).add(c).multiplyScalar(1 / 3).sub(centre);
  if (normal.dot(away) > 0) outward += 1;
  else inward += 1;
}
check("三角形法向全部朝外", inward === 0, `朝外 ${outward} / 朝内 ${inward}`);
const rampBox = new THREE.Box3().setFromBufferAttribute(position);
check("包围盒覆盖平顶与低边",
  Math.abs(rampBox.min.z + 7.2) < 1e-6 && Math.abs(rampBox.max.z - 80) < 1e-6);

// 4. 平面区域的高度：实体顶面落在加工面高度上，地面网格不穿过工件 ----------
console.log("加工面高度：");
const boxRegion = (height) => ({
  id: "square",
  bounds_mm: [[-40, 40], [-40, 40]],
  surface: { kind: "flat", base_z_mm: height, top_z_mm: height },
});
const raised = viewport.Viewport.prototype._workpiece(boxRegion(20), 7.2, 20);
raised.updateMatrixWorld(true);
const raisedBox = new THREE.Box3().setFromObject(raised);
check("高度 20：实体顶面在 Z = 20", Math.abs(raisedBox.max.z - 20) < 1e-6,
  `顶面 ${raisedBox.max.z.toFixed(3)}`);
check("基体挂在顶面之下 7.2", Math.abs(raisedBox.min.z - 12.8) < 1e-6,
  `底面 ${raisedBox.min.z.toFixed(3)}`);

const gridHost = { gridGroup: new THREE.Group(), _clear() {} };
viewport.Viewport.prototype._rebuildGrid.call(gridHost, 80, 7.2, 20);
check("抬高的工件悬在地面网格之上", gridHost.gridGroup.children[0].position.z < 12.8,
  `网格 Z ${gridHost.gridGroup.children[0].position.z.toFixed(2)}`);
const flatHost = { gridGroup: new THREE.Group(), _clear() {} };
viewport.Viewport.prototype._rebuildGrid.call(flatHost, 80, 7.2, 0);
check("默认高度时网格仍贴着工件底面（不穿工件）",
  Math.abs(flatHost.gridGroup.children[0].position.z + 7.3) < 1e-6,
  `网格 Z ${flatHost.gridGroup.children[0].position.z.toFixed(2)}`);

// 5. 部件厚度：只把基体加厚/减薄，加工面不动 ---------------------------
console.log("部件厚度：");
const deep = viewport.Viewport.prototype._workpiece(boxRegion(20), 30, 20);
deep.updateMatrixWorld(true);
const deepBox = new THREE.Box3().setFromObject(deep);
check("厚度 30：基体挂在顶面之下 30", Math.abs(deepBox.min.z + 10) < 1e-6,
  `底面 ${deepBox.min.z.toFixed(3)}`);
check("厚度不改变加工面高度", Math.abs(deepBox.max.z - 20) < 1e-6,
  `顶面 ${deepBox.max.z.toFixed(3)}`);

// 6. 毛坯：竖直面贴紧区域（不留余量）、顶面按设置的余量抬高，形状随区域 ----------
console.log("毛坯：");
const blankOf = (region, margin) => {
  const group = viewport.buildBlankMesh(region, margin);
  group.updateMatrixWorld(true);
  return { group, box: new THREE.Box3().setFromObject(group), mesh: group.children[0] };
};
const blankRegion = (id, topZ = 0) => ({
  id,
  bounds_mm: [[-40, 40], [-40, 40]],
  thickness_mm: 20,
  surface: { kind: id === "ramp" ? "ramp" : "flat", base_z_mm: 0, top_z_mm: topZ },
});
const partBox = new THREE.Box3(new THREE.Vector3(-40, -40, -20), new THREE.Vector3(40, 40, 0));

const squareBlank = blankOf(blankRegion("square"));
check("方形毛坯是长方体", squareBlank.mesh.geometry.type === "BoxGeometry",
  squareBlank.mesh.geometry.type);
check("竖直面贴紧区域（XY 不留余量）",
  Math.abs(squareBlank.box.min.x + 40) < 1e-6 && Math.abs(squareBlank.box.max.x - 40) < 1e-6
  && Math.abs(squareBlank.box.min.y + 40) < 1e-6,
  `X ${squareBlank.box.min.x.toFixed(1)}…${squareBlank.box.max.x.toFixed(1)}`);
check("底面与工件底面齐平", Math.abs(squareBlank.box.min.z + 20) < 1e-6,
  `底 ${squareBlank.box.min.z.toFixed(2)}`);
check("顶面默认留 2 mm", Math.abs(squareBlank.box.max.z - 2) < 1e-6,
  `顶 ${squareBlank.box.max.z.toFixed(2)}`);
check("毛坯把工件整个包住", squareBlank.box.containsBox(partBox));

const tallerBlank = blankOf(blankRegion("square"), 5);
check("顶部余量可设（5 mm → 顶面 5）",
  Math.abs(tallerBlank.box.max.z - 5) < 1e-6
  && Math.abs(tallerBlank.box.max.x - 40) < 1e-6,
  `顶 ${tallerBlank.box.max.z.toFixed(2)}，X 仍 ${tallerBlank.box.max.x.toFixed(1)}`);
const flushBlank = blankOf(blankRegion("square"), 0);
check("余量 0 时毛坯与工件严丝合缝", flushBlank.box.min.equals(partBox.min)
  && flushBlank.box.max.equals(partBox.max));
const clampedBlank = blankOf(blankRegion("square"), -5);
check("负余量按 0 处理", Math.abs(clampedBlank.box.max.z) < 1e-6,
  `顶 ${clampedBlank.box.max.z.toFixed(2)}`);

const circleBlank = blankOf(blankRegion("circle"));
check("圆形毛坯是竖直圆柱", circleBlank.mesh.geometry.type === "CylinderGeometry",
  circleBlank.mesh.geometry.type);
check("圆柱毛坯竖直摆放（高度沿 Z）",
  Math.abs((circleBlank.box.max.z - circleBlank.box.min.z) - 22) < 1e-6,
  `高 ${(circleBlank.box.max.z - circleBlank.box.min.z).toFixed(2)}`);
check("圆柱毛坯直径贴紧区域（80）",
  Math.abs((circleBlank.box.max.x - circleBlank.box.min.x) - 80) < 1e-6,
  `直径 ${(circleBlank.box.max.x - circleBlank.box.min.x).toFixed(1)}`);

const rampBlank = blankOf(blankRegion("ramp", 80), 3);
check("斜坡毛坯顶面 = Z 上限 + 余量", Math.abs(rampBlank.box.max.z - 83) < 1e-6,
  `顶 ${rampBlank.box.max.z.toFixed(2)}`);
check("斜坡毛坯也贴紧区域", Math.abs(rampBlank.box.max.x - 40) < 1e-6);

// ---------------------------------------------------------------- 切削仿真（stock.js）
const stock = await import(
  pathToFileURL(path.join(root, "toolpath_lab", "web", "js", "stock.js")).href
);

const flatTool = { kind: "flat", diameter_mm: 6, corner_radius_mm: 0 };
const ballTool = { kind: "ball", diameter_mm: 6, corner_radius_mm: 3 };
const bullTool = { kind: "bull", diameter_mm: 6, corner_radius_mm: 1.5 };
check("平底刀：整个刀盘等高",
  [0, 1.5, 3].every((r) => Math.abs(stock.toolFaceHeight(flatTool, r, 10) - 10) < 1e-9));
check("球头刀：只有刀尖最低、刃口高一个半径",
  Math.abs(stock.toolFaceHeight(ballTool, 0, 10) - 10) < 1e-9
  && Math.abs(stock.toolFaceHeight(ballTool, 3, 10) - 13) < 1e-9,
  `r=3 处 ${stock.toolFaceHeight(ballTool, 3, 10).toFixed(3)}`);
check("圆鼻刀：R−Rc 之内是平面，之外圆角抬到 +Rc",
  Math.abs(stock.toolFaceHeight(bullTool, 1.5, 10) - 10) < 1e-9
  && Math.abs(stock.toolFaceHeight(bullTool, 3, 10) - 11.5) < 1e-9,
  `r=3 处 ${stock.toolFaceHeight(bullTool, 3, 10).toFixed(3)}`);

const simRegion = {
  id: "square",
  bounds_mm: [[-40, 40], [-40, 40]],
  thickness_mm: 20,
  surface: { kind: "flat", base_z_mm: 0, top_z_mm: 0 },
};
const nodeAt = (sim, x, y) => {
  const i = Math.round((x - sim.x0) / sim.dx);
  const j = Math.round((y - sim.y0) / sim.dy);
  return sim.heights[j * sim.nx1 + i];
};
const simOf = (positions, kinds, tool = flatTool, region = simRegion) =>
  new stock.StockSimulation(region, tool, 2, { timeline: { positions }, kinds });

const onePass = simOf([[-20, 0, 0], [20, 0, 0]], ["cut", "cut"]);
onePass.syncTo(1, [20, 0, 0]);
onePass.updateGeometry();
check("刀盘扫过的地方压到刀尖高度", Math.abs(nodeAt(onePass, 0, 0)) < 1e-6,
  `(0,0) 高 ${nodeAt(onePass, 0, 0).toFixed(3)}`);
check("刀盘外面不动", Math.abs(nodeAt(onePass, 0, 10) - 2) < 1e-6
  && Math.abs(nodeAt(onePass, 35, 0) - 2) < 1e-6,
  `(0,10) 高 ${nodeAt(onePass, 0, 10).toFixed(3)}`);

// 顶点着色：没切到的料保持毛坯色，切到加工面的地方换色，这样"哪里铣过了"一眼可辨。
const colourOf = (sim, x, y) => {
  const i = Math.round((x - sim.x0) / sim.dx);
  const j = Math.round((y - sim.y0) / sim.dy);
  const vertex = sim.nodeVertex[j * sim.nx1 + i];
  const attribute = sim.geometry.attributes.color;
  return [attribute.getX(vertex), attribute.getY(vertex), attribute.getZ(vertex)];
};
const sameColour = (value, colour) => Math.abs(value[0] - colour.r) < 1e-6
  && Math.abs(value[1] - colour.g) < 1e-6 && Math.abs(value[2] - colour.b) < 1e-6;
check("没切到的料保持毛坯色", sameColour(colourOf(onePass, 0, 10), new THREE.Color(0xb07cf0)),
  colourOf(onePass, 0, 10).map((v) => v.toFixed(2)).join(","));
check("切到加工面的地方换色",
  sameColour(colourOf(onePass, 0, 0), new THREE.Color(stock.STOCK_CUT_COLOUR)),
  colourOf(onePass, 0, 0).map((v) => v.toFixed(2)).join(","));

// 覆盖整块区域的往复刀路（切宽 4、D6）：走完以后内部应该完全变成区域形状。
const raster = [];
const kinds = [];
for (let y = -40; y <= 40 + 1e-9; y += 4) {
  const forward = (Math.round((y + 40) / 4) % 2) === 0;
  raster.push([forward ? -37 : 37, y, 0]);
  raster.push([forward ? 37 : -37, y, 0]);
  kinds.push("cut", "cut");
}
const full = simOf(raster, kinds);
full.syncTo(raster.length - 1, raster[raster.length - 1]);
full.updateGeometry();
let interior = 0;
let reached = 0;
let leftover = 0;
let leftoverOutsideBand = 0;
for (let j = 0; j < full.ny1; j += 1) {
  for (let i = 0; i < full.nx1; i += 1) {
    const node = j * full.nx1 + i;
    if (!full.active[node]) continue;
    const x = full.x0 + i * full.dx;
    const y = full.y0 + j * full.dy;
    if (Math.abs(x) <= 33 && Math.abs(y) <= 33) {
      interior += 1;
      if (Math.abs(full.heights[node]) < 1e-6) reached += 1;
    }
    if (full.heights[node] > 1e-6) {
      leftover += 1;
      // 残留只该出现在"刀具够不到"的边界带里：距区域边界不超过一个刀半径加一格。
      if (40 - Math.max(Math.abs(x), Math.abs(y)) > 3 + 1.5 * Math.max(full.dx, full.dy)) {
        leftoverOutsideBand += 1;
      }
    }
  }
}
check("走完整块区域后内部完全到加工面", reached === interior,
  `${reached}/${interior} 个内部节点`);
check("残留只出现在刀具够不到的边界带", leftover > 0 && leftoverOutsideBand === 0,
  `残留 ${leftover} 个节点，越界 ${leftoverOutsideBand} 个`);

const rapidOnly = simOf([[-20, 0, 0], [20, 0, 0]], ["rapid", "rapid"]);
rapidOnly.syncTo(1, [20, 0, 0]);
check("快移不切料", Math.abs(nodeAt(rapidOnly, 0, 0) - 2) < 1e-6,
  `(0,0) 高 ${nodeAt(rapidOnly, 0, 0).toFixed(3)}`);

const rewind = simOf([[-20, 0, 0], [20, 0, 0], [20, 20, 0]], ["cut", "cut", "cut"]);
rewind.syncTo(2, [20, 20, 0]);
const cut = nodeAt(rewind, 0, 0);
rewind.syncTo(0, [-20, 0, 0]);
check("回拖到开头会重放成未切削的毛坯",
  Math.abs(cut - 0) < 1e-6 && Math.abs(nodeAt(rewind, 0, 0) - 2) < 1e-6,
  `切过 ${cut.toFixed(2)} → 回拖后 ${nodeAt(rewind, 0, 0).toFixed(2)}`);

const circleRegion = Object.assign({}, simRegion, { id: "circle" });
const circle = simOf([[-20, 0, 0], [20, 0, 0]], ["cut", "cut"], flatTool, circleRegion);
const activeAt = (sim, x, y) => {
  const i = Math.round((x - sim.x0) / sim.dx);
  const j = Math.round((y - sim.y0) / sim.dy);
  return sim.active[j * sim.nx1 + i];
};
check("圆形毛坯是圆柱：圆内算有料、圆外不算",
  activeAt(circle, 0, 0) === 1 && activeAt(circle, -39, -39) === 0,
  `圆心 ${activeAt(circle, 0, 0)}，角落 ${activeAt(circle, -39, -39)}`);

const meshNodes = full.geometry.attributes.position.count;
check("可切削实体的顶点跟着高度场更新",
  meshNodes > full.count * 0.9 && full.geometry.attributes.position.needsUpdate !== false,
  `${meshNodes} 个顶点`);

console.log(failed === 0 ? "\n全部通过" : `\n有 ${failed} 项不通过`);
process.exit(failed === 0 ? 0 : 1);
