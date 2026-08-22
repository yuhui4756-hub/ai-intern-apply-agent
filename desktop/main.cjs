const { app, BrowserWindow, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const PORT = Number(process.env.JOB_AGENT_PORT || 8011);
let backend = null;

function appRoot() {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
}

function backendCommand() {
  if (app.isPackaged) {
    return { file: path.join(process.resourcesPath, "backend", "job-agent-backend.exe"), args: [] };
  }
  return { file: path.join(appRoot(), ".venv", "Scripts", "python.exe"), args: ["-m", "uvicorn", "app.main:app"] };
}

function startBackend() {
  const command = backendCommand();
  backend = spawn(command.file, [...command.args, "--host", "127.0.0.1", "--port", String(PORT)], {
    cwd: appRoot(),
    windowsHide: true,
    env: { ...process.env, APP_HOME_DIR: app.getPath("userData") },
    stdio: "ignore",
  });
}

function waitForBackend(retries = 80) {
  return new Promise((resolve, reject) => {
    const attempt = (remaining) => {
      const request = http.get(`http://127.0.0.1:${PORT}/agent`, (response) => {
        response.resume();
        if (response.statusCode === 200) resolve();
        else if (remaining > 0) setTimeout(() => attempt(remaining - 1), 150);
        else reject(new Error("本地求职agent服务未能启动。"));
      });
      request.on("error", () => remaining > 0 ? setTimeout(() => attempt(remaining - 1), 150) : reject(new Error("本地求职agent服务未能启动。")));
      request.setTimeout(500, () => request.destroy());
    };
    attempt(retries);
  });
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1080,
    minHeight: 720,
    title: "求职agent",
    backgroundColor: "#f4f5f6",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(`http://127.0.0.1:${PORT}/`)) return { action: "allow" };
    shell.openExternal(url);
    return { action: "deny" };
  });
  window.loadURL(`http://127.0.0.1:${PORT}/agent`);
}

app.whenReady().then(async () => {
  startBackend();
  try { await waitForBackend(); createWindow(); }
  catch (error) { await shell.openExternal(`http://127.0.0.1:${PORT}/agent`); app.quit(); }
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => { if (backend && !backend.killed) backend.kill(); });
