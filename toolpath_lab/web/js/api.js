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

// 导出：后端一律回 Content-Disposition，文件名与类型都由它决定，前端只负责触发下载。
const FALLBACK_NAMES = { gcode: "toolpath.nc", csv: "toolpath.csv" };

export async function downloadExport(kind, payload) {
  const response = await fetch("/api/export/" + kind, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await readError(response));
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const name = match ? match[1] : (FALLBACK_NAMES[kind] || "toolpath.dat");
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
