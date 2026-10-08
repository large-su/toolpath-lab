// 应用装配：目录 → 参数面板 → 规划请求 → 视口与播放。
//
// 两种工作模式共用同一个视口与播放条：
//   * 实验台（bench）—— 原有功能：区域 + 栅格刀路 + 播放，逻辑完全没变；
//   * CAM 加工（cam）—— 导入 STEP → 建毛坯 → 工序树 → 自动编程 → 切削仿真 → 出程序。
// 两套内容各用一组 Group，切换模式只是换掉面板与显示开关，互不干扰。

import {
  addOperation,
  closeProject,
  createTool,
  deleteOperation,
  deleteTemplate,
  deleteTool,
  downloadGcode,
  downloadNc,
  downloadText,
  duplicateOperation,
  duplicateTool,
  fetchCatalog,
  fetchFeatures,
  fetchModel,
  fetchOperations,
  fetchParameters,
  fetchProjects,
  fetchStock,
  fetchTools,
  generateAllOperations,
  generateOperation,
  importStep,
  moveOperation,
  openProject,
  requestPlan,
  restoreDefaultTools,
  saveParameters,
  saveStock,
  saveTemplate,
  simulate,
  updateOperation,
  updateTool,
} from "./api.js";
import { CamPanel } from "./cam-panel.js";
import { Banner, Modal, Progress, openImportDialog } from "./modal.js";
import { ParameterPanel } from "./panel.js";
import { Playback } from "./playback.js";
import { openToolLibrary } from "./tool-library.js";
import { OperationTreePanel } from "./tree.js";
import { VIEW_BUTTONS, Viewport } from "./viewport.js";

const REGENERATE_DEBOUNCE_MS = 200;

const dom = {
  panel: document.getElementById("panel"),
  camPanel: document.getElementById("cam-panel"),
  sidebar: document.querySelector(".sidebar"),
  treePanel: document.getElementById("tree-panel"),
  treeBody: document.getElementById("tree-body"),
  viewport: document.getElementById("viewport"),
  viewToolbar: document.getElementById("view-toolbar"),
  pickToolbar: document.getElementById("pick-toolbar"),
  pickHint: document.getElementById("pick-hint"),
  modeSwitch: document.getElementById("mode-switch"),
  stats: document.getElementById("stats"),
  banner: document.getElementById("banner"),
  progress: document.getElementById("progress"),
  modal: document.getElementById("modal"),
  generate: document.getElementById("btn-generate"),
  exportButton: document.getElementById("btn-export"),
  importButton: document.getElementById("btn-import"),
  stockButton: document.getElementById("btn-stock"),
  toolsButton: document.getElementById("btn-tools"),
  simulateButton: document.getElementById("btn-simulate"),
  projectButton: document.getElementById("btn-project"),
  play: document.getElementById("btn-play"),
  stop: document.getElementById("btn-stop"),
  step: document.getElementById("btn-step"),
  speed: document.getElementById("speed"),
  speedValue: document.getElementById("speed-value"),
  scrub: document.getElementById("scrub"),
  time: document.getElementById("time"),
  simReset: document.getElementById("btn-sim-reset"),
  opAdd: document.getElementById("btn-op-add"),
  opGenerate: document.getElementById("btn-op-generate"),
  opUp: document.getElementById("btn-op-up"),
  opDown: document.getElementById("btn-op-down"),
  opToggle: document.getElementById("btn-op-toggle"),
  opCopy: document.getElementById("btn-op-copy"),
  opDelete: document.getElementById("btn-op-delete"),
  opExport: document.getElementById("btn-op-export"),
};

let catalog = null;
let panel = null;
let camPanel = null;
let tree = null;
let viewport = null;
let playback = null;
let banner = null;
let modal = null;
let progress = null;

let mode = "bench";
let busy = false;
let queued = false;
let debounceTimer = 0;
let scrubbing = false;
let lastResult = null;

// CAM 状态
const cam = {
  model: null,          // 当前零件的响应（含 mesh / faces / features）
  features: [],
  selectedFaces: [],
  stock: null,
  operations: [],
  templates: [],
  tools: [],            // 刀具库（全局，与工程无关）
  toolCatalog: null,    // 刀具类型目录（来自 /api/catalog 或 /api/tools）
  activeOperationId: null,
  simulation: null,     // 最近一次仿真结果
  simulationFrame: 0,
  playing: false,
  lastResult: null,     // 最近一次 CAM 刀路结果（实验台模式下只缓存，切到 CAM 再画）
};

// ------------------------------------------------------------------ 工具
function seconds(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 60) return value.toFixed(1) + " s";
  const minutes = Math.floor(value / 60);
  return minutes + " min " + (value - minutes * 60).toFixed(0) + " s";
}

function showBanner(message, kind = "error") {
  banner.show(message, kind, kind === "info" ? 0 : 0);
}

function hideBanner() {
  banner.hide();
}

function setBusy(state, message) {
  busy = state;
  dom.generate.disabled = state;
  dom.simulateButton.disabled = state;
  if (state && message) progress.start(message);
  if (!state) progress.stop();
}

