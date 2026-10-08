// Optional headless UI check using the existing Edge/Playwright installation.
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
  await page.evaluate(async () => {
    const app = window.toolpathLab;
    const descriptor = app.panel.catalog.planners.list.find(item => item.id === "five_axis");
    const defaults = Object.fromEntries(descriptor.parameters.map(p => [p.key, p.default]));
    const surfaceDescriptor = app.panel.catalog.surfaces.types.find(item => item.id === "composite");
    const surfaceDefaults = Object.fromEntries(surfaceDescriptor.parameters.map(p => [p.key, p.default]));
    app.panel.state.tool.kind = "ball";
    app.panel.state.region.values.side_mm = 50;
    app.panel.state.surface = { id: "composite", values: { ...surfaceDefaults,
      secondary_wavelength_x_mm: 18, secondary_wavelength_y_mm: 20,
    } };
    app.panel.state.planner = { id: "five_axis", values: { ...defaults,
      smoothing_span_mm: 8, max_axis_deviation_deg: 5, max_angular_speed_deg_s: 10,
      feed_mm_per_min: 1500, lead_deg: 18, side_tilt_deg: 5,
    } };
    app.panel.render();
    await app.regenerate();
  });
  const toggle = page.getByLabel("五轴姿态平滑", { exact: true });
  assert.ok(await toggle.isChecked());
  assert.ok(await page.getByLabel("平滑范围", { exact: true }).isVisible());
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.toolpath.metadata?.orientation_smoothing);
  const metrics = await page.evaluate(() => {
    const app = window.toolpathLab, payload = app.viewport.stockPayload;
    const samples = payload.timeline;
    let peakSpeed = 0;
    for (let i = 1; i < samples.times.length; i++) {
      const dt = samples.times[i] - samples.times[i - 1];
      if (dt < 1e-6) continue;
      const norm = a => { const length = Math.hypot(...a); return a.map(x => x / length); };
      const a = norm(samples.tool_axes[i - 1]), b = norm(samples.tool_axes[i]);
      const dot = Math.max(-1, Math.min(1, a.reduce((s, x, j) => s + x * b[j], 0)));
      const cross = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
      peakSpeed = Math.max(peakSpeed, Math.atan2(Math.hypot(...cross), dot) * 180 / Math.PI / dt);
    }
    const cutRun = samples.move_runs.find(r => payload.toolpath.moves[r[1]].kind === "cut");
    app.playback.time = samples.times[cutRun[0]];
    const first = app.playback.state().toolAxis;
    app.playback.time += .2;
    const later = app.playback.state().toolAxis;
    app.playback.stop();
    return { metadata: payload.toolpath.metadata.orientation_smoothing, peakSpeed,
      first, later, stats: document.getElementById("stats").innerText };
  });
  assert.ok(metrics.metadata.actual_max_deviation_deg <= 5.0001);
  assert.ok(metrics.metadata.smoothed_peak_gradient_deg_mm < metrics.metadata.raw_peak_gradient_deg_mm);
  assert.ok(metrics.peakSpeed < 10.02, JSON.stringify(metrics));
  assert.match(metrics.stats, /刀轴平滑/);
  assert.notDeepEqual(metrics.first, metrics.later);
  console.log("Smoothed UI:", JSON.stringify(metrics));
  if (process.env.TOOLPATH_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_SCREENSHOT });
  await toggle.uncheck();
  await page.waitForFunction(() => !window.toolpathLab.viewport.stockPayload.toolpath.metadata?.orientation_smoothing);
  assert.ok(!await page.getByLabel("平滑范围", { exact: true }).isVisible());
  await toggle.check();
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.toolpath.metadata?.orientation_smoothing);
  await page.getByLabel("先分层粗加工", { exact: true }).check();
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload.toolpath.metadata?.roughing?.layer_count > 0);
  const combined = await page.evaluate(() => {
    const data = window.toolpathLab.viewport.stockPayload;
    const start = data.toolpath.metadata.roughing.finish_start_move_index;
    return { smoothing: data.toolpath.metadata.orientation_smoothing,
      transitionRate: data.toolpath.moves[start - 1].angular_speed_deg_s };
  });
  assert.equal(combined.transitionRate, 10);
  assert.ok(combined.smoothing);
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log("PASS: smoothing controls, actual bounded angular clock, axis variation, off/on comparison and roughing transition.");
} finally {
  await browser.close();
}
