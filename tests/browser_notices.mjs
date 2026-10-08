// Optional UI regression: every warning path uses an accessible dismiss button.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.TOOLPATH_BROWSER_MODULE || "playwright");
const browser = await chromium.launch({ channel: "msedge", headless: true,
  args: ["--enable-unsafe-swiftshader"] });
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  const url = process.env.TOOLPATH_TEST_URL || "http://127.0.0.1:8771/";
  await page.goto(url);
  await page.waitForFunction(() => window.toolpathLab?.viewport.stockPayload);
  const banner = page.locator("#banner");
  await page.evaluate(async () => {
    const app = window.toolpathLab;
    app.panel.state.planner.values.stepover_mm = 6.5;
    app.panel.render();
    await app.regenerate();
  });
  await banner.waitFor({ state: "visible" });
  assert.match(await banner.innerText(), /切宽 6.5 mm/);
  const payload = await page.evaluate(() => JSON.stringify(window.toolpathLab.panel.payload()));
  const close = banner.getByRole("button", { name: "关闭警告", exact: true });
  const rects = await page.evaluate(() => {
    const outer = document.querySelector("#banner").getBoundingClientRect();
    const button = document.querySelector("#banner .notice-close").getBoundingClientRect();
    return { right: outer.right - button.right, top: button.top - outer.top };
  });
  assert.ok(rects.right >= 0 && rects.right < 10 && rects.top >= 0 && rects.top < 10);
  if (process.env.TOOLPATH_NOTICE_SCREENSHOT) await page.screenshot({ path: process.env.TOOLPATH_NOTICE_SCREENSHOT });
  await close.click();
  assert.equal(await banner.isVisible(), false);
  assert.equal(await page.evaluate(() => JSON.stringify(window.toolpathLab.panel.payload())), payload);
  await page.getByRole("button", { name: "生成刀路", exact: true }).click();
  await banner.waitFor({ state: "visible" });
  await close.focus();
  await page.keyboard.press("Space");
  assert.equal(await banner.isVisible(), false);
  assert.equal(await page.evaluate(() => window.toolpathLab.playback.playing), false);

  // A success timer must not dismiss a subsequent persistent parameter warning.
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 NC", exact: true }).click();
  await download;
  await banner.getByRole("button", { name: "关闭提示", exact: true }).waitFor();
  await page.getByRole("button", { name: "生成刀路", exact: true }).click();
  await close.waitFor();
  await page.waitForTimeout(4250);
  assert.equal(await banner.isVisible(), true);
  await close.click();

  const failPlan = route => route.fulfill({ status: 500, contentType: "application/json",
    body: JSON.stringify({ error: "测试规划失败" }) });
  await page.route("**/api/plan", failPlan);
  await page.getByRole("button", { name: "生成刀路", exact: true }).click();
  await banner.waitFor({ state: "visible" });
  assert.match(await banner.innerText(), /测试规划失败/);
  await close.click();
  await page.unroute("**/api/plan", failPlan);
  await page.route("**/api/export/gcode", route => route.fulfill({ status: 500,
    contentType: "application/json", body: JSON.stringify({ error: "测试导出失败" }) }));
  await page.getByRole("button", { name: "导出 NC", exact: true }).click();
  await banner.waitFor({ state: "visible" });
  assert.match(await banner.innerText(), /导出失败/);
  await close.click();
  await page.locator('input[type="file"]').setInputFiles({ name: "invalid.obj",
    mimeType: "text/plain", buffer: Buffer.from("not a model") });
  await page.waitForFunction(() => document.querySelector("#banner").textContent.includes("模型导入失败"));
  await close.click();

  // Updating live warning text must preserve its button and dismissal state.
  await page.getByLabel("材料切除仿真", { exact: true }).check();
  const collision = page.locator(".collision-runtime");
  const hit = { part: "刀身", position: [0, 0, 0], overlap_mm: 1, time_s: 10 };
  await page.evaluate(hit => window.toolpathLab.viewport._setCollisionState(hit), hit);
  const collisionClose = collision.getByRole("button", { name: "关闭干涉警告", exact: true });
  await collisionClose.waitFor();
  await collisionClose.click();
  assert.equal(await collision.isVisible(), false);
  assert.equal(await page.getByLabel("刀身碰撞检测", { exact: true }).isChecked(), true);
  assert.equal(await page.getByLabel("碰撞时暂停", { exact: true }).isChecked(), true);
  await page.evaluate(hit => window.toolpathLab.viewport._setCollisionState({ ...hit, time_s: 11 }), hit);
  assert.equal(await collision.isVisible(), false);
  await page.getByLabel("刀具", { exact: true }).uncheck();
  assert.equal(await collision.isVisible(), false, "stats rebuild must not resurrect the dismissed notice");
  await page.evaluate(hit => window.toolpathLab.viewport._setCollisionState({ ...hit, time_s: 5 }), hit);
  assert.equal(await collision.isVisible(), true, "rewind should allow a new warning");
  await collisionClose.click();
  await page.evaluate(hit => {
    window.toolpathLab.viewport._setCollisionState(null);
    window.toolpathLab.viewport._setCollisionState({ ...hit, time_s: 12 });
  }, hit);
  assert.equal(await collision.isVisible(), true, "a new collision must remain visible");
  await collisionClose.focus();
  await page.keyboard.press("Enter");
  assert.equal(await collision.isVisible(), false);
  await page.evaluate(async () => window.toolpathLab.regenerate());
  await page.evaluate(hit => window.toolpathLab.viewport._setCollisionState(hit), hit);
  assert.equal(await collision.isVisible(), true, "regeneration must reset dismissal");

  // This covers a failure before app initialization; it must not need wireButtons.
  const unavailable = await browser.newPage();
  await unavailable.route("**/api/catalog", route => route.abort("failed"));
  await unavailable.goto(url);
  const disconnected = unavailable.locator("#banner");
  await disconnected.waitFor({ state: "visible" });
  assert.match(await disconnected.innerText(), /无法连接后端/);
  await disconnected.getByRole("button", { name: "关闭警告", exact: true }).click();
  assert.equal(await disconnected.isVisible(), false);
  await unavailable.close();
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log("PASS: parameter, plan, export, import and backend notices; close placement, keyboard, timer replacement, collision dismissal and safety settings.");
} finally { await browser.close(); }
