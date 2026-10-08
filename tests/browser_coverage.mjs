import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.TOOLPATH_BROWSER_MODULE || "playwright");
const browser = await chromium.launch({ channel: "msedge", headless: true,
  args: ["--enable-unsafe-swiftshader"] });
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 1050 } });
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(process.env.TOOLPATH_TEST_URL || "http://127.0.0.1:8771/");
  await page.waitForFunction(() => window.toolpathLab?.viewport.stockPayload?.coverage);
  const toggle = page.getByLabel("未覆盖区域（XY估算）", { exact: true });
  assert.equal(await toggle.isChecked(), false);
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.coverageGroup.visible), false);
  await page.evaluate(async () => {
    const { panel, regenerate } = window.toolpathLab;
    panel.state.tool.kind = "flat";
    panel.state.tool.diameter_mm = 6;
    panel.state.region = { id: "square", values: { side_mm: 80 } };
    panel.state.surface = { id: "flat", values: { base_z_mm: 0 } };
    panel.state.planner = { id: "raster", values: { mode: "zigzag", stepover_mm: 12,
      direction_deg: 0, feed_mm_per_min: 600 } };
    panel.state.roughing.enabled = false;
    panel.render();
    await regenerate();
  });
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.request.planner.parameters.stepover_mm === 12);
  await toggle.check();
  await page.getByLabel("快移", { exact: true }).uncheck();
  await page.getByLabel("刀具", { exact: true }).uncheck();
  await page.locator('[data-view="top"]').click();
  const sparse = await page.evaluate(() => {
    const { viewport } = window.toolpathLab;
    const mesh = viewport.coverageGroup.children[0];
    return { coverage: viewport.stockPayload.coverage, visible: viewport.coverageGroup.visible,
      texture: Array.from(mesh.material.map.image.data), meshVertices: mesh.geometry.attributes.position.count,
      stats: document.getElementById("stats").innerText };
  });
  assert.ok(sparse.visible && sparse.meshVertices > 0);
  assert.ok(sparse.coverage.coverage_percent < 65);
  assert.equal(sparse.texture.filter((_, i) => i % 4 === 3 && sparse.texture[i] === 255).length,
    sparse.coverage.uncovered_cell_count);
  assert.match(sparse.stats, /覆盖率（XY）/);
  assert.match(sparse.stats, /未覆盖面积/);
  assert.match(sparse.stats, /投影估算/);
  if (process.env.TOOLPATH_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_SCREENSHOT });
  const coverageBefore = JSON.stringify(sparse.coverage);
  await page.getByLabel("工件", { exact: true }).uncheck();
  await page.getByLabel("刀路", { exact: true }).uncheck();
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.coverageGroup.visible), true);
  await toggle.uncheck();
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.coverageGroup.visible), false);
  await toggle.check();
  await page.getByLabel("材料切除仿真", { exact: true }).check();
  await page.getByLabel("碰撞时暂停", { exact: true }).uncheck();
  await page.evaluate(() => window.toolpathLab.playback.seekProgress(.25));
  await page.waitForFunction(() => window.toolpathLab.viewport.stockSimulation?.stats().removed_percent > 0);
  assert.equal(await page.evaluate(() => JSON.stringify(window.toolpathLab.viewport.stockPayload.coverage)), coverageBefore);
  const simulation = await page.evaluate(() => window.toolpathLab.viewport.stockSimulation);
  assert.ok(simulation);
  await toggle.uncheck();
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.stockSimulation !== null), true);
  await page.getByLabel("材料切除仿真", { exact: true }).uncheck();
  await page.getByLabel("工件", { exact: true }).check();
  await page.getByLabel("刀路", { exact: true }).check();
  await toggle.check();
  await page.evaluate(async () => {
    const app = window.toolpathLab;
    app.panel.state.planner.values.stepover_mm = 3;
    app.panel.render();
    await app.regenerate();
  });
  const dense = await page.evaluate(() => window.toolpathLab.viewport.stockPayload.coverage);
  assert.ok(dense.coverage_percent > 99);
  assert.ok(dense.uncovered_area_mm2 < sparse.coverage.uncovered_area_mm2);
  if (process.env.TOOLPATH_DENSE_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_DENSE_SCREENSHOT });

  // A concave imported projection is accepted without importing a new robot or
  // machining arbitrary mesh heights. Check geometry-boundary clipping too.
  await page.evaluate(async () => {
    const app = window.toolpathLab;
    app.panel.setImportedModel({ name: "coverage-L-test.obj", vertex_count: 6, triangle_count: 4,
      boundaries: { hull: [[0, 0], [20, 0], [20, 8], [8, 8], [8, 20], [0, 20]] } });
    await app.regenerate();
  });
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.region.id === "polygon");
  const imported = await page.evaluate(() => window.toolpathLab.viewport.stockPayload.coverage);
  assert.ok(imported.active_cell_count < imported.grid.nx * imported.grid.ny);

  await page.evaluate(async () => {
    const app = window.toolpathLab, { panel } = app;
    panel.setImportedModel(null);
    panel.state.tool.kind = "ball";
    panel.state.region = { id: "square", values: { side_mm: 30 } };
    const surface = panel.catalog.surfaces.types.find(x => x.id === "freeform");
    panel.state.surface = { id: "freeform", values: Object.fromEntries(surface.parameters.map(p => [p.key, p.default])) };
    const planner = panel.catalog.planners.list.find(x => x.id === "five_axis_adaptive");
    panel.state.planner = { id: "five_axis_adaptive", values: Object.fromEntries(planner.parameters.map(p => [p.key, p.default])) };
    panel.state.roughing.enabled = true;
    panel.render();
    await app.regenerate();
  });
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.toolpath.planner === "five_axis_adaptive"
    && window.toolpathLab.viewport.stockPayload.toolpath.metadata.roughing?.layer_count > 0);
  const combined = await page.evaluate(() => {
    const { viewport } = window.toolpathLab;
    return { data: viewport.stockPayload.coverage, lines: viewport.adaptiveSpacingGroup.children.length,
      axes: viewport.stockPayload.timeline.tool_axes.length,
      stats: document.getElementById("stats").innerText };
  });
  assert.ok(combined.data.approximate_contact && combined.axes && combined.lines);
  assert.ok(combined.data.finish_start_move_index > 0);
  assert.match(combined.stats, /不代表实际到面/);
  await page.getByLabel("材料切除仿真", { exact: true }).check();
  await page.getByRole("button", { name: "跳至精加工", exact: true }).click();
  await page.waitForFunction(() => window.toolpathLab.viewport.stockSimulation?.stats().removed_percent > 1);
  await page.evaluate(() => window.toolpathLab.playback.seekProgress(0));
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log("PASS: coverage toggle, texture mask, boundary clipping, independent stock/playback, sparse/dense comparison, imported region and five-axis adaptive roughing.");
  console.log(JSON.stringify({ sparse_percent: sparse.coverage.coverage_percent,
    dense_percent: dense.coverage_percent, combined_percent: combined.data.coverage_percent }));
} finally {
  await browser.close();
}