// ------------------------------------------------------------------ 启动
async function boot() {
  viewport = new Viewport(dom.viewport);
  playback = new Playback();
  playback.onStateChange = (state) => renderPlaybar(state);
  banner = new Banner({ root: dom.banner });
  modal = new Modal({ root: dom.modal });
  progress = new Progress({ root: dom.progress });

  buildViewToolbar();
  wireAppearanceToolbar();
  wireModeSwitch();
  wirePickToolbar();
  window.addEventListener("resize", () => viewport.resize());
  window.addEventListener("keydown", (event) => {
    const tag = document.activeElement ? document.activeElement.tagName : "";
    if (["INPUT", "SELECT", "TEXTAREA"].includes(tag)) return;
    if (event.code === "Space") {
      event.preventDefault();
      if (mode === "cam") toggleSimulation();
      else playback.toggle();
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
  });
  viewport.setDisplayOptions(panel.displayOptions());

  camPanel = new CamPanel({
    root: dom.camPanel,
    catalog: catalog,
    onChange: () => { /* 参数变化不自动落盘，等用户生成 */ },
    onStockChange: (payload) => previewStock(payload),
    onStockCommit: (payload) => commitStock(payload),
    onDisplayChange: (options) => {
      viewport.setCamDisplay(options);
      // 工具栏「毛坯」按钮与面板勾选是同一份状态，文案跟着走
      syncStockButton();
    },
    onTemplateSave: (name, body) => handleTemplateSave(name, body),
    onTemplateApply: (template) => {
      camPanel.syncValues(template.parameters || {});
      showBanner(`已应用模板 ${template.name}`, "info");
    },
    onParameterCommit: (kind, values) => persistController(values),
    onToolChange: (toolId) => selectToolForOperation(toolId),
    onToolLibrary: () => openToolLibraryDialog(),
  });

  tree = new OperationTreePanel({
    root: dom.treeBody,
    onChange: (id, changes) => handleOperationChange(id, changes),
    onSelect: (operation) => selectOperation(operation),
    onGenerate: (operation) => generateOne(operation.id),
  });

  wireButtons();
  window.toolpathLab = {
    viewport, panel, camPanel, tree, playback, regenerate, cam, modal,
    refreshTools, openToolLibraryDialog, selectToolForOperation, refreshOperations,
    // 自检（electron/smoke.mjs）用：它直接改 cam.simulation 造一份"仿真在场"的状态，
    // 绕开了 runSimulation，需要手动补一次按钮状态同步才能点到「重置」。
    syncSimulationControls,
  };

  // 刀具库是全局的：启动就拉一次，之后面板一直可用
  await refreshTools({ quiet: true });
  await regenerate();
  await loadProjectState();
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

function wireModeSwitch() {
  for (const button of dom.modeSwitch.children) {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  }
}

function wirePickToolbar() {
  const pick = dom.pickToolbar.querySelector("[data-pick='face']");
  const clear = dom.pickToolbar.querySelector("[data-pick='clear']");
  pick.addEventListener("click", () => {
    const next = !pick.classList.contains("active");
    pick.classList.toggle("active", next);
    viewport.setPickable(next);
    showBanner(next ? "已开启面拾取：单击加工面（Shift 多选）" : "已关闭面拾取", "info");
  });
  clear.addEventListener("click", () => {
    cam.selectedFaces = [];
    viewport.setSelectedFaces([]);
    updatePickHint();
    if (cam.activeOperationId) {
      handleOperationChange(cam.activeOperationId, { faces: [] });
    }
  });
  viewport.onFacePick = (faceId, options) => {
    const id = Number(faceId);
    const set = new Set(cam.selectedFaces);
    if (options && options.additive) {
      if (set.has(id)) set.delete(id); else set.add(id);
    } else {
      set.clear();
      set.add(id);
    }
    cam.selectedFaces = Array.from(set);
    viewport.setSelectedFaces(cam.selectedFaces);
    updatePickHint();
    if (cam.activeOperationId) {
      handleOperationChange(cam.activeOperationId, { faces: cam.selectedFaces });
    }
  };
}

function updatePickHint() {
  if (!cam.selectedFaces.length) {
    dom.pickHint.textContent = "未选中任何面";
    return;
  }
  const labels = cam.selectedFaces.map((id) => "#" + id);
  dom.pickHint.textContent = `已选 ${cam.selectedFaces.length} 个面：${labels.join("、")}`;
}

function wireButtons() {
  dom.generate.addEventListener("click", () => {
    if (mode === "cam") generateOne(cam.activeOperationId);
    else regenerate();
  });
  dom.exportButton.addEventListener("click", () => {
    if (mode === "cam") exportNc();
    else exportGcode();
  });
  dom.importButton.addEventListener("click", () => openImport());
  dom.stockButton.addEventListener("click", () => {
    // CAM 模式下切换毛坯显隐。与面板「显示·毛坯」勾选共用同一份状态
    // （camPanel.syncDisplay → onDisplayChange → 视口 + 按钮文案），因此
    // 仿真结果在场时同样有效：显隐只由这个状态决定，不被仿真硬性顶掉——
    // 切削仿真之后到再次点「切削仿真」之前，随时可以显示/隐藏。
    setMode("cam");
    const current = viewport.display ? viewport.display.showStock : true;
    camPanel.syncDisplay("showStock", !current);
  });
  dom.simulateButton.addEventListener("click", () => runSimulation());
  dom.projectButton.addEventListener("click", () => openProjectDialog());
  dom.play.addEventListener("click", () => (mode === "cam" ? toggleSimulation() : playback.toggle()));
  dom.stop.addEventListener("click", () => {
    if (mode === "cam") seekSimulation(0);
    else playback.stop();
  });
  dom.step.addEventListener("click", () => {
    if (mode === "cam") seekSimulation(cam.simulationFrame + 1);
    else playback.stepForward();
  });
  // 退出切削仿真：余料删掉、显示回到生成刀路环节，改参数后接着生成
  dom.simReset.addEventListener("click", () => resetSimulation());
  // 速度是滑条（1×–64×）：input 拖动过程中就换倍速并刷新读数，change 兜底。
  const syncSpeed = () => {
    playback.speed = Number(dom.speed.value) || 1;
    dom.speedValue.textContent = playback.speed + "×";
  };
  dom.speed.addEventListener("input", syncSpeed);
  dom.speed.addEventListener("change", syncSpeed);
  dom.scrub.addEventListener("input", () => {
    scrubbing = true;
    if (mode === "cam") {
      // 进度条是"时间比例"而不是"帧序号"：帧间由扫掠补全，拖到哪就连续显示到哪
      seekSimulationTime(Number(dom.scrub.value));
    } else {
      playback.seekProgress(Number(dom.scrub.value));
    }
  });
  dom.scrub.addEventListener("change", () => {
    scrubbing = false;
  });

  dom.opAdd.addEventListener("click", () => createOperation());
  dom.opGenerate.addEventListener("click", () => generateAll());
  dom.opUp.addEventListener("click", () => reorder(-1));
  dom.opDown.addEventListener("click", () => reorder(1));
  dom.opToggle.addEventListener("click", () => toggleOperation());
  dom.opCopy.addEventListener("click", () => copyOperation());
  dom.opDelete.addEventListener("click", () => removeOperation());
  dom.opExport.addEventListener("click", () => exportTree());
  if (dom.toolsButton) {
    dom.toolsButton.addEventListener("click", () => {
      setMode("cam");
      openToolLibraryDialog();
    });
  }
}

// ------------------------------------------------------------------ 模式
function setMode(next) {
  if (next === mode) return;
  mode = next;
  for (const button of dom.modeSwitch.children) {
    button.classList.toggle("active", button.dataset.mode === next);
  }
  const isCam = next === "cam";
  dom.panel.hidden = isCam;
  dom.camPanel.hidden = !isCam;
  dom.treePanel.hidden = !isCam;
  dom.pickToolbar.hidden = !isCam;
  dom.stockButton.hidden = !isCam;
  if (dom.toolsButton) dom.toolsButton.hidden = !isCam;
  if (isCam) syncStockButton();
  dom.simulateButton.hidden = !isCam;
  syncSimulationControls();
  dom.projectButton.hidden = !isCam;
  dom.sidebar.classList.toggle("cam-mode", isCam);
  // 两套内容互斥显示，避免实验台的规则工件与导入的零件叠在一起。
  // 可见性统一由 viewport 算（setPart / setStockMesh 也会走同一条逻辑），
  // 否则启动时"恢复工程"会把导入的零件点亮在实验台场景里，连相机都被带偏。
  viewport.setSceneMode(next);
  if (isCam) {
    viewport.setPickable(true);
    dom.pickToolbar.querySelector("[data-pick='face']").classList.add("active");
    viewport.clearToolpath();
    if (cam.model) viewport.setPart(cam.model, { frame: false });
    if (cam.stock) viewport.setStockMesh(cam.stock.mesh);
    // 实验台模式下产生的 CAM 结果只做了缓存，切过来时才画
    if (cam.lastResult) applyCamResult(cam.lastResult);
    if (!cam.model) {
      showBanner("还没有导入模型：点顶部「导入模型」选择 STEP 文件", "info");
    }
    viewport.resetView();
  } else {
    viewport.setPickable(false);
    dom.pickToolbar.querySelector("[data-pick='face']").classList.remove("active");
    viewport.clearSimulation();
    if (lastResult) {
      viewport.setResult(lastResult);
      viewport.setTool(lastResult.tool);
      viewport.setPlayhead([0, 0, 0], 0);
    }
    viewport.resetView();
  }
  renderStatsForMode();
}

// ------------------------------------------------------------------ 刀具库
/**
 * 拉取刀具库并刷新面板。
 *
 * 刀库是**全局**的，与当前工程无关，所以启动、改工程、开关对话框都会走这里；
 * 失败不是致命的（面板会给出"刀库打不开"的提示），因此只提示不抛。
 */
async function refreshTools({ quiet = false } = {}) {
  try {
    const payload = await fetchTools();
    cam.tools = payload.tools || [];
    cam.toolCatalog = payload.catalog || cam.toolCatalog;
    camPanel.setTools(cam.tools);
    if (!quiet && !cam.tools.length) {
      showBanner("刀具库是空的：打开「刀具库」新建一把刀，或恢复出厂刀具", "info");
    }
    return cam.tools;
  } catch (error) {
    showBanner("刀具库打不开：" + error.message);
    return [];
  }
}

/**
 * 给当前工序选刀。
 *
 * 选刀是**明确动作**，所以立刻落盘并重算刀路：用户期望"换刀 → 刀路跟着变"，
 * 而不是还要再点一次「生成刀路」。（面板其余参数仍然要等生成才落盘。）
 */
async function selectToolForOperation(toolId) {
  const operation = cam.operations.find((item) => item.id === cam.activeOperationId);
  if (!operation) {
    showBanner("请先在工序树里选择一道工序，再选刀具", "info");
    return;
  }
  const tool = cam.tools.find((item) => item.id === toolId) || null;
  await handleOperationChange(operation.id, {
    parameters: { ...camPanel.parameters(), tool_id: toolId },
  });
  if (tool) {
    showBanner(`当前工序使用「${tool.name}」：D${formatNumber(tool.values.diameter_mm)} mm`, "info");
  } else {
    showBanner("已取消刀具库引用：刀路按面板里手填的刀具尺寸计算", "info");
  }
}

/**
 * 打开刀具库对话框（`pick` 为 true 时是"给工序选刀"模式）。
 *
 * 对话框里的保存/删除都直接打接口，成功后重新拉列表；如果改动影响到了当前工序
 * 引用的刀具，就重算一次刀路——否则界面上显示的是旧刀路，用户会以为改刀没生效。
 */
function openToolLibraryDialog({ pick = false } = {}) {
  if (!cam.toolCatalog && catalog) cam.toolCatalog = catalog.tool_library || null;
  const previewBackup = viewport.tool;
  const dialog = openToolLibrary({
    modal,
    pickMode: pick,
    tools: cam.tools,
    catalog: cam.toolCatalog || { types: [] },
    selectedId: camPanel.toolId() || undefined,
    onMessage: (message) => showBanner(message),
    onPreview: (payload) => viewport.setTool(payload || null),
    onSelect: pick ? async (tool) => {
      await selectToolForOperation(tool.id);
      // 选完关掉对话框，把视口里的刀还原成"当前工序的刀"
      viewport.setTool(previewBackup || (cam.lastResult ? cam.lastResult.tool : null));
    } : null,
    onSave: async (draft) => {
      const saved = draft.id
        ? (await updateTool(draft.id, {
            name: draft.name, kind: draft.kind, values: draft.values, note: draft.note,
          })).tool
        : (await createTool({
            name: draft.name, kind: draft.kind, values: draft.values, note: draft.note,
          })).tool;
      await refreshTools({ quiet: true });
      dialog.setTools(cam.tools, saved.id);
      showBanner(`已保存刀具「${saved.name}」`, "info");
      await regenerateToolUsers(saved.id);
      return saved;
    },
    onDelete: async (toolId) => {
      const record = cam.tools.find((item) => item.id === toolId);
      const used = cam.operations.filter(
        (item) => String((item.parameters || {}).tool_id || "") === toolId
      );
      const warning = used.length
        ? `\n\n有 ${used.length} 道工序正在用它（${used.map((item) => item.name).join("、")}），`
          + "删除后这些工序需要重新选刀。"
        : "";
      if (!window.confirm(`删除刀具「${record ? record.name : toolId}」？${warning}`)) return;
      await deleteTool(toolId);
      await refreshTools({ quiet: true });
      dialog.setTools(cam.tools);
      showBanner(`已删除刀具「${record ? record.name : toolId}」`, "info");
    },
    onDuplicate: async (toolId) => {
      const clone = (await duplicateTool(toolId)).tool;
      await refreshTools({ quiet: true });
      dialog.setTools(cam.tools, clone.id);
      showBanner(`已复制为「${clone.name}」`, "info");
    },
    onRestoreDefaults: async () => {
      const payload = await restoreDefaultTools();
      cam.tools = payload.tools || [];
      camPanel.setTools(cam.tools);
      dialog.setTools(cam.tools);
      showBanner("已恢复出厂刀具（同 id 的刀具不会被覆盖）", "info");
    },
    onClose: () => {
      // 对话框关闭后视口里不应留着一把"没在任何工序里"的刀
      viewport.setTool(previewBackup || (cam.lastResult ? cam.lastResult.tool : null));
    },
  });
  return dialog;
}

/**
 * 刀具改了之后，把引用它的工序刀路重新算一遍。
 *
 * 只重算当前选中的那道：其余工序保持"待生成"状态，用户点「全部生成」时自然会用新刀具
 * （后端每次生成都按刀具库回写几何），避免改一把刀就触发整条工序链的重算。
 */
async function regenerateToolUsers(toolId) {
  const active = cam.operations.find((item) => item.id === cam.activeOperationId);
  if (!active) return;
  if (String((active.parameters || {}).tool_id || "") !== toolId) return;
  try {
    await refreshOperations();
    const operation = cam.operations.find((item) => item.id === cam.activeOperationId);
    if (operation) {
      camPanel.setOperation(operation);
      await drawOperationToolpath(operation);
    }
  } catch (error) {
    showBanner("重新生成刀路失败：" + error.message);
  }
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return Number.isInteger(number) ? String(number) : String(Math.round(number * 1000) / 1000);
}

// ------------------------------------------------------------------ 实验台
function scheduleRegenerate() {
  if (mode !== "bench") return;
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
    if (mode === "bench") {
      viewport.setResult(result);
      viewport.setTool(result.tool);
    }
    playback.load(result.timeline);
    if (mode === "bench") {
      renderBenchStats(result);
    } else {
      renderStats(result);
    }
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

// ------------------------------------------------------------------ CAM 加载
/**
 * 启动落点：后端有**打开着的**工程才恢复它，否则落进 CAM 的空状态。
 *
 * 默认路径是空的——服务启动不再自动打开上次的工程（见 ``server/app.py``），
 * 所以每次打开都是"没有模型、没有毛坯、没有工序"的界面，模型靠手动导入；
 * 想接着上次干就去顶栏「工程」里手动打开（历史工程都还在，数据不删）。
 *
 * "有工程就恢复"这条分支要保留：**刷新页面**（桌面端按 F5 / 重载）不等于重启程序
 * ——后端进程还活着、工程还开着，这时得把界面接回去，否则界面是空的、后端却挂着
 * 一个工程，就成了两边对不上（点"导出 NC"还会导出那个看不见的工程）。
 */
async function loadProjectState() {
  try {
    const projects = await fetchProjects();
    if (projects.current) {
      setMode("cam");
      await refreshModel({ frame: true });
      showBanner(`已恢复工程「${projects.current.name}」`, "info");
      return;
    }
  } catch (error) {
    /* 后端没响应时照样落进空界面：空状态本身是安全的 */
  }
  // 空状态：进 CAM 模式（setMode 会提示"点顶部「导入模型」选择 STEP 文件"）
  setMode("cam");
}

/**
 * 拉取当前工程的全部 CAM 状态并刷新界面。
 *
 * ``frame`` 为 true 时重新取景，用于**导入新模型**与**启动恢复工程**：
 * 这两种情况用户都期望马上看到零件本身，而不是延续上一次的视角。
 * 其余场景（切换工序、改参数）保持用户已经调好的视角。
 */
async function refreshModel({ frame = false } = {}) {
  // 换工程 / 重新导入模型时，先把上一条刀路与仿真结果清干净。
  // 否则视口和统计里还挂着**上一个工程**的刀路，用户改了参数再生成，
  // 界面上看到的仍是旧刀路，就会以为"重新生成没反应"。
  cam.lastResult = null;
  cam.simulation = null;
  cam.simulationFrame = 0;
  cam.selectedFaces = [];
  cam.activeOperationId = null;
  // 仿真没了，播放条「重置」按钮要跟着回到灰态
  syncSimulationControls();
  // 面板上的刀具引用也要清掉：新工程的第一道工序不该沿用上一个零件选过的刀
  camPanel.resetOperation();
  viewport.clearToolpath();
  viewport.clearSimulation();
  viewport.setSelectedFaces([]);
  const model = await fetchModel();
  cam.model = model.project.part;
  viewport.setPart(cam.model, { frame: false });
  const stock = await fetchStock();
  cam.stock = stock;
  camPanel.setStock(stock);
  viewport.setStockMesh(stock.mesh);
  const parameters = await fetchParameters();
  camPanel.setController(parameters.controller);
  const features = await fetchFeatures();
  cam.features = features.features;
  await refreshOperations();
  updatePickHint();
  // 必须放在最后：等零件、毛坯都进了视口，bounds 才是新模型的尺寸
  if (frame) viewport.resetView();
}

async function refreshOperations() {
  const payload = await fetchOperations();
  cam.operations = payload.operations || [];
  cam.templates = payload.templates || [];
  tree.setOperations(payload);
  camPanel.setTemplates(cam.templates);
  if (!cam.operations.some((item) => item.id === cam.activeOperationId)) {
    cam.activeOperationId = cam.operations.length ? cam.operations[0].id : null;
    const active = cam.operations.find((item) => item.id === cam.activeOperationId);
    tree.select(cam.activeOperationId);
    camPanel.setOperation(active || null);
    if (active) drawOperationToolpath(active);
  }
  renderStatsForMode();
}

// ------------------------------------------------------------------ 导入
function openImport() {
  const limits = (catalog && catalog.import) || {};
  openImportDialog({
    modal,
    suffixes: limits.suffixes || [".step", ".stp"],
    maxBytes: limits.max_bytes || 32 * 1024 * 1024,
    onError: (message) => showBanner(message),
    onFile: async (file) => {
      setBusy(true, "正在解析 STEP 模型…");
      try {
        const response = await importStep(file);
        setMode("cam");
        await refreshModel({ frame: true });
        showBanner(
          `已导入 ${response.project.part.name}：${response.project.part.statistics.faces} 个面，` +
          `${response.project.part.statistics.triangles} 个三角面`,
          "info"
        );
      } catch (error) {
        showBanner("导入失败：" + error.message);
      } finally {
        setBusy(false);
      }
    },
  });
}

// ------------------------------------------------------------------ 毛坯
function previewStock(payload) {
  // 预览：只做一次轻量的后端计算，失败也不打断输入
  saveStock(payload.shape, payload.parameters)
    .then((stock) => {
      cam.stock = stock;
      viewport.setStockMesh(stock.mesh);
      renderStockNote(stock);
    })
    .catch((error) => showBanner("毛坯预览失败：" + error.message));
}

function commitStock(payload) {
  setBusy(true, "正在生成毛坯…");
  saveStock(payload.shape, payload.parameters)
    .then((stock) => {
      cam.stock = stock;
      camPanel.setStock(stock);
      viewport.setStockMesh(stock.mesh);
      viewport.resetView();
      renderStockNote(stock);
      showBanner(`毛坯已更新：${stock.label} ${stock.bounds.size.map((v) => v.toFixed(1)).join(" × ")} mm`, "info");
    })
    .catch((error) => showBanner("毛坯生成失败：" + error.message))
    .finally(() => setBusy(false));
}

function renderStockNote(stock) {
  const residual = (stock.residual_mm || []).map((value) => value.toFixed(1) + " mm").join(" / ");
  dom.stockButton.title = `毛坯 ${stock.label}：${stock.bounds.size.map((v) => v.toFixed(1)).join(" × ")} mm，余量 ${residual}`;
}

/** 同步"显示/隐藏毛坯"按钮文案与 viewport 当前状态。 */
function syncStockButton() {
  const shown = viewport.display && viewport.display.showStock !== false;
  dom.stockButton.textContent = shown ? "隐藏毛坯" : "显示毛坯";
  dom.stockButton.title = shown
    ? "点击隐藏毛坯网格（毛坯仍参与加工面/刀路计算）"
    : "点击显示毛坯网格";
}

// ------------------------------------------------------------------ 工序
async function createOperation() {
  if (!cam.model) {
    showBanner("请先导入 STEP 模型");
    return;
  }
  // 曲面加工（平行行切 / 等高铣）可以不选面：不选就是整个零件，
  // 选了面就只加工这些面（平行行切按面裁剪网格，等高铣按整层剖切）。
  const needsFaces = camPanel.needsFaces();
  if (needsFaces && !cam.selectedFaces.length) {
    showBanner("请先在三维视图中选择加工面（打开左上角「拾取面」，点击面）");
    return;
  }
  setBusy(true, "正在生成刀路…");
  try {
    const response = await addOperation({
      kind: camPanel.state.kind,
      faces: cam.selectedFaces,
      parameters: camPanel.parameters(),
    });
    cam.activeOperationId = response.operation.id;
    await refreshOperations();
    if (response.result) {
      applyCamResult(response.result);
    }
    showBanner(`已新增工序「${response.operation.name}」`, "info");
  } catch (error) {
    showBanner("新增工序失败：" + error.message);
  } finally {
    setBusy(false);
  }
}

function selectOperation(operation) {
  cam.activeOperationId = operation ? operation.id : null;
  camPanel.setOperation(operation);
  if (!operation) return;
  cam.selectedFaces = (operation.faces || []).map(Number);
  viewport.setSelectedFaces(cam.selectedFaces);
  updatePickHint();
  drawOperationToolpath(operation);
}

async function drawOperationToolpath(operation) {
  try {
    const response = await generateOperation(operation.id);
    // 实验台模式下不碰视口：刀路分组是两种模式共用的，画上去会把实验台的刀路顶掉，
    // 包围盒也会被改成 CAM 的范围。这里只缓存，切到 CAM 时再画。
    if (mode !== "cam") {
      cam.lastResult = response.result;
      return;
    }
    applyCamResult(response.result);
  } catch (error) {
    showBanner(`工序「${operation.name}」无法生成刀路：${error.message}`);
  }
}

/**
 * 同步播放条「重置」按钮的显示与可点状态。
 *
 * 按钮只在 CAM 模式出现，而且**只有真有一份切削仿真可退**时才可点：没跑过仿真
 * 就没有余料可删，与其点下去弹一句"没有仿真"，不如直接灰着。
 *
 * 仿真状态的改动集中在两处——`runSimulation`（建）与 `invalidateSimulation`（销）；
 * 换工程 / 关工程会直接把 `cam.simulation` 清掉，所以那两处也要补一句同步。
 */
function syncSimulationControls() {
  dom.simReset.hidden = mode !== "cam";
  dom.simReset.disabled = mode !== "cam" || !cam.simulation;
}

/**
 * 作废上一次的切削仿真：刀路变了（重新生成 / 换工序 / 新增工序）就调用。
 *
 * 仿真结果是按**旧刀路**算的——还留在视口里会与新刀路叠在一起、影响观察；
 * 播放头、进度条、统计面板也全是旧数据。这里连数据带视口一起清干净：
 * 统计随 renderStats 回到刀路本身，再点「切削仿真」就是对新刀路的全新计算。
 *
 * 被删掉的只有**仿真余料**：原始毛坯的网格不动，它与刀路的显隐本就由工具栏按钮
 * 与面板勾选控制。仿真期间被自动关掉的那两个开关在这里一并还原——关它们是为了
 * 给余料腾地方，仿真一没就该回到"零件 + 毛坯 + 刀路"的默认外观（播放条「重置」
 * 要的正是这个效果）。只在**确有一份仿真被作废**时才还原：没有仿真时的开关是
 * 用户自己调的，与仿真无关。
 */
function invalidateSimulation() {
  const hadSimulation = Boolean(cam.simulation);
  cam.simulation = null;
  cam.simulationFrame = 0;
  cam.playing = false;
  simPath = null;
  simClock = 0;
  simAnchor = { index: -1, swept: 0 };
  viewport.clearSimulation();
  dom.play.textContent = "▶";
  dom.scrub.value = "0";
  dom.time.textContent = "0.00 / 0.00 s";
  if (hadSimulation) {
    camPanel.syncDisplay("showPath", true);
    camPanel.syncDisplay("showStock", true);
  }
  syncSimulationControls();
}

/**
 * 退出切削仿真，回到"生成刀路"环节（播放条「重置」按钮）。
 *
 * 仿真看完效果不理想时的出口：余料连数据带网格一起删掉（`invalidateSimulation`），
 * 刀路与毛坯的显示重新点亮，统计回到刀路本身，进度条与播放头复位——
 * 参数面板随即可以随便改，再点「生成刀路」就是对新参数的新结果。
 */
function resetSimulation() {
  if (!cam.simulation) {
    showBanner("当前没有切削仿真：先点顶栏「切削仿真」", "info");
    return;
  }
  invalidateSimulation();
  if (cam.lastResult) renderStats(cam.lastResult);
  else renderStatsForMode();
  showBanner("已退出切削仿真：仿真余料已删除，可改参数后重新生成刀路", "info");
}

/** 把一道 CAM 工序的结果画进视口（只在 CAM 模式下调用）。 */
function applyCamResult(result) {
  // 画新刀路 = 上一次仿真作废（旧余料叠着新刀路会干扰观察）
  invalidateSimulation();
  // 点「生成刀路」就是要看刀路：仿真期间它被自动关掉过、或用户手动隐藏过，
  // 这里一律重新点亮——"点了生成却什么都看不见"最容易被当成按钮没反应。
  camPanel.syncDisplay("showPath", true);
  cam.lastResult = result;
  viewport.setTool(result.tool);
  viewport.setPathOnly(result.toolpath);
  renderStats(result);
  if (result.warnings && result.warnings.length) showBanner(result.warnings.join("；"));
}

async function generateOne(operationId) {
  if (!operationId) {
    showBanner("请先选择一道工序");
    return;
  }
  setBusy(true, "正在生成刀路…");
  try {
    await commitPanelEdits(operationId);
    const response = await generateOperation(operationId);
    applyCamResult(response.result);
    await refreshOperations();
    showBanner("刀路已生成", "info");
  } catch (error) {
    showBanner("生成失败：" + error.message);
  } finally {
    setBusy(false);
  }
}

/**
 * 把参数面板里**刚被改过**的值落盘到当前工序。
 *
 * 面板编辑只改内存里的 `camPanel.state.values`，不会自动保存（避免每拖一下滑块就写盘）。
 * 如果"生成刀路"直接用后端的旧参数重新算，用户改了刀具直径 / 步距 / 切深再点生成，
 * 刀路看起来"完全没反应" —— 这正是之前的问题。生成前先对齐一次，改了才发请求。
 */
async function commitPanelEdits(operationId) {
  const operation = cam.operations.find((item) => item.id === operationId);
  if (!operation) return;
  const edited = camPanel.parameters();
  const kind = camPanel.state.kind;
  const changes = {};
  if (JSON.stringify(edited) !== JSON.stringify(operation.parameters || {})) {
    changes.parameters = edited;
  }
  if (kind && kind !== operation.kind) changes.kind = kind;
  if (!Object.keys(changes).length) return;
  const response = await updateOperation(operationId, changes);
  const index = cam.operations.findIndex((item) => item.id === operationId);
  if (index >= 0) cam.operations[index] = response.operation;
  tree.setOperations({ operations: cam.operations, templates: cam.templates });
}

async function generateAll() {
  if (!cam.operations.length) {
    showBanner("还没有工序");
    return;
  }
  setBusy(true, "正在按工序顺序生成全部刀路…");
  try {
    // 当前选中那道工序的面板参数也可能刚改过，批量生成前同样要落盘
    if (cam.activeOperationId) await commitPanelEdits(cam.activeOperationId);
    const response = await generateAllOperations();
    const failed = (response.results || []).filter((item) => !item.ok);
    await refreshOperations();
    const active = cam.operations.find((item) => item.id === cam.activeOperationId);
    if (active) await drawOperationToolpath(active);
    showBanner(
      failed.length
        ? `有 ${failed.length} 道工序失败：${failed.map((item) => item.name).join("、")}`
        : `已生成 ${(response.results || []).length} 道工序`,
      failed.length ? "error" : "info"
    );
  } catch (error) {
    showBanner("批量生成失败：" + error.message);
  } finally {
    setBusy(false);
  }
}

async function handleOperationChange(id, changes) {
  try {
    const response = await updateOperation(id, changes);
    const index = cam.operations.findIndex((item) => item.id === id);
    if (index >= 0) cam.operations[index] = response.operation;
    tree.setOperations({ operations: cam.operations, templates: cam.templates });
    if (id === cam.activeOperationId) camPanel.setOperation(response.operation);
    if (changes.parameters || changes.faces) await drawOperationToolpath(response.operation);
  } catch (error) {
    showBanner("更新工序失败：" + error.message);
  }
}

async function reorder(direction) {
  const id = cam.activeOperationId;
  if (!id) return;
  const index = cam.operations.findIndex((item) => item.id === id);
  const target = Math.max(0, Math.min(cam.operations.length - 1, index + direction));
  if (target === index) return;
  try {
    await moveOperation(id, target);
    await refreshOperations();
  } catch (error) {
    showBanner("排序失败：" + error.message);
  }
}

async function toggleOperation() {
  const operation = cam.operations.find((item) => item.id === cam.activeOperationId);
  if (!operation) return;
  await handleOperationChange(operation.id, { enabled: !operation.enabled });
}

async function copyOperation() {
  if (!cam.activeOperationId) return;
  try {
    const response = await duplicateOperation(cam.activeOperationId);
    cam.activeOperationId = response.operation.id;
    await refreshOperations();
    showBanner(`已复制工序「${response.operation.name}」`, "info");
  } catch (error) {
    showBanner("复制失败：" + error.message);
  }
}

async function removeOperation() {
  if (!cam.activeOperationId) return;
  const operation = cam.operations.find((item) => item.id === cam.activeOperationId);
  try {
    await deleteOperation(cam.activeOperationId);
    cam.activeOperationId = null;
    await refreshOperations();
    viewport.clearToolpath();
    showBanner(`已删除工序「${operation ? operation.name : ""}」`, "info");
  } catch (error) {
    showBanner("删除失败：" + error.message);
  }
}

function exportTree() {
  const lines = cam.operations.map((operation) => {
    const parameters = Object.entries(operation.parameters || {})
      .map(([key, value]) => `${key}=${value}`)
      .join(", ");
    return [
      `${operation.sequence + 1}\t${operation.name}\t${operation.kind_label}\t${operation.state_label}`,
      `\t面: ${(operation.faces || []).map((id) => "#" + id).join(" ") || "-"}`,
      `\t参数: ${parameters}`,
      `\t统计: ${JSON.stringify(operation.statistics || {})}`,
    ].join("\n");
  });
  const text = `工序表\t${new Date().toLocaleString()}\n\n` + lines.join("\n\n") + "\n";
  downloadText("operations.txt", text);
  showBanner("已导出工序表", "info");
}

async function handleTemplateSave(name, body) {
  if (name === "__delete__") {
    try {
      await deleteTemplate(body.id);
      await refreshOperations();
      showBanner("模板已删除", "info");
    } catch (error) {
      showBanner("删除模板失败：" + error.message);
    }
    return;
  }
  try {
    await saveTemplate(name, body.kind, body.parameters);
    await refreshOperations();
    showBanner(`已保存模板「${name}」`, "info");
  } catch (error) {
    showBanner("保存模板失败：" + error.message);
  }
}

async function persistController(values) {
  try {
    await saveParameters(undefined, values);
  } catch (error) {
    showBanner("后处理参数保存失败：" + error.message);
  }
}

// ------------------------------------------------------------------ 仿真
//
// 播放模型：**锚帧 + 帧间连续扫掠**。
//
// 旧做法按固定 12fps 逐帧切换（帧距可达几十 mm），慢放时刀具与材料一起跳，一卡一卡；
// 想更细就得加帧，而每帧是一张全栅格高度图——帧数一多，后端计算/序列化、传输、前端
// 预建网格的耗时全部线性上涨。
//
// 现在播放头是连续时间（每 rAF tick 推进），锚帧只是"快照重置点"：帧间多出来的时间
// 由刀路本身补——刀具沿真实刀路按里程插值移动，材料在锚帧快照上按刀具扫过的刀路段
// 实时压低（viewport.sweepSimulation，公式与后端切削逐字一致）。于是：
//   - 动画步长 = 一帧渲染，与仿真帧数无关 → 慢放快放都连续；
//   - 材料按真实扫掠渐进被切走，比"帧跳变"更贴近真机，质量不降反升；
//   - 不需要为了流畅而加帧，计算时间反而变短。
let simPath = null;               // 刀路里程索引（{moves, total}）
let simClock = 0;                 // 播放头时间（s）
let simAnchor = { index: -1, swept: 0 };  // 当前锚帧与已扫掠到的里程

/** 把 toolpath payload 变成"里程 → 位置"索引：逐 move 记起点里程与段累计里程。 */
function buildSimPath(toolpath) {
  const moves = [];
  let total = 0;
  for (const move of (toolpath && toolpath.moves) || []) {
    const points = move.points || [];
    if (points.length < 2) continue;
    const flat = new Float64Array(points.length * 3);
    const cum = new Float64Array(points.length - 1);
    let length = 0;
    for (let k = 0; k < points.length; k += 1) {
      flat[k * 3] = points[k][0];
      flat[k * 3 + 1] = points[k][1];
      flat[k * 3 + 2] = points[k][2];
      if (k > 0) {
        length += Math.hypot(flat[k * 3] - flat[(k - 1) * 3],
          flat[k * 3 + 1] - flat[(k - 1) * 3 + 1],
          flat[k * 3 + 2] - flat[(k - 1) * 3 + 2]);
        cum[k - 1] = length;
      }
    }
    moves.push({ kind: move.kind, points: flat, cum, start: total, length });
    total += length;
  }
  return { moves, total };
}

/** 帧的里程（旧响应没有 travelled_mm 时按帧序号线性兜底）。 */
function frameMiles(frames, index) {
  const frame = frames[index];
  if (frame && typeof frame.travelled_mm === "number") return frame.travelled_mm;
  const total = simPath ? simPath.total : 0;
  return frames.length > 1 ? (index / (frames.length - 1)) * total : 0;
}

/** 时刻 → 里程：在锚帧与下一帧之间线性插值（两端都由帧自带，锚帧边界上精确对齐）。 */
function simMilesAt(frames, anchor, clock) {
  const next = Math.min(anchor + 1, frames.length - 1);
  const t0 = frames[anchor].time_s;
  const t1 = frames[next].time_s;
  const s0 = frameMiles(frames, anchor);
  const s1 = frameMiles(frames, next);
  if (next === anchor || t1 <= t0) return s0;
  const ratio = Math.min(Math.max((clock - t0) / (t1 - t0), 0), 1);
  return s0 + (s1 - s0) * ratio;
}

/** 里程 → 刀路位置（二分 move → 二分段 → 段内插值）。 */
function simStateAt(path, miles) {
  const moves = path.moves;
  if (!moves.length) return { position: [0, 0, 0], kind: "cut" };
  const s = Math.min(Math.max(miles, 0), path.total);
  let low = 0;
  let high = moves.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (moves[mid].start <= s) low = mid;
    else high = mid - 1;
  }
  const move = moves[low];
  const local = Math.min(Math.max(s - move.start, 0), move.length);
  let a = 0;
  let b = move.cum.length - 1;
  while (a < b) {
    const mid = (a + b) >> 1;
    if (move.cum[mid] >= local) b = mid;
    else a = mid + 1;
  }
  const segStart = a > 0 ? move.cum[a - 1] : 0;
  const segLength = move.cum[a] - segStart;
  const t = segLength > 1e-9 ? (local - segStart) / segLength : 0;
  const p = move.points;
  const i0 = a * 3;
  const i1 = (a + 1) * 3;
  return {
    position: [p[i0] + (p[i1] - p[i0]) * t,
      p[i0 + 1] + (p[i1 + 1] - p[i0 + 1]) * t,
      p[i0 + 2] + (p[i1 + 2] - p[i0 + 2]) * t],
    kind: move.kind,
  };
}

/** 里程区间 [from, to] → 刀路段列表 [[ax,ay,az,bx,by,bz,rapid], …]（跨 move 拼接）。 */
function simSegmentsBetween(path, from, to) {
  const segments = [];
  if (!(to > from) || !path.moves.length) return segments;
  const moves = path.moves;
  let start = 0;
  let high = moves.length - 1;
  while (start < high) {
    const mid = (start + high + 1) >> 1;
    if (moves[mid].start <= from + 1e-9) start = mid;
    else high = mid - 1;
  }
  const pointAt = (move, distance, segment) => {
    const s0 = segment > 0 ? move.cum[segment - 1] : 0;
    const segLength = move.cum[segment] - s0;
    const t = segLength > 1e-9 ? (distance - s0) / segLength : 0;
    const p = move.points;
    const i0 = segment * 3;
    const i1 = (segment + 1) * 3;
    return [p[i0] + (p[i1] - p[i0]) * t,
      p[i0 + 1] + (p[i1 + 1] - p[i0 + 1]) * t,
      p[i0 + 2] + (p[i1 + 2] - p[i0 + 2]) * t];
  };
  for (let m = start; m < moves.length; m += 1) {
    const move = moves[m];
    if (move.start > to) break;
    const lo = Math.max(from, move.start);
    const hi = Math.min(to, move.start + move.length);
    if (hi <= lo + 1e-9) continue;
    const rapid = move.kind === "rapid" ? 1 : 0;
    const fromLocal = lo - move.start;
    const toLocal = hi - move.start;
    let segment = 0;
    while (segment < move.cum.length - 1 && move.cum[segment] < fromLocal - 1e-9) segment += 1;
    let previous = fromLocal;
    for (let k = segment; k < move.cum.length; k += 1) {
      const segEnd = move.cum[k];
      if (toLocal <= segEnd + 1e-9) {
        segments.push([...pointAt(move, previous, k), ...pointAt(move, toLocal, k), rapid]);
        previous = toLocal;
        break;
      }
      if (segEnd > previous + 1e-9) {
        segments.push([...pointAt(move, previous, k), ...pointAt(move, segEnd, k), rapid]);
      }
      previous = segEnd;
    }
  }
  return segments;
}

/**
 * 把播放头时间 ``clock`` 渲染出来：定位锚帧 → 必要时重置/扫掠 → 更新刀具与进度条。
 * 播放、拖动、单步全部走这里，因此任何时刻的画面都由同一个函数决定。
 */
function renderSimulation(clock) {
  const sim = cam.simulation;
  if (!sim || !simPath) return;
  const frames = sim.frames || [];
  if (!frames.length) return;
  const duration = sim._duration || 0;
  const clamped = Math.min(Math.max(clock, 0), duration);

  // 锚帧 = 最后一个 time <= 播放头的帧（时间精确定位，切帧时刻不会前后抖）
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (frames[mid].time_s <= clamped + 1e-9) low = mid;
    else high = mid - 1;
  }
  const anchor = low;
  const miles = simMilesAt(frames, anchor, clamped);

  if (anchor !== simAnchor.index || miles < simAnchor.swept - 1e-6) {
    // 换锚帧 / 往回拖：从帧快照整体重置（前一锚帧上"预扫"的内容在这里被覆盖）
    viewport.setSimulationFrame(anchor);
    simAnchor = { index: anchor, swept: frameMiles(frames, anchor) };
  }
  if (miles > simAnchor.swept + 1e-9) {
    const segments = simSegmentsBetween(simPath, simAnchor.swept, miles);
    if (segments.length) {
      viewport.sweepSimulation(segments, sim.tool ? sim.tool.radius_mm : 5);
    }
    simAnchor.swept = miles;
  }

  viewport.setPlayhead(simStateAt(simPath, miles).position, 0);
  cam.simulationFrame = anchor;
  if (!scrubbing) dom.scrub.value = String(duration > 0 ? clamped / duration : 0);
  dom.time.textContent = `${clamped.toFixed(2)} / ${duration.toFixed(2)} s`;
}

async function runSimulation() {
  if (!cam.model) {
    showBanner("请先导入模型并生成工序");
    return;
  }
  if (!cam.operations.length) {
    showBanner("还没有工序：先拾取加工面并新增工序");
    return;
  }
  setBusy(true, "正在计算毛坯切除仿真…");
  try {
    // 不写死 cell_mm：后端按毛坯大小自适应（200 mm 的件给 0.5 mm 会变成 40 万格、
    // 响应几十 MB，仿真要几分钟）。默认 max_frames 调大让动画更细。
    const payload = { operation_id: cam.activeOperationId || undefined, max_frames: 180 };
    const result = await simulate(payload);
    const frames = result.frames || [];
    result._duration = frames.length ? frames[frames.length - 1].time_s : 0;
    cam.simulation = result;
    cam.simulationFrame = 0;
    cam.playing = false;
    syncSimulationControls();
    playback.load(null);
    simPath = buildSimPath(result.toolpath);
    simClock = 0;
    simAnchor = { index: -1, swept: 0 };
    // 几何拓扑只建一次（与帧无关），帧只是 height 快照——不再是每帧预建一个网格
    viewport.precomputeSimulationFrames(result.grid, frames);
    // 毛坯默认让位给仿真结果（毛坯不透明度 0.85，两块料互相遮挡）：走统一
    // 显示状态隐藏，按钮与面板勾选同步更新——之后到再次点「切削仿真」之前，
    // 用户随时可以再显示/隐藏（旧实现在视口里硬性隐藏，按钮点了没反应）。
    camPanel.syncDisplay("showStock", false);
    // 刀路同理自动让位：整屏刀线会盖住正在成形的切除体。同样只改显示状态，
    // 想看刀线随时在面板「显示·刀路」里勾回来，与毛坯走的是同一条同步路径。
    camPanel.syncDisplay("showPath", false);
    if (result.toolpath) drawToolpath(result.toolpath, { keepTool: true });
    renderSimulationStats(result);
    if (result.summary && result.summary.warnings && result.summary.warnings.length) {
      showBanner(result.summary.warnings.join("；"));
    } else {
      showBanner(`仿真完成：切除 ${result.summary.removed_volume_mm3.toFixed(0)} mm³`, "info");
    }
    renderSimulation(0);
    dom.play.textContent = "❚❚";
    cam.playing = true;
  } catch (error) {
    showBanner("仿真失败：" + error.message);
  } finally {
    setBusy(false);
  }
}

/** 单步/停止：跳到锚帧 ``index``（显示帧快照本身，不做帧间扫掠）。 */
function seekSimulation(index) {
  const frames = cam.simulation ? cam.simulation.frames || [] : [];
  if (!frames.length) return;
  cam.playing = false;
  const clamped = Math.max(0, Math.min(index, frames.length - 1));
  simClock = frames[clamped].time_s;
  renderSimulation(simClock);
  dom.play.textContent = "▶";
}

/** 拖动进度条：按时间比例定位（锚帧之间的空档照样被连续扫掠补出来）。 */
function seekSimulationTime(progress) {
  const duration = cam.simulation ? cam.simulation._duration || 0 : 0;
  cam.playing = false;
  simClock = Math.min(Math.max(progress, 0), 1) * duration;
  renderSimulation(simClock);
  dom.play.textContent = "▶";
}

function toggleSimulation() {
  if (!cam.simulation) {
    runSimulation();
    return;
  }
  const duration = cam.simulation._duration || 0;
  cam.playing = !cam.playing;
  if (cam.playing && simClock >= duration - 1e-9) {
    simClock = 0;
    renderSimulation(0);
  }
  dom.play.textContent = cam.playing ? "❚❚" : "▶";
}

function advanceSimulation(dt) {
  if (!cam.playing || !cam.simulation) return;
  const duration = cam.simulation._duration || 0;
  simClock += dt * (Number(dom.speed.value) || 1);
  if (simClock >= duration) {
    simClock = duration;
    cam.playing = false;
    dom.play.textContent = "▶";
  }
  renderSimulation(simClock);
}

async function exportNc() {
  try {
    const name = await downloadNc({ operation_id: cam.activeOperationId || undefined });
    showBanner("已导出 " + name, "info");
  } catch (error) {
    showBanner("导出失败：" + error.message);
  }
}

// ------------------------------------------------------------------ 工程
async function openProjectDialog() {
  const list = document.createElement("div");
  list.className = "project-list";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "button tiny danger";
  close.textContent = "关闭当前工程";
  close.addEventListener("click", async () => {
    try {
      await closeProject();
      cam.model = null;
      cam.stock = null;
      cam.operations = [];
      cam.simulation = null;
      cam.activeOperationId = null;
      syncSimulationControls();
      tree.setOperations({ operations: [], templates: [] });
      viewport.clearToolpath();
      viewport.clearSimulation();
      modal.close();
      showBanner("已关闭工程", "info");
    } catch (error) {
      showBanner(error.message);
    }
  });

  try {
    const projects = await fetchProjects();
    if (!projects.projects.length) {
      const empty = document.createElement("p");
      empty.className = "note";
      empty.textContent = "还没有保存过的工程：导入一个 STEP 模型就会自动创建。";
      list.appendChild(empty);
    }
    for (const item of projects.projects) {
      const row = document.createElement("div");
      row.className = "project-row";
      const info = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = item.name || item.id;
      const meta = document.createElement("small");
      meta.textContent = `${item.part_name || ""} · ${item.operations || 0} 道工序 · ${item.updated_at || ""}`;
      info.append(name, meta);
      const open = document.createElement("button");
      open.type = "button";
      open.className = "button tiny";
      open.textContent = "打开";
      open.addEventListener("click", async () => {
        setBusy(true, "正在打开工程…");
        try {
          await openProject(item.id);
          await refreshModel({ frame: true });
          modal.close();
          showBanner(`已打开工程「${item.name}」`, "info");
        } catch (error) {
          showBanner("打开失败：" + error.message);
        } finally {
          setBusy(false);
        }
      });
      row.append(info, open);
      list.appendChild(row);
    }
  } catch (error) {
    showBanner("无法读取工程列表：" + error.message);
  }

  modal.open({ title: "工程", body: list, footer: close });
}

// ------------------------------------------------------------------ 刀路绘制
function drawToolpath(toolpath, options = {}) {
  // 刀路分组是两种模式共用的；实验台模式下不许 CAM 结果覆盖它
  if (mode !== "cam") return;
  viewport.setPathOnly(toolpath);
  if (!options.keepTool) viewport.setTool(cam.simulation ? cam.simulation.tool : currentTool());
}

function currentTool() {
  const operation = cam.operations.find((item) => item.id === cam.activeOperationId);
  if (!operation) return viewport.tool;
  const diameter = Number(operation.parameters.tool_diameter_mm || 10);
  const length = Number(operation.parameters.tool_length_mm || 40);
  return { diameter_mm: diameter, radius_mm: diameter / 2, length_mm: length, kind_label: "平底刀" };
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
  // 统计面板只在 CAM 模式显示；实验台的统计由 renderBenchStats 负责
  if (mode !== "cam") return;
  const stats = result.toolpath ? result.toolpath.statistics : result.statistics;
  const rows = [
    ["工序", result.kind_label || (result.toolpath && result.toolpath.planner_label) || "—"],
    ["刀轨", String(stats.pass_count ?? 0)],
    ["刀点", String(stats.point_count ?? 0)],
    ["切削长度", (stats.cut_length_mm || 0).toFixed(1) + " mm"],
    ["预计工时", seconds(stats.estimated_time_s || 0)],
  ];
  // 斜面/曲面型腔：把底面几何摊开写清楚，否则用户只看到"刀路在动"，
  // 不知道刀轴 Z 是跟着底面走的、也不知道最深到哪。
  const floor = (result.regions && result.regions.length)
    ? (result.regions[0].floor || null) : null;
  if (floor && !floor.flat) {
    const isCurved = floor.planar === false;
    rows.push([
      isCurved ? "底面（曲面）" : "底面（斜面）",
      `坡度 ≤ ${(floor.max_slope_deg ?? 0).toFixed(1)}°`,
    ]);
    rows.push(["底面高度", `${(floor.z_min ?? 0).toFixed(2)} ~ ${(floor.z_max ?? 0).toFixed(2)} mm`]);
  }
  if (result.statistics && result.statistics.volume_deviation !== undefined
      && result.statistics.volume_deviation !== null) {
    rows.push(["体积偏差", (result.statistics.volume_deviation * 100).toFixed(1) + " %"]);
  }
  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    for (const node of statRow(label, value)) list.appendChild(node);
  }
  const heading = document.createElement("h4");
  heading.textContent = "CAM 刀路";
  const container = document.createElement("div");
  container.append(heading, list);
  dom.stats.replaceChildren(container);
}

