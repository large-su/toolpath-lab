// End-to-end planning -> actual browser stock/collision algorithm, requires a local service.
import assert from "node:assert/strict";
import { StockSimulation } from "../toolpath_lab/web/js/stock.js";
import { detectShankCollision } from "../toolpath_lab/web/js/collision.js";
const url = process.env.TOOLPATH_TEST_URL || "http://127.0.0.1:8771/";
async function plan(enabled, planner = "raster", extra = {}) {
  const response = await fetch(new URL("api/plan", url), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tool: { kind: "ball", diameter_mm: 6, length_mm: 30 },
      region: { shape: "square", parameters: { side_mm: 80 } },
      surface: { type: "freeform", parameters: { amplitude_mm: 4, wavelength_x_mm: 80, wavelength_y_mm: 60 } },
      planner: { id: planner }, roughing: { enabled, depth_mm: 2, allowance_mm: 0.5 }, ...extra,
    }),
  });
  const payload = await response.json();
  assert.ok(response.ok && payload.ok, JSON.stringify(payload));
  return payload;
}
function check(payload) {
  const stock = new StockSimulation(payload.stock, payload.timeline, payload.tool);
  const inspector = (point, axis) => detectShankCollision(stock, point, axis, payload.tool);
  const result = stock.updateAt(payload.timeline.duration_s, inspector);
  return { ...result, stats: stock.stats() };
}
const direct = await plan(false);
const directCheck = check(direct);
assert.ok(directCheck.first, "the reported D6 / amplitude 4 direct-finishing interference must reproduce");
const layered = await plan(true);
const layeredCheck = check(layered);
assert.equal(layeredCheck.first, null, JSON.stringify(layeredCheck.first));
assert.equal(layered.toolpath.metadata.roughing.layer_count, 5);
assert.ok(layeredCheck.stats.removed_percent > 0);
assert.ok(layered.timeline.duration_s > direct.timeline.duration_s);
const meta = layered.toolpath.metadata.roughing;
const finish = layered.toolpath.moves.slice(meta.finish_start_move_index);
for (let i = 0; i < finish.length; i++) assert.deepEqual(finish[i].points, direct.toolpath.moves[i].points);
console.log("Original raster:", JSON.stringify(directCheck.first));
console.log("Layered raster:", JSON.stringify({ layers: meta.layer_count, first_collision: layeredCheck.first,
  samples: layered.timeline.sample_count, duration_s: layered.timeline.duration_s, ...layeredCheck.stats }));

// Keep evaluating oriented finish too; a difficult tilt is not falsely certified as safe.
for (const strategy of ["five_axis", "adaptive_scallop", "five_axis_adaptive"]) {
  const payload = await plan(true, strategy);
  const result = check(payload);
  console.log(`Layered ${strategy}:`, JSON.stringify({ first_collision: result.first, remaining: result.stats.remaining_volume_mm3 }));
  assert.ok(payload.toolpath.metadata.roughing.layer_count > 0);
  if (strategy.startsWith("five_axis")) assert.ok(payload.timeline.tool_axes.some(a => Math.hypot(a[0], a[1]) > 0.1));
  if (strategy === "five_axis_adaptive") {
    assert.ok(payload.toolpath.metadata.adaptive && payload.toolpath.metadata.orientation_smoothing);
    assert.equal(result.first, null, JSON.stringify(result.first));
  }
}
console.log("PASS: direct interference reproduced; layered raster clears it without altering the finish.");
