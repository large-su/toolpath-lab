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
check("包围盒覆盖平顶与低边",
  Math.abs(new THREE.Box3().setFromBufferAttribute(position).min.z + 7.2) < 1e-6
  && Math.abs(new THREE.Box3().setFromBufferAttribute(position).max.z - 80) < 1e-6);

console.log(failed === 0 ? "\n全部通过" : `\n有 ${failed} 项不通过`);
process.exit(failed === 0 ? 0 : 1);
