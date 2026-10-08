import test from "node:test";
import assert from "node:assert/strict";
import { ShapeUtils, Vector2 } from "../toolpath_lab/web/vendor/three.core.js";
import { StockSimulation } from "../toolpath_lab/web/js/stock.js";
import { StockMesh } from "../toolpath_lab/web/js/stock_mesh.js";

const triangulate = boundary => ShapeUtils.triangulateShape(boundary.map(p => new Vector2(...p)), []);
const circle = Array.from({ length: 180 }, (_, i) => [40 * Math.cos(i * Math.PI / 90), 40 * Math.sin(i * Math.PI / 90)]);
const square = [[-40, -40], [40, -40], [40, 40], [-40, 40]];
function fixture(boundary, grid = [41, 41]) {
  const spec = { bounds_mm: [[-40, 40], [-40, 40]], boundary, grid_shape: grid,
    initial_top_z_mm: 1, bottom_z_mm: -4, resolution_mm: 2, tool_radius_mm: 3 };
  const timeline = { times: [0, 1, 2], positions: [[-39, 0, 1], [-39, 0, 0], [39, 0, 0]],
    kind_runs: [[0, 2], [1, 0]] };
  const stock = new StockSimulation(spec, timeline, { radius_mm: 3 });
  return { stock, mesh: new StockMesh(stock, triangulate) };
}
function signedArea(points) {
  return Math.abs(points.reduce((sum, p, i) => {
    const q = points[(i + 1) % points.length];
    return sum + p[0] * q[1] - p[1] * q[0];
  }, 0)) / 2;
}
function distanceToBoundary(p, boundary) {
  return Math.min(...boundary.map((a, i) => {
    const b = boundary[(i + 1) % boundary.length];
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)));
    return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
  }));
}
function checkMesh(mesh, boundary) {
  let area = 0;
  for (let i = 0; i < mesh.topIndices.length; i += 3) {
    const [a, b, c] = mesh.topIndices.slice(i, i + 3).map(j => mesh.xy[j]);
    const cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    assert.ok(cross > 0, "top triangle must have positive area and upward winding");
    area += cross / 2;
  }
  assert.ok(Math.abs(area - signedArea(boundary)) < 1e-5, "top must cover the complete region, not an inset staircase");
  for (const edge of mesh.edges) {
    for (const id of edge) assert.ok(distanceToBoundary(mesh.xy[id], boundary) < 1e-6, "no internal cracks or extra walls");
  }
  const uses = new Map();
  const key = point => [...point].map(v => Math.round(v * 1e6)).join(",");
  for (const [positions, indices] of [[mesh.topPositions, mesh.topIndices], [mesh.shellPositions, mesh.shellIndices]]) {
    for (let i = 0; i < indices.length; i += 3) {
      const face = indices.slice(i, i + 3).map(j => key(positions.subarray(j * 3, j * 3 + 3)));
      for (let j = 0; j < 3; j += 1) {
        const edge = [face[j], face[(j + 1) % 3]].sort().join(";");
        uses.set(edge, (uses.get(edge) || 0) + 1);
      }
    }
  }
  assert.ok([...uses.values()].every(n => n === 2), "each solid edge must have exactly two faces");
}

for (const [name, boundary] of [
  ["circle", circle], ["square", square], ["clockwise square", [...square].reverse()],
  ["rotated ellipse", circle.map(([x, y]) => [x * .8 - y * .3, x * .3 + y * .6])],
  ["concave region", [[-40, -40], [40, -40], [40, 40], [0, 0], [-40, 40]]],
]) {
  test(`${name}: clipped top, walls and bottom form a closed stock without hanging triangles`, () => {
    const { stock, mesh } = fixture(boundary);
    for (let i = 2; i < mesh.topPositions.length; i += 3) assert.equal(mesh.topPositions[i], 1);
    for (const p of boundary) assert.ok(Math.abs(stock.boundaryHeight(...p) - 1) < 1e-8);
    checkMesh(mesh, boundary);
    assert.ok(mesh.edges.length > boundary.length, "long walls must be subdivided with the top grid");
  });
}

test("circle rim interpolation ignores exterior bottom-height samples", () => {
  const { stock } = fixture(circle);
  for (const p of circle) assert.ok(Math.abs(stock.heightAt(...p) - 1) < 1e-8);
  assert.equal(stock.heightAt(40.1, 0), null);
  for (const p of square) {
    const { stock: block } = fixture(square);
    assert.equal(block.heightAt(...p), 1);
  }
});

test("cutting and rewind keep the same connected geometry and cached interpolation", () => {
  const { stock, mesh } = fixture(circle);
  const faces = mesh.topIndices;
  const samples = mesh.samples;
  stock.updateAt(2);
  mesh.update();
  assert.ok([...mesh.topPositions].some((v, i) => i % 3 === 2 && v === 0));
  mesh.edges.forEach(([a, b], i) => {
    const offset = (mesh.xy.length + i * 4) * 3;
    assert.deepEqual([...mesh.shellPositions.subarray(offset, offset + 3)], [...mesh.topPositions.subarray(a * 3, a * 3 + 3)]);
    assert.deepEqual([...mesh.shellPositions.subarray(offset + 6, offset + 9)], [...mesh.topPositions.subarray(b * 3, b * 3 + 3)]);
  });
  checkMesh(mesh, circle);
  stock.updateAt(0);
  mesh.update();
  for (let i = 2; i < mesh.topPositions.length; i += 3) assert.equal(mesh.topPositions[i], 1);
  assert.equal(mesh.topIndices, faces);
  assert.equal(mesh.samples, samples);
});

test("uneven remaining boundary heights move the side wall with the clipped top", () => {
  const { stock, mesh } = fixture(circle);
  for (let row = 0; row < stock.ny; row += 1) {
    for (let col = 0; col < stock.nx; col += 1) {
      if (stock.active[row * stock.nx + col]) stock.heights[row * stock.nx + col] =
        -.5 + .4 * Math.sin(stock.xs[col] / 8) * Math.cos(stock.ys[row] / 10);
    }
  }
  mesh.update();
  checkMesh(mesh, circle);
  const rim = mesh.edges.map(([a]) => mesh.topPositions[a * 3 + 2]);
  assert.ok(Math.max(...rim) - Math.min(...rim) > .1);
  assert.ok(Math.min(...rim) > stock.spec.bottom_z_mm + 1);
});
