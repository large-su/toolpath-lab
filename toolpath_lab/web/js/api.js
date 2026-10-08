// 后端 JSON 接口的薄封装：任何错误都变成带后端消息的 Error，界面直接显示。

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

export async function fetchCatalog() {
  const response = await fetch("/api/catalog");
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function requestPlan(payload) {
  const response = await fetch("/api/plan", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function downloadGcode(payload) {
  const response = await fetch("/api/export/gcode", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await readError(response));
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const name = match ? match[1] : "toolpath.nc";
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

// ---------------------------------------------------------------- 模型（STL）
// 上传用 base64 塞进 JSON：后端只靠标准库解析，两行代码就够，脚本也好拼。

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunk = 0x8000;
  let binary = "";
  for (let start = 0; start < bytes.length; start += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(start, start + chunk));
  }
  return btoa(binary);
}

export async function fetchModels() {
  const response = await fetch("/api/models");
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function uploadModel(file) {
  const buffer = await file.arrayBuffer();
  const response = await fetch("/api/models", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ name: file.name, data_base64: toBase64(buffer) }),
  });
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()).model;
}

export async function fetchModelMesh(modelId) {
  const response = await fetch("/api/models/" + encodeURIComponent(modelId));
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()).model;
}

export async function deleteModel(modelId) {
  const response = await fetch("/api/models/" + encodeURIComponent(modelId), {
    method: "DELETE",
  });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}
