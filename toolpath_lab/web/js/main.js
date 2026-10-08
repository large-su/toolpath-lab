// App assembly: catalog -> parameter panel -> planning request -> viewport and playback.

import { downloadExport, fetchCatalog, importDxf, requestPlan } from "./api.js";
import { ParameterPanel } from "./panel.js";
import { Playback } from "./playback.js";
import { VIEW_BUTTONS, Viewport } from "./viewport.js";

const REGENERATE_DEBOUNCE_MS = 200;

const dom = {
  panel: document.getElementById("panel"),
  viewport: document.getElementById("viewport"),
  viewToolbar: document.getElementById("view-toolbar"),
  stats: document.getElementById("stats"),
  banner: document.getElementById("banner"),
  generate: document.getElementById("btn-generate"),
  exportButton: document.getElementById("btn-export"),
  exportCsvButton: document.getElementById("btn-export-csv"),
  play: document.getElementById("btn-play"),
  stop: document.getElementById("btn-stop"),
  scrub: document.getElementById("scrub"),
  time: document.getElementById("time"),
};

let catalog = null;
let panel = null;
let viewport = null;
let playback = null;
let busy = false;
let queued = false;
let debounceTimer = 0;
let scrubbing = false;
let lastResult = null;

// ------------------------------------------------------------------ tools
function seconds(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 60) return value.toFixed(1) + " s";
  const minutes = Math.floor(value / 60);
  return minutes + " min " + (value - minutes * 60).toFixed(0) + " s";
}

function showBanner(message, kind) {
  dom.banner.textContent = message;
  dom.banner.className = "banner" + (kind === "info" ? " info" : "");
  if (kind === "info") {
    window.clearTimeout(showBanner.timer);
    showBanner.timer = window.setTimeout(hideBanner, 4000);
  }
}

function hideBanner() {
  dom.banner.className = "banner hidden";
}

// ------------------------------------------------------------------ startup
async function boot() {
  viewport = new Viewport(dom.viewport);
  playback = new Playback();
  playback.onStateChange = (state) => renderPlaybar(state);
  buildViewToolbar();
  wireAppearanceToolbar();
  window.addEventListener("resize", () => viewport.resize());
  window.addEventListener("keydown", (event) => {
    const tag = document.activeElement ? document.activeElement.tagName : "";
    if (event.code === "Space" && !["INPUT", "SELECT", "TEXTAREA"].includes(tag)) {
      event.preventDefault();
      playback.toggle();
    }
  });

  try {
    catalog = await fetchCatalog();
  } catch (error) {
    showBanner("无法连接后端：" + error.message);
    return;
  }

  panel = new ParameterPanel({
    root: dom.panel,
    catalog: catalog,
    onChange: scheduleRegenerate,
    onDisplayChange: (options) => viewport.setDisplayOptions(options),
    // The import is stateless: the drawing text goes up, the outlines come back to the panel.
    importDxf: importDxf,
    notice: showBanner,
  });
  viewport.setDisplayOptions(panel.displayOptions());
  wireButtons();
  // Console entry point: to poke at the viewport or the parameters while experimenting, use this in DevTools.
  window.toolpathLab = { viewport, panel, playback, regenerate };
  await regenerate();
  requestAnimationFrame(animate);
}

function buildViewToolbar() {
  dom.viewToolbar.replaceChildren();
  for (const item of VIEW_BUTTONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.view = item.view;
    button.title = item.view === "fit" ? "最佳视角并居中" : item.label + "视图（再次点击切到对面）";
    const strong = document.createElement("strong");
    strong.textContent = item.label;
    const small = document.createElement("small");
    small.textContent = item.sub;
    button.append(strong, small);
    button.addEventListener("click", () => {
      const target = item.view !== "fit" && viewport.activeView === item.view
        ? viewport.oppositeOf(item.view)
        : item.view;
      if (target === "fit") viewport.resetView();
      else viewport.applyView(target);
      markActiveView();
    });
    dom.viewToolbar.appendChild(button);
  }
  markActiveView();
}

function markActiveView() {
  for (const button of dom.viewToolbar.children) {
    button.classList.toggle("active", button.dataset.view === viewport.activeView);
  }
}

function wireAppearanceToolbar() {
  const buttons = document.querySelectorAll("[data-appearance]");
  for (const button of buttons) {
    button.addEventListener("click", () => {
      const key = button.dataset.appearance;
      const next = !button.classList.contains("active");
      button.classList.toggle("active", next);
      const options = {};
      options[key] = next;
      viewport.setAppearance(options);
      viewport.render();
    });
  }
}

function wireButtons() {
  dom.generate.addEventListener("click", () => regenerate());
  dom.exportButton.addEventListener("click", () => exportFile("gcode"));
  dom.exportCsvButton.addEventListener("click", () => exportFile("csv"));
  dom.play.addEventListener("click", () => playback.toggle());
  dom.stop.addEventListener("click", () => playback.stop());
  dom.scrub.addEventListener("input", () => {
    scrubbing = true;
    playback.seekProgress(Number(dom.scrub.value));
  });
  dom.scrub.addEventListener("change", () => {
    scrubbing = false;
  });
}

// ------------------------------------------------------------------ planning
function scheduleRegenerate() {
  hideBanner();
  window.clearTimeout(debounceTimer);
  debounceTimer = window.setTimeout(() => regenerate(), REGENERATE_DEBOUNCE_MS);
}

