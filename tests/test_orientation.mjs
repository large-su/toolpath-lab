import test from "node:test";
import assert from "node:assert/strict";
import { slerpAxis } from "../toolpath_lab/web/js/orientation.js";
import { Playback } from "../toolpath_lab/web/js/playback.js";
import { poseSamples } from "../toolpath_lab/web/js/collision.js";
import { StockSimulation } from "../toolpath_lab/web/js/stock.js";

const angle = (a, b) => Math.acos(Math.max(-1, Math.min(1, a.reduce((s, x, i) => s + x * b[i], 0)))) * 180 / Math.PI;
test("axis slerp preserves direction and advances with constant angular speed", () => {
  for (const t of [0, .1, .25, .75, 1]) {
    const result = slerpAxis([0, 0, 1], [1, 0, 0], t);
    assert.ok(Math.abs(Math.hypot(...result) - 1) < 1e-12);
    assert.ok(Math.abs(angle([0, 0, 1], result) - 90 * t) < 1e-5);
  }
  assert.ok(Math.abs(angle([0, 0, 1], slerpAxis([0, 0, 1], [0, 0, -1], .5)) - 90) < 1e-9);
});
test("playback includes stationary rotation with the same angular clock", () => {
  const playback = new Playback();
  playback.load({ times: [0, 3], positions: [[0, 0, 10], [0, 0, 10]],
    tool_axes: [[0, 0, 1], [1, 0, 0]], duration_s: 3, move_runs: [[0, 0]] });
  playback.seekProgress(.25);
  assert.deepEqual(playback.state().position, [0, 0, 10]);
  assert.ok(Math.abs(angle([0, 0, 1], playback.state().toolAxis) - 22.5) < 1e-9);
});
test("collision sweep shares the same spherical trajectory as playback", () => {
  const poses = poseSamples([0, 0, 0], [0, 0, 0], [0, 0, 1], [1, 0, 0],
    { diameter_mm: 6, length_mm: 30 }, 2);
  assert.ok(poses.length > 2);
  for (const pose of poses) assert.ok(Math.abs(angle([0, 0, 1], pose.axis) - pose.ratio * 90) < 1e-5);
});
test("partial material simulation and playback use the same interpolated pose", () => {
  const timeline = { times: [0, 3], positions: [[0, 0, 10], [0, 0, 10]],
    tool_axes: [[0, 0, 1], [1, 0, 0]], duration_s: 3, kind_runs: [[0, 2]] };
  const spec = { boundary: [[-5, -5], [5, -5], [5, 5], [-5, 5]], resolution_mm: 2,
    bounds_mm: [[-5, 5], [-5, 5]], grid_shape: [6, 6],
    bottom_z_mm: -5, initial_top_z_mm: 0, tool_radius_mm: 3 };
  const stock = new StockSimulation(spec, timeline, { diameter_mm: 6, radius_mm: 3, length_mm: 30 });
  stock.updateAt(.75);
  assert.ok(Math.abs(angle([0, 0, 1], stock.partialPose.axis) - 22.5) < 1e-9);
});
