// 应用装配：目录 -> 参数面板 -> 规划请求 -> 视口与播放。

import {
  deleteModel, downloadGcode, fetchCatalog, fetchModelMesh, fetchModels,
  requestPlan, uploadModel,
} from "./api.js";
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
// 视口里当前显示的模型网格（按 id 缓存，避免每次规划都重新拉一遍）。
let shownModelId = "";
let modelMeshes = new Map();

// ------------------------------------------------------------------ 工具
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

// ------------------------------------------------------------------ 启动
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
    onModelUpload: importModel,
    onModelDelete: removeModel,
  });
  viewport.setDisplayOptions(panel.displayOptions());
  wireButtons();
  await refreshModels();
  // 控制台入口：想在做实验时直接操作视口/参数，可以在浏览器 DevTools 里用这个对象。
  window.toolpathLab = { viewport, panel, playback, regenerate };
  await regenerate();
  requestAnimationFrame(animate);
}

// ------------------------------------------------------------------ 模型
async function refreshModels(selectedId) {
  try {
    const payload = await fetchModels();
    panel.setModels(payload.models, selectedId);
  } catch (error) {
    showBanner("读取模型列表失败：" + error.message);
  }
}

async function importModel(file) {
  showBanner("正在导入 " + file.name + "…", "info");
  try {
    const model = await uploadModel(file);
    modelMeshes.clear();
    shownModelId = "";
    await refreshModels(model.id);
    panel.useModelEverywhere();
    const simplified = model.triangle_count > 40000 ? "（显示时已抽样）" : "";
    showBanner(
      "已导入 " + model.name + "：" + model.triangle_count + " 个三角形" + simplified
      + "，区域与加工面已切到该模型", "info"
    );
    await regenerate();
  } catch (error) {
    showBanner("导入失败：" + error.message);
  }
}

async function removeModel(modelId) {
  try {
    await deleteModel(modelId);
    modelMeshes.delete(modelId);
    if (shownModelId === modelId) {
      shownModelId = "";
      viewport.setModel(null);
    }
    await refreshModels("");
    showBanner("已删除模型", "info");
    await regenerate();
  } catch (error) {
    showBanner("删除失败：" + error.message);
  }
}

/** 让视口显示当前选中的模型：只在切换模型时才去取网格。 */
async function ensureModelShown(modelId) {
  if (modelId === shownModelId) return;
  shownModelId = modelId;
  if (!modelId) {
    viewport.setModel(null);
    return;
  }
  try {
    let mesh = modelMeshes.get(modelId);
    if (!mesh) {
      mesh = await fetchModelMesh(modelId);
      modelMeshes.set(modelId, mesh);
    }
    viewport.setModel(mesh);
  } catch (error) {
    viewport.setModel(null);
    showBanner("读取模型网格失败：" + error.message);
  }
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
  dom.exportButton.addEventListener("click", exportGcode);
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

// ------------------------------------------------------------------ 规划
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
    await ensureModelShown(panel.modelId());
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

async function exportGcode() {
  try {
    const name = await downloadGcode(panel.payload());
    showBanner("已导出 " + name, "info");
  } catch (error) {
    showBanner("导出失败：" + error.message);
  }
}

// ------------------------------------------------------------------ 渲染
function statRow(label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  return [term, detail];
}

function renderStats(result) {
  const stats = result.toolpath.statistics;
  const region = result.region;
  const surface = result.surface || { label: "平面", is_planar: true };
  const stock = result.stock || { is_set: false };
  const size = region.id === "circle"
    ? "直径 " + (region.bounds_mm[0][1] - region.bounds_mm[0][0]).toFixed(0) + " mm"
    : (region.bounds_mm[0][1] - region.bounds_mm[0][0]).toFixed(0) + " × "
      + (region.bounds_mm[1][1] - region.bounds_mm[1][0]).toFixed(0) + " mm";
  const stockSize = stock.is_set
    ? stock.size_mm.map((value) => value.toFixed(0)).join(" × ") + " mm"
    : "无";
  const rows = [
    ["区域", size],
    ["加工面", surface.label + (surface.is_planar ? "" : "（曲面）")],
    ["毛坯", stockSize],
    ["刀轨", String(stats.pass_count)],
    ["刀点", String(stats.point_count)],
    ["切削长度", stats.cut_length_mm.toFixed(1) + " mm"],
    ["预计工时", seconds(stats.estimated_time_s)],
  ];
  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    for (const node of statRow(label, value)) list.appendChild(node);
  }
  const heading = document.createElement("h4");
  heading.textContent = result.tool.kind_label.split(" ")[0] + " D"
    + result.tool.diameter_mm.toFixed(1) + " · " + result.toolpath.planner_label;
  const container = document.createElement("div");
  container.append(heading, list);
  dom.stats.replaceChildren(container);
}

function renderPlaybar(state) {
  if (!scrubbing) dom.scrub.value = String(state.progress);
  dom.time.textContent = state.time.toFixed(2) + " / " + state.duration.toFixed(2) + " s";
  dom.play.textContent = state.playing ? "❚❚" : "▶";
}

// ------------------------------------------------------------------ 循环
let previousTime = 0;

function animate(now) {
  const dt = previousTime ? Math.min((now - previousTime) / 1000, 0.1) : 0;
  previousTime = now;
  const state = playback.update(dt);
  if (state && playback.timeline) {
    viewport.setPlayhead(state.position, state.index);
    if (playback.playing || scrubbing) renderPlaybar(state);
  }
  viewport.render();
  requestAnimationFrame(animate);
}

boot();