async function regenerate() {
  if (busy) {
    queued = true;
    return;
  }
  busy = true;
  dom.generate.disabled = true;
  try {
    const result = await requestPlan(panel.payload());
    lastResult = result;
    viewport.setResult(result);
    viewport.setTool(result.tool);
    playback.load(result.timeline);
    renderStats(result);
    if (result.warnings && result.warnings.length) showBanner(result.warnings.join("；"));
    else hideBanner();
  } catch (error) {
    showBanner(error.message);
  } finally {
    busy = false;
    dom.generate.disabled = false;
    if (queued) {
      queued = false;
      regenerate();
    }
  }
}

async function exportFile(kind) {
  try {
    const name = await downloadExport(kind, panel.payload());
    showBanner("已导出 " + name, "info");
  } catch (error) {
    showBanner("导出失败：" + error.message);
  }
}

// ------------------------------------------------------------------ rendering
function statRow(label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  return [term, detail];
}

// The region size comes from the parameters the shape declares, so a new shape needs no change here.
function regionSummary(region) {
  const values = region.parameters || {};
  if (region.id === "imported") {
    // An imported outline is not in the catalogue: report how many points it is made of.
    const count = values.point_count !== undefined
      ? values.point_count
      : (region.boundary || []).length;
    return (region.label || "导入轮廓") + " · " + count + " 个点";
  }
  const shapes = (catalog && catalog.regions && catalog.regions.shapes) || [];
  const shape = shapes.find((item) => item.id === region.id);
  const parts = ((shape && shape.parameters) || [])
    .filter((item) => values[item.key] !== undefined)
    .map((item) => item.label + " " + values[item.key] + (item.unit ? " " + item.unit : ""));
  return parts.length ? parts.join(" · ") : (region.label || region.id);
}

// Notes (the collapsible list in the statistics panel): strategies record process facts here -
// ring and layer counts, ring direction, adaptive rounds.
// Collapsed by default, expand it for the cost/benefit curve. Filled with textContent, never HTML.
function renderNotes(result) {
  const notes = (result.toolpath && result.toolpath.notes) || [];
  if (!notes.length) return null;
  const details = document.createElement("details");
  details.className = "notes";
  const summary = document.createElement("summary");
  summary.textContent = "刀路说明 · " + notes.length + " 条";
  const list = document.createElement("ul");
  for (const note of notes) {
    const item = document.createElement("li");
    item.textContent = note;
    list.appendChild(item);
  }
  details.append(summary, list);
  return details;
}

function renderStats(result) {
  const stats = result.toolpath.statistics;
  const region = result.region;
  const rows = [
    ["区域", regionSummary(region)],
    ["刀轨", String(stats.pass_count)],
    ["刀点", String(stats.point_count)],
    ["切削长度", stats.cut_length_mm.toFixed(1) + " mm"],
    ["预计工时", seconds(stats.estimated_time_s)],
  ];
  if (result.coverage) {
    const coverage = result.coverage;
    rows.push(["覆盖率", (coverage.ratio * 100).toFixed(1) + " %"]);
    if (coverage.uncut_area_mm2 > 0) {
      rows.push([
        "未切除",
        coverage.uncut_area_mm2.toFixed(1) + " mm² / " + coverage.patch_count + " 处",
      ]);
    }
  }
  if (result.removal && result.removal.removed_volume_mm3 > 0) {
    // The height map answers a question the planar coverage cannot: how deep did it get.
    const removal = result.removal;
    rows.push(["到面率", (removal.floor_ratio * 100).toFixed(1) + " %"]);
    rows.push(["切除体积", removal.removed_volume_mm3.toFixed(0) + " mm³"]);
    if (removal.cusp_mm !== null && removal.cusp_mm !== undefined && removal.cusp_mm > 0) {
      // The ridge a curved bottom leaves between two passes: h = profile(s/2), theory before any grid.
      rows.push(["刀间残留", removal.cusp_mm.toFixed(3) + " mm（理论）"]);
    }
    if (removal.remaining_volume_mm3 > 0) {
      rows.push(["剩余余量", removal.remaining_volume_mm3.toFixed(0) + " mm³"]);
    }
  }
  if (result.holder && result.holder.engaged_points > 0 && result.holder.clearance_mm !== null) {
    // The tool above the flutes is inside the pocket: how much room is left to the wall.
    const holder = result.holder;
    rows.push([
      "刀柄间隙",
      holder.clearance_mm.toFixed(2) + " mm"
        + (holder.shortfall_mm > 0 ? "（差 " + holder.shortfall_mm.toFixed(2) + " mm）" : ""),
    ]);
  }
  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    for (const node of statRow(label, value)) list.appendChild(node);
  }
  const heading = document.createElement("h4");
  heading.textContent = result.tool.kind_label.split(" ")[0] + " D"
    + result.tool.diameter_mm.toFixed(1) + " · " + result.toolpath.planner_label;
  const container = document.createElement("div");
  container.append(heading, list);
  const notes = renderNotes(result);
  if (notes) container.appendChild(notes);
  dom.stats.replaceChildren(container);
}

function renderPlaybar(state) {
  if (!scrubbing) dom.scrub.value = String(state.progress);
  dom.time.textContent = state.time.toFixed(2) + " / " + state.duration.toFixed(2) + " s";
  dom.play.textContent = state.playing ? "❚❚" : "▶";
}

// ------------------------------------------------------------------ loop
let previousTime = 0;

function animate(now) {
  const dt = previousTime ? Math.min((now - previousTime) / 1000, 0.1) : 0;
  previousTime = now;
  const state = playback.update(dt);
  if (state && playback.timeline) {
    viewport.setPlayhead(state.position, state.index, state.moveIndex);
    if (playback.playing || scrubbing) renderPlaybar(state);
  }
  viewport.render();
  requestAnimationFrame(animate);
}

boot();
