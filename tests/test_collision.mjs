import assert from "node:assert/strict";
import test from "node:test";
import { cylinderColumnInterval, detectShankCollision, poseSamples, toolEnvelope } from "../toolpath_lab/web/js/collision.js";
import { StockSimulation } from "../toolpath_lab/web/js/stock.js";

const ball = { kind: "ball", radius_mm: 3, diameter_mm: 6, length_mm: 30 };
const flat = { ...ball, kind: "flat" };
function fixture(positions = [[0, 0, 20], [0, 0, 0]], kinds = [[0, 2], [1, 0]], axes = null, top = 10) {
  const spec = {
    bounds_mm: [[-20, 20], [-20, 20]], boundary: [[-20, -20], [20, -20], [20, 20], [-20, 20]],
    bottom_z_mm: -5, initial_top_z_mm: top, resolution_mm: 1, grid_shape: [41, 41], tool_radius_mm: 3,
  };
  const timeline = { positions, times: positions.map((_, i) => i), kind_runs: kinds,
    tool_axes: axes || positions.map(() => [0, 0, 1]) };
  return new StockSimulation(spec, timeline, ball);
}
function inspect(stock, tool = ball) {
  return (p, axis) => detectShankCollision(stock, p, axis, tool);
}
function close(actual, expected, epsilon = 1e-8) {
  assert.ok(Math.abs(actual - expected) < epsilon, `${actual} != ${expected}`);
}

test("tool envelope shares exact total length and cutting boundary across tool types", () => {
  assert.deepEqual(toolEnvelope(ball), { radius: 3, length: 30, cuttingLength: 6, shankRadius: 3.75 });
  assert.equal(toolEnvelope(flat).cuttingLength, 18);
  assert.equal(toolEnvelope({ ...ball, kind: "bull" }).cuttingLength, 18);
  assert.equal(toolEnvelope({ ...ball, length_mm: 4 }).cuttingLength, 4);
});

test("vertical finite cylinder excludes radial exterior and clips both axial end planes", () => {
  assert.deepEqual(cylinderColumnInterval(0, 0, [0, 0, 2], [0, 0, 1], 6, 30, 3), [8, 32]);
  assert.equal(cylinderColumnInterval(3.1, 0, [0, 0, 0], [0, 0, 1], 6, 30, 3), null);
  assert.deepEqual(cylinderColumnInterval(0, 0, [0, 0, 2], [0, 0, -1], 6, 30, 3), [-28, -4]);
});

test("horizontal cylinder catches side contact but does not protrude past end caps", () => {
  assert.deepEqual(cylinderColumnInterval(10, 0, [0, 0, 5], [1, 0, 0], 6, 30, 3), [2, 8]);
  assert.equal(cylinderColumnInterval(5.9, 0, [0, 0, 5], [1, 0, 0], 6, 30, 3), null);
  assert.equal(cylinderColumnInterval(30.1, 0, [0, 0, 5], [1, 0, 0], 6, 30, 3), null);
});

test("tilted cylinder intersection has correct finite axial bounds", () => {
  const axis = [Math.SQRT1_2, 0, Math.SQRT1_2];
  const interval = cylinderColumnInterval(10, 0, [0, 0, 0], axis, 6, 30, 3);
  close(interval[0], 10 - 3 * Math.SQRT2);
  close(interval[1], 10 + 3 * Math.SQRT2);
});

test("normal cutting flute contact is not counted as shank collision", () => {
  const stock = fixture();
  assert.equal(detectShankCollision(stock, [0, 0, 0], [0, 0, 1], flat), null);
  assert.equal(detectShankCollision(stock, [0, 0, 5], [0, 0, 1], ball), null);
  assert.equal(detectShankCollision(stock, [50, 50, 0], [0, 0, 1], ball), null);
});

test("deep plunge and horizontal shaft detect remaining stock, with unit-axis normalization", () => {
  const stock = fixture();
  const hit = detectShankCollision(stock, [0, 0, 0], [0, 0, 9], ball);
  assert.equal(hit.part, "刀身");
  close(hit.overlap_mm, 4);
  assert.ok(detectShankCollision(stock, [-15, 0, 5], [1, 0, 0], ball));
  assert.equal(detectShankCollision(stock, [0, 0, 40], [0, 0, 1], ball), null);
});

test("cleared stock is not treated as the original solid", () => {
  const stock = fixture();
  assert.ok(detectShankCollision(stock, [0, 0, 0], [0, 0, 1], ball));
  for (let i = 0; i < stock.heights.length; i++) if (stock.active[i]) stock.heights[i] = 0;
  assert.equal(detectShankCollision(stock, [0, 0, 0], [0, 0, 1], ball), null);
});

