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

// 6. 毛坯：形状随区域（方形/斜坡长方体、圆形竖直圆柱），且一定把工件整个包住 ------
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

const squareBlank = blankOf(blankRegion("square"));
check("方形毛坯是长方体", squareBlank.mesh.geometry.type === "BoxGeometry",
  squareBlank.mesh.geometry.type);
check("四周各宽出 2 mm",
  Math.abs(squareBlank.box.max.x - 42) < 1e-6 && Math.abs(squareBlank.box.min.y + 42) < 1e-6,
  `X ${squareBlank.box.min.x.toFixed(1)}…${squareBlank.box.max.x.toFixed(1)}`);
check("底面与工件底面齐平", Math.abs(squareBlank.box.min.z + 20) < 1e-6,
  `底 ${squareBlank.box.min.z.toFixed(2)}`);
check("顶面高出工件 2 mm", Math.abs(squareBlank.box.max.z - 2) < 1e-6,
  `顶 ${squareBlank.box.max.z.toFixed(2)}`);
check("毛坯把工件整个包住", squareBlank.box.containsBox(
  new THREE.Box3(new THREE.Vector3(-40, -40, -20), new THREE.Vector3(40, 40, 0))
));
check("余量可调（5 mm → 90 宽）",
  Math.abs(blankOf(blankRegion("square"), 5).box.max.x - 45) < 1e-6);

const circleBlank = blankOf(blankRegion("circle"));
check("圆形毛坯是竖直圆柱", circleBlank.mesh.geometry.type === "CylinderGeometry",
  circleBlank.mesh.geometry.type);
check("圆柱毛坯竖直摆放（高度沿 Z）",
  Math.abs((circleBlank.box.max.z - circleBlank.box.min.z) - 22) < 1e-6,
  `高 ${(circleBlank.box.max.z - circleBlank.box.min.z).toFixed(2)}`);
check("圆柱毛坯直径 = 工件直径 + 4",
  Math.abs((circleBlank.box.max.x - circleBlank.box.min.x) - 84) < 1e-6,
  `直径 ${(circleBlank.box.max.x - circleBlank.box.min.x).toFixed(1)}`);

const rampBlank = blankOf(blankRegion("ramp", 80));
check("斜坡毛坯包到 Z 上限之上", Math.abs(rampBlank.box.max.z - 82) < 1e-6,
  `顶 ${rampBlank.box.max.z.toFixed(2)}`);

console.log(failed === 0 ? "\n全部通过" : `\n有 ${failed} 项不通过`);
process.exit(failed === 0 ? 0 : 1);
