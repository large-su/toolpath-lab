// Optional browser smoke test, same prerequisites as browser_collision.mjs.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.TOOLPATH_BROWSER_MODULE || "playwright");
const browser = await chromium.launch({ channel: "msedge", headless: true, args: ["--enable-unsafe-swiftshader"] });
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 1050 } });
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(process.env.TOOLPATH_TEST_URL || "http://127.0.0.1:8771/");
  await page.waitForFunction(() => window.toolpathLab?.viewport.stockPayload);
  const rough = page.getByLabel("先分层粗加工", { exact: true });
  assert.ok(!await rough.isChecked());
  assert.ok(!await page.getByLabel("每层切深", { exact: true }).isVisible());
  await page.evaluate(async () => {
    const app = window.toolpathLab;
    app.panel.state.tool.kind = "ball";
    app.panel.state.surface = { id: "freeform", values: { amplitude_mm: 4, wavelength_x_mm: 80, wavelength_y_mm: 60 } };
    app.panel.render();
    await app.regenerate();
  });
  await page.getByLabel("材料切除仿真", { exact: true }).check();
  await rough.check();
  await page.waitForFunction(() => window.toolpathLab.viewport.stockPayload?.toolpath.metadata?.roughing?.layer_count === 5);
  assert.ok(await page.getByLabel("每层切深", { exact: true }).isVisible());
  assert.ok(await page.getByLabel("精加工余量", { exact: true }).isVisible());
  await page.waitForFunction(() => document.querySelector(".roughing-runtime")?.textContent.includes("第 1/5 层"));
  assert.equal(await page.evaluate(() => window.toolpathLab.viewport.roughLine.material.color.getHex()), 0xbb8eff);
  await page.getByLabel("粗加工刀路", { exact: true }).uncheck();
  assert.ok(!await page.evaluate(() => window.toolpathLab.viewport.roughLine.visible));
  await page.getByLabel("粗加工刀路", { exact: true }).check();
  assert.ok(await page.evaluate(() => window.toolpathLab.viewport.roughLine.visible));
  await page.getByRole("button", { name: "跳至精加工", exact: true }).click();
  await page.waitForFunction(() => document.querySelector(".roughing-runtime")?.textContent.includes("阶段：精加工")
    && window.toolpathLab.viewport.stockSimulation?.stats().removed_percent > 1);
  const snapshot = await page.evaluate(() => ({
    time: window.toolpathLab.playback.time, collision: window.toolpathLab.viewport.collisionState,
    stats: document.getElementById("stats").innerText,
  }));
  assert.equal(snapshot.collision, null);
  console.log("Roughing jump:", JSON.stringify(snapshot));
  if (process.env.TOOLPATH_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_SCREENSHOT });
  await page.getByRole("button", { name: "■", exact: true }).click();
  await page.waitForFunction(() => window.toolpathLab.viewport.stockSimulation.stats().removed_percent === 0
    && document.querySelector(".roughing-runtime")?.textContent.includes("第 1/5 层"));
  await rough.uncheck();
  await page.waitForFunction(() => !window.toolpathLab.viewport.stockPayload?.toolpath.metadata?.roughing);
  assert.equal(await page.locator(".roughing-runtime").count(), 0);
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log("PASS: independent toggle, parameters, purple paths, phase status, jump reconstruction, rewind and disabled behavior.");
} finally {
  await browser.close();
}
