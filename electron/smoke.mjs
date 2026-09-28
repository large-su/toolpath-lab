// 无头自检：拉起真实窗口加载界面，收集控制台错误与关键 DOM 状态，然后走一遍 CAM 主链路。
//
// 这是"改了前端但没打开过页面"的兜底检查：ES 模块的导入错误、DOM id 拼错、
// 面板构造函数抛异常、接口与前端对不上，都会在这里以非零退出码暴露出来。
//
// 用法：node electron/smoke.mjs
// 环境变量：TOOLPATH_LAB_PYTHON 指定解释器（与主程序一致）。
//
// 实现说明：页面里的请求统一用 **相对地址 + XMLHttpRequest**，并在页面脚本里发；
// 主进程只负责读文件与发 multipart（Node 的 fetch + FormData）。
// 这样自检脚本自身不依赖任何前端内部实现，只走真实接口。

import { app, BrowserWindow } from "electron";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TIMEOUT_MS = 60000;
const python = process.env.TOOLPATH_LAB_PYTHON || "python";

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];

//: 浏览器/Electron 自身的良性告警（不是应用错误），不计入失败。
//: "Electron Security Warning" 是开发态（未打包）才会出现的 CSP 提示，打包后自动消失。
const IGNORED_CONSOLE = [
  "ResizeObserver loop",
  "Download the React DevTools",
  "Electron Security Warning",
];

function isRealError(message) {
  return !IGNORED_CONSOLE.some((pattern) => String(message).includes(pattern));
}

/** 在页面里注入请求小工具（相对地址 + XHR）。 */
const PAGE_HTTP = `
  window.__get = (url) => new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", url, true);
    xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText });
    xhr.onerror = () => resolve({ status: 0, text: "" });
    xhr.send();
  });
  true;
`;

/**
 * 带重试的请求。
 *
 * 注意：这里**不再**对 501 做重试。曾经以为 501（请求行变成 `{}POST`）是 Electron 的怪癖，
 * 实际上是服务端漏读请求体导致的持久连接错位；已修复并有 KeepAliveTests 守着。
 * 再重试只会把这个 bug 藏起来。
 */
async function request(url, options = {}) {
  const response = await fetch(url, options);
  return { status: response.status, text: await response.text() };
}

function json(text, fallback = {}) {
  try {
    return JSON.parse(text);
  } catch (error) {
    return fallback;
  }
}

function findFreePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

async function waitForBackend(url, deadline) {
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url + "api/health");
      if (response.ok) return true;
    } catch (error) {
      /* 还没起来 */
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  return false;
}

/** 在页面里等一个条件成立（用于等异步的导入 / 生成完成）。 */
async function waitInPage(window, expression, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await window.webContents.executeJavaScript(expression);
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return null;
}

/**
 * 走一遍 CAM 主链路，全部通过页面里的真实代码路径：
 * 打开导入对话框 → 上传示例 STEP → 前端拉模型并按面渲染
 * → 触发面拾取回调 → 点"生成毛坯" → 新增型腔铣工序 → 生成刀路 → 切削仿真 → 导出 NC。
 *
 * 返回 { ok, steps }；失败时 steps 里保留已经完成的步骤与失败原因，便于定位。
 */
