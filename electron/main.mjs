// The Electron desktop shell.
//
// It does three things: start the Python backend (picking a free port itself), load the local
// address into a native window, and stop the backend when the window closes. Startup is deliberately
// "window first, backend after": the window shows a built-in loading page within tens of milliseconds
// and swaps in the real interface once the backend is ready, so a double click never means waiting.
//
// Startup timings go to %TEMP%\toolpathlab-launch.log (override with TOOLPATH_LAB_TIMING_LOG),
// and the launcher prints that file when something goes wrong.

import { app, BrowserWindow, dialog, shell } from "electron";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const startedAt = Date.now();
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const STARTUP_TIMEOUT_MS = 30000;

// App icon: a multi-size .ico on Windows (crisper small taskbar icons), PNG elsewhere.
const WEB_ICON_DIR = path.join(projectRoot, "toolpath_lab", "web");
const APP_ICON = path.join(
  WEB_ICON_DIR,
  process.platform === "win32" ? "icon.ico" : "icon.png"
);

// Tried in order: the interpreter handed over by the launcher, then python / python3 / py on PATH.
// So a plain npm start can find a usable interpreter on its own.
const PYTHON_CANDIDATES = [
  process.env.TOOLPATH_LAB_PYTHON,
  "python",
  "python3",
  "py",
].filter(Boolean);

const LOG_FILE =
  process.env.TOOLPATH_LAB_TIMING_LOG ||
  path.join(os.tmpdir(), "toolpathlab-launch.log");

let backend = null;
let output = "";

// Startup timing: it shows which stage hangs, and the launcher reads the file to tell success.
function report(message) {
  const line =
    "[toolpath-lab] " + ((Date.now() - startedAt) / 1000).toFixed(2) + "s  " + message;
  console.log(line);
  try {
    fs.appendFileSync(LOG_FILE, line + "\n");
  } catch (error) {
    /* Failing to record it is fine, startup does not depend on it */
  }
}

function resetLog() {
  try {
    fs.writeFileSync(
      LOG_FILE,
      "ToolpathLab " + new Date().toISOString() + "  " + process.platform + "\n"
    );
  } catch (error) {
    /* Same as above */
  }
}

// The built-in loading page shown first: it needs neither the backend nor any external file.
const LOADING_PAGE =
  "data:text/html;charset=utf-8," +
  encodeURIComponent(
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
      "<style>html,body{margin:0;height:100%;background:#071014;color:#899b99;" +
      'font:13px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;' +
      "display:grid;place-items:center}" +
      ".box{text-align:center;letter-spacing:.06em}" +
      ".spin{width:26px;height:26px;margin:0 auto 14px;border-radius:50%;" +
      "border:2px solid rgba(84,214,196,.22);border-top-color:#54d6c4;" +
      "animation:spin 900ms linear infinite}" +
      "@keyframes spin{to{transform:rotate(360deg)}}</style></head>" +
      '<body><div class="box"><div class="spin"></div>正在启动 ToolpathLab …</div></body></html>'
  );

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

async function waitForBackend(url) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url + "api/health");
      if (response.ok) return true;
    } catch (error) {
      /* Not up yet, keep waiting */
    }
    await new Promise((resolve) => setTimeout(resolve, 60));
  }
  return false;
}

async function startBackend(python) {
  const port = await findFreePort();
  const url = "http://127.0.0.1:" + port + "/";
  backend = spawn(python, ["-m", "toolpath_lab", "--port", String(port), "--no-browser"], {
    cwd: projectRoot,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONPATH: projectRoot },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const collect = (chunk) => {
    output = (output + chunk.toString()).slice(-4000);
  };
  backend.stdout.on("data", collect);
  backend.stderr.on("data", collect);
  backend.on("error", (error) => {
    output += "\n无法启动 " + python + "：" + error.message;
  });
  if (!(await waitForBackend(url))) {
    stopBackend();
    throw new Error(
      "后端未能在 " + STARTUP_TIMEOUT_MS / 1000 + " 秒内就绪。\n\n" +
        "解释器：" + python + "\n\n" + output
    );
  }
  report("后端就绪（" + python + "，端口 " + port + "）");
  return url;
}

async function startFirstWorkingBackend() {
  const failures = [];
  for (const candidate of PYTHON_CANDIDATES) {
    output = "";
    try {
      return await startBackend(candidate);
    } catch (error) {
      failures.push(String((error && error.message) || error));
    }
  }
  throw new Error(failures.join("\n\n"));
}

function stopBackend() {
  if (backend && !backend.killed) {
    backend.kill();
    backend = null;
  }
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    backgroundColor: "#071014",
    title: "ToolpathLab",
    icon: APP_ICON,
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  window.once("ready-to-show", () => {
    window.show();
    report("窗口已显示");
  });
  // External links go to the system browser; the window itself only ever hosts the local interface.
  window.webContents.setWindowOpenHandler(({ url: target }) => {
    shell.openExternal(target);
    return { action: "deny" };
  });
  window.loadURL(LOADING_PAGE);
  return window;
}

async function boot() {
  // Window and backend start together: the window shows the loading page while the backend prepares.
  const window = createWindow();
  const backendUrl = startFirstWorkingBackend();
  try {
    const url = await backendUrl;
    await window.loadURL(url);
    // The ASCII marker UI-READY is for the launcher (matching Chinese text in a batch file is unreliable).
    report("界面就绪 UI-READY");
  } catch (error) {
    stopBackend();
    report("启动失败：" + String((error && error.message) || error));
    dialog.showErrorBox("ToolpathLab 启动失败", String((error && error.message) || error));
    app.quit();
  }
}

// With an instance already running, bring its window forward instead of starting another backend and window.
if (!app.requestSingleInstanceLock()) {
  // An instance is already running: report success to the launcher and exit quietly.
  report("已有实例在运行，已切到已打开的窗口 UI-READY");
  app.quit();
} else {
  app.on("second-instance", () => {
    const [window] = BrowserWindow.getAllWindows();
    if (window) {
      if (window.isMinimized()) window.restore();
      window.focus();
    }
  });

  app.whenReady().then(async () => {
    resetLog();
    // A distinct AppUserModelID: otherwise Windows may file this window under another Electron app
    // in the taskbar.
    if (process.platform === "win32") app.setAppUserModelId("com.toolpathlab.desktop");
    report("桌面壳就绪");
    await boot();
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) boot();
  });

  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", stopBackend);
  process.on("exit", stopBackend);
}
