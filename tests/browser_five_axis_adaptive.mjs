import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.TOOLPATH_BROWSER_MODULE || "playwright");
const browser = await chromium.launch({ channel: "msedge", headless: true, args: ["--enable-unsafe-swiftshader"] });
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(process.env.TOOLPATH_TEST_URL || "http://127.0.0.1:8771/");
  await page.waitForFunction(() => window.toolpathLab?.viewport.stockPayload);
  await page.getByLabel("刀具类型", { exact: true }).selectOption("ball");
  await page.evaluate(async () => {
    const app = window.toolpathLab;
    const descriptor = app.panel.catalog.surfaces.types.find(x => x.id === "freeform");
    const defaults = Object.fromEntries(descriptor.parameters.map(p => [p.key, p.default]));
    app.panel.state.surface = { id: "freeform", values: { ...defaults, amplitude_mm: 4,
      wavelength_x_mm: 80, wavelength_y_mm: 60 } };
    app.panel.state.region.values.side_mm = 40;
    app.panel.render();
    await app.regenerate();
  });
  await page.getByLabel("策略", { exact: true }).selectOption("five_axis_adaptive");
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.toolpath.planner === "five_axis_adaptive");
  for (const label of ["目标残留高度", "最小步距", "最大步距", "前倾角", "侧倾角", "五轴姿态平滑"])
    assert.ok(await page.getByLabel(label, { exact: true }).isVisible());
  assert.equal(await page.getByLabel("切宽 ae", { exact: true }).count(), 0);
  const result = await page.evaluate(() => {
    const app = window.toolpathLab, data = app.viewport.stockPayload;
    const cuts = data.toolpath.moves.filter(m => m.kind === "cut");
    return { metadata: data.toolpath.metadata, cuts: cuts.length, axes: cuts[0].tool_axes,
      coloredLines: app.viewport.adaptiveSpacingGroup.children.length,
      stats: document.getElementById("stats").innerText };
  });
  assert.ok(result.metadata.adaptive && result.metadata.orientation_smoothing);
  assert.equal(result.cuts, result.metadata.adaptive.stepover_profile_mm.length);
  assert.equal(result.coloredLines, result.cuts);
  assert.ok(result.axes.some(a => Math.abs(a[0] - result.axes[0][0]) > .01));
  assert.match(result.stats, /目标残留/);
  assert.match(result.stats, /刀轴平滑/);
  assert.match(result.stats, /球头刀·估算/);
  await page.getByLabel("自适应步距着色", { exact: true }).uncheck();
  assert.ok(!await page.evaluate(() => window.toolpathLab.viewport.adaptiveSpacingGroup.visible));
  await page.getByLabel("自适应步距着色", { exact: true }).check();
  await page.getByLabel("五轴姿态平滑", { exact: true }).uncheck();
  await page.waitForFunction(() => !window.toolpathLab.viewport.stockPayload.toolpath.metadata.orientation_smoothing);
  assert.ok(await page.evaluate(() => window.toolpathLab.viewport.stockPayload.timeline.tool_axes.length > 0));
  await page.getByLabel("五轴姿态平滑", { exact: true }).check();
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.toolpath.metadata.orientation_smoothing);
  await page.getByLabel("材料切除仿真", { exact: true }).check();
  await page.getByLabel("先分层粗加工", { exact: true }).check();
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.toolpath.metadata.roughing?.layer_count > 0);
  await page.getByRole("button", { name: "跳至精加工", exact: true }).click();
  await page.waitForFunction(() => document.querySelector(".roughing-runtime")?.textContent.includes("精加工")
    && window.toolpathLab.viewport.stockSimulation?.stats().removed_percent > 1);
  const snapshot = await page.evaluate(() => ({ stats: document.getElementById("stats").innerText,
    collision: window.toolpathLab.viewport.collisionState,
    removed: window.toolpathLab.viewport.stockSimulation.stats().removed_percent }));
  assert.ok(snapshot.removed > 1);
  assert.equal(snapshot.collision, null);
  console.log("Combined UI:", JSON.stringify({ cuts: result.cuts, adaptive: result.metadata.adaptive,
    smoothing: result.metadata.orientation_smoothing, ...snapshot }));
  if (process.env.TOOLPATH_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_SCREENSHOT });
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log("PASS: combined parameters, oriented adaptive paths, coloring, smoothing toggle and roughing playback.");
} finally { await browser.close(); }
