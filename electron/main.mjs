// Electron 桌面壳。
//
// 只做三件事：拉起 Python 后端（自己挑一个空闲端口）、把本地地址装进原生窗口、
// 关窗时结束后端。启动过程刻意做成"先出窗口再等后端"：
// 窗口先显示一张内置的加载页（几十毫秒），后端就绪后再换成真正的界面，
// 因此双击之后是"立刻看到窗口"，而不是先干等一段时间。
//
// 启动耗时写在 %TEMP%\toolpathlab-launch.log 里（也可以用 TOOLPATH_LAB_TIMING_LOG 改路径），
// 启动脚本出错时会把它打印出来。

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

// 应用图标：Windows 用多尺寸 .ico（任务栏小图标更清晰），其它平台用 PNG。
const WEB_ICON_DIR = path.join(projectRoot, "toolpath_lab", "web");
const APP_ICON = path.join(
  WEB_ICON_DIR,
  process.platform === "win32" ? "icon.ico" : "icon.png"
);

// 依次尝试：启动脚本传来的解释器、PATH 里的 python / python3 / py。
// 直接 npm start 时也能自己找到可用的解释器。
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

// 启动计时：卡住时能看出卡在哪一段；启动脚本也会读这个文件判断是否成功。
function report(message) {
  const line =
    "[toolpath-lab] " + ((Date.now() - startedAt) / 1000).toFixed(2) + "s  " + message;
  console.log(line);
  try {
    fs.appendFileSync(LOG_FILE, line + "\n");
  } catch (error) {
    /* 记不下来就算了，不影响启动 */
  }
}

function resetLog() {
  try {
    fs.writeFileSync(
      LOG_FILE,
      "ToolpathLab " + new Date().toISOString() + "  " + process.platform + "\n"
    );
  } catch (error) {
    /* 同上 */
  }
}

// 窗口先显示的内置加载页：不依赖后端，也不依赖任何外部文件。
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
      /* 还没起来，继续等 */
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
  // 外链一律交给系统浏览器，窗口本身只装本机界面。
  window.webContents.setWindowOpenHandler(({ url: target }) => {
    shell.openExternal(target);
    return { action: "deny" };
  });
  window.loadURL(LOADING_PAGE);
  return window;
}

async function boot() {
  // 窗口与后端同时起步：窗口先显示加载页，后端在后台准备。
  const window = createWindow();
  const backendUrl = startFirstWorkingBackend();
  try {
    const url = await backendUrl;
    await window.loadURL(url);
    // UI-READY 这个 ASCII 标记是给启动脚本看的（批处理里用中文字符串匹配不可靠）。
    report("界面就绪 UI-READY");
  } catch (error) {
    stopBackend();
    report("启动失败：" + String((error && error.message) || error));
    dialog.showErrorBox("ToolpathLab 启动失败", String((error && error.message) || error));
    app.quit();
  }
}

// 已经有实例在运行时，把已有窗口带到前面，而不是再开一个后端和窗口。
if (!app.requestSingleInstanceLock()) {
  // 已经有实例在跑：把成功标记写给启动脚本，然后安静退出。
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
    // 独立的 AppUserModelID：否则 Windows 可能把这个窗口归到机器上另一个 Electron 应用的
    // 任务栏图标下。
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