test("height query uses interpolated remaining stock rather than just nearest grid point", () => {
  const stock = fixture();
  stock.heights[20 * 41 + 20] = 0;
  stock.heights[20 * 41 + 21] = 4;
  stock.heights[21 * 41 + 20] = 8;
  stock.heights[21 * 41 + 21] = 12;
  close(stock.heightAt(0.5, 0.5), 6);
  assert.equal(stock.heightAt(25, 0), null);
});

test("motion sampling covers long translations and rotation-only motions", () => {
  const straight = poseSamples([-40, 0, 0], [40, 0, 0], [0, 0, 1], [0, 0, 1], ball, 1);
  assert.ok(straight.length >= 160);
  assert.deepEqual(straight.at(-1).position, [40, 0, 0]);
  const rotation = poseSamples([0, 0, 0], [0, 0, 0], [0, 0, 1], [1, 0, 0], ball, 1);
  assert.ok(rotation.length > 60);
  assert.ok(rotation.some((p) => p.axis[0] > 0.5 && p.axis[2] > 0.5));
  for (const pose of rotation) close(Math.hypot(...pose.axis), 1);
});

test("rapid crossing detects intermediate collision despite safe endpoints and removes no material", () => {
  const stock = fixture([[-40, 0, 0], [40, 0, 0]], [[0, 2]]);
  const before = stock.stats().remaining_volume_mm3;
  assert.equal(inspect(stock)(stock.timeline.positions[0], [0, 0, 1]), null);
  assert.equal(inspect(stock)(stock.timeline.positions[1], [0, 0, 1]), null);
  const result = stock.updateAt(1, inspect(stock));
  assert.ok(result.first.time_s > 0 && result.first.time_s < 1);
  assert.equal(result.current, null);
  assert.equal(stock.stats().remaining_volume_mm3, before);
});

test("five-axis rotation-only sweep detects shaft intrusion", () => {
  const stock = fixture([[0, 0, 7], [0, 0, 7]], [[0, 2]], [[0, 0, 1], [1, 0, 0]]);
  const result = stock.updateAt(1, inspect(stock));
  assert.ok(result.first);
  assert.ok(result.current);
  assert.ok(result.first.time_s > 0 && result.first.time_s < 1);
});

test("collision inspection runs before material removal in a cutting plunge", () => {
  const stock = fixture();
  const result = stock.updateAt(1, inspect(stock));
  assert.ok(result.first, "deep plunge must not erase its collision before checking");
  assert.ok(result.first.time_s > 0 && result.first.time_s <= 1);
  assert.ok(stock.stats().removed_percent > 0);
});

test("partial interval follows time continuously and rewinding inside the same interval restores stock", () => {
  const stock = fixture([[-8, 0, 0], [8, 0, 0]], [[0, 0]]);
  stock.updateAt(0.8);
  assert.equal(stock.heightAt(4, 0), 0);
  stock.updateAt(0.1);
  assert.equal(stock.heightAt(4, 0), 10);
  assert.equal(stock.heightAt(-7, 0), 0);
  const fresh = fixture([[-8, 0, 0], [8, 0, 0]], [[0, 0]]);
  fresh.updateAt(0.1);
  assert.deepEqual(stock.heights, fresh.heights);
});

test("normal planar cutting remains collision-free when inspected", () => {
  const stock = fixture([[-8, 0, 0], [8, 0, 0]], [[0, 0]], null, 1);
  const result = stock.updateAt(1, inspect(stock));
  assert.equal(result.first, null);
  assert.equal(result.current, null);
  assert.ok(stock.stats().removed_percent > 0);
});

test("disabled inspector performs no collision work, and can be enabled while paused", () => {
  const stock = fixture([[0, 0, 0], [0, 0, 0]], [[0, 2]]);
  assert.equal(stock.updateAt(0).first, null);
  assert.ok(stock.updateAt(0, inspect(stock)).current);
  assert.equal(stock.updateAt(0).current, null);
});

test("rewinding resets collision history, and repeated paused queries do not cut again", () => {
  const stock = fixture();
  stock.updateAt(1, inspect(stock));
  const result = stock.updateAt(0, inspect(stock));
  assert.equal(result.first, null);
  assert.equal(stock.stats().removed_percent, 0);
  stock.updateAt(0.5, inspect(stock));
  const before = stock.heights.slice();
  stock.updateAt(0.5, inspect(stock));
  assert.deepEqual(stock.heights, before);
});
