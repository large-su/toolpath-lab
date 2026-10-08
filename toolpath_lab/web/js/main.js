// 应用装配：目录 -> 参数面板 -> 规划请求 -> 视口与播放。

import { downloadGcode, fetchCatalog, requestPlan } from "./api.js";
import { ParameterPanel } from "./panel.js";
import { Playback } from "./playback.js";
import { loadModelFile } from "./model.js";
import { VIEW_BUTTONS, Viewport } from "./viewport.js";
import { renderNotice } from "./notice.js";

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
  pose: document.getElementById("pose"),
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
let latestStockStats = null;
let latestCollision = null;
let collisionEpisodeActive = false;
let lastCollisionTime = -1;
let collisionNoticeDismissed = false;
let collisionNoticeTime = -1;

// ------------------------------------------------------------------ 工具
function seconds(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 60) return value.toFixed(1) + " s";
  const minutes = Math.floor(value / 60);
  return minutes + " min " + (value - minutes * 60).toFixed(0) + " s";
}

function showBanner(message, kind) {
  // An old info timer must not hide a newly displayed warning or error.
  window.clearTimeout(showBanner.timer);
  renderNotice(dom.banner, message, hideBanner, kind === "info" ? "关闭提示" : "关闭警告",
    kind === "info" ? "status" : "alert");
  dom.banner.className = "banner" + (kind === "info" ? " info" : "");
  if (kind === "info") {
    showBanner.timer = window.setTimeout(hideBanner, 4000);
  }
}

function hideBanner() {
  window.clearTimeout(showBanner.timer);
  dom.banner.className = "banner hidden";
}