function renderSimulationStats(result) {
  if (!result) return;
  const summary = result.summary || {};
  const rows = [
    ["工序", result.kind_label || (result.toolpath && result.toolpath.planner_label) || "—"],
    ["帧", String(summary.frame_count ?? (result.frames || []).length)],
    ["毛坯体积", (summary.initial_volume_mm3 || 0).toFixed(0) + " mm³"],
    ["已切除", (summary.removed_volume_mm3 || 0).toFixed(0) + " mm³"],
    ["切除率", ((summary.removed_ratio || 0) * 100).toFixed(1) + " %"],
    ["剩余", (summary.remaining_volume_mm3 || 0).toFixed(0) + " mm³"],
  ];
  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    for (const node of statRow(label, value)) list.appendChild(node);
  }
  const heading = document.createElement("h4");
  heading.textContent = "切削仿真";
  const container = document.createElement("div");
  container.append(heading, list);
  dom.stats.replaceChildren(container);
}

function renderStockNoteFromPayload() {
  if (cam.stock) renderStockNote(cam.stock);
}

function renderStatsForMode() {
  if (mode === "cam" && cam.simulation) renderSimulationStats(cam.simulation);
  else if (mode === "cam" && !cam.operations.length) dom.stats.replaceChildren();
  else if (mode === "bench" && lastResult) renderBenchStats(lastResult);
}

