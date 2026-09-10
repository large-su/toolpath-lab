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