// ------------------------------------------------------------------ 启动
async function boot() {
  viewport = new Viewport(dom.viewport);
  viewport.onStockUpdate = (stats) => {
    latestStockStats = stats;
    renderStockStats(stats);
  };
  viewport.onCollisionUpdate = (hit) => {
    latestCollision = hit;
    renderCollisionStats(hit);
  };
  playback = new Playback();
  playback.onStateChange = (state) => renderPlaybar(state);
  buildViewToolbar();
  wireAppearanceToolbar();
  window.addEventListener("resize", () => viewport.resize());
  window.addEventListener("keydown", (event) => {
    const tag = document.activeElement ? document.activeElement.tagName : "";
    if (event.code === "Space" && !["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(tag)) {
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
    onDisplayChange: (options) => {
      collisionEpisodeActive = false;
      viewport.setDisplayOptions(options);
      if (lastResult) renderStats(lastResult);
    },
    onModelFile: importModel,
    onModelChange: (model) => viewport.setImportedModel(model),
  });
  viewport.setDisplayOptions(panel.displayOptions());
  wireButtons();
  // 控制台入口：想在做实验时直接操作视口/参数，可以在浏览器 DevTools 里用这个对象。
  window.toolpathLab = { viewport, panel, playback, regenerate };
  await regenerate();
  requestAnimationFrame(animate);
}

async function importModel(file) {
  try {
    showBanner("正在读取模型：" + file.name, "info");
    const model = await loadModelFile(file);
    panel.setImportedModel(model);
    showBanner(`已导入 ${model.name}，请点击“生成刀路”确认区域`, "info");
  } catch (error) {
    showBanner("模型导入失败：" + error.message);
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
    const result = await requestPlan(panel.payload());
    lastResult = result;
    collisionNoticeDismissed = false;
    collisionNoticeTime = -1;
    viewport.setResult(result);
    viewport.setTool(result.tool);
    playback.load(result.timeline);
    collisionEpisodeActive = false;
    lastCollisionTime = -1;
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
  const width = region.bounds_mm[0][1] - region.bounds_mm[0][0];
  const height = region.bounds_mm[1][1] - region.bounds_mm[1][0];
  const size = region.id === "circle"
    ? "直径 " + width.toFixed(0) + " mm"
    : width.toFixed(0) + " × " + height.toFixed(0) + " mm";
  const rows = [
    ["区域", size],
    ["刀轨", String(stats.pass_count)],
    ["刀点", String(stats.point_count)],
    ["切削长度", stats.cut_length_mm.toFixed(1) + " mm"],
    ["预计工时", seconds(stats.estimated_time_s)],
  ];
  const adaptive = result.toolpath.metadata && result.toolpath.metadata.adaptive;
  const roughing = result.toolpath.metadata?.roughing;
  const smoothing = result.toolpath.metadata?.orientation_smoothing;
  const contour = result.toolpath.metadata?.contour;
  if (contour) rows.push(["偏置圈数", String(contour.ring_count)]);
  if (result.toolpath.metadata?.spiral) rows.push(["连续切削", "1 段（精加工）"]);
  if (result.toolpath.metadata?.five_axis_adaptive) rows.push(["残留模型", "球头刀·估算"]);
  if (smoothing) rows.push(
    ["刀轴平滑", Number(smoothing.raw_peak_gradient_deg_mm).toFixed(1) + " → "
      + Number(smoothing.smoothed_peak_gradient_deg_mm).toFixed(1) + " °/mm"],
    ["最大姿态偏差", Number(smoothing.actual_max_deviation_deg).toFixed(1) + "°"],
    ["仿真角速度", Number(smoothing.peak_angular_speed_deg_s).toFixed(1) + " / "
      + Number(smoothing.max_angular_speed_deg_s).toFixed(1) + " °/s"]
  );
  if (roughing) rows.splice(2, 0,
    ["粗加工层数", String(roughing.layer_count)],
    ["实际层切深", Number(roughing.depth_mm).toFixed(2) + " mm"],
    ["精加工余量", Number(roughing.allowance_mm).toFixed(2) + " mm"]
  );
  if (adaptive) {
    rows.splice(2, 0,
      ["目标残留", Number(adaptive.target_scallop_mm).toFixed(2) + " mm"],
      ["实际步距", Number(adaptive.min_stepover_mm).toFixed(2)
        + "～" + Number(adaptive.max_stepover_mm).toFixed(2) + " mm"]
    );
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
  const coverage = result.coverage;
  if (coverage) {
    const analysis = document.createElement("div");
    analysis.className = "coverage-summary";
    analysis.title = coverage.note;
    const summary = document.createElement("dl");
    const displayRows = coverage.available ? [
      ["覆盖率（XY）", coverage.coverage_percent.toFixed(1) + "%"],
      ["未覆盖面积", coverage.uncovered_area_mm2.toFixed(1) + " mm²"],
      ["分析网格", coverage.grid.cell_size_mm.map(v => v.toFixed(2)).join(" × ") + " mm"],
    ] : [["覆盖分析", "区域过窄，网格未采到"]];
    for (const [label, value] of displayRows) summary.append(...statRow(label, value));
    const note = document.createElement("p");
    note.textContent = "整条精加工·投影估算，非切除比例";
    analysis.append(summary, note);
    if (coverage.approximate_contact) {
      const limitation = document.createElement("p");
      limitation.textContent = "曲面/圆角/倾斜刀具：不代表实际到面";
      analysis.appendChild(limitation);
    }
    if (panel?.displayOptions().showUncovered) {
      const legend = document.createElement("p");
      legend.className = "coverage-legend";
      legend.textContent = "■ 红色：预计未覆盖（目标面，受毛坯遮挡）";
      analysis.appendChild(legend);
    }
    container.appendChild(analysis);
  }
  if (roughing) {
    const stage = document.createElement("div");
    stage.className = "roughing-runtime";
    container.appendChild(stage);
    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "button roughing-jump";
    jump.textContent = "跳至精加工";
    jump.title = "暂停并重建此前粗加工后的毛坯状态，查看精加工阶段";
    jump.disabled = !roughing.layer_count;
    jump.addEventListener("click", () => {
      const timeline = playback.timeline;
      const run = timeline?.move_runs.find((item) => item[1] >= roughing.finish_start_move_index);
      if (!run) return;
      playback.pause();
      playback.seekProgress(timeline.times[run[0]] / Math.max(playback.duration, 1e-9));
    });
    container.appendChild(jump);
  }
  if (adaptive) {
    const legend = document.createElement("div");
    legend.className = "adaptive-legend";
    const title = document.createElement("span");
    title.textContent = "步距着色";
    const bar = document.createElement("i");
    bar.className = "adaptive-legend-bar";
    const labels = document.createElement("span");
    labels.textContent = "密集  →  稀疏";
    legend.append(title, bar, labels);
    container.appendChild(legend);
  }
  if (panel && panel.displayOptions().showStock) {
    const stock = document.createElement("div");
    stock.className = "stock-runtime";
    stock.textContent = "材料仿真：等待播放";
    container.appendChild(stock);
    if (panel.displayOptions().checkToolCollision) {
      const collision = document.createElement("div");
      collision.className = "collision-runtime";
      container.appendChild(collision);
    }
  }
  dom.stats.replaceChildren(container);
  if (panel && panel.displayOptions().showStock) renderStockStats(latestStockStats);
  renderCollisionStats(latestCollision);
  if (playback) renderMachiningStage(playback.state());
}

function renderStockStats(stats) {
  const element = dom.stats.querySelector(".stock-runtime");
  if (!element) return;
  if (!stats) {
    element.textContent = "材料仿真：未启用";
    return;
  }
  element.textContent = "已切除 " + Number(stats.removed_percent).toFixed(1)
    + "% · 剩余 " + Number(stats.remaining_volume_mm3).toFixed(0) + " mm³";
}

function renderCollisionStats(hit) {
  // Closing a live notice suppresses this episode, not the detector or next collision.
  // Reset outside the collision or on timeline rewind; re-rendering stats alone preserves dismissal.
  if (!hit || hit.time_s < collisionNoticeTime - 1e-9) collisionNoticeDismissed = false;
  collisionNoticeTime = hit ? hit.time_s : -1;
  const element = dom.stats.querySelector(".collision-runtime");
  if (!element) return;
  element.classList.toggle("collision-alert", Boolean(hit));
  element.hidden = Boolean(hit && collisionNoticeDismissed);
  if (hit) {
    renderNotice(element,
      `⚠ ${hit.part}干涉 · ${hit.time_s.toFixed(2)} s\n位置 ${hit.position.map((v) => v.toFixed(1)).join(", ")} mm\n竖直重叠约 ${hit.overlap_mm.toFixed(2)} mm`,
      () => {
        collisionNoticeDismissed = true;
        element.hidden = true;
      }, "关闭干涉警告");
  } else {
    element.removeAttribute("role");
    element.textContent = "刀身检测：当前未发现干涉（高度场近似）";
  }
}

function renderPlaybar(state) {
  if (!scrubbing) dom.scrub.value = String(state.progress);
  const [x, y, z] = state.toolAxis || [0, 0, 1];
  const azimuth = Math.hypot(x, y) <= 1e-9 ? 0 : Math.atan2(y, x) * 180 / Math.PI;
  const tilt = Math.atan2(Math.hypot(x, y), z) * 180 / Math.PI;
  dom.pose.textContent = "姿态 A" + azimuth.toFixed(1) + "° B" + tilt.toFixed(1) + "°";
  dom.time.textContent = state.time.toFixed(2) + " / " + state.duration.toFixed(2) + " s";
  dom.play.textContent = state.playing ? "❚❚" : "▶";
  renderMachiningStage(state);
}

function renderMachiningStage(state) {
  const element = dom.stats.querySelector(".roughing-runtime");
  const roughing = lastResult?.toolpath.metadata?.roughing;
  if (!element || !roughing) return;
  const index = state.moveIndex || 0;
  const layer = roughing.layers.find((item) => index >= item.start_move_index && index <= item.end_move_index);
  element.textContent = index < roughing.finish_start_move_index
    ? (layer ? `阶段：粗加工第 ${layer.index}/${roughing.layer_count} 层 · 层高 ${layer.z_mm.toFixed(2)} mm`
      : "阶段：粗加工结束，安全转入精加工")
    : "阶段：精加工（原选定策略）";
}

// ------------------------------------------------------------------ 循环
let previousTime = 0;

function animate(now) {
  const dt = previousTime ? Math.min((now - previousTime) / 1000, 0.1) : 0;
  previousTime = now;
  const wasPlaying = playback.playing;
  let state = playback.update(dt);
  if (state && playback.timeline) {
    if (state.time < lastCollisionTime) collisionEpisodeActive = false;
    const collisions = viewport.setPlayhead(state.position, state.index, state.toolAxis, state.time);
    if (collisions.updated) {
      if (wasPlaying && collisions.first && viewport.display.pauseOnCollision && !collisionEpisodeActive) {
        playback.pause();
        playback.seekProgress(collisions.first.time_s / Math.max(playback.duration, 1e-9));
        state = playback.state();
        viewport.setPlayhead(state.position, state.index, state.toolAxis, state.time);
        collisionEpisodeActive = true;
        showBanner("检测到刀身与剩余毛坯干涉，已暂停。可调整刀具/曲面参数重新生成；再次播放可继续观察。", "info");
      } else if (collisions.first || collisions.current) {
        collisionEpisodeActive = true;
      } else {
        collisionEpisodeActive = false;
      }
    }
    lastCollisionTime = state.time;
    if (wasPlaying || scrubbing) renderPlaybar(state);
  }
  viewport.render();
  requestAnimationFrame(animate);
}

boot();
