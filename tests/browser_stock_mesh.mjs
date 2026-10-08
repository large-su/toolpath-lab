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
  await page.waitForFunction(() => window.toolpathLab?.viewport.stockPayload);
  await page.evaluate(async () => {
    const app = window.toolpathLab, { panel } = app;
    panel.state.tool.kind = "flat";
    panel.state.tool.diameter_mm = 6;
    panel.state.region = { id: "circle", values: { diameter_mm: 80 } };
    panel.state.surface = { id: "flat", values: { base_z_mm: 0 } };
    const descriptor = panel.catalog.planners.list.find(p => p.id === "spiral");
    panel.state.planner = { id: "spiral", values: Object.fromEntries(descriptor.parameters.map(p => [p.key, p.default])) };
    panel.state.planner.values.stepover_mm = 6;
    panel.state.roughing.enabled = false;
    panel.render();
    await app.regenerate();
  });
  await page.getByLabel("材料切除仿真", { exact: true }).check();
  await page.getByLabel("刀身碰撞检测", { exact: true }).uncheck();
  await page.getByLabel("刀具", { exact: true }).uncheck();
  await page.getByLabel("快移", { exact: true }).uncheck();
  async function check(progress) {
    await page.evaluate(progress => window.toolpathLab.playback.seekProgress(progress), progress);
    await page.waitForFunction(() => Math.abs(window.toolpathLab.viewport.stockSimulation.queryTime
      - window.toolpathLab.playback.time) < 1e-8);
    const result = await page.evaluate(() => {
      const app = window.toolpathLab;
      const { stockRenderMesh: mesh, stockMesh: top, stockWallMesh: shell, stockSimulation: stock } = app.viewport;
      const heights = Array.from(mesh.topPositions).filter((_, i) => i % 3 === 2);
      return { vertices: mesh.xy.length, triangles: mesh.topIndices.length / 3,
        edges: mesh.edges.length, min: Math.min(...heights), max: Math.max(...heights),
        top: stock.spec.initial_top_z_mm, bottom: stock.spec.bottom_z_mm,
        transparent: top.material.transparent || shell.material.transparent,
        seamMatches: mesh.edges.every(([a, b], i) => {
          const offset = (mesh.xy.length + i * 4) * 3;
          return [a, b].every((id, j) => [0, 1, 2].every(k =>
            mesh.topPositions[id * 3 + k] === mesh.shellPositions[offset + j * 6 + k]));
        }), stats: stock.stats() };
    });
    assert.ok(result.vertices > 100 && result.triangles > 100 && result.edges > 10);
    assert.equal(result.transparent, false);
    assert.equal(result.seamMatches, true);
    assert.ok(result.min >= result.bottom && result.max <= result.top + 1e-5);
    if (progress === 0) assert.equal(result.min, result.top);
    return result;
  }
  const initial = await check(0);
  if (process.env.TOOLPATH_STOCK_INITIAL_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_STOCK_INITIAL_SCREENSHOT });
  const cut = await check(.6);
  assert.ok(cut.stats.removed_percent > 0);
  if (process.env.TOOLPATH_STOCK_CUT_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_STOCK_CUT_SCREENSHOT });
  await page.evaluate(() => window.toolpathLab.playback.play());
  await page.waitForFunction(() => window.toolpathLab.playback.time > window.toolpathLab.playback.duration * .6 + .1);
  await page.evaluate(() => window.toolpathLab.playback.pause());
  await check(1);
  const reset = await check(0);
  assert.equal(reset.stats.removed_percent, 0);
  assert.equal(reset.vertices, initial.vertices);
  await page.getByLabel("材料切除仿真", { exact: true }).uncheck();
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.stockRenderMesh), null);
  await page.getByLabel("材料切除仿真", { exact: true }).check();
  await check(0);
  await page.evaluate(async () => {
    const app = window.toolpathLab;
    app.panel.state.region = { id: "square", values: { side_mm: 80 } };
    app.panel.state.planner = { id: "raster", values: { mode: "zigzag", stepover_mm: 9,
      direction_deg: 0, feed_mm_per_min: 600 } };
    app.panel.render();
    await app.regenerate();
  });
  await check(0);
  await check(.3);
  if (process.env.TOOLPATH_STOCK_SQUARE_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_STOCK_SQUARE_SCREENSHOT });
  await page.getByLabel("未覆盖区域（XY估算）", { exact: true }).check();
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.coverageGroup.children[0].material.depthTest), true);
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log("PASS: opaque closed stock, circular spiral, square raster, playback/seek/rewind, toggle release and depth-aware overlay.", JSON.stringify({ initial, cut }));
} finally { await browser.close(); }