function renderBenchStats(result) {
  const stats = result.toolpath.statistics;
  const region = result.region;
  const size = region.id === "circle"
    ? "直径 " + (region.bounds_mm[0][1] - region.bounds_mm[0][0]).toFixed(0) + " mm"
    : (region.bounds_mm[0][1] - region.bounds_mm[0][0]).toFixed(0) + " × "
      + (region.bounds_mm[1][1] - region.bounds_mm[1][0]).toFixed(0) + " mm";
  const rows = [
    ["区域", size],
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
  if (mode !== "bench") return;
  if (!scrubbing) dom.scrub.value = String(state.progress);
  dom.time.textContent = state.time.toFixed(2) + " / " + state.duration.toFixed(2) + " s";
  dom.play.textContent = state.playing ? "❚❚" : "▶";
}

// ------------------------------------------------------------------ 循环
let previousTime = 0;

function animate(now) {
  const dt = previousTime ? Math.min((now - previousTime) / 1000, 0.1) : 0;
  previousTime = now;
  if (mode === "bench") {
    const state = playback.update(dt * (Number(dom.speed.value) || 1));
    if (state && playback.timeline) {
      viewport.setPlayhead(state.position, state.index);
      if (playback.playing || scrubbing) renderPlaybar(state);
    }
  } else {
    advanceSimulation(dt);
  }
  viewport.render();
  requestAnimationFrame(animate);
}

boot();
