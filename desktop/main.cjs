const { app, BrowserWindow, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

const REQUESTED_PORT = Number(process.env.JOB_AGENT_PORT || 0);
const STARTUP_TIMEOUT_MS = 15_000;
let port = 0;
let backend = null;
let mainWindow = null;

function appRoot() {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
}

function backendCommand() {
  if (app.isPackaged) {
    return { file: path.join(process.resourcesPath, "backend", "job-agent-backend.exe"), args: [] };
  }
  return { file: path.join(appRoot(), ".venv", "Scripts", "python.exe"), args: ["-m", "uvicorn", "app.main:app"] };
}

function findAvailablePort(candidate) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(null));
    server.listen(candidate, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

async function choosePort() {
  if (REQUESTED_PORT > 0) return REQUESTED_PORT;
  for (const candidate of [8011, 8012, 8013, 8014, 8015]) {
    const available = await findAvailablePort(candidate);
    if (available) return available;
  }
  return findAvailablePort(0);
}

function startBackend() {
  const command = backendCommand();
  const logDir = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(logDir, { recursive: true });
  const logFile = path.join(logDir, "backend-startup.log");
  const logHandle = fs.openSync(logFile, "a");
  backend = spawn(command.file, [...command.args, "--host", "127.0.0.1", "--port", String(port)], {
    cwd: appRoot(),
    windowsHide: true,
    env: { ...process.env, APP_HOME_DIR: app.getPath("userData") },
    stdio: ["ignore", logHandle, logHandle],
  });
  backend.once("error", (error) => fs.appendFileSync(logFile, `spawn error: ${error.message}\n`));
  backend.once("exit", (code, signal) => {
    if (code || signal) fs.appendFileSync(logFile, `backend exited: code=${code} signal=${signal}\n`);
  });
  return logFile;
}

function htmlEscape(value) {
  return String(value).replace(/[&<>"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
  })[character]);
}

function waitForBackend(timeoutMs = STARTUP_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const attempt = (remaining) => {
      if (backend && backend.exitCode !== null) {
        reject(new Error("本地后端进程已提前退出。"));
        return;
      }
      const request = http.get(`http://127.0.0.1:${port}/agent`, (response) => {
        response.resume();
        if (response.statusCode === 200) resolve(Date.now() - startedAt);
        else if (Date.now() < startedAt + timeoutMs) setTimeout(attempt, 120);
        else reject(new Error(`本地求职agent服务在 ${Math.ceil(timeoutMs / 1000)} 秒内未能启动。`));
      });
      request.on("error", () => Date.now() < startedAt + timeoutMs
        ? setTimeout(attempt, 120)
        : reject(new Error(`本地求职agent服务在 ${Math.ceil(timeoutMs / 1000)} 秒内未能启动。`)));
      request.setTimeout(500, () => request.destroy());
    };
    attempt();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1080,
    minHeight: 720,
    title: "求职agent",
    backgroundColor: "#f4f5f6",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(`http://127.0.0.1:${port}/`)) return { action: "allow" };
    shell.openExternal(url);
    return { action: "deny" };
  });
  return mainWindow;
}

function loadingPage() {
  return "data:text/html;charset=utf-8," + encodeURIComponent(`
    <!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>求职agent</title>
    <style>body{margin:0;background:#f4f5f6;color:#1b2730;font-family:'Microsoft YaHei',sans-serif;display:grid;place-items:center;height:100vh}.panel{width:440px;padding:36px 40px;background:#fff;border:1px solid #dce5e7}.title{font-size:28px;font-weight:700;color:#167f84}.note{margin-top:16px;line-height:1.8;color:#52616a}</style>
    </head><body><main class="panel"><div class="title">求职agent</div><div class="note">正在启动本地服务并读取你的本机资料，通常只需几秒。请勿重复打开。</div></main></body></html>
  `);
}

function errorPage(error, logFile) {
  const text = htmlEscape(error?.message || error || "本地服务未能启动。");
  const safeLogFile = htmlEscape(logFile);
  return "data:text/html;charset=utf-8," + encodeURIComponent(`
    <!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>求职agent启动失败</title>
    <style>body{margin:0;background:#f4f5f6;color:#1b2730;font-family:'Microsoft YaHei',sans-serif;display:grid;place-items:center;height:100vh}.panel{width:560px;padding:36px 40px;background:#fff;border:1px solid #e7caca}.title{font-size:24px;font-weight:700;color:#a33a3a}.note{margin-top:16px;line-height:1.8;color:#52616a}.detail{margin-top:12px;padding:12px;background:#f8f8f8;word-break:break-all;font-family:Consolas,monospace;font-size:12px}</style>
    </head><body><main class="panel"><div class="title">本地服务启动失败</div><div class="note">应用没有打开浏览器，也没有发送任何资料。请关闭后重试；若仍失败，可查看本机启动日志。</div><div class="detail">${text}\n日志：${safeLogFile}</div></main></body></html>
  `);
}

app.whenReady().then(async () => {
  createWindow();
  await mainWindow.loadURL(loadingPage());
  port = await choosePort();
  const logFile = startBackend();
  try {
    const startupMs = await waitForBackend();
    fs.appendFileSync(logFile, `backend ready in ${startupMs}ms on port ${port}\n`);
    await mainWindow.loadURL(`http://127.0.0.1:${port}/agent`);
  } catch (error) {
    await mainWindow.loadURL(errorPage(error, logFile));
  }
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => { if (backend && !backend.killed) backend.kill(); });
