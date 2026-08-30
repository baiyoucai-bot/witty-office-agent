"use strict";

const { app, BrowserWindow, ipcMain, dialog, shell, screen } = require("electron");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const api = require("./api");

if (!app.isPackaged) {
  // 调试口只留给开发与窗口检查脚本；安装版对外开 CDP 端口等于把整个壳交出去
  app.commandLine.appendSwitch("remote-debugging-port", "9333");
}

let mainWindow = null;
let apiChild = null;

function repoRoot() {
  return path.resolve(__dirname, "..", "..");
}

function createWindow() {
  const work = screen.getPrimaryDisplay().workArea;
  const width = Math.max(800, Math.min(work.width - 24, Math.round(work.width * 0.88)));
  const height = Math.max(600, Math.min(work.height - 24, Math.round(work.height * 0.88)));
  mainWindow = new BrowserWindow({
    width,
    height,
    minWidth: Math.min(960, work.width),
    minHeight: Math.min(640, work.height),
    x: work.x + Math.max(0, Math.round((work.width - width) / 2)),
    y: work.y + Math.max(0, Math.round((work.height - height) / 2)),
    title: "人和",
    acceptFirstMouse: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  const query = {};
  if (process.env.WITTY_API_BASE) {
    query.base = process.env.WITTY_API_BASE;
  }
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"), { query });
  mainWindow.webContents.on("console-message", (...args) => {
    let message = "";
    const first = args[0];
    if (first && typeof first === "object" && first.message) {
      message = String(first.message);
    } else if (args[2] != null) {
      message = String(args[2]);
    } else {
      message = args.map(String).join(" ");
    }
    if (message.includes("HITTEST")) {
      try {
        fs.writeFileSync("/tmp/witty-hit.log", `${new Date().toISOString()} ${message}\n`);
      } catch {
        // ignore
      }
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function stopApiChild() {
  if (!apiChild || apiChild.killed) {
    return;
  }
  apiChild.kill();
  apiChild = null;
}

function bundledPython() {
  // 安装版把 python-build-standalone 放在 resources/python，见 scripts/build_windows_installer.py
  if (!app.isPackaged) {
    return null;
  }
  const exe =
    process.platform === "win32"
      ? path.join(process.resourcesPath, "python", "python.exe")
      : path.join(process.resourcesPath, "python", "bin", "python3");
  return fs.existsSync(exe) ? exe : null;
}

async function startApi() {
  try {
    await api.health();
    return { ok: true, already: true, base: api.apiBase() };
  } catch {
    // fall through and spawn
  }
  if (apiChild && !apiChild.killed) {
    return { ok: true, already: true, pid: apiChild.pid, base: api.apiBase() };
  }
  const python = bundledPython();
  if (python) {
    const binDir = path.join(process.resourcesPath, "bin");
    const env = {
      ...process.env,
      // uv.exe（沙箱建 venv 用）和 busybox（bash 工具用）都在 resources/bin
      PATH: `${binDir}${path.delimiter}${process.env.PATH || ""}`,
      // 中文 Windows 默认 GBK；后端全按 UTF-8 读写，强制 UTF-8 模式
      PYTHONUTF8: "1",
      PYTHONNOUSERSITE: "1",
    };
    const busybox = path.join(binDir, "busybox64.exe");
    if (fs.existsSync(busybox)) {
      env.WITTY_BASH = busybox;
    }
    apiChild = spawn(python, ["-m", "witty_agent", "serve"], {
      cwd: app.getPath("home"),
      env,
      stdio: "ignore",
      windowsHide: true,
    });
  } else {
    apiChild = spawn("uv", ["run", "witty-agent", "serve"], {
      cwd: repoRoot(),
      env: process.env,
      stdio: "ignore",
    });
  }
  apiChild.on("exit", () => {
    apiChild = null;
  });
  return { ok: true, pid: apiChild.pid, base: api.apiBase() };
}

function registerIpc() {
  ipcMain.handle("api:health", () => api.health());
  ipcMain.handle("api:createSession", (_event, options) => api.createSession(options || {}));
  ipcMain.handle("api:forkSession", (_event, sessionId) => api.forkSession(sessionId));
  ipcMain.handle("api:sendPrompt", (_event, sessionId, prompt) => api.sendPrompt(sessionId, prompt));
  ipcMain.handle("api:startPrompt", (_event, sessionId, prompt, approvalMode, thinkLevel) =>
    api.startPrompt(sessionId, prompt, approvalMode, thinkLevel),
  );
  ipcMain.handle("api:abortSession", (_event, sessionId) => api.abortSession(sessionId));
  ipcMain.handle("api:steerSession", (_event, sessionId, text) => api.steerSession(sessionId, text));
  ipcMain.handle("api:getRun", (_event, sessionId) => api.getRun(sessionId));
  ipcMain.handle("api:submitApproval", (_event, sessionId, toolCallId, decision) =>
    api.submitApproval(sessionId, toolCallId, decision),
  );
  ipcMain.handle("api:submitAnswer", (_event, sessionId, answers) =>
    api.submitAnswer(sessionId, answers),
  );
  ipcMain.handle("api:getMessages", (_event, sessionId) => api.getMessages(sessionId));
  ipcMain.handle("api:listSessions", (_event, projectId, agentId) => api.listSessions(projectId, agentId));
  ipcMain.handle("api:listSkills", (_event, projectId, agentId) => api.listSkills(projectId, agentId));
  ipcMain.handle("api:getSkill", (_event, name) => api.getSkill(name));
  ipcMain.handle("api:setSkillEnabled", (_event, name, enabled, projectId, agentId) =>
    api.setSkillEnabled(name, enabled, projectId, agentId),
  );
  ipcMain.handle("api:installSkill", (_event, body) => api.installSkill(body || {}));
  ipcMain.handle("api:uninstallSkill", (_event, name, projectId, agentId) =>
    api.uninstallSkill(name, projectId, agentId),
  );
  ipcMain.handle("api:reloadPlugins", () => api.reloadPlugins());
  ipcMain.handle("api:getPlugins", () => api.getPlugins());
  ipcMain.handle("api:attachSkillPath", (_event, path) => api.attachSkillPath(path));
  ipcMain.handle("api:detachSkillPath", (_event, path) => api.detachSkillPath(path));
  ipcMain.handle("api:attachToolPackage", (_event, name, path) =>
    api.attachToolPackage(name, path),
  );
  ipcMain.handle("api:detachToolPackage", (_event, name) => api.detachToolPackage(name));
  ipcMain.handle("shell:pickSkill", async () => {
    if (!mainWindow) {
      return null;
    }
    const choice = await dialog.showMessageBox(mainWindow, {
      type: "question",
      buttons: ["选择 SKILL.md", "选择技能目录", "取消"],
      defaultId: 0,
      cancelId: 2,
      message: "安装本地技能",
      detail: "选一份 SKILL.md，或选包含 SKILL.md 的技能目录。会写入当前 Agent 的用户技能目录。",
    });
    if (choice.response === 2) {
      return null;
    }
    const pickDir = choice.response === 1;
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: pickDir ? ["openDirectory"] : ["openFile"],
      filters: pickDir ? [] : [{ name: "SKILL.md", extensions: ["md"] }],
    });
    if (result.canceled || !result.filePaths.length) {
      return null;
    }
    return { source: result.filePaths[0] };
  });
  ipcMain.handle("shell:confirm", async (_event, message) => {
    if (!mainWindow) {
      return false;
    }
    const result = await dialog.showMessageBox(mainWindow, {
      type: "question",
      buttons: ["覆盖", "取消"],
      defaultId: 1,
      cancelId: 1,
      message: String(message || "覆盖已有技能？"),
    });
    return result.response === 0;
  });
  ipcMain.handle("api:listTools", (_event, projectId, agentId) => api.listTools(projectId, agentId));
  ipcMain.handle("api:setToolEnabled", (_event, name, enabled, projectId, agentId) =>
    api.setToolEnabled(name, enabled, projectId, agentId),
  );
  ipcMain.handle("api:listCommands", (_event, sessionId) => api.listCommands(sessionId));
  ipcMain.handle("api:getMemory", (_event, projectId, agentId, workspaceDir, recall) =>
    api.getMemory(projectId, agentId, workspaceDir, recall),
  );
  ipcMain.handle("api:saveMemory", (_event, body) => api.saveMemory(body));
  ipcMain.handle("api:saveInbox", (_event, body) => api.saveInbox(body));
  ipcMain.handle("api:previewFile", (_event, workspaceDir, target) =>
    api.previewFile(workspaceDir, target),
  );
  ipcMain.handle("shell:pickDirectory", async () => {
    if (!mainWindow) {
      return "";
    }
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ["openDirectory"],
    });
    return result.canceled ? "" : result.filePaths[0] || "";
  });
  ipcMain.handle("shell:pickFiles", async () => {
    if (!mainWindow) {
      return [];
    }
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ["openFile", "multiSelections"],
    });
    return result.canceled ? [] : result.filePaths;
  });
  ipcMain.handle("shell:listWorkspace", async (_event, dir) => {
    const root = String(dir || "").trim();
    if (!root) {
      return [];
    }
    const skip = new Set([".git", "node_modules", ".venv", "__pycache__", ".codegraph", "dist"]);
    const out = [];
    const deadline = Date.now() + 1200;
    const walk = async (current, depth) => {
      if (out.length >= 80 || depth > 1 || Date.now() > deadline) {
        return;
      }
      let entries;
      try {
        entries = await fs.promises.readdir(current, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        if (out.length >= 80 || Date.now() > deadline) {
          return;
        }
        if (entry.name.startsWith(".") || skip.has(entry.name)) {
          continue;
        }
        const full = path.join(current, entry.name);
        if (entry.isDirectory()) {
          out.push(full.replace(/\\/g, "/") + "/");
        } else {
          out.push(full);
        }
      }
    };
    await walk(root, 0);
    const art = /\.(pptx|ppt|docx|xlsx|xls|pdf|csv|png|jpe?g|gif|html)$/i;
    const ranked = out.filter((item) => art.test(item));
    const seen = new Set(ranked);
    for (const full of out) {
      if (!seen.has(full)) {
        ranked.push(full);
        seen.add(full);
      }
    }
    return ranked;
  });
  ipcMain.handle("shell:openPath", async (_event, target) => {
    const raw = String(target || "").trim();
    if (!raw) {
      return { ok: false, error: "empty" };
    }
    const full = path.resolve(raw);
    if (!fs.existsSync(full)) {
      return { ok: false, error: "missing" };
    }
    const err = await shell.openPath(full);
    return { ok: !err, error: err || "" };
  });
  // 产物栏要显示大小、时间，还要认出已经被删掉的条目。渲染进程没有 fs，走这里。
  ipcMain.handle("shell:statPaths", async (_event, targets) => {
    const rows = Array.isArray(targets) ? targets.slice(0, 60) : [];
    const out = [];
    for (const target of rows) {
      const raw = String(target || "").trim();
      if (!raw) {
        continue;
      }
      try {
        const info = await fs.promises.stat(path.resolve(raw));
        out.push({
          path: raw,
          exists: true,
          directory: info.isDirectory(),
          size: info.size,
          mtime: info.mtimeMs,
        });
      } catch {
        out.push({ path: raw, exists: false, directory: false, size: 0, mtime: 0 });
      }
    }
    return out;
  });
  ipcMain.handle("shell:revealPath", async (_event, target) => {
    const raw = String(target || "").trim();
    if (!raw) {
      return { ok: false, error: "empty" };
    }
    const full = path.resolve(raw);
    if (!fs.existsSync(full)) {
      return { ok: false, error: "missing" };
    }
    shell.showItemInFolder(full);
    return { ok: true, error: "" };
  });
  ipcMain.handle("api:deleteSession", (_event, sessionId, projectId, agentId) =>
    api.deleteSession(sessionId, projectId, agentId),
  );
  ipcMain.handle("api:getWeb", () => api.getWeb());
  ipcMain.handle("api:saveWeb", (_event, body) => api.saveWeb(body || {}));
  ipcMain.handle("api:getModel", (_event, projectId, agentId) => api.getModel(projectId, agentId));
  ipcMain.handle("api:saveModel", (_event, body) => api.saveModel(body));
  ipcMain.handle("api:listModels", (_event, projectId, agentId) => api.listModels(projectId, agentId));
  ipcMain.handle("api:saveModelProfile", (_event, body) => api.saveModelProfile(body));
  ipcMain.handle("api:deleteModel", (_event, name, projectId, agentId) =>
    api.deleteModel(name, projectId, agentId),
  );
  ipcMain.handle("api:activateModel", (_event, name, projectId, agentId) =>
    api.activateModel(name, projectId, agentId),
  );
  ipcMain.handle("api:startServer", () => startApi());
  ipcMain.handle("api:base", () => api.apiBase());
  ipcMain.handle("api:getMail", (_event, projectId, agentId) => api.getMail(projectId, agentId));
  ipcMain.handle("api:listSchedules", (_event, projectId, agentId) =>
    api.listSchedules(projectId, agentId),
  );
  ipcMain.handle("api:saveSchedule", (_event, body) => api.saveSchedule(body || {}));
  ipcMain.handle("api:setScheduleEnabled", (_event, name, enabled, projectId, agentId) =>
    api.setScheduleEnabled(name, enabled, projectId, agentId),
  );
  ipcMain.handle("api:deleteSchedule", (_event, name, projectId, agentId) =>
    api.deleteSchedule(name, projectId, agentId),
  );
  ipcMain.handle("api:tickSchedules", () => api.tickSchedules());
  ipcMain.handle("api:saveMail", (_event, body) => api.saveMail(body || {}));
  ipcMain.handle("api:getWiki", (_event, workspaceDir) => api.getWiki(workspaceDir));
  ipcMain.handle("api:addWiki", (_event, body) => api.addWiki(body || {}));
  ipcMain.handle("api:removeWiki", (_event, sourceId, workspaceDir) =>
    api.removeWiki(sourceId, workspaceDir),
  );
  ipcMain.handle("api:getLinks", (_event, query) => api.getLinks(query));
  ipcMain.handle("api:addLink", (_event, body) => api.addLink(body || {}));
  ipcMain.handle("api:getDiary", (_event, day, list) => api.getDiary(day, list));
  ipcMain.handle("api:writeDiary", (_event, text, day) => api.writeDiary(text, day));
  ipcMain.handle("api:listPrompts", () => api.listPrompts());
  ipcMain.handle("api:getPrompt", (_event, name) => api.getPrompt(name));
  ipcMain.handle("api:savePrompt", (_event, name, text) => api.savePrompt(name, text));
}

async function runVerify() {
  const health = await api.health();
  const session = await api.createSession({
    project_id: process.env.WITTY_PROJECT_ID || "default_project",
    agent_id: process.env.WITTY_AGENT_ID || "default_agent",
    workspace_dir: process.env.WITTY_WORKSPACE || repoRoot(),
  });
  const reply = await api.sendPrompt(session.session_id, process.env.WITTY_VERIFY_PROMPT || "hello");
  process.stdout.write(`${JSON.stringify({ health, session, reply })}\n`);
  if (!reply || typeof reply.text !== "string") {
    throw new Error("missing reply text");
  }
}

const verifyOnly = process.argv.includes("--verify");
const launchCheck = process.argv.includes("--launch-check");
const windowChatCheck = process.argv.includes("--window-chat-check");
const windowApprovalCheck = process.argv.includes("--window-approval-check");
const windowUiCheck = process.argv.includes("--window-ui-check");
// 界面改版夹具：切一遍视图并落 PNG 到 .ui-preview，用来对照参考图看现状。
const uiShot = process.argv.includes("--ui-shot");

if (verifyOnly) {
  app.whenReady().then(async () => {
    try {
      await runVerify();
      app.exit(0);
    } catch (error) {
      process.stderr.write(`${error && error.message ? error.message : error}\n`);
      app.exit(1);
    }
  });
} else if (launchCheck) {
  registerIpc();
  app.whenReady().then(() => {
    createWindow();
    if (!mainWindow) {
      process.stderr.write("window not created\n");
      app.exit(1);
      return;
    }
    const timer = setTimeout(() => {
      process.stderr.write("launch-check timeout\n");
      app.exit(1);
    }, 20000);
    mainWindow.webContents.on("did-finish-load", () => {
      const url = mainWindow.webContents.getURL();
      process.stdout.write(`electron-launch-ok ${url}\n`);
      clearTimeout(timer);
      app.exit(0);
    });
    mainWindow.webContents.on("did-fail-load", (_event, code, desc) => {
      process.stderr.write(`did-fail-load ${code} ${desc}\n`);
      clearTimeout(timer);
      app.exit(1);
    });
  });
} else if (windowUiCheck) {
  registerIpc();
  app.whenReady().then(() => {
    createWindow();
    if (!mainWindow) {
      process.stderr.write("window not created\n");
      app.exit(1);
      return;
    }
    const timer = setTimeout(() => {
      process.stderr.write("window-ui-check timeout\n");
      app.exit(1);
    }, 30000);
    mainWindow.webContents.on("console-message", (_event, level, message) => {
      process.stderr.write(`renderer[${level}] ${message}\n`);
    });
    mainWindow.webContents.on("did-finish-load", async () => {
      try {
        const result = await mainWindow.webContents.executeJavaScript(
          `(async () => {
            try {
              if (!window.__wittyTest || !window.__wittyTest.checkDesktopUi) {
                return { ok: false, error: "no checkDesktopUi" };
              }
              return await window.__wittyTest.checkDesktopUi();
            } catch (error) {
              return { ok: false, error: String(error && error.message ? error.message : error) };
            }
          })()`,
        );
        if (!result || !result.ok) {
          throw new Error(`window ui failed: ${JSON.stringify(result)}`);
        }
        process.stdout.write(`window-ui-ok ${JSON.stringify(result)}\n`);
        clearTimeout(timer);
        app.exit(0);
      } catch (error) {
        process.stderr.write(`${error && error.message ? error.message : error}\n`);
        clearTimeout(timer);
        app.exit(1);
      }
    });
    mainWindow.webContents.on("did-fail-load", (_event, code, desc) => {
      process.stderr.write(`did-fail-load ${code} ${desc}\n`);
      clearTimeout(timer);
      app.exit(1);
    });
  });
} else if (uiShot) {
  registerIpc();
  app.whenReady().then(() => {
    createWindow();
    if (!mainWindow) {
      process.stderr.write("window not created\n");
      app.exit(1);
      return;
    }
    const timer = setTimeout(() => {
      process.stderr.write("ui-shot timeout\n");
      app.exit(1);
    }, 60000);
    mainWindow.webContents.on("did-finish-load", async () => {
      try {
        const outDir = process.env.WITTY_SHOT_DIR || path.join(repoRoot(), ".ui-preview");
        fs.mkdirSync(outDir, { recursive: true });
        const views = (process.env.WITTY_SHOT_VIEWS || "chat,skills,memory,tools,settings")
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean);
        const shot = async (name) => {
          const image = await mainWindow.webContents.capturePage();
          fs.writeFileSync(path.join(outDir, `${name}.png`), image.toPNG());
          process.stdout.write(`ui-shot ${name}.png\n`);
        };
        // 先让 boot 走完再拍，否则拍到的是空列表和骨架。
        await mainWindow.webContents.executeJavaScript(
          `(async () => { if (window.__wittyTest && window.__wittyTest.waitBoot) { await window.__wittyTest.waitBoot(); } })()`,
        );
        // boot 里 loadPersisted 会把上次的主题写回去，所以要等 boot 完再切。
        if (process.env.WITTY_SHOT_THEME) {
          await mainWindow.webContents.executeJavaScript(
            `(() => {
              const pick = document.getElementById("theme-pick");
              if (!pick) { return false; }
              pick.value = ${JSON.stringify(process.env.WITTY_SHOT_THEME)};
              pick.dispatchEvent(new Event("change"));
              return true;
            })()`,
          );
          await new Promise((resolve) => setTimeout(resolve, 300));
        }
        if (process.env.WITTY_SHOT_FAKE_CHAT === "1") {
          await mainWindow.webContents.executeJavaScript(
            `(async () => {
              document.getElementById("prompt").value = "你好";
              document.getElementById("composer").requestSubmit();
              await new Promise((resolve) => setTimeout(resolve, 2600));
              const bubble = document.querySelector(".bubble.assistant");
              if (bubble && window.__wittyTest && window.__wittyTest.setEvidence) {
                window.__wittyTest.setEvidence(bubble, [
                  { kind: "file", path: "docs/change_maintenance/PROGRESS.md", text: "当前目标：对话页观感统一" },
                ], "");
              }
              return true;
            })()`,
          );
        }
        // 待办浮层只在模型建了清单后出现，需要时塞一份中间态再拍。
        if (process.env.WITTY_SHOT_TODOS === "1") {
          await mainWindow.webContents.executeJavaScript(
            `(() => {
              if (!window.__wittyTest || !window.__wittyTest.seedTodos) { return false; }
              window.__wittyTest.seedTodos([
                { content: "读取三个季度的台账并对齐口径", status: "completed" },
                { content: "汇总供电可靠性指标，标出越限项", status: "completed" },
                { content: "生成对比图表并落盘 sandbox/report", status: "in_progress" },
                { content: "写结论一页纸，引用图表", status: "pending" },
                { content: "输出 PPT 汇报稿", status: "pending" },
              ]);
              return true;
            })()`,
          );
          await new Promise((resolve) => setTimeout(resolve, 400));
        }
        // 产物栏平时是空的，每张截图都看不出排版。需要时塞几条真文件再拍。
        if (process.env.WITTY_SHOT_ARTIFACTS === "1") {
          const seed = [
            path.join(repoRoot(), "examples", "editable-deck.pptx"),
            path.join(repoRoot(), ".ui-preview", "00-chat.png"),
            path.join(repoRoot(), "docs", "change_maintenance", "CHANGELOG.md"),
            path.join(repoRoot(), "README.md"),
            "/Users/demo/.witty/data/sandbox/work/reports/2026-Q3/季度经营分析.xlsx",
            // 压一压长名字：动作键悬停时浮在标题右上角，得确认它盖得干净、也没把名字挤折行。
            "/Users/demo/.witty/data/sandbox/work/2026年第三季度供电可靠性专项核查底稿汇总.docx",
          ];
          await mainWindow.webContents.executeJavaScript(
            `(() => {
              if (!window.__wittyTest || !window.__wittyTest.seedArtifacts) { return false; }
              window.__wittyTest.seedArtifacts(${JSON.stringify(seed)});
              const toggle = document.getElementById("side-toggle");
              if (toggle && !toggle.hidden && toggle.textContent !== "收起产物栏") { toggle.click(); }
              return true;
            })()`,
          );
          await new Promise((resolve) => setTimeout(resolve, 700));
        }
        // 能力中心的详情是弹窗，落地页那张拍不到，需要时补一张点开第一张卡的。
        const shotSkillModal = process.env.WITTY_SHOT_SKILL_MODAL === "1";
        for (const [index, view] of views.entries()) {
          await mainWindow.webContents.executeJavaScript(
            `(() => {
              const el = document.querySelector('.rail-btn[data-view=${JSON.stringify(view)}]');
              if (el) { el.click(); }
              return !!el;
            })()`,
          );
          await new Promise((resolve) => setTimeout(resolve, 900));
          await shot(`${String(index).padStart(2, "0")}-${view}`);
          if (process.env.WITTY_SHOT_ARTIFACTS === "1" && view === "chat") {
            // 产物行的动作键只在悬停/聚焦时露出，补一张聚焦态的。切页会抢走焦点，
            // 所以只能等这一页拍完再点上去。
            const focused = await mainWindow.webContents.executeJavaScript(
              `(() => {
                const act = document.querySelector(".art-row .art-act");
                if (act) { act.focus(); }
                return !!act;
              })()`,
            );
            if (focused) {
              await new Promise((resolve) => setTimeout(resolve, 400));
              await shot(`${String(index).padStart(2, "0")}-chat-artifact-actions`);
            }
          }
          // 设置页有子面板，落地页只拍得到外观；需要时按面板逐张补。
          if (view === "settings" && process.env.WITTY_SHOT_SETTINGS_PANELS) {
            const panels = process.env.WITTY_SHOT_SETTINGS_PANELS.split(",").map((item) => item.trim()).filter(Boolean);
            for (const panel of panels) {
              const clicked = await mainWindow.webContents.executeJavaScript(
                `(() => {
                  const el = document.querySelector('#settings-nav .catalog-item[data-panel=${JSON.stringify(panel)}]');
                  if (el) { el.click(); }
                  return !!el;
                })()`,
              );
              if (clicked) {
                await new Promise((resolve) => setTimeout(resolve, 500));
                await shot(`${String(index).padStart(2, "0")}-settings-${panel}`);
              }
            }
          }
          if (shotSkillModal && view === "skills") {
            const opened = await mainWindow.webContents.executeJavaScript(
              `(() => {
                const card = document.querySelector("#skill-list .skill-card");
                if (card) { card.click(); }
                return !!card;
              })()`,
            );
            if (opened) {
              await new Promise((resolve) => setTimeout(resolve, 900));
              await shot(`${String(index).padStart(2, "0")}-skills-modal`);
              // 再展开 SKILL.md 正文，看长正文是在窗里滚还是把窗撑穿。
              await mainWindow.webContents.executeJavaScript(
                `(() => {
                  const fold = document.querySelector("#skill-detail details.skill-body");
                  if (fold) { fold.open = true; }
                  return !!fold;
                })()`,
              );
              await new Promise((resolve) => setTimeout(resolve, 500));
              await shot(`${String(index).padStart(2, "0")}-skills-modal-body`);
            }
          }
        }
        clearTimeout(timer);
        app.exit(0);
      } catch (error) {
        process.stderr.write(`${error && error.message ? error.message : error}\n`);
        clearTimeout(timer);
        app.exit(1);
      }
    });
    mainWindow.webContents.on("did-fail-load", (_event, code, desc) => {
      process.stderr.write(`did-fail-load ${code} ${desc}\n`);
      clearTimeout(timer);
      app.exit(1);
    });
  });
} else if (windowChatCheck || windowApprovalCheck) {
  registerIpc();
  app.whenReady().then(() => {
    createWindow();
    if (!mainWindow) {
      process.stderr.write("window not created\n");
      app.exit(1);
      return;
    }
    const timer = setTimeout(() => {
      process.stderr.write(
        windowApprovalCheck ? "window-approval-check timeout\n" : "window-chat-check timeout\n",
      );
      app.exit(1);
    }, 45000);
    mainWindow.webContents.on("console-message", (_event, level, message) => {
      process.stderr.write(`renderer[${level}] ${message}\n`);
    });
    mainWindow.webContents.on("did-finish-load", async () => {
      try {
        const prompt = process.env.WITTY_VERIFY_PROMPT || "hello-window";
        const options = {
          approve: windowApprovalCheck ? process.env.WITTY_TEST_APPROVE || "allow" : "",
          workspace: process.env.WITTY_WORKSPACE || "",
        };
        const result = await mainWindow.webContents.executeJavaScript(
          `(async () => {
            try {
              if (!window.__wittyTest) return { ok: false, error: "no __wittyTest hook" };
              return await window.__wittyTest.runChat(${JSON.stringify(prompt)}, ${JSON.stringify(options)});
            } catch (error) {
              return { ok: false, error: String(error && error.message ? error.message : error) };
            }
          })()`,
        );
        if (!result || !result.ok || !result.reply) {
          throw new Error(`window chat failed: ${JSON.stringify(result)}`);
        }
        if (windowApprovalCheck && !result.approved) {
          throw new Error(`window approval missing: ${JSON.stringify(result)}`);
        }
        const tag = windowApprovalCheck ? "window-approval-ok" : "window-chat-ok";
        process.stdout.write(`${tag} ${result.reply}\n`);
        clearTimeout(timer);
        app.exit(0);
      } catch (error) {
        process.stderr.write(`${error && error.message ? error.message : error}\n`);
        clearTimeout(timer);
        app.exit(1);
      }
    });
    mainWindow.webContents.on("did-fail-load", (_event, code, desc) => {
      process.stderr.write(`did-fail-load ${code} ${desc}\n`);
      clearTimeout(timer);
      app.exit(1);
    });
  });
} else {
  registerIpc();
  app.whenReady().then(async () => {
    if (app.isPackaged) {
      // 安装版没有终端可跑 serve：开窗前先拉起后端并等它就绪，免得首屏报「API 未连接」。
      // 起不来也照样开窗，设置页还有手动「启动 API」入口。
      try {
        await startApi();
        const deadline = Date.now() + 20000;
        for (;;) {
          try {
            await api.health();
            break;
          } catch {
            if (Date.now() > deadline) {
              break;
            }
            await new Promise((resolve) => setTimeout(resolve, 300));
          }
        }
      } catch {
        // ignore, window still opens
      }
    }
    createWindow();
  });
  app.on("window-all-closed", () => {
    stopApiChild();
    app.quit();
  });
  app.on("before-quit", stopApiChild);
}
