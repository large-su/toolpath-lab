// Optional UI smoke check. Requires a local server and an existing Playwright + Edge installation.
// TOOLPATH_BROWSER_MODULE may point to Playwright's installed directory (no project dependency needed).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.TOOLPATH_BROWSER_MODULE || "playwright");
const browser = await chromium.launch({ channel: "msedge", headless: true, args: ["--enable-unsafe-swiftshader"] });
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.TOOLPATH_TEST_URL || "http://127.0.0.1:8771/");
  await page.waitForFunction(() => window.toolpathLab?.viewport.stockPayload);
  const stockToggle = page.getByLabel("材料切除仿真", { exact: true });
  const checkToggle = page.getByLabel("刀身碰撞检测", { exact: true });
  const pauseToggle = page.getByLabel("碰撞时暂停", { exact: true });
  assert.ok(await checkToggle.isDisabled());
  assert.ok(await pauseToggle.isDisabled());
  await stockToggle.check();
  assert.ok(await checkToggle.isEnabled());
  await page.waitForFunction(() => document.querySelector(".collision-runtime")?.textContent.includes("当前未发现干涉"));

  await page.evaluate(async () => {
    const app = window.toolpathLab;
    app.panel.state.tool.kind = "ball";
    app.panel.state.surface = { id: "freeform", values: {
      base_z_mm: 0, amplitude_mm: 8, wavelength_x_mm: 40, wavelength_y_mm: 50, phase_deg: 0,
    } };
    app.panel.state.planner = { id: "five_axis", values: {
      stepover_mm: 6, direction_deg: 0, lead_deg: 25, side_tilt_deg: 0, feed_mm_per_min: 600,
    } };
    app.panel.render();
    await app.regenerate();
    app.playback.seekProgress(0.6);
    app.playback.play();
  });
  await page.waitForFunction(() => !window.toolpathLab.playback.playing
    && document.querySelector("#banner").textContent.includes("已暂停"), null, { timeout: 20000 });
  const hit = await page.evaluate(() => {
    const app = window.toolpathLab;
    return { time: app.playback.time, collision: app.viewport.collisionState,
      marker: app.viewport.collisionMarker.visible,
      shankColor: app.viewport.shankMesh.material.color.getHex(),
      text: document.querySelector(".collision-runtime").textContent };
  });
  assert.ok(hit.collision, JSON.stringify(hit));
  assert.ok(hit.marker);
  assert.equal(hit.shankColor, 0xff4035);
  assert.match(hit.text, /刀身干涉/);
  console.log("Auto-pause and marker:", JSON.stringify(hit));

  await page.evaluate(() => window.toolpathLab.playback.play());
  await page.waitForFunction((time) => window.toolpathLab.playback.time > time + 0.03, hit.time);
  assert.ok(await page.evaluate(() => window.toolpathLab.playback.playing), "resuming the same collision must not pause every frame");
  await page.evaluate(() => window.toolpathLab.playback.pause());
  await pauseToggle.uncheck();
  await page.evaluate(() => { window.toolpathLab.playback.seekProgress(0.6); window.toolpathLab.playback.play(); });
  await page.waitForFunction(() => window.toolpathLab.playback.time > window.toolpathLab.playback.duration * 0.6);
  assert.ok(await page.evaluate(() => window.toolpathLab.playback.playing));
  await page.evaluate(() => window.toolpathLab.playback.pause());

  await checkToggle.uncheck();
  assert.ok(await pauseToggle.isDisabled());
  assert.equal(await page.locator(".collision-runtime").count(), 0);
  assert.ok(!await page.evaluate(() => window.toolpathLab.viewport.collisionMarker.visible));
  await stockToggle.uncheck();
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.stockSimulation), null);
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log("PASS: UI toggles, red shaft, collision marker, exact-time pause, resume, detection-off and stock-off.");
} finally {
  await browser.close();
}
