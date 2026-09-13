// 后端 JSON 接口的薄封装：任何错误都变成带后端消息的 Error，界面直接显示。
//
// 分成两组：
//   * 基座（实验台）—— /api/catalog、/api/plan、/api/export/gcode
//   * CAM 加工     —— 工程、导入、毛坯、工序树、仿真、出程序
// 每个函数只负责"发请求 + 把错误消息抠出来"，业务状态都放在 main.js / cam-panel.js 里。

const JSON_HEADERS = { "Content-Type": "application/json" };

async function readError(response) {
  try {
    const data = await response.json();
    if (data && typeof data.error === "string") return data.error;
  } catch (error) {
    /* 退回到状态行 */
  }
  return response.status + " " + response.statusText;
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

function jsonBody(payload) {
  return { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(payload ?? {}) };
}

// --------------------------------------------------------------- 基座
export async function fetchCatalog() {
  return requestJson("/api/catalog");
}

export async function requestPlan(payload) {
  return requestJson("/api/plan", jsonBody(payload));
}

export async function downloadGcode(payload) {
  return download("/api/export/gcode", payload, "toolpath.nc");
}

// --------------------------------------------------------------- CAM
/** 导入 STEP：优先用 multipart 直接传文件，没有文件对象时退回 JSON+base64。 */
export async function importStep(file, onProgress) {
  if (file) {
    const form = new FormData();
    form.append("file", file, file.name);
    const response = await fetch("/api/import/step", { method: "POST", body: form });
    if (!response.ok) throw new Error(await readError(response));
    return response.json();
  }
  throw new Error("请选择 STEP 文件");
}

export async function fetchProjects() {
  return requestJson("/api/projects");
}

export async function openProject(id) {
  return requestJson("/api/projects/open", jsonBody({ id }));
}

export async function deleteProject(id) {
  return requestJson(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function closeProject() {
  return requestJson("/api/projects/current", { method: "DELETE" });
}

export async function fetchModel() {
  return requestJson("/api/model");
}

export async function fetchFeatures() {
  return requestJson("/api/model/features");
}

export async function fetchStock() {
  return requestJson("/api/stock");
}

export async function saveStock(shape, parameters) {
  return requestJson("/api/stock", jsonBody({ shape, parameters }));
}

export async function fetchParameters() {
  return requestJson("/api/parameters");
}

export async function saveParameters(cam, controller) {
  return requestJson("/api/parameters", jsonBody({ cam, controller }));
}

export async function fetchOperations() {
  return requestJson("/api/operations");
}

export async function addOperation(payload) {
  return requestJson("/api/operations", jsonBody(payload));
}

export async function updateOperation(id, payload) {
  return requestJson(`/api/operations/${encodeURIComponent(id)}`, jsonBody(payload));
}

export async function deleteOperation(id) {
  return requestJson(`/api/operations/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function generateOperation(id) {
  return requestJson(`/api/operations/${encodeURIComponent(id)}/generate`, jsonBody({}));
}

export async function duplicateOperation(id) {
  return requestJson(`/api/operations/${encodeURIComponent(id)}/duplicate`, jsonBody({}));
}

export async function moveOperation(id, sequence) {
  return requestJson(`/api/operations/${encodeURIComponent(id)}/move`, jsonBody({ sequence }));
}

export async function generateAllOperations() {
  return requestJson("/api/operations/generate", jsonBody({}));
}

export async function saveTemplate(name, kind, parameters) {
  return requestJson("/api/templates", jsonBody({ name, kind, parameters }));
}

export async function deleteTemplate(id) {
  return requestJson(`/api/templates/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function simulate(payload) {
  return requestJson("/api/simulate", jsonBody(payload));
}

export async function downloadNc(payload) {
  return download("/api/export/nc", payload, "program.nc");
}

// --------------------------------------------------------------- 公共
async function download(path, payload, fallbackName) {
  const response = await fetch(path, jsonBody(payload));
  if (!response.ok) throw new Error(await readError(response));
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const name = match ? match[1] : fallbackName;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  return name;
}

/** 工序树 / 工序参数的文本导出（前端直接生成，不需要再请求后端）。 */
export function downloadText(name, text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  return name;
}