async function driveCamFlow(window, base) {
  const steps = {};
  const sample = path.join(projectRoot, "examples", "sample_plate.step");

  // 1. 切到 CAM 模式并打开导入对话框
  await window.webContents.executeJavaScript(`(() => {
    document.querySelector("#mode-switch button[data-mode='cam']").click();
    document.querySelector("#btn-import").click();
    return true;
  })()`);
  await new Promise((resolve) => setTimeout(resolve, 200));
  const hasInput = await window.webContents.executeJavaScript(
    `Boolean(document.querySelector("#modal input[type=file]"))`
  );
  steps.importDialog = hasInput;
  if (!hasInput) return { ok: false, steps, reason: "导入对话框里没有 file input" };

  // 2. 上传示例模型：multipart 由主进程发（Node 的 fetch + FormData），走真实接口
  const contents = await fs.readFile(sample);
  const form = new FormData();
  form.append("file", new Blob([contents], { type: "application/step" }), path.basename(sample));
  steps.upload = { status: 0 };
  try {
    const response = await request(new URL("api/import/step", base).href, {
      method: "POST", body: form,
    });
    const body = json(response.text);
    steps.upload = {
      status: response.status,
      faces: body.project ? body.project.part.statistics.faces : 0,
      size: body.project ? body.project.part.size_mm : null,
    };
    if (response.status !== 200) {
      return { ok: false, steps, reason: "STEP 导入返回 " + response.status };
    }
  } catch (error) {
    return { ok: false, steps, reason: "上传失败：" + (error && error.message) };
  }

  // 3. 前端拉模型并按面渲染（等价于点完导入对话框之后的刷新）
  const refreshed = await window.webContents.executeJavaScript(`(async () => {
    const app = window.toolpathLab;
    const model = await window.__get("api/model");
    if (model.status !== 200) {
      return { step: "model", status: model.status };
    }
    const part = JSON.parse(model.text).project.part;
    // 先检查数据本身：任何 NaN / 越界索引都会让前端的包围盒计算报警
    const bad = [];
    part.mesh.positions.forEach((row, index) => {
      if (!row.every((v) => Number.isFinite(v))) bad.push(index);
    });
    app.cam.model = part;
    app.viewport.setPart(part, { frame: true });
    const stock = await window.__get("api/stock");
    if (stock.status !== 200) return { step: "stock", status: stock.status };
    app.cam.stock = JSON.parse(stock.text);
    app.viewport.setStockMesh(app.cam.stock.mesh);
    return { ok: true, faces: app.viewport.faceMeshes.length, badPositions: bad };
  })()`);
  steps.render = refreshed;
  if (!refreshed || !refreshed.ok) {
    return { ok: false, steps, reason: "拉模型 / 渲染失败：" + JSON.stringify(refreshed) };
  }

  // 4. 通过真实的"面拾取回调"选中一个朝上的面（等价于在视图里点一下）
  const picked = await window.webContents.executeJavaScript(`(async () => {
    const app = window.toolpathLab;
    const response = await window.__get("api/model/features");
    if (response.status !== 200) return { step: "features", status: response.status };
    const features = JSON.parse(response.text).features;
    const top = features.find((f) => f.horizontal && f.normal[2] > 0.9);
    if (!top) return { step: "pick", reason: "没有朝上的平面" };
    app.viewport.onFacePick(top.face_id, { additive: false });
    return { faceId: top.face_id, selected: Array.from(app.viewport.selectedFaces) };
  })()`);
  steps.pick = picked;
  if (!picked || !picked.selected || picked.selected.length !== 1) {
    return { ok: false, steps, reason: "面拾取回调没有选中面：" + JSON.stringify(picked) };
  }

  // 5. 点"生成毛坯"（走界面上的按钮）
  const clicked = await window.webContents.executeJavaScript(`(() => {
    const button = Array.from(document.querySelectorAll("#cam-panel button"))
      .find((b) => b.textContent.includes("生成毛坯"));
    if (button) button.click();
    return Boolean(button);
  })()`);
  steps.stockButtonClicked = clicked;
  const stock = await waitInPage(window, `(() => {
    const app = window.toolpathLab;
    return app.cam.stock && app.viewport.stockGroup.children.length > 0
      ? { id: app.cam.stock.id, size: app.cam.stock.bounds.size } : null;
  })()`, 20000);
  steps.stock = stock;
  if (!stock) return { ok: false, steps, reason: "毛坯没有生成或没有进入视口" };

  // 6. 新增一道型腔铣工序（走界面的真实入口 + 前端参数面板当前值）
  const panelParameters = await window.webContents.executeJavaScript(
    `window.toolpathLab.camPanel.parameters()`
  );
  const createResponse = await request(new URL("api/operations", base).href, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "pocket_mill",
      faces: picked.selected,
      parameters: Object.assign({}, panelParameters, {
        tool_diameter_mm: 10, stepover_mm: 6, cut_depth_mm: 3,
        stock_allowance_mm: 0, finish_allowance_mm: 0, safe_height_mm: 10,
        cut_mode: "contour", finish_pass: false,
      }),
    }),
  });
  const createdBody = json(createResponse.text);
  const created = createResponse.status === 200 ? {
    id: createdBody.operation.id,
    name: createdBody.operation.name,
    state: createdBody.operation.state,
    moves: createdBody.result.toolpath.statistics.move_count,
    cut: createdBody.result.toolpath.statistics.cut_length_mm,
  } : { step: "create", status: createResponse.status };
  await window.webContents.executeJavaScript(`(() => {
    window.toolpathLab.cam.activeOperationId = ${JSON.stringify(createdBody.operation ? createdBody.operation.id : "")};
    return true;
  })()`);
  steps.operation = created;
  if (!created || !created.moves) {
    return { ok: false, steps, reason: "新增工序没有生成刀路：" + JSON.stringify(created) };
  }

  // 7. 把刀路画进视口（等价于工序树里选中该工序）
  const generateResponse = await request(
    new URL(`api/operations/${created.id}/generate`, base).href,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
  );
  const generateBody = json(generateResponse.text);
  const drawn = generateResponse.status === 200 ? await window.webContents.executeJavaScript(`(() => {
    const app = window.toolpathLab;
    const result = ${JSON.stringify(generateBody.result)};
    app.viewport.setTool(result.tool);
    app.viewport.setPathOnly(result.toolpath);
    return { pathGroups: app.viewport.pathGroup.children.length,
             moves: result.toolpath.moves.length };
  })()`) : { step: "generate", status: generateResponse.status };
  steps.toolpath = drawn;
  if (!drawn || drawn.pathGroups < 2) {
    return { ok: false, steps, reason: "刀路没有画进视口：" + JSON.stringify(drawn) };
  }

  // 8. 切削仿真：请求 → 画首帧与末帧
  const simulateResponse = await request(new URL("api/simulate", base).href, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operation_id: created.id, cell_mm: 1.0, max_frames: 12 }),
  });
  const simulateBody = json(simulateResponse.text);
  const simulated = simulateResponse.status === 200 ? await window.webContents.executeJavaScript(`(() => {
    const app = window.toolpathLab;
    const body = ${JSON.stringify({
      grid: simulateBody.grid,
      frameHeight: simulateBody.frames[0].height,
      lastHeight: simulateBody.frames[simulateBody.frames.length - 1].height,
      frameCount: simulateBody.frames.length,
      removed: simulateBody.summary ? simulateBody.summary.removed_volume_mm3 : 0,
    })};
    app.cam.simulation = { grid: body.grid, frames: [{ height: body.frameHeight }] };
    app.viewport.setSimulationMesh(body.grid, body.frameHeight);
    const children = app.viewport.simulationGroup.children.length;
    app.viewport.setSimulationMesh(body.grid, body.lastHeight);
    return {
      frames: body.frameCount, removed: body.removed,
      simulationChildren: children, grid: body.grid.rows + "x" + body.grid.cols,
    };
  })()`) : { step: "simulate", status: simulateResponse.status };
  steps.simulation = simulated;
  if (!simulated || simulated.simulationChildren < 1 || simulated.frames < 1) {
    return { ok: false, steps, reason: "仿真结果没有画进视口：" + JSON.stringify(simulated) };
  }

  // 9. 导出 NC
  const ncResponse = await request(new URL("api/export/nc", base).href, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operation_id: created.id }),
  });
  const ncText = ncResponse.text;
  const nc = { status: ncResponse.status, lines: ncText.split("\n").length,
               header: ncText.split("\n")[0] };
  steps.nc = nc;
  if (!nc || nc.status !== 200 || nc.lines < 10) {
    return { ok: false, steps, reason: "NC 导出失败：" + JSON.stringify(nc) };
  }

  // 9d. 斜面 / 曲面型腔：用真实接口在**倾斜底面**上建一道型腔铣。
  //     这一步专门验证"底面不是水平面"这条新路径：区域要带上高度场、刀路要按底面取 Z。
  const slopedPocket = await (async () => {
    const sample = path.join(projectRoot, "examples", "sample_plate.step");
    const contents2 = await fs.readFile(sample);
    const form2 = new FormData();
    form2.append("file", new Blob([contents2], { type: "application/step" }),
                 path.basename(sample));
    // 用示例零件的**侧面**当"倾斜底面"：它的法向不朝上，应当被拒绝；
    // 再找一个朝上但不水平的斜面，验证型腔铣接受它。
    const response = await request(new URL("api/model/features", base).href);
    const features = json(response.text).features || [];
    const inclined = features.filter(
      (item) => item.normal && item.normal[2] > 0.5 && item.normal[2] < 0.999
    );
    const downward = features.filter((item) => item.normal && item.normal[2] < -0.2);
    let downwardRejected = null;
    if (downward.length) {
      const rejected = await request(new URL("api/operations", base).href, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "pocket_mill", faces: [downward[0].face_id],
                               parameters: { tool_diameter_mm: 6 } }),
      });
      // 朝下的面必须被拒（422：几何上不可加工）。
      // 回 200 就说明"底面必须朝上"这条校验漏了。
      downwardRejected = rejected.status === 422;
    }
    if (!inclined.length) {
      // 示例零件是个方板，没有朝上的斜面：这一段只做"朝下的面被拒"这条检查。
      return { skipped: true, reason: "示例零件里没有朝上的斜面",
               featureCount: features.length, downwardCount: downward.length,
               downwardRejected: downwardRejected };
    }
    const target = inclined[0];
    const created2 = await request(new URL("api/operations", base).href, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: "pocket_mill", faces: [target.face_id],
        parameters: { tool_diameter_mm: 6, cut_depth_mm: 1, stepover_ratio: 0.5,
                      cut_mode: "contour", finish_pass: false },
      }),
    });
    const body2 = json(created2.text);
    if (created2.status !== 200) {
      return { status: created2.status, error: body2.error, face: target.face_id };
    }
    const region = (body2.result.regions || [])[0] || {};
    const cuts = (body2.result.toolpath.moves || []).filter((m) => m.kind === "cut");
    const zs = cuts.flatMap((m) => m.points.map((p) => p[2]));
    return {
      face: target.face_id,
      floorCapable: target.floor_capable,
      kinds: target.machinable_kinds,
      floor: region.floor || null,
      floorZ: region.floor_z,
      cutMoves: cuts.length,
      zSpread: zs.length ? Math.max(...zs) - Math.min(...zs) : 0,
      // 注意用外层算好的布尔（422 才算"被拒"）：rejected 是 if 块内的 const，
      // 出了块就取不到；这里原先写成不存在的 downwardResult，一旦样件上真有
      // 朝上的斜面（走到这个 return）就会 ReferenceError。
      downwardRejected: downwardRejected,
    };
  })();
  steps.slopedPocket = slopedPocket;
  // 示例零件是个方板，正常不会命中"斜底"那一段（会被 skip）；命中时把断言查完。
  // 无论哪种情况都必须确认："朝下的面不能当型腔底"。
  if (slopedPocket.downwardRejected === false) {
    return { ok: false, steps, reason: "朝下的面居然被接受为型腔底：" + JSON.stringify(slopedPocket) };
  }
  if (!slopedPocket.skipped) {
    if (!slopedPocket.floor || slopedPocket.floor.flat !== false) {
      return { ok: false, steps, reason: "倾斜底面的加工区域没有带上高度场："
        + JSON.stringify(slopedPocket) };
    }
    if (!(slopedPocket.cutMoves > 0) || !(slopedPocket.zSpread > 0.1)) {
      return { ok: false, steps, reason: "斜底型腔的刀路没有跟着底面起伏："
        + JSON.stringify(slopedPocket) };
    }
  }

  // 9e. 曲面加工（平行行切）：全程走真实界面 —— 参数面板的加工类型下拉框 → 新增工序按钮
  //     → 刀路画进视口 → 仿真。曲面工序**不需要选面**，所以要先把已选的面清掉再走一遍，
  //     否则测不出"不选面也能建"这条路径。
  const surface = await window.webContents.executeJavaScript(`(async () => {
    const app = window.toolpathLab;
    // 清空已选的面：第 4 步选过一个，这里把同一个面再点一次（additive = 取消选择）。
    // 曲面工序必须能在**一个面都没选**的情况下建立。
    const picked = Array.from(app.viewport.selectedFaces);
    if (picked.length) app.viewport.onFacePick(picked[0], { additive: true });

    const select = Array.from(document.querySelectorAll("#cam-panel select"))
      .find((item) => Array.from(item.options).some((o) => o.textContent.includes("平行行切")));
    if (!select) return { step: "select", reason: "参数面板里没有曲面加工类型" };
    select.value = "parallel_surface";
    select.dispatchEvent(new Event("change", { bubbles: true }));

    const labels = Array.from(document.querySelectorAll("#cam-panel .row > label"))
      .map((item) => item.textContent.trim());
    const kind = app.camPanel.state.kind;
    const parameters = app.camPanel.parameters();

    // 只认**这一步新建出来**的那道工序：数据目录里如果已经有曲面工序（例如上一次
    // 自检留下的），按类型找会立刻命中旧的，测出来的就不是"能不能新建"。
    const before = new Set(app.cam.operations.map((item) => item.id));
    document.querySelector("#btn-op-add").click();
    const deadline = Date.now() + 60000;
    let operation = null;
    while (Date.now() < deadline) {
      operation = app.cam.operations.find(
        (item) => item.kind === "parallel_surface" && !before.has(item.id)) || null;
      if (operation && operation.state === "generated") break;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    return {
      kind, labels, parameters, selectedFaces: app.cam.selectedFaces.length,
      operation: operation ? {
        id: operation.id, kind: operation.kind, faces: operation.faces,
        state: operation.state, movers: operation.statistics.move_count,
        cut: operation.statistics.cut_length_mm,
      } : null,
      pathGroups: app.viewport.pathGroup.children.length,
      toolKind: app.viewport.tool ? app.viewport.tool.kind : null,
    };
  })()`);
  steps.surface = surface;
  if (!surface || surface.kind !== "parallel_surface") {
    return { ok: false, steps, reason: "参数面板没有切到平行行切：" + JSON.stringify(surface) };
  }
  if (!surface.labels.includes("行距") || surface.labels.includes("层高")) {
    return { ok: false, steps,
             reason: "曲面参数面板没有按类型联动（应只有行距、没有层高）："
                     + JSON.stringify(surface.labels) };
  }
  if (!surface.operation || !surface.operation.movers) {
    return { ok: false, steps, reason: "不选面没能建出曲面工序：" + JSON.stringify(surface) };
  }
  if (surface.operation.faces.length) {
    return { ok: false, steps, reason: "曲面工序不该带上加工面：" + JSON.stringify(surface.operation) };
  }

  const surfaceSimulation = await request(new URL("api/simulate", base).href, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operation_id: surface.operation.id, cell_mm: 1.5, max_frames: 8 }),
  });
  const surfaceSimBody = json(surfaceSimulation.text);
  const surfaceSim = surfaceSimulation.status === 200 ? {
    kind: surfaceSimBody.kind,
    frames: surfaceSimBody.frames.length,
    removed: surfaceSimBody.summary ? surfaceSimBody.summary.removed_volume_mm3 : 0,
    cut: surfaceSimBody.toolpath.statistics.cut_length_mm,
  } : { step: "simulate", status: surfaceSimulation.status, body: surfaceSimBody };
  steps.surfaceSimulation = surfaceSim;
  if (!surfaceSim.frames || !surfaceSim.removed) {
    return { ok: false, steps, reason: "曲面刀路没能仿真：" + JSON.stringify(surfaceSim) };
  }

  // 9c. 等高铣：同样走界面，但**不选面**（等高铣按整个零件分层，BRep 由后端补）
  const waterline = await window.webContents.executeJavaScript(`(async () => {
    const app = window.toolpathLab;
    // 再清一次选中的面（第 9b 步的平行行切工序建完后，点工序会把它的面选回来）
    const picked = Array.from(app.viewport.selectedFaces);
    if (picked.length) app.viewport.onFacePick(picked[0], { additive: true });

    const select = Array.from(document.querySelectorAll("#cam-panel select"))
      .find((item) => Array.from(item.options).some((o) => o.textContent.includes("等高铣")));
    if (!select) return { step: "select", reason: "参数面板里没有等高铣" };
    select.value = "waterline";
    select.dispatchEvent(new Event("change", { bubbles: true }));

    const labels = Array.from(document.querySelectorAll("#cam-panel .row > label"))
      .map((item) => item.textContent.trim());
    const parameters = app.camPanel.parameters();
    const before = new Set(app.cam.operations.map((item) => item.id));
    document.querySelector("#btn-op-add").click();
    const deadline = Date.now() + 90000;
    let operation = null;
    while (Date.now() < deadline) {
      operation = app.cam.operations.find(
        (item) => item.kind === "waterline" && !before.has(item.id)) || null;
      if (operation && operation.state === "generated") break;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    return {
      labels, parameters, selectedFaces: app.cam.selectedFaces.length,
      operation: operation ? {
        id: operation.id, kind: operation.kind, faces: operation.faces,
        state: operation.state, movers: operation.statistics.move_count,
        cut: operation.statistics.cut_length_mm, warnings: operation.warnings,
      } : null,
    };
  })()`);
  steps.waterline = waterline;
  if (!waterline || !waterline.labels.includes("层高") || waterline.labels.includes("行距")) {
    return { ok: false, steps, reason: "等高铣参数面板不对：" + JSON.stringify(waterline) };
  }
  if (!waterline.operation || !waterline.operation.movers) {
    return { ok: false, steps, reason: "等高铣没能建出工序：" + JSON.stringify(waterline) };
  }

  // 10. 切回实验台：CAM 的零件/毛坯必须立刻消失（否则会压住规则工件，还会把相机带偏，
  //     现象就是"实验台的基础模型只剩一角"）。
  const benchState = await window.webContents.executeJavaScript(`(() => {
    document.querySelector("#mode-switch button[data-mode='bench']").click();
    const viewport = window.toolpathLab.viewport;
    const box = viewport.bounds;
    return {
      mode: document.querySelector("#mode-switch button.active").dataset.mode,
      partVisible: viewport.partGroup.visible,
      stockVisible: viewport.stockGroup.visible,
      workpieceVisible: viewport.workpieceGroup.visible,
      finiteBounds: box ? [box.min.x, box.min.y, box.min.z, box.max.x, box.max.y, box.max.z]
        .every(Number.isFinite) : false,
      boundsMax: box ? box.max.toArray().map((value) => +value.toFixed(1)) : null,
    };
  })()`);
  steps.bench = benchState;
  if (benchState.mode !== "bench" || benchState.partVisible || benchState.stockVisible
      || !benchState.workpieceVisible || !benchState.finiteBounds) {
    return { ok: false, steps, reason: "切回实验台后两套内容没有互斥：" + JSON.stringify(benchState) };
  }

  // 11. 持久连接：在**页面里**连着发请求（浏览器会复用同一条连接）。
  //     "生成刀路"的 POST 带一个 `{}` 而路由不读 body，漏读就会让下一个请求的请求行
  //     变成 `{}POST ...`（服务端回 501）。用户看到的就是"导入失败 / 模型不显示"。
  const keepAlive = await window.webContents.executeJavaScript(`(async () => {
    const status = (method, url, body) => new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open(method, url, true);
      if (body !== null) xhr.setRequestHeader("Content-Type", "application/json");
      xhr.onload = () => resolve(xhr.status);
      xhr.onerror = () => resolve(0);
      xhr.send(body);
    });
    const first = await status("POST", "api/operations/generate", "{}");
    const second = await status("POST", "api/operations/generate", "{}");
    const third = await status("GET", "api/health", null);
    return { first, second, third };
  })()`);
  steps.keepAlive = keepAlive;
  if (keepAlive.first !== 200 || keepAlive.second !== 200 || keepAlive.third !== 200) {
    return { ok: false, steps, reason: "持久连接上连续请求失败：" + JSON.stringify(keepAlive) };
  }

  return { ok: true, steps };
}

