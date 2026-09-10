// Electron 桌面壳。
//
// 只做三件事：拉起 Python 后端（自己挑一个空闲端口）、等它就绪、把本地地址装进原生窗口。
// 关掉窗口时把后端进程一起结束。前端是普通静态文件，由 Python 一起提供，
// 所以这里不需要打包器，也不需要 preload。

import { app, BrowserWindow, dialog, shell } from "electron";
import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const STARTUP_TIMEOUT_MS = 30000;

// 依次尝试：启动脚本传来的解释器、PATH 里的 python / python3 / py。
// 直接 npm start 时也能自己找到可用的解释器。
const PYTHON_CANDIDATES = [
  process.env.TOOLPATH_LAB_PYTHON,
  "python",
  "python3",
  "py",
].filter(Boolean);

let backend = null;
let output = "";

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
    await new Promise((resolve) => setTimeout(resolve, 150));
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
      "后端未能在 " + STARTUP_TIMEOUT_MS / 1000 + " 秒内就绪。\n\n"
      + "解释器：" + python + "\n\n" + output
    );
  }
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

function createWindow(url) {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    backgroundColor: "#071014",
    title: "ToolpathLab",
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  window.once("ready-to-show", () => window.show());
  // 外链一律交给系统浏览器，窗口本身只装本机界面。
  window.webContents.setWindowOpenHandler(({ url: target }) => {
    shell.openExternal(target);
    return { action: "deny" };
  });
  window.loadURL(url);
  return window;
}

app.whenReady().then(async () => {
  try {
    createWindow(await startFirstWorkingBackend());
  } catch (error) {
    dialog.showErrorBox("ToolpathLab 启动失败", String((error && error.message) || error));
    app.quit();
    return;
  }
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      startFirstWorkingBackend().then(createWindow).catch(() => app.quit());
    }
  });
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", stopBackend);
process.on("exit", stopBackend);