async function main() {
  const port = await findFreePort();
  const base = `http://127.0.0.1:${port}/`;
  // 自检用一个**临时数据目录**：默认目录里是用户自己的工程，跑一次自检往里塞一个
  // "sample_plate + 型腔铣 + 曲面工序"的工程，既污染用户列表，又会让下一次自检
  // 一启动就恢复上次的工程 —— 于是自检结果依赖上一次跑过什么，不再是可重复的。
  const dataDir = await fs.mkdtemp(path.join(os.tmpdir(), "tplab-smoke-"));
  const backend = spawn(python, ["-m", "toolpath_lab", "--port", String(port),
                                "--no-browser", "--data-dir", dataDir], {
    cwd: projectRoot,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONPATH: projectRoot },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let backendOutput = "";
  backend.stdout.on("data", (chunk) => { backendOutput += chunk.toString(); });
  backend.stderr.on("data", (chunk) => { backendOutput += chunk.toString(); });

  const window = new BrowserWindow({
    width: 1400, height: 900, show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  window.__smokeBase = base;
  // Electron 43 起 console-message 传的是事件对象（level 为 'error' / 'warning' 字符串），
  // 位置参数已废弃；继续用 `level >= 3` 会因为字符串比较恒为 false 而**漏掉所有错误**。
  window.webContents.on("console-message", (event) => {
    const level = String((event && event.level) || "");
    const message = String((event && event.message) || "");
    if (level !== "error" && level !== "warning") return;
    if (isRealError(message)) consoleErrors.push(level + ": " + message);
  });
  window.webContents.on("render-process-gone", (event, details) => {
    pageErrors.push("render-process-gone: " + JSON.stringify(details));
  });
  window.webContents.on("did-fail-load", (event, code, description, url) => {
    failedRequests.push(`${code} ${description} ${url}`);
  });

  let workflow = { ok: false, steps: {}, reason: "未执行" };
  try {
    if (!(await waitForBackend(base, Date.now() + TIMEOUT_MS))) {
      throw new Error("后端未就绪\n" + backendOutput.slice(-2000));
    }
    await window.loadURL(base);
    await window.webContents.executeJavaScript(PAGE_HTTP);

    // 前端装配完成 + 首次规划返回（统计面板出现数字）。
    // 注意：有工程时启动会直接进 CAM 模式，那种情况下工序为空、统计面板本来就是空的，
    // 所以"统计有数字"只在实验台模式下要求。
    const state = await waitInPage(window, `(() => {
      const app = window.toolpathLab;
      if (!app || !app.viewport || !app.camPanel) return null;
      if (document.querySelectorAll("#panel .section").length === 0) return null;
      if (document.querySelectorAll("#cam-panel .section").length === 0) return null;
      const mode = document.querySelector("#mode-switch button.active").dataset.mode;
      if (mode === "bench" && document.querySelectorAll("#stats dd").length === 0) return null;
      return {
        booted: true,
        hasViewport: Boolean(app.viewport),
        hasPanel: Boolean(app.panel),
        hasCamPanel: Boolean(app.camPanel),
        hasTree: Boolean(app.tree),
        modes: Array.from(document.querySelectorAll("#mode-switch button")).map((b) => b.dataset.mode),
        statsRows: document.querySelectorAll("#stats dd").length,
        panelSections: document.querySelectorAll("#panel .section").length,
        camPanelSections: document.querySelectorAll("#cam-panel .section").length,
        toolpathGroups: app.viewport.pathGroup.children.length,
        mode: mode,
        // 实验台模式下 CAM 的零件/毛坯必须是隐藏的（启动时"恢复工程"最容易破坏这一点）。
        // 有工程时启动会**自动进 CAM 模式**，那时 CAM 内容本来就该可见。
        camGroupsVisible: app.viewport.partGroup.visible || app.viewport.stockGroup.visible,
      };
    })()`, 30000);
    if (!state) throw new Error("前端没有完成装配（面板或统计未出现）");
    if (state.mode === "bench" && state.camGroupsVisible) {
      throw new Error("实验台模式下 CAM 的零件/毛坯居然是可见的（两套内容没有互斥）");
    }

    // 切到 CAM 模式，确认 CAM 面板与工序树出现
    const camState = await window.webContents.executeJavaScript(`(() => {
      document.querySelector("#mode-switch button[data-mode='cam']").click();
      return {
        camSections: document.querySelectorAll("#cam-panel .section").length,
        treeVisible: !document.querySelector("#tree-panel").hidden,
        pickVisible: !document.querySelector("#pick-toolbar").hidden,
        camPanelHidden: document.querySelector("#cam-panel").hidden,
      };
    })()`);

    workflow = await driveCamFlow(window, base);

    console.log(JSON.stringify({
      first: state, cam: camState, workflow,
      consoleErrors, pageErrors, failedRequests,
    }, null, 2));
  } catch (error) {
    console.error("SMOKE FAILED:", (error && error.message) || error);
    console.error("workflow:", JSON.stringify(workflow, null, 2));
    console.error("consoleErrors:", consoleErrors);
    try { backend.kill(); } catch (killError) { /* 忽略 */ }
    app.exit(1);
    return;
  }

  let exitCode = 0;
  if (!workflow.ok) {
    console.error("CAM 工作流失败：", workflow.reason);
    console.error(JSON.stringify(workflow.steps, null, 2));
    exitCode = 1;
  }
  if (consoleErrors.length || pageErrors.length || failedRequests.length) exitCode = 1;
  try { backend.kill(); } catch (error) { /* 忽略 */ }
  await removeDataDir(dataDir);
  app.exit(exitCode);
}

/** 删掉自检的临时数据目录（Windows 上后端刚退出时文件可能还被占着，失败就算了）。 */
async function removeDataDir(dataDir) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      await fs.rm(dataDir, { recursive: true, force: true });
      return;
    } catch (error) {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  }
}

app.whenReady().then(main);
