"use strict";

function pickSkillFromBrowser() {
  return new Promise((resolve, reject) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".md,text/markdown";
    input.onchange = async () => {
      const file = input.files && input.files[0];
      if (!file) {
        resolve(null);
        return;
      }
      try {
        resolve({ text: await file.text() });
      } catch (err) {
        reject(err);
      }
    };
    input.click();
  });
}

function browserClient() {
  const params = new URLSearchParams(window.location.search);
  const base = (params.get("base") || "http://127.0.0.1:8765").replace(/\/$/, "");
  async function request(method, path, body) {
    const options = { method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(`${base}${path}`, options);
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(payload.error || `${response.status} ${response.statusText}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }
  return {
    apiBase: async () => base,
    health: () => request("GET", "/v1/health"),
    forkSession: (sessionId) => request("POST", `/v1/sessions/${encodeURIComponent(sessionId)}/fork`, {}),
    createSession: (options) => {
      const body = {
        project_id: options.project_id,
        agent_id: options.agent_id,
      };
      if (options.workspace_dir) {
        body.workspace_dir = options.workspace_dir;
      }
      if (options.session_id) {
        body.session_id = options.session_id;
      }
      return request("POST", "/v1/sessions", body);
    },
    sendPrompt: (sessionId, prompt) =>
      request("POST", `/v1/sessions/${sessionId}/messages`, {
        prompt,
        approval_mode: "allow-all",
      }),
    startPrompt: (sessionId, prompt, approvalMode, thinkLevel) =>
      request("POST", `/v1/sessions/${sessionId}/messages`, {
        prompt,
        approval_mode: approvalMode || "always-ask",
        think_level: thinkLevel || "short",
        wait: false,
      }),
    abortSession: (sessionId) => request("POST", `/v1/sessions/${sessionId}/abort`, {}),
    steerSession: (sessionId, text) => request("POST", `/v1/sessions/${sessionId}/steer`, { text }),
    deleteSession: (sessionId, projectId, agentId) =>
      request(
        "DELETE",
        `/v1/sessions/${encodeURIComponent(sessionId)}?project_id=${encodeURIComponent(projectId || "default_project")}&agent_id=${encodeURIComponent(agentId || "default_agent")}`,
      ),
    getModel: (projectId, agentId) =>
      request(
        "GET",
        `/v1/model?project_id=${encodeURIComponent(projectId || "default_project")}&agent_id=${encodeURIComponent(agentId || "default_agent")}`,
      ),
    saveModel: (body) => request("PUT", "/v1/model", body || {}),
    listModels: (projectId, agentId) =>
      request(
        "GET",
        `/v1/models?project_id=${encodeURIComponent(projectId || "default_project")}&agent_id=${encodeURIComponent(agentId || "default_agent")}`,
      ),
    saveModelProfile: (body) => request("PUT", "/v1/models", body || {}),
    deleteModel: (name, projectId, agentId) =>
      request(
        "DELETE",
        `/v1/models/${encodeURIComponent(name)}?project_id=${encodeURIComponent(projectId || "default_project")}&agent_id=${encodeURIComponent(agentId || "default_agent")}`,
      ),
    activateModel: (name, projectId, agentId) =>
      request(
        "POST",
        `/v1/models/${encodeURIComponent(name)}/activate?project_id=${encodeURIComponent(projectId || "default_project")}&agent_id=${encodeURIComponent(agentId || "default_agent")}`,
        { name },
      ),
    getRun: (sessionId) => request("GET", `/v1/sessions/${sessionId}/run`),
    submitApproval: (sessionId, toolCallId, decision) =>
      request("POST", `/v1/sessions/${sessionId}/approval`, {
        tool_call_id: toolCallId,
        decision,
      }),
    submitAnswer: (sessionId, answers) =>
      request("POST", `/v1/sessions/${sessionId}/answer`, { answers }),
    getMessages: (sessionId) => request("GET", `/v1/sessions/${sessionId}/messages`),
    listSessions: (projectId, agentId) =>
      request("GET", `/v1/sessions?project_id=${encodeURIComponent(projectId)}&agent_id=${encodeURIComponent(agentId)}`),
    listSkills: (projectId, agentId) =>
      request("GET", `/v1/skills?project_id=${encodeURIComponent(projectId)}&agent_id=${encodeURIComponent(agentId)}`),
    getSkill: (name) => request("GET", `/v1/skills/${encodeURIComponent(name)}`),
    setSkillEnabled: (name, enabled, projectId, agentId) =>
      request("PUT", `/v1/skills/${encodeURIComponent(name)}`, {
        enabled,
        project_id: projectId,
        agent_id: agentId,
      }),
    installSkill: (body) => request("POST", "/v1/skills", body || {}),
    uninstallSkill: (name, projectId, agentId) =>
      request("DELETE", `/v1/skills/${encodeURIComponent(name)}`, {
        project_id: projectId,
        agent_id: agentId,
      }),
    reloadPlugins: () => request("POST", "/v1/plugins/reload", {}),
    getPlugins: () => request("GET", "/v1/plugins"),
    attachSkillPath: (path) => request("POST", "/v1/plugins/paths", { path }),
    detachSkillPath: (path) => request("DELETE", "/v1/plugins/paths", { path }),
    attachToolPackage: (name, path) =>
      request("POST", "/v1/plugins/packages", { package: name, path: path || "" }),
    detachToolPackage: (name) => request("DELETE", "/v1/plugins/packages", { package: name }),
    pickSkill: () => pickSkillFromBrowser(),
    listTools: (projectId, agentId) =>
      request("GET", `/v1/tools?project_id=${encodeURIComponent(projectId)}&agent_id=${encodeURIComponent(agentId)}`),
    setToolEnabled: (name, enabled, projectId, agentId) =>
      request("PUT", `/v1/tools/${encodeURIComponent(name)}`, {
        enabled,
        project_id: projectId,
        agent_id: agentId,
      }),
    startServer: async () => {
      throw new Error("浏览器里不能拉起 API，请先运行 uv run witty-agent serve");
    },
    listCommands: (sessionId) =>
      request("GET", sessionId ? `/v1/commands?session_id=${encodeURIComponent(sessionId)}` : "/v1/commands"),
    listPrompts: () => request("GET", "/v1/prompts"),
    getPrompt: (name) => request("GET", `/v1/prompts/${encodeURIComponent(name)}`),
    savePrompt: (name, text) => request("PUT", `/v1/prompts/${encodeURIComponent(name)}`, { text }),
    getMemory: (projectId, agentId, workspaceDir, recall) => {
      const query = new URLSearchParams({
        project_id: projectId || "default_project",
        agent_id: agentId || "default_agent",
        scope: "user",
      });
      if (workspaceDir) {
        query.set("workspace_dir", workspaceDir);
      }
      if (recall) {
        query.set("q", recall);
      }
      return request("GET", `/v1/memory?${query.toString()}`);
    },
    saveMemory: (body) => request("POST", "/v1/memory", body || {}),
    saveInbox: (body) => request("POST", "/v1/inbox", body || {}),
    openPath: async () => ({ ok: false, error: "browser" }),
    statPaths: async () => [],
    revealPath: async () => ({ ok: false, error: "browser" }),
    listWorkspace: async () => [],
    getMail: (projectId, agentId) => {
      const query = new URLSearchParams({
        project_id: projectId || "default_project",
        agent_id: agentId || "default_agent",
      });
      return request("GET", `/v1/mail?${query.toString()}`);
    },
    saveMail: (body) => request("PUT", "/v1/mail", body || {}),
    listSchedules: (projectId, agentId) => {
      const query = new URLSearchParams({
        project_id: projectId || "default_project",
        agent_id: agentId || "default_agent",
      });
      return request("GET", `/v1/schedules?${query.toString()}`);
    },
    saveSchedule: (body) => request("PUT", "/v1/schedules", body || {}),
    setScheduleEnabled: (name, enabled, projectId, agentId) => {
      const query = new URLSearchParams({
        project_id: projectId || "default_project",
        agent_id: agentId || "default_agent",
      });
      return request("PATCH", `/v1/schedules/${encodeURIComponent(name)}?${query.toString()}`, {
        enabled: Boolean(enabled),
      });
    },
    deleteSchedule: (name, projectId, agentId) => {
      const query = new URLSearchParams({
        project_id: projectId || "default_project",
        agent_id: agentId || "default_agent",
      });
      return request("DELETE", `/v1/schedules/${encodeURIComponent(name)}?${query.toString()}`);
    },
    tickSchedules: () => request("POST", "/v1/schedules/tick", {}),
    getLinks: (query) => {
      const asked = String(query || "").trim();
      const suffix = asked ? `?q=${encodeURIComponent(asked)}` : "";
      return request("GET", `/v1/links${suffix}`);
    },
    addLink: (body) => request("POST", "/v1/links", body || {}),
    getDiary: (day, list) => {
      const params = new URLSearchParams();
      if (list) {
        params.set("list", "1");
      }
      if (day) {
        params.set("day", day);
      }
      const suffix = params.toString() ? `?${params}` : "";
      return request("GET", `/v1/diary${suffix}`);
    },
    writeDiary: (text, day) => request("POST", "/v1/diary", { text, day: day || "" }),
    getWiki: (workspaceDir) => {
      const query = new URLSearchParams();
      if (workspaceDir) {
        query.set("workspace_dir", workspaceDir);
      }
      const suffix = query.toString() ? `?${query.toString()}` : "";
      return request("GET", `/v1/wiki${suffix}`);
    },
    addWiki: (body) => request("POST", "/v1/wiki", body || {}),
    removeWiki: (sourceId, workspaceDir) => {
      const query = new URLSearchParams();
      if (sourceId) {
        query.set("id", sourceId);
      }
      if (workspaceDir) {
        query.set("workspace_dir", workspaceDir);
      }
      const suffix = query.toString() ? `?${query.toString()}` : "";
      return request("DELETE", `/v1/wiki${suffix}`);
    },
    getWeb: () => request("GET", "/v1/web"),
    saveWeb: (body) => request("PUT", "/v1/web", body || {}),
    pickDirectory: async () => String(window.prompt("工作区路径") || ""),
  };
}

(function startDesktopApp() {
  window.__wittyLastError = null;
  window.addEventListener("error", (event) => {
    window.__wittyLastError = String((event && (event.error && event.error.stack)) || event.message || "error");
  });
  window.addEventListener("unhandledrejection", (event) => {
    window.__wittyLastError = String((event && event.reason && event.reason.stack) || event.reason || "rejection");
  });
  document.body.dataset.js = "1";
  const api = window.witty || browserClient();
  const md = window.wittyMarkdown || { render: (text) => text, escapeHtml: (text) => text };
  const cite = window.wittyCite || {
    visibleCites: (items) => (Array.isArray(items) ? items : []).filter((item) => item && item.kind !== "browse"),
    citeNeedles: (item) => {
      const locator = String((item && item.locator) || "").trim();
      return locator.length >= 3 ? [locator] : [];
    },
    citeNeedle: (item) => String((item && item.locator) || ""),
    citeLabel: (item) => String((item && item.locator) || item.source || ""),
    citeChipText: (item) => String((item && item.locator) || item.source || ""),
    citePreview: (items) => (Array.isArray(items) ? items : []).filter((item) => item && item.kind !== "browse").slice(0, 6),
    citeRest: (items) => (Array.isArray(items) ? items : []).filter((item) => item && item.kind !== "browse").slice(6),
    citeMoreLabel: (count) => (Number(count) > 0 ? `还有 ${count} 条` : ""),
    clipExcerpt: (text) => String(text || ""),
    excerptNeedsFold: () => false,
    evidencePreview: (items) => (Array.isArray(items) ? items : []).slice(0, 4),
    evidenceRest: (items) => (Array.isArray(items) ? items : []).slice(4),
    evidenceMoreLabel: (count) => (Number(count) > 0 ? `其余 ${count} 条` : ""),
  };
  const recall = window.wittyRecall || {
    recallScore: (hit) => Number((hit && hit.score) || 0),
    recallIsWeak: () => false,
    recallScoreMark: () => "",
    recallIsArchive: () => false,
    recallHitsLayer: () => "working",
    recallLayerMark: () => "",
    recallHitCaption: (hit) => String((hit && (hit.title || hit.slug)) || ""),
    recallExcerptPaths: () => [],
    recallReadHint: () => "read",
  };
  const STORAGE_KEY = "witty.desktop.v1";
  const FEEDBACK_KEY = "witty.feedback.v1";
  let skillWatchTimer = 0;
  let skillGenSeen = -1;

  const statusEl = document.getElementById("status");
  const logEl = document.getElementById("log");
  const apiBaseEl = document.getElementById("api-base");
  const projectEl = document.getElementById("project-id");
  const agentEl = document.getElementById("agent-id");
  const workspaceEl = document.getElementById("workspace");
  const promptEl = document.getElementById("prompt");
  const sendBtn = document.getElementById("send");
  const newSessionBtn = document.getElementById("new-session");
  const sessionListEl = document.getElementById("session-list");
  const sessionFilterEl = document.getElementById("session-filter");
  const sessionSearchPanelEl = document.getElementById("session-search-panel");
  const sessionSearchToggleEl = document.getElementById("session-search-toggle");
  const sessionSearchClearEl = document.getElementById("session-search-clear");
  const sessionSearchFieldEl = document.querySelector(".task-search-field");
  const chatTitleEl = document.getElementById("chat-title");
  const chatSubEl = document.getElementById("chat-sub");
  const approvalDock = document.getElementById("approval-dock");
  const todoDock = document.getElementById("todo-dock");
  const queueDock = document.getElementById("queue-dock");
  const skillListEl = document.getElementById("skill-list");
  const skillDetailEl = document.getElementById("skill-detail");
  const skillModalEl = document.getElementById("skill-modal");
  // 分栏和分类记在 loadSkills 外面。装卸技能、开关启用都会重跑一次 loadSkills，
  // 状态放函数里的话每次都弹回「Skill / 精选推荐」，在弹窗里点开关尤其明显。
  let skillHub = "all";
  let skillCat = "all";
  let skillOpenName = "";
  let skillLoadChain = Promise.resolve();
  const toolListEl = document.getElementById("tool-list");
  const toolDetailEl = document.getElementById("tool-detail");
  const approvalModeEl = document.getElementById("approval-mode");
  const composerHintEl = document.getElementById("composer-hint");
  const modelStatusEl = document.getElementById("model-status");
  const modelListEl = document.getElementById("model-list");
  const modelFormTitleEl = document.getElementById("model-form-title");
  const modelNameEl = document.getElementById("model-name");
  const modelDisplayEl = document.getElementById("model-display");
  const modelIdEl = document.getElementById("model-id");
  const modelBaseEl = document.getElementById("model-base");
  const modelKeyEl = document.getElementById("model-key");
  const modelMaxEl = document.getElementById("model-max-tokens");
  const modelTimeoutEl = document.getElementById("model-timeout");
  const saveModelBtn = document.getElementById("save-model");
  const newModelBtn = document.getElementById("new-model");
  const deleteModelBtn = document.getElementById("delete-model");
  const modelPickEl = document.getElementById("model-pick");
  const thinkLevelEl = document.getElementById("think-level");
  const themePickEl = document.getElementById("theme-pick");
  const settingsNavEl = document.getElementById("settings-nav");
  const promptNavEl = document.getElementById("prompt-nav");
  const promptFilterEl = document.getElementById("prompt-filter");
  const promptCountEl = document.getElementById("prompt-count");
  const promptTitleEl = document.getElementById("prompt-title");
  const promptMetaEl = document.getElementById("prompt-meta");
  const promptStatusEl = document.getElementById("prompt-status");
  const promptBodyEl = document.getElementById("prompt-body");
  const savePromptBtn = document.getElementById("save-prompt");
  const revertPromptBtn = document.getElementById("revert-prompt");
  const SETTINGS_SECTIONS = [
    {
      id: "appearance",
      title: "外观",
      hint: "主题",
      icon: '<svg viewBox="0 0 20 20" fill="none"><path d="M10 3.4a6.6 6.6 0 1 0 0 13.2c1 0 1.6-.6 1.6-1.4 0-.6-.5-1-.5-1.6 0-.9.7-1.5 1.6-1.5h1.1a2.8 2.8 0 0 0 2.8-2.8A6.7 6.7 0 0 0 10 3.4Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><circle cx="7" cy="8" r="1" fill="currentColor"/><circle cx="10.6" cy="6.4" r="1" fill="currentColor"/><circle cx="13.6" cy="8.6" r="1" fill="currentColor"/></svg>',
    },
    {
      id: "model",
      title: "模型",
      hint: "接口与钥匙",
      icon: '<svg viewBox="0 0 20 20" fill="none"><rect x="6" y="6" width="8" height="8" rx="1.6" stroke="currentColor" stroke-width="1.4"/><path d="M8 3.4v2.2M12 3.4v2.2M8 14.4v2.2M12 14.4v2.2M3.4 8h2.2M3.4 12h2.2M14.4 8h2.2M14.4 12h2.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    },
    {
      id: "network",
      title: "网络",
      hint: "外网 / 内网",
      icon: '<svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="6.6" stroke="currentColor" stroke-width="1.4"/><path d="M3.6 10h12.8M10 3.6c-3.6 3.6-3.6 9.2 0 12.8M10 3.6c3.6 3.6 3.6 9.2 0 12.8" stroke="currentColor" stroke-width="1.4"/></svg>',
    },
    {
      id: "email",
      title: "邮件",
      hint: "IMAP / SMTP",
      icon: '<svg viewBox="0 0 20 20" fill="none"><rect x="3.4" y="5" width="13.2" height="10" rx="1.6" stroke="currentColor" stroke-width="1.4"/><path d="m4.2 6.2 5.8 4.6 5.8-4.6" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
    },
    {
      id: "schedule",
      title: "定时",
      hint: "创建 / 暂停 / 下次触发",
      icon: '<svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="6.6" stroke="currentColor" stroke-width="1.4"/><path d="M10 6.4V10l2.6 1.8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    },
    {
      id: "workspace",
      title: "工作区",
      hint: "项目 / Agent / 目录",
      icon: '<svg viewBox="0 0 20 20" fill="none"><path d="M3.4 6.2a1.6 1.6 0 0 1 1.6-1.6h3l1.6 2h5.4a1.6 1.6 0 0 1 1.6 1.6v6.2a1.6 1.6 0 0 1-1.6 1.6H5a1.6 1.6 0 0 1-1.6-1.6V6.2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
    },
    {
      id: "prompt",
      title: "提示词",
      hint: "发给模型的配置",
      icon: '<svg viewBox="0 0 20 20" fill="none"><path d="M5 4.75h10A1.75 1.75 0 0 1 16.75 6.5v6A1.75 1.75 0 0 1 15 14.25H9.2L5.4 16.7a.6.6 0 0 1-.9-.52V14.25H5A1.75 1.75 0 0 1 3.25 12.5v-6A1.75 1.75 0 0 1 5 4.75Z" stroke="currentColor" stroke-width="1.4"/><path d="M7 8.4h6M7 11h3.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    },
  ];
  const PROMPT_GROUPS = [
    { id: "system", title: "系统与角色", test: (name) => /^(harness_|system_|tools_attached|vault|denied_|unknown_|invalid_|truncated_|commands$)/.test(name) },
    { id: "guideline", title: "决策指引", test: (name) => name.startsWith("guideline_") },
    { id: "evidence", title: "证据与追溯", test: (name) => /^(evidence_|trace_)/.test(name) },
    { id: "memory", title: "记忆", test: (name) => /^(memory_|recalled_)/.test(name) },
    { id: "dispatch", title: "分配", test: (name) => name.startsWith("dispatch_") },
    { id: "plan", title: "计划", test: (name) => name.startsWith("plan_") },
    { id: "todo", title: "待办", test: (name) => name.startsWith("todo_") },
    { id: "skill", title: "技能", test: (name) => /^(skill_|skills_)/.test(name) },
    { id: "tool", title: "工具说明", test: (name) => name.startsWith("tool_") },
    { id: "loop", title: "循环守卫", test: (name) => /^(stall_|fail_|empty_|answer_|repeat_)/.test(name) },
    { id: "time", title: "时间", test: (name) => name.startsWith("time_") },
    { id: "job", title: "作业与编排", test: (name) => /^(job_|orchestrator_|evolve_|goal_|scheduled_|session_|compaction_|spill_|project_|command_|web_|ask_)/.test(name) },
    { id: "other", title: "其他", test: () => true },
  ];
  let promptRows = [];
  let settingsPanel = "appearance";
  let currentPromptName = "";
  let currentPromptSaved = "";
  const THEME_IDS = ["paper", "day", "glass", "dusk", "pine", "ink"];
  const THEME_LABELS = { paper: "浅色", day: "晴空", glass: "琉璃", dusk: "暮色", pine: "青松", ink: "墨夜" };
  const ICONS = {
    copy: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="7.2" y="6.6" width="8.2" height="10" rx="1.6" stroke="currentColor" stroke-width="1.4"/><path d="M12.4 6.6V5.4A1.6 1.6 0 0 0 10.8 3.8H5.6A1.6 1.6 0 0 0 4 5.4v8.2A1.6 1.6 0 0 0 5.6 15.2h1.6" stroke="currentColor" stroke-width="1.4"/></svg>',
    fork: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="6.2" cy="5.2" r="1.7" stroke="currentColor" stroke-width="1.4"/><circle cx="6.2" cy="14.8" r="1.7" stroke="currentColor" stroke-width="1.4"/><circle cx="14.2" cy="10" r="1.7" stroke="currentColor" stroke-width="1.4"/><path d="M6.2 6.9v6.2M6.2 10h6.3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    retry: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M15.4 10a5.4 5.4 0 1 1-1.5-3.7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M14.2 3.6v3.4h-3.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    up: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M7.2 8.8v7.6H5.2A1.6 1.6 0 0 1 3.6 14.8V10.4A1.6 1.6 0 0 1 5.2 8.8h2Zm0 0 2-4.4A1.7 1.7 0 0 1 10.8 3.4 1.6 1.6 0 0 1 12.4 5.6V8.8h2.8a1.7 1.7 0 0 1 1.7 2l-.7 4.1a1.9 1.9 0 0 1-1.9 1.5H7.2" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
    down: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M7.2 11.2V3.6H5.2A1.6 1.6 0 0 0 3.6 5.2v4.4A1.6 1.6 0 0 0 5.2 11.2h2Zm0 0 2 4.4a1.7 1.7 0 0 0 1.6 1 1.6 1.6 0 0 0 1.6-2.2v-3.2h2.8a1.7 1.7 0 0 0 1.7-2l-.7-4.1A1.9 1.9 0 0 0 14.3 3.6H7.2" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
  };
  const slashPickerEl = document.getElementById("slash-picker");
  const mentionPickerEl = document.getElementById("mention-picker");
  const attachBtn = document.getElementById("attach-files");
  const fileInputEl = document.getElementById("file-input");
  const clipsEl = document.getElementById("composer-clips");
  let composerClips = [];
  let modelProfiles = [];
  let editingModel = "";
  const FALLBACK_COMMANDS = [
    { name: "plan", description: "先规划再改文件；/plan off 跳过规划直接动手", kernel: true },
    { name: "abort", description: "中止当前运行", kernel: true },
    { name: "compact", description: "压缩本会话较早记录；会话忙碌时拒绝", kernel: true },
    { name: "loop", description: "对本会话开循环；/loop 5m 开始，/loop off 停止", kernel: true },
  ];
  const MODE_HINTS = {
    "always-ask": "危险工具会先问你",
    "allow-all": "写文件和命令会直接执行",
    "deny-all": "危险工具一律拒绝",
    "read-only": "只能读，不能改磁盘或执行",
  };

  let sessionId = "";
  let busy = false;
  let runPhase = "idle";
  let streamStallTimer = 0;
  let genId = 0;
  let lastSendError = "";
  let lastFailedPrompt = "";
  let sessions = [];
  let currentView = "chat";
  let pinToBottom = true;
  let lastApproval = { decision: "", tool: "", callId: "" };
  let lastAsk = { id: "", selected: "" };
  let lastEnterSent = false;
  let slashCommands = FALLBACK_COMMANDS.slice();
  let workspaceFiles = [];
  let listedWorkspaceDir = "";
  let sessionArtifacts = [];
  let turnArtifacts = [];
  // 路径 → {exists, size, mtime}。主进程 stat 回来的，只用来点缀副行和标出已删掉的条目。
  const artifactMeta = new Map();
  let artifactMetaPending = false;
  let promptQueue = [];
  let queueSeq = 0;
  let sideOpen = false;
  let railOpen = true;
  let pickerIndex = 0;
  const ARTIFACT_EXT = /\.(pptx|ppt|docx|xlsx|xls|pdf|csv|png|jpe?g|gif|html)$/i;
  const FILE_PATH_RE =
    /(?:[A-Za-z]:)?(?:\/[^\s<>"'`]{1,160}){1,12}\.(?:pptx|ppt|docx|xlsx|xls|pdf|csv|png|jpe?g|gif|html|md|txt)/gi;

  function persistState() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          session_id: sessionId,
          project_id: projectEl.value.trim(),
          agent_id: agentEl.value.trim(),
          workspace_dir: workspaceEl.value.trim(),
          approval_mode: approvalModeEl ? approvalModeEl.value : "always-ask",
          think_level: thinkLevelEl ? thinkLevelEl.value : "short",
          think_wired: true,
          theme: currentTheme(),
          side_open: sideOpen,
          rail_open: railOpen,
        }),
      );
    } catch {
      // ignore quota / private mode
    }
  }

  function loadPersisted() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) {
        return;
      }
      const data = JSON.parse(raw);
      if (data.project_id) {
        projectEl.value = data.project_id;
      }
      if (data.agent_id) {
        agentEl.value = data.agent_id;
      }
      if (data.workspace_dir) {
        workspaceEl.value = data.workspace_dir;
      }
      if (typeof data.session_id === "string") {
        sessionId = data.session_id;
      }
      if (data.approval_mode && approvalModeEl) {
        approvalModeEl.value = data.approval_mode;
      }
      if (thinkLevelEl && data.think_level && ["off", "short", "long"].includes(data.think_level)) {
        thinkLevelEl.value = !data.think_wired && data.think_level === "off" ? "short" : data.think_level;
      }
      applyTheme(data.theme || "glass");
      if (typeof data.side_open === "boolean") {
        sideOpen = data.side_open;
      }
      if (typeof data.rail_open === "boolean") {
        railOpen = data.rail_open;
      }
    } catch {
      sessionId = "";
    }
  }

  function currentTheme() {
    const fromDom = document.documentElement.dataset.theme;
    if (THEME_IDS.includes(fromDom)) {
      return fromDom;
    }
    return themePickEl && THEME_IDS.includes(themePickEl.value) ? themePickEl.value : "glass";
  }

  function applyTheme(name) {
    const id = THEME_IDS.includes(name) ? name : "glass";
    document.documentElement.dataset.theme = id;
    if (themePickEl) {
      themePickEl.value = id;
    }
    document.querySelectorAll(".theme-swatch").forEach((button) => {
      button.classList.toggle("active", button.dataset.theme === id);
    });
    const label = THEME_LABELS[id] || id;
    const railLabel = document.querySelector(".theme-rail .rail-label");
    if (railLabel) {
      railLabel.textContent = label;
    }
    const rail = document.querySelector(".theme-rail");
    if (rail) {
      rail.title = `外观：${label}。点击切换`;
    }
  }

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = `status ${kind}`;
    statusEl.title = text;
  }

  function shortPath(value) {
    const text = String(value || "").trim();
    if (!text) {
      return "";
    }
    const parts = text.split("/").filter(Boolean);
    if (parts.length <= 2) {
      return text;
    }
    return parts.slice(-2).join("/");
  }

  function transcriptPort() {
    return logEl.closest(".transcript-scroll") || logEl;
  }

  function isNearBottom(port) {
    return port.scrollHeight - port.scrollTop - port.clientHeight < 64;
  }

  function scrollThread() {
    if (!pinToBottom) {
      return;
    }
    const port = transcriptPort();
    port.scrollTop = port.scrollHeight;
  }

  function followLatest() {
    pinToBottom = true;
    scrollThread();
  }

  function mountTurn() {
    const prev = logEl.querySelector(".turn.live");
    if (prev) {
      renderTurnFiles(prev, turnArtifacts.slice());
    }
    turnArtifacts = [];
    logEl.querySelectorAll(".turn.live").forEach((node) => node.classList.remove("live"));
    const turn = document.createElement("div");
    turn.className = "turn live";
    const hero = logEl.querySelector(".hero");
    if (hero) {
      hero.remove();
    }
    logEl.appendChild(turn);
    return turn;
  }

  function liveTurn() {
    return logEl.querySelector(".turn.live") || mountTurn();
  }

  function ensureWorkProcess(turn) {
    let wp = turn.querySelector(":scope > .work-process");
    if (!wp) {
      wp = document.createElement("details");
      wp.className = "work-process";
      wp.open = true;
      wp.addEventListener("toggle", (event) => {
        if (event.isTrusted) {
          wp.dataset.userToggled = "1";
        }
        if (wp.open) {
          collapseToolRows(wp);
        }
      });
      const summary = document.createElement("summary");
      summary.innerHTML = "工作过程 · <span class=\"wp-count\">0</span> 条记录";
      const body = document.createElement("div");
      body.className = "wp-body";
      wp.append(summary, body);
      const answer = turn.querySelector(":scope > .say.assistant, :scope > .bubble.assistant, :scope > .bubble.meta");
      if (answer) {
        turn.insertBefore(wp, answer);
      } else {
        turn.appendChild(wp);
      }
    } else if (wp.dataset.userToggled !== "1") {
      wp.open = true;
    }
    return wp;
  }

  function workHost(turn) {
    return ensureWorkProcess(turn).querySelector(".wp-body");
  }

  function recountWork(turn) {
    const body = turn.querySelector(".wp-body");
    const el = turn.querySelector(".wp-count");
    if (el && body) {
      el.textContent = String(body.children.length);
    }
  }

  function collapseToolRows(wp) {
    (wp ? wp.querySelectorAll(".node.tool") : []).forEach((node) => {
      if (node.dataset.userToggled === "1") {
        return;
      }
      node.open = false;
      node.removeAttribute("open");
    });
  }

  function finishWorkProcess(turn) {
    const wp = turn && turn.querySelector(":scope > .work-process");
    if (!wp) {
      return;
    }
    recountWork(turn);
    collapseToolRows(wp);
    if (turn.querySelector(".node.tool.running")) {
      return;
    }
    const body = wp.querySelector(".wp-body");
    if (!body || !body.children.length) {
      wp.remove();
      return;
    }
    if (wp.dataset.userToggled !== "1") {
      wp.open = false;
    }
    renderTurnFiles(turn);
  }

  function scope() {
    return {
      project_id: projectEl.value.trim() || "default_project",
      agent_id: agentEl.value.trim() || "default_agent",
      workspace_dir: workspaceEl.value.trim(),
    };
  }

  function syncRunChrome() {
    const app = document.querySelector(".app");
    if (!app) {
      return;
    }
    const has =
      Boolean(logEl.querySelector(".work-process")) ||
      Boolean(logEl.querySelector(".bubble.user"));
    app.classList.toggle("has-run", has);
    app.classList.toggle("side-collapsed", has && !sideOpen);
    const toggle = document.getElementById("side-toggle");
    if (toggle) {
      toggle.hidden = !has;
      toggle.textContent = sideOpen ? "收起产物栏" : "产物栏";
    }
  }

  function sideCollapseAt() {
    return Math.max(168, Math.round(window.innerWidth * 0.14));
  }

  function applySideWidth(px) {
    const app = document.querySelector(".app");
    if (!app) {
      return;
    }
    const width = Math.round(px);
    app.style.setProperty("--side-w", `${width}px`);
    if (width > sideCollapseAt() + 24) {
      writeSplit("side", width);
    }
  }

  function restoreSideWidth() {
    const saved = Number(readSplits().side || 0);
    const floor = sideCollapseAt() + 24;
    applySideWidth(saved >= floor ? saved : 380);
  }

  function setSideOpen(open) {
    const next = Boolean(open);
    if (next && !sideOpen) {
      restoreSideWidth();
    }
    sideOpen = next;
    persistState();
    syncRunChrome();
  }

  function syncRailChrome() {
    const app = document.querySelector(".app");
    if (!app) {
      return;
    }
    app.classList.toggle("rail-collapsed", !railOpen);
    if (!railOpen) {
      document.querySelectorAll(".rail-pack").forEach((pack) => {
        pack.open = true;
      });
    }
    const toggle = document.getElementById("rail-toggle");
    if (toggle) {
      toggle.setAttribute("aria-expanded", railOpen ? "true" : "false");
      toggle.title = railOpen ? "收起左栏" : "展开左栏";
      toggle.setAttribute("aria-label", toggle.title);
      const label = toggle.querySelector(".rail-label");
      if (label) {
        label.textContent = railOpen ? "收起左栏" : "展开左栏";
      }
    }
  }

  const SPLIT_KEY = "witty.splits.v1";

  function readSplits() {
    try {
      const data = JSON.parse(localStorage.getItem(SPLIT_KEY) || "{}");
      return data && typeof data === "object" ? data : {};
    } catch {
      return {};
    }
  }

  function writeSplit(name, px) {
    const data = readSplits();
    data[name] = Math.round(px);
    try {
      localStorage.setItem(SPLIT_KEY, JSON.stringify(data));
    } catch {
      // ignore quota
    }
  }

  function endResize(handle, pointerId) {
    document.body.classList.remove("is-resizing");
    if (handle && pointerId != null && handle.hasPointerCapture && handle.hasPointerCapture(pointerId)) {
      try {
        handle.releasePointerCapture(pointerId);
      } catch {
        // already released
      }
    }
  }

  function bindDragResize(handle, { min, max, getWidth, setWidth, invert }) {
    if (!handle || handle.dataset.bound === "1") {
      return;
    }
    handle.dataset.bound = "1";
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) {
        return;
      }
      event.preventDefault();
      const origin = event.clientX;
      const start = getWidth();
      const pointerId = event.pointerId;
      const move = (ev) => {
        const delta = invert ? origin - ev.clientX : ev.clientX - origin;
        const ceiling = typeof max === "function" ? max() : max;
        setWidth(Math.min(ceiling, Math.max(min, start + delta)));
      };
      const up = () => {
        window.removeEventListener("pointermove", move, true);
        window.removeEventListener("pointerup", up, true);
        window.removeEventListener("pointercancel", up, true);
        endResize(handle, pointerId);
      };
      window.addEventListener("pointermove", move, true);
      window.addEventListener("pointerup", up, true);
      window.addEventListener("pointercancel", up, true);
      document.body.classList.add("is-resizing");
    });
  }

  function makeSplitHandle(title) {
    const handle = document.createElement("div");
    handle.className = "split-handle";
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.title = title || "拖动调整宽度";
    return handle;
  }

  function bindGridSplit(host, name, fallback, min) {
    if (!host || host.dataset.splitReady === "1") {
      return;
    }
    const first = host.children[0];
    if (!first) {
      return;
    }
    host.dataset.splitReady = "1";
    const handle = makeSplitHandle("拖动调整宽度");
    if (first.nextSibling) {
      host.insertBefore(handle, first.nextSibling);
    } else {
      host.appendChild(handle);
    }
    const saved = Number(readSplits()[name] || 0);
    if (saved >= min) {
      host.style.setProperty("--split-left", `${saved}px`);
    } else if (fallback) {
      host.style.setProperty("--split-left", fallback);
    }
    bindDragResize(handle, {
      min,
      max: () => Math.max(min + 40, host.getBoundingClientRect().width - 220),
      getWidth: () => first.getBoundingClientRect().width,
      setWidth: (px) => {
        host.style.setProperty("--split-left", `${Math.round(px)}px`);
        writeSplit(name, px);
      },
    });
  }

  function bindSideResize() {
    const side = document.getElementById("chat-sidebar");
    const app = document.querySelector(".app");
    const reopen = document.getElementById("side-open-handle");
    if (!side || !app) {
      return;
    }
    const maxW = () => Math.max(320, Math.round(window.innerWidth * 0.48));
    const dragTo = (px) => {
      const width = Math.min(maxW(), Math.max(0, px));
      if (width <= sideCollapseAt()) {
        setSideOpen(false);
        return;
      }
      if (!sideOpen) {
        sideOpen = true;
        persistState();
        syncRunChrome();
      }
      applySideWidth(width);
    };
    if (side.dataset.splitReady !== "1") {
      side.dataset.splitReady = "1";
      const handle = makeSplitHandle("拖动调整产物栏宽度");
      handle.classList.add("split-handle-side");
      side.prepend(handle);
      const saved = Number(readSplits().side || 0);
      if (saved >= sideCollapseAt()) {
        app.style.setProperty("--side-w", `${saved}px`);
      }
      bindDragResize(handle, {
        min: 0,
        invert: true,
        max: maxW,
        getWidth: () => side.getBoundingClientRect().width,
        setWidth: dragTo,
      });
    }
    if (reopen && reopen.dataset.splitReady !== "1") {
      if (reopen.parentElement !== document.body) {
        document.body.appendChild(reopen);
      }
      reopen.dataset.splitReady = "1";
      let ignoreClick = false;
      bindDragResize(reopen, {
        min: 0,
        invert: true,
        max: maxW,
        getWidth: () => 0,
        setWidth: (px) => {
          if (px > 8) {
            ignoreClick = true;
          }
          dragTo(px);
        },
      });
      reopen.addEventListener("click", (event) => {
        if (ignoreClick) {
          ignoreClick = false;
          event.preventDefault();
          return;
        }
        setSideOpen(true);
      });
    }
  }

  function bindAllSplits() {
    restoreRailWidth();
    const side = document.getElementById("side-open-handle");
    if (side) {
      side.setAttribute("hidden", "");
      side.style.pointerEvents = "none";
    }
    const rail = document.getElementById("rail-open-handle");
    if (rail) {
      rail.setAttribute("hidden", "");
      rail.style.pointerEvents = "none";
    }
  }

  function railCollapseAt() {
    return 168;
  }

  function applyRailWidth(px) {
    const app = document.querySelector(".app");
    if (!app || !railOpen) {
      return;
    }
    const ceiling = Math.max(280, Math.round(window.innerWidth * 0.4));
    const width = Math.min(ceiling, Math.max(railCollapseAt() + 8, Math.round(px)));
    app.style.setProperty("--rail-w", `${width}px`);
    if (width > railCollapseAt() + 24) {
      writeSplit("rail", width);
    }
  }

  function restoreRailWidth() {
    const saved = Number(readSplits().rail || 0);
    const floor = railCollapseAt() + 24;
    applyRailWidth(saved >= floor ? saved : 256);
  }

  function setRailOpen(open) {
    const next = Boolean(open);
    if (next && !railOpen) {
      restoreRailWidth();
    }
    railOpen = next;
    persistState();
    syncRailChrome();
  }

  function bindRailResize() {
    const rail = document.querySelector(".rail");
    const app = document.querySelector(".app");
    if (!rail || !app) {
      return;
    }
    const maxW = () => Math.max(280, Math.round(window.innerWidth * 0.4));
    const dragTo = (px) => {
      const width = Math.min(maxW(), Math.max(0, px));
      if (width <= railCollapseAt()) {
        setRailOpen(false);
        return;
      }
      if (!railOpen) {
        railOpen = true;
        persistState();
        syncRailChrome();
      }
      applyRailWidth(width);
    };
    if (rail.dataset.splitReady !== "1") {
      rail.dataset.splitReady = "1";
      const handle = makeSplitHandle("拖动调整左栏宽度");
      handle.classList.add("split-handle-rail");
      rail.appendChild(handle);
      if (railOpen) {
        const saved = Number(readSplits().rail || 0);
        if (saved >= railCollapseAt() + 24) {
          app.style.setProperty("--rail-w", `${saved}px`);
        }
      }
      bindDragResize(handle, {
        min: 0,
        invert: false,
        max: maxW,
        getWidth: () => rail.getBoundingClientRect().width,
        setWidth: dragTo,
      });
    }
    let reopen = document.getElementById("rail-open-handle");
    if (!reopen) {
      reopen = document.createElement("div");
      reopen.id = "rail-open-handle";
      reopen.className = "split-handle split-handle-reopen-rail";
      reopen.setAttribute("role", "separator");
      reopen.setAttribute("aria-orientation", "vertical");
      reopen.title = "拖出左栏";
      document.body.appendChild(reopen);
    }
    if (reopen.dataset.splitReady !== "1") {
      reopen.dataset.splitReady = "1";
      let ignoreClick = false;
      bindDragResize(reopen, {
        min: 0,
        invert: false,
        max: maxW,
        getWidth: () => 0,
        setWidth: (px) => {
          if (px > 8) {
            ignoreClick = true;
          }
          dragTo(px);
        },
      });
      reopen.addEventListener("click", (event) => {
        if (ignoreClick) {
          ignoreClick = false;
          event.preventDefault();
          return;
        }
        setRailOpen(true);
      });
    }
  }

  function showHero() {
    logEl.innerHTML = `
      <div class="hero">
        <h2>你的任务，一句话搞定</h2>
        <p class="muted">选好工作区，直接说要做什么。技能和工具在左边，这里只负责把事做完。</p>
      </div>`;
    chatTitleEl.textContent = "新对话";
    chatSubEl.textContent = shortPath(workspaceEl.value.trim()) || "选择工作区后直接说事";
    renderTodos([]);
    syncWorkspaceChip();
    syncRunChrome();
  }

  function syncWorkspaceChip() {
    const label = document.getElementById("workspace-label");
    if (!label) {
      return;
    }
    const path = workspaceEl && workspaceEl.value ? workspaceEl.value.trim() : "";
    label.textContent = path ? shortPath(path) : "选择工作区";
  }

  async function pickWorkspaceDir() {
    let dir = "";
    if (typeof api.pickDirectory === "function") {
      dir = String((await api.pickDirectory()) || "").trim();
    } else {
      dir = String(window.prompt("工作区路径") || "").trim();
    }
    if (!dir || !workspaceEl) {
      return;
    }
    workspaceEl.value = dir;
    persistState();
    syncWorkspaceChip();
    refreshWorkspaceFiles(true).catch(() => {});
    if (chatSubEl && !logEl.querySelector(".bubble.user")) {
      chatSubEl.textContent = shortPath(dir);
    }
  }

  function fillScene(kind) {
    const scenes = {
      ppt: "做一份 16:9 汇报幻灯片，主题：",
      data: "先质检工作区里的表格，再给出结论：",
      doc: "阅读工作区文档并回答：",
      mail: "看收件箱，列出待办。主机没配就说明缺配置，不要假装发成功。",
      loop: "/loop 5m until 3h",
      skill: "列出当前可用技能，并建议接下来用哪一个。",
    };
    const text = scenes[kind];
    if (!text) {
      return;
    }
    promptEl.value = text;
    fitPrompt();
    promptEl.focus();
  }

  function currentThinkLevel() {
    return thinkLevelEl && ["off", "short", "long"].includes(thinkLevelEl.value)
      ? thinkLevelEl.value
      : "short";
  }

  function addBubble(role, text, reasoning, evidence, traceReason, seal) {
    const node = document.createElement("div");
    node.className = `bubble ${role}`;
    node.dataset.role = role;
    node.dataset.raw = text || "";
    if (role === "assistant") {
      setThinkText(node, reasoning || "", { live: false });
      const body = document.createElement("div");
      body.className = "md";
      try {
        body.innerHTML = md.render(text || "");
      } catch {
        body.textContent = text || "";
      }
      wireFileLinks(body);
      node.appendChild(body);
      setEvidence(node, evidence || [], traceReason || "");
      setSeal(node, seal || "");
    } else if (role === "user") {
      node.textContent = text;
    } else {
      node.textContent = text;
    }
    const host = role === "user" ? mountTurn() : liveTurn();
    if (role === "user" || role === "assistant") {
      const say = document.createElement("div");
      say.className = `say ${role}`;
      say.appendChild(node);
      attachCopyAction(node);
      host.appendChild(say);
    } else {
      host.appendChild(node);
    }
    if (role === "assistant" && (text || seal)) {
      finishWorkProcess(host);
      renderTurnFiles(host);
    }
    syncRunChrome();
    scrollThread();
    return node;
  }

  function bubbleCopyText(node) {
    if (!node) {
      return "";
    }
    return String(node.dataset.raw || (node.querySelector(".md") || {}).textContent || "").trim();
  }

  function iconButton(className, label, svg) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `icon-btn ${className}`;
    button.title = label;
    button.setAttribute("aria-label", label);
    button.innerHTML = svg;
    return button;
  }

  function flashAction(button, label, restore) {
    button.title = label;
    button.setAttribute("aria-label", label);
    window.setTimeout(() => {
      button.title = restore;
      button.setAttribute("aria-label", restore);
    }, 1200);
  }

  function actionRoot(node) {
    return (node && node.closest && node.closest(".say")) || node;
  }

  function attachCopyAction(node) {
    const root = actionRoot(node);
    if (!node || !root || root.querySelector(":scope > .bubble-actions")) {
      return;
    }
    const row = document.createElement("div");
    row.className = "bubble-actions";
    const button = iconButton("bubble-copy", "复制", ICONS.copy);
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const text = bubbleCopyText(node);
      if (!text) {
        return;
      }
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) {
          throw new Error("no clipboard");
        }
        await navigator.clipboard.writeText(text);
        flashAction(button, "已复制", "复制");
      } catch {
        flashAction(button, "复制失败", "复制");
      }
    });
    const fork = iconButton("bubble-fork", "分叉到新会话", ICONS.fork);
    fork.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      try {
        await forkCurrentSession();
      } catch (error) {
        flashAction(fork, "分叉失败", "分叉到新会话");
        setStatus(error.message || "分叉失败", "busy");
      }
    });
    row.append(button, fork);
    if (node.classList.contains("user")) {
      const retry = iconButton("bubble-retry", "重发", ICONS.retry);
      retry.title = "再发这条；如果正在生成，就等本轮结束后再发";
      retry.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const text = bubbleCopyText(node);
        if (!text) {
          return;
        }
        promptEl.value = text;
        fitPrompt();
        if (busy) {
          enqueueFromComposer();
          return;
        }
        sendPrompt({ preventDefault() {} });
      });
      row.append(retry);
    }
    if (node.classList.contains("assistant")) {
      attachRateActions(node, row);
    }
    root.appendChild(row);
  }

  function transcriptMarkdown() {
    const title = String((chatTitleEl && chatTitleEl.textContent) || "对话").trim() || "对话";
    const lines = [`# ${title}`, ""];
    if (sessionId) {
      lines.push(`会话：${sessionId}`, "");
    }
    logEl.querySelectorAll(".bubble.user, .bubble.assistant").forEach((node) => {
      const text = bubbleCopyText(node);
      if (!text) {
        return;
      }
      const role = node.classList.contains("user") ? "用户" : "助手";
      lines.push(`## ${role}`, "", text, "");
    });
    return `${lines.join("\n").trim()}\n`;
  }

  function feedbackStore() {
    try {
      const raw = localStorage.getItem(FEEDBACK_KEY);
      const data = raw ? JSON.parse(raw) : {};
      return data && typeof data === "object" ? data : {};
    } catch {
      return {};
    }
  }

  function writeFeedbackStore(data) {
    try {
      localStorage.setItem(FEEDBACK_KEY, JSON.stringify(data || {}));
    } catch {
      // ignore quota / private mode
    }
  }

  function messageRateKey(text) {
    const body = String(text || "").trim();
    let hash = 0;
    for (let i = 0; i < body.length; i += 1) {
      hash = ((hash << 5) - hash + body.charCodeAt(i)) | 0;
    }
    return `${sessionId || "none"}:${hash}:${body.length}`;
  }

  function getMessageRate(text) {
    const value = feedbackStore()[messageRateKey(text)];
    return value === "up" || value === "down" ? value : "";
  }

  function setMessageRate(text, next) {
    const store = feedbackStore();
    const key = messageRateKey(text);
    if (next === "up" || next === "down") {
      store[key] = next;
    } else {
      delete store[key];
    }
    writeFeedbackStore(store);
    return getMessageRate(text);
  }

  function syncRateButtons(node) {
    const text = bubbleCopyText(node);
    const current = getMessageRate(text);
    const root = actionRoot(node);
    const up = root.querySelector(".bubble-up");
    const down = root.querySelector(".bubble-down");
    if (up) {
      up.classList.toggle("on", current === "up");
      up.setAttribute("aria-pressed", current === "up" ? "true" : "false");
    }
    if (down) {
      down.classList.toggle("on", current === "down");
      down.setAttribute("aria-pressed", current === "down" ? "true" : "false");
    }
  }

  function attachRateActions(node, row) {
    const make = (kind, label, svg) => {
      const button = iconButton(`bubble-rate bubble-${kind}`, label, svg);
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const text = bubbleCopyText(node);
        const current = getMessageRate(text);
        setMessageRate(text, current === kind ? "" : kind);
        syncRateButtons(node);
      });
      return button;
    };
    row.append(make("up", "有用", ICONS.up), make("down", "不准", ICONS.down));
    syncRateButtons(node);
  }

  async function forkCurrentSession() {
    if (!sessionId || typeof api.forkSession !== "function") {
      throw new Error("当前壳没有分叉接口");
    }
    if (busy) {
      throw new Error("生成中不能分叉");
    }
    const child = await api.forkSession(sessionId);
    const next = child && child.session_id;
    if (!next) {
      throw new Error("分叉没有返回会话");
    }
    sessionId = next;
    persistState();
    await hydrateMessages();
    await refreshSessions();
    setStatus(`已分叉 · ${sessionId.slice(0, 8)}`, "ok");
    return child;
  }

  function setThinkText(node, reasoning, options) {
    const opts = options || {};
    const level = currentThinkLevel();
    let panel = node.querySelector(".think");
    if (level === "off" || !reasoning) {
      if (panel && !opts.live) {
        panel.remove();
      }
      if (level === "off") {
        return;
      }
      if (!reasoning) {
        return;
      }
    }
    if (!panel) {
      panel = document.createElement("details");
      panel.className = "think";
      const summary = document.createElement("summary");
      const body = document.createElement("pre");
      body.className = "think-body";
      panel.appendChild(summary);
      panel.appendChild(body);
      const mdBody = node.querySelector(".md");
      if (mdBody) {
        node.insertBefore(panel, mdBody);
      } else {
        node.appendChild(panel);
      }
    }
    panel.classList.toggle("live", Boolean(opts.live));
    panel.querySelector("summary").textContent = opts.live ? "正在思考" : "思考过程";
    const body = panel.querySelector(".think-body");
    const follow = opts.live && body.dataset.userScrolled !== "1";
    body.textContent = reasoning;
    if (!body.dataset.scrollBound) {
      body.dataset.scrollBound = "1";
      body.addEventListener(
        "scroll",
        () => {
          body.dataset.userScrolled = isNearBottom(body) ? "0" : "1";
        },
        { passive: true },
      );
    }
    if (opts.live || level === "long") {
      panel.open = true;
    } else if (opts.collapse) {
      panel.open = false;
    } else if (level === "short" && !opts.live) {
      panel.open = false;
    }
    if (follow) {
      body.scrollTop = body.scrollHeight;
    }
    node.dataset.reasoning = reasoning;
  }

  function setAssistantText(node, text) {
    let body = node.querySelector(".md");
    if (!body) {
      body = document.createElement("div");
      body.className = "md";
      node.appendChild(body);
    }
    try {
      body.innerHTML = md.render(text || "");
    } catch {
      body.textContent = text || "";
    }
    wireFileLinks(body);
    node.dataset.raw = text || "";
    linkifyCites(body, node._evidenceItems || []);
    const say = node.closest && node.closest(".say");
    if (say) {
      const hasSeal = Boolean((node.querySelector(".seal") || {}).textContent);
      say.hidden = !String(text || "").trim() && !hasSeal;
    }
    scrollThread();
  }

  let pendingMemoryFocus = null;
  let pendingMemoryRelocated = null;
  let pendingMemoryReads = Object.create(null);
  let loadedBrowseHits = [];

  function attachLoadedHits(payload, loaded) {
    if (!payload || !Array.isArray(loaded) || !loaded.length) {
      return payload;
    }
    const hits = Array.isArray(payload.hits) ? payload.hits.slice() : [];
    const index = new Map();
    hits.forEach((hit, offset) => {
      const slug = String((hit && (hit.slug || hit.id)) || "").trim();
      const scope = String((hit && hit.scope) || "");
      if (slug) {
        index.set(`${scope}:${slug}`, offset);
      }
    });
    loaded.forEach((hit) => {
      const slug = String((hit && (hit.slug || hit.id)) || "").trim();
      if (!slug) {
        return;
      }
      const scope = String((hit && hit.scope) || "");
      const key = `${scope}:${slug}`;
      if (index.has(key)) {
        const cur = hits[index.get(key)];
        hits[index.get(key)] = { ...cur, loaded: true };
        return;
      }
      hits.push({
        slug,
        id: slug,
        title: (hit && hit.title) || slug,
        text: (hit && (hit.text || hit.excerpt)) || "",
        scope,
        loaded: true,
      });
    });
    return { ...payload, hits };
  }

  function rememberLoadedRead(item) {
    if (!item || item.tool_name !== "memory_read") {
      return;
    }
    const callId = String(item.tool_call_id || item.tool_name || "").trim();
    if (item.type === "tool_execution_start") {
      if (callId) {
        pendingMemoryReads[callId] = item.args || {};
      }
      return;
    }
    if (item.is_error) {
      return;
    }
    const args = item.args || pendingMemoryReads[callId] || {};
    const slug = String((args && args.slug) || "").trim();
    if (!slug) {
      return;
    }
    const scope = String((args && args.scope) || "user");
    const key = `${scope}:${slug}`;
    loadedBrowseHits = loadedBrowseHits.filter((hit) => `${hit.scope || ""}:${hit.slug || hit.id}` !== key);
    loadedBrowseHits.push({
      slug,
      id: slug,
      title: slug,
      text: String(item.text || "").trim(),
      scope,
      loaded: true,
    });
  }

  function attachRelocatedHits(payload, overlay) {
    if (!payload || !overlay || !Array.isArray(overlay.relocated) || !overlay.relocated.length) {
      return payload;
    }
    const locator = String(overlay.id || "").trim();
    const hits = Array.isArray(payload.hits) ? payload.hits : [];
    return {
      ...payload,
      hits: hits.map((hit) => {
        const slug = String((hit && (hit.slug || hit.id)) || "").trim();
        if (locator && slug && slug !== locator) {
          return hit;
        }
        return { ...hit, relocated: overlay.relocated };
      }),
    };
  }

  function useEvidence(item) {
    const locator = String((item && item.locator) || "").trim();
    const source = String((item && item.source) || "").trim();
    const token = locator || source;
    if (!token) {
      return;
    }
    if (source === "memory_read" || source === "memory_status" || item.kind === "memory" || item.kind === "browse") {
      pendingMemoryFocus = { id: locator, scope: String((item && item.scope) || "") };
      if (item.kind === "browse") {
        fillMemoryQuery("");
      } else {
        fillMemoryQuery(archiveQuerySeed({ excerpt: item.excerpt, id: locator }) || locator);
        if (recall.recallIsWeak(item)) {
          const hint =
            typeof recall.recallReadHint === "function" ? recall.recallReadHint(item) : "read";
          insertComposerText(hint);
        }
      }
      const moved = Array.isArray(item.relocated) ? item.relocated.filter((row) => row && (row.to || row.found)) : [];
      pendingMemoryRelocated = moved.length ? { id: locator, relocated: moved } : null;
      switchView("memory");
      highlightMemoryCells([pendingMemoryFocus]);
      return loadMemory().catch(() => {});
    }
    if (source === "skill" || item.kind === "skill") {
      switchView("skills");
      return showSkill(locator).catch(() => {});
    }
    if (looksLikeFilePath(token)) {
      return openLocalPath(token);
    }
    insertComposerText(token);
  }

  function citeChipNode(item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cite-chip";
    button.textContent = cite.citeChipText(item);
    button.title = item.locator || item.source || "";
    button.addEventListener("click", () => useEvidence(item));
    return button;
  }

  function renderCiteRow(node, items) {
    const preview = typeof cite.citePreview === "function" ? cite.citePreview(items) : cite.visibleCites(items).slice(0, 6);
    const rest = typeof cite.citeRest === "function" ? cite.citeRest(items) : cite.visibleCites(items).slice(6);
    let row = node.querySelector(".cite-row");
    if (!preview.length) {
      if (row) {
        row.remove();
      }
      return;
    }
    if (!row) {
      row = document.createElement("div");
      row.className = "cite-row";
      const fold = node.querySelector(".evidence");
      if (fold) {
        node.insertBefore(row, fold);
      } else {
        node.appendChild(row);
      }
    }
    row.replaceChildren();
    preview.forEach((item) => {
      row.appendChild(citeChipNode(item));
    });
    if (!rest.length) {
      return;
    }
    const more = document.createElement("details");
    more.className = "cite-more";
    const summary = document.createElement("summary");
    summary.className = "cite-chip";
    summary.textContent =
      typeof cite.citeMoreLabel === "function" ? cite.citeMoreLabel(rest.length) : `还有 ${rest.length} 条`;
    const extra = document.createElement("span");
    extra.className = "cite-more-list";
    rest.forEach((item) => {
      extra.appendChild(citeChipNode(item));
    });
    more.append(summary, extra);
    wireUserFold(more);
    row.appendChild(more);
  }

  function linkifyCites(root, items) {
    if (!root) {
      return;
    }
    const needles = cite
      .visibleCites(items)
      .map((item) => {
        const list = typeof cite.citeNeedles === "function" ? cite.citeNeedles(item) : [cite.citeNeedle(item)];
        return {
          item,
          needles: (Array.isArray(list) ? list : [])
            .map((needle) => String(needle || "").trim())
            .filter((needle) => needle.length >= 3),
        };
      })
      .filter((row) => row.needles.length)
      .sort((a, b) => Math.max(...b.needles.map((needle) => needle.length)) - Math.max(...a.needles.map((needle) => needle.length)));
    if (!needles.length) {
      return;
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      nodes.push(walker.currentNode);
    }
    const used = new Set();
    nodes.forEach((textNode) => {
      if (!textNode.nodeValue || !textNode.parentElement) {
        return;
      }
      if (textNode.parentElement.closest("button, a, .cite-inline")) {
        return;
      }
      needles.forEach((row) => {
        if (!textNode.parentNode || row.needles.some((needle) => used.has(needle))) {
          return;
        }
        const match = row.needles.find((needle) => textNode.nodeValue.indexOf(needle) >= 0);
        if (!match) {
          return;
        }
        const idx = textNode.nodeValue.indexOf(match);
        const text = textNode.nodeValue;
        const before = document.createTextNode(text.slice(0, idx));
        const button = document.createElement("button");
        button.type = "button";
        button.className = "cite-inline";
        button.textContent = match;
        button.title = `${row.item.source || ""} ${match}`.trim();
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          useEvidence(row.item);
        });
        const after = document.createTextNode(text.slice(idx + match.length));
        const parent = textNode.parentNode;
        parent.insertBefore(before, textNode);
        parent.insertBefore(button, textNode);
        parent.insertBefore(after, textNode);
        parent.removeChild(textNode);
        row.needles.forEach((needle) => used.add(needle));
      });
    });
  }

  function wireUserFold(el) {
    if (!el || el.dataset.wiredFold === "1") {
      return;
    }
    el.dataset.wiredFold = "1";
    el.addEventListener("toggle", (event) => {
      if (event.isTrusted) {
        el.dataset.userToggled = "1";
      }
    });
  }

  function evidenceFoldKey(item) {
    const locator = String((item && item.locator) || "").trim();
    const source = String((item && item.source) || "").trim();
    const scope = String((item && item.scope) || "").trim();
    return `${source}:${scope}:${locator}`;
  }

  function snapshotEvidenceFolds(node) {
    const excerpts = {};
    node.querySelectorAll(".evidence-excerpt").forEach((el) => {
      if (el.dataset.userToggled !== "1") {
        return;
      }
      const key = el.dataset.foldKey || "";
      if (key) {
        excerpts[key] = Boolean(el.open);
      }
    });
    const citeMore = node.querySelector(".cite-more");
    const evMore = node.querySelector(".evidence-more");
    return {
      citeMore: citeMore && citeMore.dataset.userToggled === "1" ? Boolean(citeMore.open) : null,
      evMore: evMore && evMore.dataset.userToggled === "1" ? Boolean(evMore.open) : null,
      excerpts,
    };
  }

  function evidenceItemNode(item) {
    const fold = foldApi();
    const miss = item.ok === false && fold.isEmptyLookup && fold.isEmptyLookup(item.excerpt);
    const mark = item.kind === "browse" ? "浏览" : miss ? "未命中" : item.ok === false ? "失败" : "源头";
    const locator = item.locator ? ` · ${item.locator}` : "";
    const scoped = item.scope ? ` · ${item.scope}` : "";
    const strength = item.kind === "memory" ? recall.recallScoreMark(item) : "";
    const layer =
      item.kind === "memory" && typeof recall.recallLayerMark === "function" ? recall.recallLayerMark(item) : "";
    const scored = strength ? ` · ${strength}` : "";
    const aged = layer ? ` · ${layer}` : "";
    const loaded = item.loaded ? " · 已读" : "";
    const head = `${mark}：${item.source || "tool"}${locator}${scoped}${scored}${aged}${loaded}`;
    const excerpt = String((item && item.excerpt) || "").trim();
    const button = document.createElement("button");
    button.type = "button";
    button.className = "evidence-item";
    button.title = item.locator || item.source || "插入依据";
    button.addEventListener("click", () => useEvidence(item));
    const needsFold = typeof cite.excerptNeedsFold === "function" && cite.excerptNeedsFold(excerpt);
    if (!excerpt || !needsFold) {
      button.textContent = excerpt ? `${head}\n${excerpt}` : head;
      return button;
    }
    const wrap = document.createElement("div");
    wrap.className = "evidence-card";
    button.textContent = head;
    const extra = document.createElement("details");
    extra.className = "evidence-excerpt";
    const summary = document.createElement("summary");
    summary.textContent = typeof cite.clipExcerpt === "function" ? cite.clipExcerpt(excerpt) : excerpt.slice(0, 72);
    const body = document.createElement("p");
    body.className = "evidence-excerpt-body";
    body.textContent = excerpt;
    extra.dataset.foldKey = evidenceFoldKey(item);
    extra.append(summary, body);
    wrap.append(button, extra);
    wireUserFold(extra);
    return wrap;
  }

  function setEvidence(node, items, reason) {
    const rows = Array.isArray(items) ? items : [];
    const why = String(reason || "");
    node._evidenceItems = rows;
    const held = snapshotEvidenceFolds(node);
    let panel = node.querySelector(".evidence");
    const onlyBrowse = rows.length > 0 && rows.every((item) => item.kind === "browse");
    if ((!rows.length && !why) || (onlyBrowse && !why)) {
      if (panel) {
        panel.remove();
      }
      const chips = node.querySelector(".cite-row");
      if (chips) {
        chips.remove();
      }
      renderContextMemory(onlyBrowse ? rows : []);
      return;
    }
    if (!panel) {
      panel = document.createElement("details");
      panel.className = "evidence";
      const summary = document.createElement("summary");
      const body = document.createElement("div");
      body.className = "evidence-body";
      panel.appendChild(summary);
      panel.appendChild(body);
      node.appendChild(panel);
    }
    panel.querySelector("summary").textContent = rows.length
      ? `依据 · ${rows.length} 条`
      : "依据";
    const body = panel.querySelector(".evidence-body");
    body.replaceChildren();
    if (why) {
      const reasonEl = document.createElement("p");
      reasonEl.className = "evidence-reason";
      reasonEl.textContent = `理由：${why}`;
      body.appendChild(reasonEl);
    }
    const preview = typeof cite.evidencePreview === "function" ? cite.evidencePreview(rows) : rows.slice(0, 4);
    const rest = typeof cite.evidenceRest === "function" ? cite.evidenceRest(rows) : rows.slice(4);
    preview.forEach((item) => {
      body.appendChild(evidenceItemNode(item));
    });
    if (rest.length) {
      const more = document.createElement("details");
      more.className = "evidence-more";
      const summary = document.createElement("summary");
      summary.textContent =
        typeof cite.evidenceMoreLabel === "function" ? cite.evidenceMoreLabel(rest.length) : `其余 ${rest.length} 条`;
      const extra = document.createElement("div");
      extra.className = "evidence-more-list";
      rest.forEach((item) => {
        extra.appendChild(evidenceItemNode(item));
      });
      more.append(summary, extra);
      if (held.evMore !== null) {
        more.open = held.evMore;
        more.dataset.userToggled = "1";
      }
      wireUserFold(more);
      body.appendChild(more);
    }
    body.querySelectorAll(".evidence-excerpt").forEach((el) => {
      const key = el.dataset.foldKey || "";
      if (Object.prototype.hasOwnProperty.call(held.excerpts, key)) {
        el.open = held.excerpts[key];
        el.dataset.userToggled = "1";
      }
    });
    node.dataset.evidence = String(rows.length);
    renderCiteRow(node, rows);
    const citeMore = node.querySelector(".cite-more");
    if (citeMore && held.citeMore !== null) {
      citeMore.open = held.citeMore;
      citeMore.dataset.userToggled = "1";
    }
    linkifyCites(node.querySelector(".md"), rows);
    renderContextMemory(rows);
  }

  function setSeal(node, text) {
    const notice = String(text || "").trim();
    let panel = node.querySelector(".seal");
    if (!notice) {
      if (panel) {
        panel.remove();
      }
      delete node.dataset.sealed;
      return;
    }
    if (!panel) {
      panel = document.createElement("details");
      panel.className = "seal";
      panel.open = true;
      const summary = document.createElement("summary");
      const body = document.createElement("p");
      body.className = "seal-body";
      panel.appendChild(summary);
      panel.appendChild(body);
      node.appendChild(panel);
    }
    panel.querySelector("summary").textContent = "未核实";
    panel.querySelector(".seal-body").textContent = notice;
    node.dataset.sealed = "1";
  }

  function foldApi() {
    return window.wittyToolFold || {
      toolLocator: () => "",
      toolLabel: (name, _args, done, failed, missed) =>
        `${failed ? "失败" : missed ? "未命中" : done ? "完成" : "运行"} · ${name || "工具"}`,
      clipResult: (text) => String(text || ""),
      isEmptyLookup: (text) => /^\((?:no matches|no hits)\)$/i.test(String(text || "").trim()),
      stackOpen: (running, held, open, prev) => {
        const live = Number(running) > 0;
        const was = Number(prev || 0) > 0;
        if (live && !was) {
          return true;
        }
        return held ? Boolean(open) : live;
      },
    };
  }

  function parkWorkNote(text) {
    const body = String(text || "").trim();
    if (!body) {
      return;
    }
    const turn = liveTurn();
    const host = workHost(turn);
    const note = document.createElement("div");
    note.className = "wp-note";
    try {
      note.innerHTML = md.render(body);
    } catch {
      note.textContent = body;
    }
    wireFileLinks(note);
    host.appendChild(note);
    recountWork(turn);
  }

  function stepTitle(name, args, done, failed, missed) {
    const loc = foldApi().toolLocator(args);
    const tool = String(name || "工具");
    if (!done) {
      return loc ? `正在执行 · ${tool} · ${loc}` : `正在执行 · ${tool}`;
    }
    if (failed) {
      return loc ? `执行失败 · ${tool} · ${loc}` : `执行失败 · ${tool}`;
    }
    if (missed) {
      return loc ? `未命中 · ${tool} · ${loc}` : `未命中 · ${tool}`;
    }
    if (tool === "write") {
      return loc ? `已写入本地文件 ${loc}` : "已写入文件";
    }
    if (tool === "edit" || tool === "apply_patch") {
      return loc ? `已修改文件 ${loc}` : "已修改文件";
    }
    if (tool === "read") {
      return loc ? `已读取 ${loc}` : "已读取";
    }
    if (tool === "bash" || tool === "exec_command") {
      return loc ? `已执行本地命令 ${loc}` : "已执行本地命令";
    }
    if (tool === "ls") {
      return loc ? `已列出 ${loc}` : "已列出目录";
    }
    if (tool === "grep") {
      return loc ? `已搜索 ${loc}` : "已搜索";
    }
    if (tool === "find") {
      return loc ? `已查找 ${loc}` : "已查找";
    }
    return loc ? `已完成 · ${tool} · ${loc}` : `已完成 · ${tool}`;
  }

  function addIoBlock(host, label, text) {
    const body = String(text || "").trim();
    if (!body) {
      return;
    }
    const cap = document.createElement("div");
    cap.className = "wp-io-label";
    cap.textContent = label;
    const pre = document.createElement("pre");
    pre.textContent = body;
    host.append(cap, pre);
  }

  function fillToolIO(node, args, result) {
    const box = node.querySelector(".wp-io");
    if (!box) {
      return;
    }
    box.replaceChildren();
    if (args && typeof args === "object" && Object.keys(args).length) {
      addIoBlock(box, "输入", formatArgs(args));
    }
    if (result) {
      addIoBlock(box, "输出", foldApi().clipResult(result));
    }
  }

  function syncWorkProcess(turn, running) {
    const wp = ensureWorkProcess(turn);
    const prev = wp.dataset.prevRunning || "0";
    if (running && prev === "0") {
      delete wp.dataset.userToggled;
      wp.open = true;
    }
    wp.dataset.prevRunning = running ? "1" : "0";
    recountWork(turn);
  }

  function addToolNode(callId, name, args, result, done, failed) {
    const id = String(callId || name || "tool");
    const fold = foldApi();
    const turn = liveTurn();
    const host = workHost(turn);
    let node = host.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
    if (!node) {
      node = document.createElement("details");
      node.className = "node tool";
      node.dataset.callId = id;
      node.innerHTML = "<summary></summary><div class=\"wp-io\"></div>";
      node.addEventListener("toggle", (event) => {
        if (event.isTrusted) {
          node.dataset.userToggled = "1";
        }
      });
      host.appendChild(node);
    }
    const err = Boolean(failed);
    const miss = !err && Boolean(done) && fold.isEmptyLookup(result || node._result || "");
    node.classList.toggle("running", !done);
    node.classList.toggle("error", err);
    node.classList.toggle("miss", miss);
    node.dataset.tool = name || "";
    if (args) {
      node._args = args;
    }
    const storedArgs = node._args || args || null;
    if (result) {
      node._result = result;
    }
    const storedResult = result || node._result || "";
    node.querySelector("summary").textContent = stepTitle(name, storedArgs, done, err, miss);
    fillToolIO(node, storedArgs, storedResult);
    if (node.dataset.userToggled !== "1") {
      node.open = false;
      node.removeAttribute("open");
    }
    syncWorkProcess(turn, !done);
    if (done && !turn.querySelector(".node.tool.running")) {
      finishWorkProcess(turn);
    }
    scrollThread();
    return node;
  }

  function formatArgs(args) {
    try {
      return JSON.stringify(args || {}, null, 2);
    } catch {
      return String(args || "");
    }
  }

  let approvalArgsFold = { callId: "", open: true, userToggled: false };
  let approvalKeyHandler = null;
  let approvalBackdropHandler = null;
  let questionDockHold = { qid: "", open: true, userToggled: false, answers: [] };

  function questionKey(questions) {
    const rows = Array.isArray(questions) ? questions : [];
    const ids = rows.map((item) => String((item && item.id) || "").trim());
    if (ids.some(Boolean)) {
      return ids.join("|");
    }
    const texts = rows.map((item) => String((item && (item.question || item.header)) || "").trim());
    if (texts.some(Boolean)) {
      return texts.join("|");
    }
    return "";
  }

  function pendingQuestionKey(question) {
    const key = questionKey((question && question.questions) || question);
    return key ? `q:${key}` : "q:_pending";
  }

  function pendingApprovalKey(pending) {
    const callId = String((pending && pending.tool_call_id) || "").trim();
    if (callId) {
      return callId;
    }
    const tool = String((pending && pending.tool_name) || "").trim();
    const locator = foldApi().toolLocator((pending && pending.args) || {});
    if (tool || locator) {
      return `a:${tool}:${locator}`;
    }
    return "a:_pending";
  }

  function snapshotQuestionAnswers() {
    return Array.from(approvalDock.querySelectorAll(".question-item")).map((block) => ({
      id: block.dataset.qid || "",
      selected: Array.from(block.querySelectorAll(".actions button.picked")).map(
        (button) => button.dataset.label || "",
      ),
      custom: String((block.querySelector(".question-custom") || {}).value || ""),
    }));
  }

  function promptApproval(pending) {
    if (approvalKeyHandler) {
      document.removeEventListener("keydown", approvalKeyHandler);
      approvalKeyHandler = null;
    }
    if (approvalBackdropHandler) {
      approvalDock.removeEventListener("click", approvalBackdropHandler);
      approvalBackdropHandler = null;
    }
    const callId = String((pending && pending.tool_call_id) || "");
    const prevArgs = approvalDock.querySelector("details.approval-args");
    const prevCard = approvalDock.querySelector("[data-role=\"approval\"]");
    if (prevArgs && prevCard && prevCard.dataset.callId === callId) {
      approvalArgsFold = {
        callId,
        open: prevArgs.open,
        userToggled: prevArgs.dataset.userToggled === "1",
      };
    }
    approvalDock.replaceChildren();
    const node = document.createElement("div");
    node.className = "bubble approval";
    node.dataset.role = "approval";
    node.dataset.tool = pending.tool_name || "";
    node.dataset.callId = callId;
    node.setAttribute("role", "dialog");
    node.setAttribute("aria-modal", "true");
    const fold = foldApi();
    const locator = fold.toolLocator(pending.args);
    const head = document.createElement("div");
    head.className = "approval-head";
    const mark = document.createElement("span");
    mark.className = "approval-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = "!";
    const copy = document.createElement("div");
    copy.className = "approval-copy";
    const kicker = document.createElement("p");
    kicker.className = "approval-kicker";
    kicker.textContent = "危险操作";
    const title = document.createElement("div");
    title.className = "approval-title";
    title.textContent = locator
      ? `需要批准：${pending.tool_name || "工具"} · ${locator}`
      : `需要批准：${pending.tool_name || "工具"}`;
    copy.append(kicker, title);
    head.append(mark, copy);
    const args = document.createElement("details");
    args.className = "approval-args";
    const raw = formatArgs(pending.args);
    if (approvalArgsFold.callId === callId && approvalArgsFold.userToggled) {
      args.open = approvalArgsFold.open;
      args.dataset.userToggled = "1";
    } else {
      args.open = raw.length < 400;
    }
    args.addEventListener("toggle", (event) => {
      if (event.isTrusted) {
        args.dataset.userToggled = "1";
        approvalArgsFold = { callId, open: args.open, userToggled: true };
      }
    });
    const summary = document.createElement("summary");
    summary.textContent = "参数";
    const body = document.createElement("pre");
    body.textContent = fold.clipResult(raw, 2000);
    args.append(summary, body);
    const actions = document.createElement("div");
    actions.className = "actions";
    const allowBtn = document.createElement("button");
    allowBtn.type = "button";
    allowBtn.className = "primary";
    allowBtn.textContent = "允许";
    const denyBtn = document.createElement("button");
    denyBtn.type = "button";
    denyBtn.className = "deny";
    denyBtn.textContent = "拒绝";
    actions.append(denyBtn, allowBtn);
    node.append(head, args, actions);
    approvalDock.appendChild(node);
    const onBackdrop = (event) => {
      if (event.target === approvalDock) {
        denyBtn.click();
      }
    };
    approvalBackdropHandler = onBackdrop;
    approvalDock.addEventListener("click", onBackdrop);
    const done = new Promise((resolve) => {
      allowBtn.addEventListener("click", () => resolve("allow"));
      denyBtn.addEventListener("click", () => resolve("deny"));
    });
    const onKey = (event) => {
      if (event.key !== "Escape") {
        return;
      }
      event.preventDefault();
      denyBtn.click();
    };
    approvalKeyHandler = onKey;
    document.addEventListener("keydown", onKey);
    const auto = window.__wittyTest && window.__wittyTest.autoApprove;
    if (auto === "allow" || auto === "deny") {
      (auto === "deny" ? denyBtn : allowBtn).click();
    }
    return done.then((decision) => {
      document.removeEventListener("keydown", onKey);
      approvalDock.removeEventListener("click", onBackdrop);
      if (approvalBackdropHandler === onBackdrop) {
        approvalBackdropHandler = null;
      }
      if (approvalKeyHandler === onKey) {
        approvalKeyHandler = null;
      }
      lastApproval = {
        decision,
        tool: pending.tool_name || "",
        callId: pending.tool_call_id || "",
      };
      approvalDock.replaceChildren();
      if (promptEl) {
        promptEl.focus();
      }
      return decision;
    });
  }

  function promptQuestion(payload) {
    const questions = Array.isArray(payload && payload.questions) ? payload.questions : [];
    const qid = questionKey(questions);
    const prevFold = approvalDock.querySelector("details.question-fold");
    const prevCard = approvalDock.querySelector("[data-role=\"question\"]");
    if (prevFold && prevCard && prevCard.dataset.qid === qid) {
      questionDockHold = {
        qid,
        open: prevFold.open,
        userToggled: prevFold.dataset.userToggled === "1",
        answers: snapshotQuestionAnswers(),
      };
    }
    approvalDock.replaceChildren();
    const node = document.createElement("div");
    node.className = "bubble approval question";
    node.dataset.role = "question";
    node.dataset.qid = qid;
    node.dataset.count = String(questions.length);
    node.setAttribute("role", "dialog");
    node.setAttribute("aria-modal", "true");
    const head = document.createElement("div");
    head.className = "approval-head";
    const mark = document.createElement("span");
    mark.className = "approval-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = "?";
    const copy = document.createElement("div");
    copy.className = "approval-copy";
    const kicker = document.createElement("p");
    kicker.className = "approval-kicker";
    kicker.textContent = "需要你选择";
    const heading = document.createElement("div");
    heading.className = "approval-title";
    heading.textContent =
      questions.length > 1 ? `需要你选择 · ${questions.length} 问` : questions[0] && questions[0].question
        ? questions[0].question
        : "需要你选择";
    copy.append(kicker, heading);
    head.append(mark, copy);
    const list = document.createElement("div");
    list.className = "question-list";
    const held = questionDockHold.qid === qid ? questionDockHold : null;
    const state = questions.map((item, index) => {
      const prev =
        (held &&
          held.answers.find((row) => row.id && row.id === (item.id || ""))) ||
        (held && held.answers[index]) ||
        null;
      return {
        id: item.id || "",
        selected: prev && Array.isArray(prev.selected) ? prev.selected.filter(Boolean) : [],
        custom: prev ? String(prev.custom || "") : "",
        multi: Boolean(item.multi_select),
        options: Array.isArray(item.options) ? item.options : [],
      };
    });

    function paintPicked(block, index) {
      block.querySelectorAll(".actions button").forEach((button) => {
        button.classList.toggle("picked", state[index].selected.includes(button.dataset.label || ""));
      });
    }

    let settle;
    const done = new Promise((resolve) => {
      settle = resolve;
    });

    function finish() {
      const answers = state.map((item) => {
        const row = { id: item.id, selected: item.selected.filter(Boolean) };
        if (item.custom.trim()) {
          row.custom = item.custom.trim();
        }
        return row;
      });
      lastAsk = {
        id: answers[0] ? answers[0].id : "",
        selected: answers[0] && answers[0].selected && answers[0].selected[0] ? answers[0].selected[0] : "",
        count: answers.length,
        custom: (answers[1] && answers[1].custom) || (answers[0] && answers[0].custom) || "",
      };
      questionDockHold = { qid: "", open: true, userToggled: false, answers: [] };
      approvalDock.replaceChildren();
      if (promptEl) {
        promptEl.focus();
      }
      settle(answers);
    }

    questions.forEach((item, index) => {
      const block = document.createElement("div");
      block.className = "question-item";
      block.dataset.qid = item.id || "";
      if (questions.length > 1) {
        const text = document.createElement("p");
        text.className = "question-text";
        text.textContent = item.question || `问题 ${index + 1}`;
        block.appendChild(text);
      }
      const actions = document.createElement("div");
      actions.className = "actions";
      state[index].options.forEach((opt) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "question-opt";
        button.textContent = opt.label || opt.description || "选项";
        button.dataset.label = opt.label || "";
        button.addEventListener("click", () => {
          const label = button.dataset.label || "";
          if (state[index].multi) {
            const pos = state[index].selected.indexOf(label);
            if (pos >= 0) {
              state[index].selected.splice(pos, 1);
            } else {
              state[index].selected.push(label);
            }
          } else {
            state[index].selected = label ? [label] : [];
          }
          paintPicked(block, index);
          if (questions.length === 1 && !state[index].multi && state[index].options.length) {
            finish();
          }
        });
        actions.appendChild(button);
      });
      const custom = document.createElement("input");
      custom.type = "text";
      custom.className = "question-custom";
      custom.placeholder = state[index].options.length ? "其他（可选）" : "请输入";
      if (state[index].custom) {
        custom.value = state[index].custom;
      }
      custom.addEventListener("input", () => {
        state[index].custom = custom.value;
      });
      custom.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          finish();
        }
      });
      block.append(actions, custom);
      list.appendChild(block);
      paintPicked(block, index);
    });

    const fold = document.createElement("details");
    fold.className = "question-fold";
    if (held && held.userToggled) {
      fold.open = held.open;
      fold.dataset.userToggled = "1";
    } else {
      fold.open = true;
    }
    fold.addEventListener("toggle", (event) => {
      if (event.isTrusted) {
        fold.dataset.userToggled = "1";
        questionDockHold = {
          qid,
          open: fold.open,
          userToggled: true,
          answers: snapshotQuestionAnswers(),
        };
      }
    });
    const summary = document.createElement("summary");
    summary.textContent = questions.length > 1 ? `选项 · ${questions.length} 问` : "选项";
    fold.append(summary, list);

    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "primary question-submit";
    submit.textContent = "提交";
    submit.addEventListener("click", finish);
    node.append(head, fold, submit);
    approvalDock.appendChild(node);

    const auto = window.__wittyTest && window.__wittyTest.autoAnswer;
    if (typeof auto === "string" && auto) {
      const match = node.querySelector(`button[data-label="${auto}"]`);
      if (match) {
        match.click();
      }
    }
    return done;
  }

  async function apiBaseValue() {
    if (typeof api.apiBase === "function") {
      return api.apiBase();
    }
    return "http://127.0.0.1:8765";
  }

  function mergeStreamPiece(current, piece) {
    const now = String(current || "");
    const next = String(piece || "");
    if (!next) {
      return now;
    }
    if (!now || next === now) {
      return next;
    }
    if (next.startsWith(now)) {
      return next;
    }
    return now + next;
  }

  function applyEvent(item, live) {
    if (!item || !item.type) {
      return live;
    }
    if (typeof item.seq === "number") {
      if (!live.seen) {
        live.seen = new Set();
      }
      if (live.seen.has(item.seq)) {
        return live;
      }
      live.seen.add(item.seq);
    }
    if (item.type !== "text_delta" && item.type !== "reasoning_delta") {
      clearStreamStall();
    }
    if (item.type === "stream_reset") {
      live.text = "";
      live.reasoning = "";
      if (live.node) {
        setAssistantText(live.node, "");
        setThinkText(live.node, "", { live: false });
      }
      return live;
    }
    if (item.type === "reasoning_delta") {
      markRunPhase("streaming");
      clearWaiting();
      if (!live.node) {
        live.node = addBubble("assistant", "", "");
        live.text = "";
        live.reasoning = "";
      }
      live.reasoning = mergeStreamPiece(live.reasoning || "", item.text || "");
      setThinkText(live.node, live.reasoning, { live: true });
      scrollThread();
      armStreamStall();
      return live;
    }
    if (item.type === "text_delta") {
      if (runPhase !== "gated" && runPhase !== "tools") {
        markRunPhase("streaming");
        clearWaiting();
      }
      if (!live.node) {
        live.node = addBubble("assistant", "");
        live.text = "";
      }
      live.text = mergeStreamPiece(live.text || "", item.text || "");
      setAssistantText(live.node, live.text);
      if (live.reasoning) {
        setThinkText(live.node, live.reasoning, { live: false, collapse: currentThinkLevel() === "short" });
      }
      armStreamStall();
      return live;
    }
    if (
      item.type === "message_end" &&
      (item.source === "plugin:recalled-verify" || item.source === "plugin:browse-read")
    ) {
      const recalledEl = document.getElementById("memory-recalled");
      if (currentView === "memory" || (recalledEl && recalledEl.childElementCount > 0)) {
        live.memoryRefresh = refreshRecalled();
      }
      return live;
    }
    if (item.type === "message_end" && item.role === "assistant") {
      if (item.source === "plugin:evidence-seal") {
        live.sealed = item.text || live.sealed || "";
        if (live.node) {
          setSeal(live.node, live.sealed);
        }
        return live;
      }
      const hasBody = Boolean(
        item.text || item.reasoning || item.trace_reason || (item.evidence && item.evidence.length),
      );
      if (!hasBody) {
        return live;
      }
      clearWaiting();
      if (!live.node) {
        live.node = addBubble("assistant", item.text || "", item.reasoning || "", item.evidence, item.trace_reason);
      }
      live.text = item.text || live.text || "";
      live.reasoning = item.reasoning || live.reasoning || "";
      live.evidence = item.evidence || live.evidence || [];
      live.traceReason = item.trace_reason || live.traceReason || "";
      setAssistantText(live.node, live.text);
      if (live.reasoning) {
        setThinkText(live.node, live.reasoning, { live: false, collapse: currentThinkLevel() === "short" });
      }
      setEvidence(live.node, live.evidence, live.traceReason);
      const pendingTools = Array.isArray(item.tool_calls) ? item.tool_calls : [];
      if (pendingTools.some((call) => call && call.name === "ask_user_question")) {
        markRunPhase("gated");
        showWaiting("等待你选择…");
      } else if (pendingTools.length) {
        markRunPhase("tools");
        const name = String((pendingTools[0] && pendingTools[0].name) || "").trim();
        showWaiting(`正在调用 ${name || "工具"}…`);
      } else {
        markRunPhase("done");
      }
      return live;
    }
    if (item.type === "done") {
      endRunChrome();
      markRunPhase("idle");
      if (live.node) {
        setEvidence(live.node, item.evidence || live.evidence || [], item.trace_reason || live.traceReason || "");
        if (item.sealed || live.sealed) {
          setSeal(live.node, item.sealed || live.sealed || "");
        }
        collectArtifactPaths(live.text).forEach(noteArtifact);
        wireFileLinks(live.node.querySelector(".md"));
      }
      refreshWorkspaceFiles(true).catch(() => {});
      return live;
    }
    if (item.type === "tool_preparing") {
      const name = String(item.tool_name || "");
      if (name === "ask_user_question") {
        markRunPhase("gated");
        showWaiting("正在准备选择题…");
      } else {
        markRunPhase("tools");
        showWaiting(`正在调用 ${name || "工具"}…`);
      }
      return live;
    }
    if (item.type === "tool_execution_start") {
      if (item.tool_name === "ask_user_question") {
        markRunPhase("gated");
        showWaiting("等待你选择…");
      } else {
        markRunPhase("tools");
        showWaiting(`正在调用 ${item.tool_name || "工具"}…`);
      }
      rememberLoadedRead(item);
      if (String(live.text || "").trim()) {
        parkWorkNote(live.text);
        live.text = "";
        if (live.node) {
          setAssistantText(live.node, "");
        }
      }
      addToolNode(item.tool_call_id || item.tool_name, item.tool_name, item.args, "", false);
      return live;
    }
    if (item.type === "tool_execution_end") {
      rememberLoadedRead(item);
      noteArtifactsFromTool(item.args, item.text || "");
      addToolNode(
        item.tool_call_id || item.tool_name,
        item.tool_name,
        item.args,
        item.text || "",
        true,
        Boolean(item.is_error),
      );
      if (busy && runPhase !== "gated" && runPhase !== "idle") {
        showWaiting("正在思考…");
      }
      return live;
    }
    if (item.type === "todos") {
      const rows = (item.args && item.args.todos) || item.todos || [];
      renderTodos(rows);
      return live;
    }
    if (isTerminalEvent(item)) {
      endRunChrome();
    }
    return live;
  }

  function parseSseBuffer(buffer) {
    const events = [];
    let rest = String(buffer || "");
    const gap = /\r?\n\r?\n/;
    while (true) {
      const found = rest.match(gap);
      if (!found || found.index == null) {
        break;
      }
      const block = rest.slice(0, found.index);
      rest = rest.slice(found.index + found[0].length);
      const dataLines = block
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).replace(/^\s/, ""));
      if (!dataLines.length) {
        continue;
      }
      try {
        events.push(JSON.parse(dataLines.join("\n")));
      } catch {
        events.push({ type: "error", error: "bad sse" });
      }
    }
    return { events, rest };
  }

  async function fetchRun(sid) {
    if (window.__wittyTest && typeof window.__wittyTest.getRun === "function") {
      return window.__wittyTest.getRun(sid);
    }
    return api.getRun(sid);
  }

  async function readRunStream(sid, onFrame) {
    if (window.__wittyTest && typeof window.__wittyTest.openRunStream === "function") {
      const stream = window.__wittyTest.openRunStream(sid);
      if (stream && typeof stream[Symbol.asyncIterator] === "function") {
        for await (const item of stream) {
          await onFrame(item);
        }
        return;
      }
    }
    const base = await apiBaseValue();
    const controller = new AbortController();
    const response = await fetch(`${base}/v1/sessions/${encodeURIComponent(sid)}/stream`, {
      headers: { Accept: "text/event-stream" },
      signal: controller.signal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`stream ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (busy) {
        const chunk = await reader.read();
        if (chunk.done) {
          break;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        const parsed = parseSseBuffer(buffer);
        buffer = parsed.rest;
        for (const item of parsed.events) {
          const stop = await onFrame(item);
          if (stop || isTerminalEvent(item)) {
            return;
          }
        }
      }
    } finally {
      controller.abort();
      try {
        await reader.cancel();
      } catch {
        // stream already closed
      }
    }
  }

  async function watchRun(sid, onEvent, startCursor) {
    let cursor = Number(startCursor) || 0;
    const seenPending = new Set();

    const consume = async (run) => {
      if (run.todos) {
        renderTodos(run.todos);
      }
      if (run.plan) {
        renderPlan(run.plan);
      }
      const timeline = run.timeline || [];
      for (; cursor < timeline.length; cursor += 1) {
        onEvent(timeline[cursor]);
      }
      if (run.status === "awaiting_approval" && run.pending) {
        markRunPhase("gated");
        showWaiting("等待你批准…");
        const callId = pendingApprovalKey(run.pending);
        if (!seenPending.has(callId)) {
          seenPending.add(callId);
          const decision = await promptApproval(run.pending);
          if (window.__wittyTest && typeof window.__wittyTest.submitApproval === "function") {
            await window.__wittyTest.submitApproval(sid, run.pending.tool_call_id || "", decision);
          } else {
            await api.submitApproval(sid, run.pending.tool_call_id || "", decision);
          }
          showWaiting("继续生成…");
        }
      }
      if (run.status === "awaiting_question" && run.question) {
        markRunPhase("gated");
        showWaiting("等待你选择…");
        const qid = pendingQuestionKey(run.question);
        if (!seenPending.has(qid)) {
          seenPending.add(qid);
          const answers = await promptQuestion(run.question);
          if (window.__wittyTest && typeof window.__wittyTest.submitAnswer === "function") {
            await window.__wittyTest.submitAnswer(sid, answers);
          } else {
            await api.submitAnswer(sid, answers);
          }
          showWaiting("继续生成…");
        }
      }
      return run;
    };

    const fromEvent = async (item) => {
      onEvent(item);
      if (item.type === "approval_required") {
        await consume({
          status: "awaiting_approval",
          pending: {
            tool_name: item.tool_name,
            tool_call_id: item.tool_call_id,
            args: item.args,
          },
        });
        return null;
      }
      if (item.type === "question_required") {
        await consume({
          status: "awaiting_question",
          question: { questions: item.questions || [] },
        });
        return null;
      }
      if (item.type === "done" || item.type === "error") {
        try {
          return await consume(await fetchRun(sid));
        } catch {
          return {
            status: item.type === "error" ? "error" : "done",
            text: item.text || "",
            error: item.error || "",
            timeline: [],
          };
        }
      }
      return null;
    };

    if (!(window.__wittyTest && window.__wittyTest.skipStream)) {
      try {
        let streamed = null;
        await readRunStream(sid, async (item) => {
          const run = await fromEvent(item);
          if (run) {
            streamed = run;
          }
          return Boolean(run && (run.status === "done" || run.status === "error"));
        });
        if (streamed && (streamed.status === "done" || streamed.status === "error")) {
          return streamed;
        }
        const latest = await fetchRun(sid).catch(() => streamed);
        if (latest) {
          return consume(latest);
        }
      } catch {
        // fall back to snapshot poll
      }
    }

    const deadline = Date.now() + 300000;
    let run = await fetchRun(sid);
    run = await consume(run);
    while (Date.now() < deadline && run.status !== "done" && run.status !== "error" && busy) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      run = await fetchRun(sid);
      run = await consume(run);
    }
    return run;
  }

  async function completePrompt(sid, prompt) {
    const mode = (approvalModeEl && approvalModeEl.value) || "always-ask";
    let run = await api.startPrompt(sid, prompt, mode, currentThinkLevel());
    const live = { node: null, text: "", reasoning: "", evidence: [], traceReason: "", sealed: "", seen: new Set() };
    if (run.status === "done" && run.text) {
      live.node = addBubble(
        "assistant",
        run.text,
        run.reasoning || "",
        run.evidence,
        run.trace_reason,
        run.sealed,
      );
      return run;
    }
    const handle = (item) => {
      applyEvent(item, live);
    };
    const initial = run.timeline || [];
    initial.forEach(handle);
    if (run.status === "done" || run.status === "error") {
      if (!live.node && run.text) {
        addBubble("assistant", run.text, run.reasoning || "", run.evidence, run.trace_reason, run.sealed);
      } else if (live.node && (run.sealed || live.sealed)) {
        setSeal(live.node, run.sealed || live.sealed);
      }
      return run;
    }
    run = await watchRun(sid, handle, initial.length);
    finishWorkProcess(logEl.querySelector(".turn.live"));
    if (!live.node && run.text) {
      addBubble("assistant", run.text, run.reasoning || "", run.evidence, run.trace_reason, run.sealed);
    } else if (live.node) {
      setEvidence(live.node, run.evidence || live.evidence || [], run.trace_reason || live.traceReason || "");
      if (run.sealed || live.sealed) {
        setSeal(live.node, run.sealed || live.sealed);
      }
    }
    return run;
  }

  async function hydrateMessages() {
    if (!sessionId) {
      showHero();
      return;
    }
    sessionArtifacts = [];
    turnArtifacts = [];
    clearPromptQueue();
    try {
      const payload = await api.getMessages(sessionId);
      logEl.replaceChildren();
      let lastAssistant = null;
      const rows = payload.messages || [];
      for (let index = 0; index < rows.length; index += 1) {
        const message = rows[index];
        if (index && index % 8 === 0) {
          await new Promise((resolve) => {
            window.setTimeout(resolve, 0);
          });
        }
        const source = String(message.source || "");
        if (message.role === "user") {
          if (source.startsWith("plugin:")) {
            continue;
          }
          addBubble("user", message.text || "");
        } else if (message.role === "assistant") {
          if (source === "plugin:evidence-seal") {
            if (lastAssistant) {
              setSeal(lastAssistant, message.text || "");
            }
            continue;
          }
          if (source.startsWith("plugin:")) {
            continue;
          }
          const hasBody = Boolean(
            message.text || message.reasoning || message.trace_reason || (message.evidence && message.evidence.length),
          );
          if (hasBody) {
            lastAssistant = addBubble(
              "assistant",
              message.text || "",
              message.reasoning || "",
              message.evidence,
              message.trace_reason,
            );
          }
          for (const call of message.tool_calls || []) {
            noteArtifactsFromTool(call.arguments, "");
            addToolNode(call.id || call.name, call.name, call.arguments, "", true);
          }
        } else if (message.role === "toolResult") {
          noteArtifactsFromTool(null, message.text || "");
          addToolNode(
            message.tool_call_id || message.tool_name,
            message.tool_name || "tool",
            null,
            message.text || "",
            true,
            Boolean(message.is_error),
          );
        }
      }
      if (!logEl.children.length) {
        showHero();
      }
      logEl.querySelectorAll(".turn").forEach((turn) => finishWorkProcess(turn));
      followLatest();
      chatTitleEl.textContent = payload.title || "未命名任务";
      renderTodos(payload.todos);
      renderPlan(payload.plan);
      setStatus(`已连接 · ${sessionId.slice(0, 8)}`, "ok");
      refreshWorkspaceFiles(true).catch(() => {});
    } catch {
      sessionId = "";
      persistState();
      showHero();
      addBubble("meta", "上次会话已失效，请新建会话。");
    }
  }

  function sessionDayKey(stamp) {
    const raw = Number(stamp) || 0;
    if (!raw) {
      return "";
    }
    const ms = raw > 1e12 ? raw : raw * 1000;
    const date = new Date(ms);
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${date.getFullYear()}-${month}-${day}`;
  }

  function sessionDayLabel(key) {
    if (!key || key === "older") {
      return "更早";
    }
    const today = sessionDayKey(Date.now());
    const prior = new Date();
    prior.setDate(prior.getDate() - 1);
    const yday = sessionDayKey(prior.getTime());
    if (key === today) {
      return "今天";
    }
    if (key === yday) {
      return "昨天";
    }
    const parts = String(key).split("-");
    if (parts.length === 3) {
      return `${Number(parts[1])}月${Number(parts[2])}日`;
    }
    return key;
  }

  function sessionWhenLabel(stamp) {
    const key = sessionDayKey(stamp);
    if (!key) {
      return "";
    }
    const day = sessionDayLabel(key);
    const today = sessionDayKey(Date.now());
    const raw = Number(stamp) || 0;
    const ms = raw > 1e12 ? raw : raw * 1000;
    const date = new Date(ms);
    if (Number.isNaN(date.getTime())) {
      return day;
    }
    const hh = String(date.getHours()).padStart(2, "0");
    const mm = String(date.getMinutes()).padStart(2, "0");
    if (key === today) {
      return `今天 ${hh}:${mm}`;
    }
    return `${day} ${hh}:${mm}`;
  }

  function sessionTitle(item) {
    const raw = String((item && item.title) || "").trim();
    if (raw) {
      return raw;
    }
    // 列表里挂一串十六进制（3bc52bea…）没人看得懂，短 ID 留给状态点。
    return "未命名任务";
  }

  function sessionRefToken(item) {
    const id = String((item && item.session_id) || "").trim();
    return id ? `session:${id}` : "";
  }

  function fileRefToken(rel) {
    const path = String(rel || "").trim().replace(/\\/g, "/");
    if (!path) {
      return "";
    }
    if (/^file:/i.test(path)) {
      return path;
    }
    return `file:${path}`;
  }

  function renderSessionButton(item) {
    const title = sessionTitle(item);
    const when = sessionWhenLabel(item.updated_at);
    const current = item.session_id === sessionId;
    const row = document.createElement("div");
    row.className = `session-item${current ? " active" : ""}`;
    row.dataset.id = item.session_id;
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.innerHTML = `<span class="session-topic">${md.escapeHtml(title)}</span>${
      when ? `<time>${md.escapeHtml(when)}</time>` : ""
    }<small>${md.escapeHtml(shortPath(item.workspace_dir) || "")}</small>`;
    if (!current) {
      const cite = document.createElement("button");
      cite.type = "button";
      cite.className = "session-cite";
      cite.title = "引用到输入框";
      cite.textContent = "@";
      cite.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const token = sessionRefToken(item);
        if (token) {
          insertComposerText(token);
        }
      });
      row.appendChild(cite);
    }
    const forget = document.createElement("button");
    forget.type = "button";
    forget.className = "forget";
    forget.title = "删除会话";
    forget.setAttribute("aria-label", "删除会话");
    forget.textContent = "×";
    forget.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      await removeSession(item.session_id);
    });
    row.appendChild(forget);
    const openRow = async () => {
      sessionId = item.session_id;
      persistState();
      switchView("chat");
      await hydrateMessages();
      renderSessionList();
    };
    row.addEventListener("click", (event) => {
      if (event.target.closest("button")) {
        return;
      }
      openRow();
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openRow();
      }
    });
    return row;
  }

  function setSessionSearchOpen(open) {
    const expanded = Boolean(open);
    if (sessionSearchPanelEl) {
      sessionSearchPanelEl.hidden = !expanded;
    }
    if (sessionSearchToggleEl) {
      sessionSearchToggleEl.setAttribute("aria-expanded", String(expanded));
      sessionSearchToggleEl.classList.toggle("has-query", Boolean(sessionFilterEl && sessionFilterEl.value.trim()));
    }
    if (sessionSearchFieldEl) {
      sessionSearchFieldEl.classList.toggle("has-query", Boolean(sessionFilterEl && sessionFilterEl.value.trim()));
    }
    if (expanded && sessionFilterEl) {
      sessionFilterEl.focus();
      const end = sessionFilterEl.value.length;
      sessionFilterEl.setSelectionRange(end, end);
    }
  }

  function renderSessionList() {
    const needle = (sessionFilterEl ? sessionFilterEl.value : "").trim().toLowerCase();
    if (sessionSearchToggleEl) {
      sessionSearchToggleEl.classList.toggle("has-query", Boolean(needle));
    }
    if (sessionSearchFieldEl) {
      sessionSearchFieldEl.classList.toggle("has-query", Boolean(needle));
    }
    const countEl = document.getElementById("session-count");
    if (countEl) {
      countEl.textContent = String(sessions.length);
      countEl.hidden = !sessions.length;
    }
    sessionListEl.replaceChildren();
    const rows = [];
    for (const item of sessions) {
      const title = sessionTitle(item);
      const when = sessionWhenLabel(item.updated_at);
      if (
        needle &&
        !title.toLowerCase().includes(needle) &&
        !when.toLowerCase().includes(needle) &&
        !item.session_id.includes(needle)
      ) {
        continue;
      }
      rows.push(item);
    }
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = needle ? "没有匹配的会话" : "还没有会话";
      sessionListEl.appendChild(empty);
      return;
    }
    rows.forEach((item) => sessionListEl.appendChild(renderSessionButton(item)));
  }

  async function refreshSessions() {
    if (typeof api.listSessions !== "function") {
      return;
    }
    const current = scope();
    try {
      const payload = await api.listSessions(current.project_id, current.agent_id);
      sessions = payload.sessions || [];
      // 头部标题跟列表同源：跑完一轮服务端起好标题后，
      // 左栏已经叫「你好」了，头部不能还挂着「新对话」。
      const mine = sessions.find((item) => item.session_id === sessionId);
      if (mine && mine.title && chatTitleEl) {
        chatTitleEl.textContent = mine.title;
      }
      renderSessionList();
    } catch {
      sessions = [];
      renderSessionList();
    }
  }

  function renderPlan(plan) {
    const active = Boolean(plan && plan.active);
    document.body.dataset.plan = active ? "1" : "0";
    const planDockEl = document.getElementById("plan-dock");
    if (planDockEl) {
      planDockEl.hidden = !active;
    }
    syncModeHint();
  }

  function revealTodoCurrent(list, row) {
    if (!list || !row || !list.clientHeight) {
      return;
    }
    const box = list.getBoundingClientRect();
    const target = row.getBoundingClientRect();
    const slack = 8;
    if (target.top >= box.top + slack && target.bottom <= box.bottom - slack) {
      return;
    }
    list.scrollTop += target.top - box.top - (list.clientHeight - target.height) / 2;
  }

  function todoCurrentVisible(list, row) {
    if (!list || !row) {
      return false;
    }
    const box = list.getBoundingClientRect();
    const target = row.getBoundingClientRect();
    return target.top >= box.top - 4 && target.bottom <= box.bottom + 4;
  }

  function renderTodos(rows) {
    if (!todoDock) {
      return;
    }
    const items = Array.isArray(rows) ? rows : [];
    if (!items.length) {
      todoDock.hidden = true;
      todoDock.replaceChildren();
      return;
    }
    todoDock.hidden = false;
    const done = items.filter((item) => item.status === "completed").length;
    const current = items.find((item) => item.status === "in_progress");
    const prev = todoDock.querySelector("details.todo-panel");
    const currentLabel = String((current && current.content) || "").trim();
    const prevCurrent = prev ? String(prev.dataset.currentKey || "") : "";
    const currentChanged = Boolean(currentLabel && currentLabel !== prevCurrent);
    const userToggled = Boolean(prev && prev.dataset.userToggled === "1");
    const card = document.createElement("details");
    card.className = "todo-panel";
    card.dataset.currentKey = currentLabel;
    if (currentChanged) {
      card.open = true;
    } else if (userToggled) {
      card.open = prev.open;
      card.dataset.userToggled = "1";
    } else {
      card.open = items.some((item) => item.status !== "completed");
    }
    card.addEventListener("toggle", (event) => {
      if (event.isTrusted) {
        card.dataset.userToggled = "1";
      }
    });
    const summary = document.createElement("summary");
    summary.textContent = currentLabel
      ? `待办 · ${done}/${items.length} · 当前：${currentLabel}`
      : `待办 · ${done}/${items.length}`;
    const list = document.createElement("ul");
    list.className = "todo-list";
    let currentRow = null;
    items.forEach((item) => {
      const row = document.createElement("li");
      const status = item.status || "pending";
      row.className = `todo-item todo-${status}`;
      if (status === "in_progress") {
        row.setAttribute("aria-current", "step");
        row.dataset.current = "1";
        if (!currentRow) {
          currentRow = row;
        }
      }
      const mark = status === "completed" ? "●" : status === "in_progress" ? "◐" : "○";
      const label = `${mark} ${item.content || ""}`;
      const content = String(item.content || "").trim();
      if (status === "completed" || !content) {
        row.textContent = label;
      } else {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "todo-steer";
        button.textContent = label;
        button.title = "插入输入框，继续这项";
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          const line = `继续：${content}`;
          if (!promptEl.value.trim()) {
            promptEl.value = line;
          } else {
            insertComposerText(`\n${line}`);
          }
          fitPrompt();
          promptEl.focus();
        });
        row.appendChild(button);
      }
      list.appendChild(row);
    });
    card.append(summary, list);
    todoDock.replaceChildren(card);
    revealTodoCurrent(list, currentRow);
  }

  function renderSendError(message, retryText) {
    lastFailedPrompt = String(retryText || "");
    const node = addBubble("meta", `发送失败：${message}`);
    if (!lastFailedPrompt) {
      return node;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "retry-send";
    button.textContent = "重试";
    button.addEventListener("click", () => retryLastPrompt());
    node.appendChild(button);
    return node;
  }

  function retryLastPrompt() {
    const text = String(lastFailedPrompt || "").trim();
    if (!text) {
      return false;
    }
    promptEl.value = text;
    fitPrompt();
    if (busy) {
      return false;
    }
    sendPrompt({ preventDefault() {} });
    return true;
  }

  function markRunPhase(next) {
    runPhase = next;
  }

  function isTerminalEvent(item) {
    return Boolean(item && (item.type === "done" || item.type === "error"));
  }

  function endRunChrome() {
    clearStreamStall();
    clearWaiting();
    setBusy(false);
    // 只收尾已有的 turn。liveTurn() 会在空对话时凭空建一个空 turn，
    // 把首页 hero 顶掉（残留的 done/error 事件会触发这里）。
    finishWorkProcess(logEl.querySelector(".turn.live"));
  }

  function runIsLive() {
    return runPhase === "streaming" || runPhase === "tools" || runPhase === "gated";
  }

  function shouldSteer(text) {
    return Boolean(String(text || "").trim() && sessionId && runIsLive());
  }

  function setBusy(next) {
    busy = next;
    document.body.dataset.busy = next ? "1" : "0";
    if (!next) {
      runPhase = "idle";
      clearStreamStall();
    } else if (runPhase === "idle") {
      runPhase = "streaming";
    }
    sendBtn.classList.toggle("is-stop", next);
    sendBtn.setAttribute("aria-label", next ? "停止" : "发送");
    sendBtn.title = next ? "停止" : "发送";
    syncModeHint();
  }

  function showWaiting(text) {
    clearStreamStall();
    let node = logEl.querySelector(".bubble.waiting");
    if (!node) {
      node = document.createElement("div");
      node.className = "bubble waiting";
      node.dataset.role = "waiting";
      workHost(liveTurn()).appendChild(node);
      recountWork(liveTurn());
    }
    node.innerHTML = `<span class="dots" aria-hidden="true"></span><span>${md.escapeHtml(text || "正在生成…")}</span>`;
    scrollThread();
  }

  function clearWaiting() {
    clearStreamStall();
    logEl.querySelectorAll(".bubble.waiting").forEach((node) => node.remove());
  }

  function streamStallMs() {
    const test = window.__wittyTest && window.__wittyTest.streamStallMs;
    return typeof test === "number" ? test : 700;
  }

  function clearStreamStall() {
    if (streamStallTimer) {
      clearTimeout(streamStallTimer);
      streamStallTimer = 0;
    }
  }

  function armStreamStall() {
    clearStreamStall();
    if (!busy) {
      return;
    }
    const wait = streamStallMs();
    if (wait < 0) {
      return;
    }
    streamStallTimer = setTimeout(() => {
      streamStallTimer = 0;
      if (!busy || runPhase === "gated" || runPhase === "idle" || runPhase === "done") {
        return;
      }
      if (logEl.querySelector(".bubble.waiting")) {
        return;
      }
      showWaiting("仍在生成…");
    }, wait);
  }

  async function refreshHealth() {
    try {
      const base = await apiBaseValue();
      apiBaseEl.textContent = base;
      const health = await api.health();
      if (health && health.ok) {
        // 品牌行显示版本号，拿不到就留空不占位。
        const brandVersion = document.getElementById("brand-version");
        if (brandVersion) {
          brandVersion.textContent = health.version ? `v${health.version}` : "";
        }
        setStatus(sessionId ? `已连接 · ${sessionId.slice(0, 8)}` : "API 已连接", "ok");
        if (health.has_key === false) {
          setStatus("已连接 · 未配置 API Key", "err");
        }
        return true;
      }
    } catch (error) {
      setStatus(`API 未连接：${error.message}`, "err");
      apiBaseEl.textContent = "";
    }
    return false;
  }

  function syncModeHint() {
    if (!composerHintEl) {
      return;
    }
    if (busy) {
      composerHintEl.textContent = "发送会先记下一条。点「调整方向」才改这一轮；空发送停止。";
    } else if (document.body.dataset.plan === "1") {
      composerHintEl.textContent = "先规划：助手只能查、不能改。你批准方案后才动手。点右上角可跳过。";
    } else if (approvalModeEl) {
      composerHintEl.textContent = MODE_HINTS[approvalModeEl.value] || "";
    }
    persistState();
  }

  function clearPromptQueue() {
    promptQueue = [];
    renderQueue();
  }

  function enqueueFromComposer() {
    const text = String(promptEl.value || "").trim();
    const merged = [text, ...takeClipTokens(text)].filter(Boolean).join(" ");
    if (!merged) {
      return false;
    }
    queueSeq += 1;
    promptQueue.push({ id: `q${queueSeq}`, text: merged });
    promptEl.value = "";
    clearComposerClips();
    fitPrompt();
    hidePickers();
    renderQueue();
    return true;
  }

  function removeQueued(id) {
    promptQueue = promptQueue.filter((item) => item.id !== id);
    renderQueue();
  }

  async function applySteer(text) {
    const note = String(text || "").trim();
    if (!note || !sessionId) {
      return false;
    }
    const steer =
      window.__wittyTest && typeof window.__wittyTest.steerSession === "function"
        ? window.__wittyTest.steerSession
        : api.steerSession;
    if (typeof steer !== "function" || !shouldSteer(note)) {
      return false;
    }
    addBubble("user", note);
    try {
      await steer(sessionId, note);
      showWaiting("已按你的话调整这一轮…");
      return true;
    } catch (error) {
      renderSendError(error.message || String(error), note);
      return false;
    }
  }

  function renderQueue() {
    if (!queueDock) {
      return;
    }
    if (!promptQueue.length) {
      queueDock.hidden = true;
      queueDock.replaceChildren();
      return;
    }
    queueDock.hidden = false;
    queueDock.replaceChildren();
    const list = document.createElement("ul");
    list.className = "queue-list";
    promptQueue.forEach((item) => {
      const row = document.createElement("li");
      row.className = "queue-item";
      row.dataset.id = item.id;
      const mark = document.createElement("span");
      mark.className = "queue-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = "↳";
      const label = document.createElement("button");
      label.type = "button";
      label.className = "queue-text";
      label.textContent = item.text;
      label.title = "点这里改回输入框";
      label.addEventListener("click", () => {
        promptEl.value = item.text;
        fitPrompt();
        removeQueued(item.id);
        promptEl.focus();
      });
      const actions = document.createElement("div");
      actions.className = "queue-actions";
      const steerBtn = document.createElement("button");
      steerBtn.type = "button";
      steerBtn.className = "ghost quiet queue-steer";
      steerBtn.textContent = "调整方向";
      steerBtn.title = "打断这一轮，按这句话继续";
      steerBtn.hidden = !shouldSteer(item.text);
      steerBtn.addEventListener("click", async () => {
        removeQueued(item.id);
        await applySteer(item.text);
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "icon-btn queue-remove";
      del.title = "删掉这条";
      del.setAttribute("aria-label", "删掉这条");
      del.textContent = "✕";
      del.addEventListener("click", () => removeQueued(item.id));
      actions.append(steerBtn, del);
      row.append(mark, label, actions);
      list.appendChild(row);
    });
    queueDock.appendChild(list);
  }

  function drainQueue() {
    if (busy || !promptQueue.length) {
      return false;
    }
    const next = promptQueue.shift();
    renderQueue();
    promptEl.value = next.text;
    fitPrompt();
    sendPrompt({ preventDefault() {} });
    return true;
  }

  function hidePickers() {
    if (slashPickerEl) {
      slashPickerEl.hidden = true;
      slashPickerEl.replaceChildren();
    }
    if (mentionPickerEl) {
      mentionPickerEl.hidden = true;
      mentionPickerEl.replaceChildren();
    }
    pickerIndex = 0;
  }

  function tokenBeforeCursor(pattern) {
    const start = promptEl.selectionStart ?? promptEl.value.length;
    const before = promptEl.value.slice(0, start);
    const match = before.match(pattern);
    return match ? match[1] : "";
  }

  function slashToken() {
    return tokenBeforeCursor(/(?:^|[\s])(\/[A-Za-z0-9_-]*)$/);
  }

  function mentionToken() {
    return tokenBeforeCursor(/(?:^|[\s])(@[^\s]*)$/);
  }

  function replaceToken(token, text) {
    const start = promptEl.selectionStart ?? promptEl.value.length;
    const before = promptEl.value.slice(0, start);
    const after = promptEl.value.slice(start);
    if (!token || !before.endsWith(token)) {
      insertComposerText(text);
      return;
    }
    const next = `${before.slice(0, -token.length)}${text}${after.startsWith(" ") || !after ? after : ` ${after}`}`;
    promptEl.value = next;
    const cursor = before.length - token.length + text.length;
    promptEl.setSelectionRange(cursor, cursor);
    fitPrompt();
    promptEl.focus();
  }

  function insertComposerText(chunk) {
    const start = promptEl.selectionStart ?? promptEl.value.length;
    const end = promptEl.selectionEnd ?? start;
    const before = promptEl.value.slice(0, start);
    const after = promptEl.value.slice(end);
    const padLeft = before && !/\s$/.test(before) ? " " : "";
    const padRight = after && !/^\s/.test(after) ? " " : "";
    const inserted = `${padLeft}${chunk}${padRight}`;
    promptEl.value = `${before}${inserted}${after}`;
    const cursor = before.length + inserted.length;
    promptEl.setSelectionRange(cursor, cursor);
    fitPrompt();
    promptEl.focus();
  }

  function displayPath(full) {
    const root = (workspaceEl.value || "").trim();
    if (root && full.startsWith(root)) {
      const rel = full.slice(root.length).replace(/^[/\\]+/, "");
      return rel || full;
    }
    return full;
  }

  function pathBase(full) {
    const text = String(full || "").replace(/[/\\]+$/, "");
    const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
    return (cut >= 0 ? text.slice(cut + 1) : text) || text;
  }

  function pathDir(full) {
    const text = String(full || "").replace(/[/\\]+$/, "");
    const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
    return cut > 0 ? text.slice(0, cut) : "";
  }

  function pathExt(full) {
    const base = pathBase(full);
    const dot = base.lastIndexOf(".");
    return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
  }

  // 产物来自哪儿。沙箱产物路径很长且跟工作区无关，标一下比让用户读全路径快。
  function artifactOrigin(full) {
    const root = (workspaceEl.value || "").trim();
    if (root && full.startsWith(root)) {
      return { key: "workspace", label: "工作区" };
    }
    if (/[/\\]sandbox[/\\]/.test(full)) {
      return { key: "sandbox", label: "沙箱" };
    }
    return { key: "outside", label: "本机" };
  }

  // 目录副行只保留「工作区/沙箱之后的那一段」，前面那串前缀对用户没有信息量。
  // 两头都不沾的路径只留父目录名：侧栏就两百来像素，塞一条中间挖空的绝对路径等于没写，
  // 「在哪个文件夹」这一问用一个名字就答完了。完整路径在整行和目录段的 title 里。
  function artifactDirLabel(full) {
    const dir = pathDir(full);
    if (!dir) {
      return "";
    }
    const root = (workspaceEl.value || "").trim();
    if (root && dir.startsWith(root)) {
      return dir.slice(root.length).replace(/^[/\\]+/, "");
    }
    const sandbox = dir.match(/[/\\]sandbox[/\\](.+)$/);
    if (sandbox) {
      return sandbox[1];
    }
    return pathBase(dir);
  }

  function formatBytes(size) {
    const n = Number(size) || 0;
    if (n < 1024) {
      return `${n} B`;
    }
    if (n < 1024 * 1024) {
      return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
    }
    if (n < 1024 * 1024 * 1024) {
      return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
    }
    return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  }

  function looksLikeFilePath(value) {
    const text = String(value || "").trim();
    if (text.length < 5 || text.length > 400) {
      return false;
    }
    if (/^https?:/i.test(text)) {
      return false;
    }
    return ARTIFACT_EXT.test(text) || /(?:^|[\\/])[^\s]+\.(?:md|txt)$/i.test(text);
  }

  function resolveLocalPath(value) {
    const raw = String(value || "").trim();
    if (!raw) {
      return "";
    }
    if (/^(?:[A-Za-z]:)?[\\/]/.test(raw)) {
      return raw;
    }
    const root = (workspaceEl.value || "").trim();
    if (!root) {
      return raw;
    }
    return `${root.replace(/[/\\]+$/, "")}/${raw.replace(/^[/\\]+/, "")}`;
  }

  async function openLocalPath(value) {
    const full = resolveLocalPath(value);
    if (!full || typeof api.openPath !== "function") {
      insertComposerText(value);
      return;
    }
    try {
      const result = await api.openPath(full);
      if (!result || result.ok === false) {
        insertComposerText(full);
      }
    } catch {
      insertComposerText(full);
    }
  }

  function noteArtifact(value) {
    const full = resolveLocalPath(value);
    if (!looksLikeFilePath(full)) {
      return;
    }
    if (!turnArtifacts.includes(full)) {
      turnArtifacts.push(full);
    }
    if (!sessionArtifacts.includes(full)) {
      sessionArtifacts.push(full);
      renderArtifacts();
    }
    const turn = logEl.querySelector(".turn.live") || logEl.querySelector(".turn:last-of-type");
    if (turn && !turn.querySelector(".hero")) {
      renderTurnFiles(turn);
    }
  }

  function renderTurnFiles(turn, files) {
    if (!turn || turn.querySelector(".hero")) {
      return;
    }
    let rows;
    if (Array.isArray(files)) {
      rows = files.filter(Boolean);
      turn._turnFiles = rows.slice();
    } else if (turn.classList.contains("live")) {
      rows = turnArtifacts.slice();
      turn._turnFiles = rows.slice();
    } else {
      rows = Array.isArray(turn._turnFiles) ? turn._turnFiles.slice() : [];
    }
    let bar = turn.querySelector(":scope > .turn-files");
    if (!rows.length) {
      if (bar) {
        bar.remove();
      }
      return;
    }
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "turn-files";
    }
    const answer = turn.querySelector(":scope > .say.assistant, :scope > .bubble.assistant");
    if (answer) {
      answer.after(bar);
    } else if (!bar.isConnected) {
      turn.appendChild(bar);
    }
    const expanded = bar.dataset.expanded === "1";
    const shown = expanded ? rows : rows.slice(0, 3);
    const extra = rows.length - shown.length;
    bar.replaceChildren();
    const label = document.createElement("span");
    label.className = "turn-files-label";
    label.textContent = "本轮产物";
    bar.appendChild(label);
    shown.forEach((full) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "turn-file";
      // 胶囊只放文件名。这里原来放 displayPath，工作区外的产物就是整条绝对路径，
      // 被 max-width 一截，剩下的恰好是最没用的前缀。全路径在 title 里。
      button.textContent = pathBase(full);
      button.title = String(full || "");
      button.addEventListener("click", () => openLocalPath(full));
      bar.appendChild(button);
    });
    if (extra > 0) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "turn-file more";
      more.textContent = `+${extra}`;
      more.title = rows.slice(shown.length).map((item) => displayPath(item)).join("\n");
      more.addEventListener("click", () => {
        bar.dataset.expanded = "1";
        renderTurnFiles(turn, rows);
      });
      bar.appendChild(more);
    }
  }

  function collectArtifactPaths(source) {
    const found = [];
    // keyed 表示这串取自 path / file / dest / output 这类字段，可以整串当路径用（文件名允许带空格）。
    // 自由文本不行：`looksLikeFilePath` 判的是「结尾像文件」，`ls -l` 的输出行正好以路径收尾，
    // 整行收进来就成了产物栏里那条「exit=0 -rw-r--r--@ 1 baiyoucai staff 30…」。文本只交给正则去抠。
    const walk = (value, keyed) => {
      if (value == null) {
        return;
      }
      if (typeof value === "string") {
        const text = value.trim();
        if (text.length > 500) {
          return;
        }
        if ((keyed || !/\s/.test(text)) && looksLikeFilePath(text)) {
          found.push(text);
        }
        const matches = text.match(FILE_PATH_RE) || [];
        matches.forEach((item) => found.push(item));
        return;
      }
      if (Array.isArray(value)) {
        value.forEach((item) => walk(item, keyed));
        return;
      }
      if (typeof value === "object") {
        Object.entries(value).forEach(([key, item]) => {
          const named = /path|file|dest|output/i.test(key);
          if (named || typeof item === "string") {
            walk(item, named);
          } else if (item && typeof item === "object") {
            walk(item, false);
          }
        });
      }
    };
    walk(source, false);
    return found;
  }

  function noteArtifactsFromTool(args, text) {
    collectArtifactPaths(args).forEach(noteArtifact);
    collectArtifactPaths(text).forEach(noteArtifact);
  }

  // 同一张图在流式重渲染里会被反复水合，按「工作区|路径」缓存 data: URL，
  // 只打一次后端。失败的不缓存，下一轮渲染还有机会重试。
  const localImageCache = new Map();

  function localImageData(raw) {
    const workspaceDir = scope().workspace_dir || "";
    const key = `${workspaceDir}|${raw}`;
    if (!localImageCache.has(key)) {
      const promise = api.previewFile(workspaceDir, raw).then((body) => {
        if (!body || !body.content_base64) {
          throw new Error((body && body.error) || "no preview");
        }
        return {
          src: `data:${body.mime || "image/png"};base64,${body.content_base64}`,
          path: body.path || raw,
        };
      });
      promise.catch(() => localImageCache.delete(key));
      localImageCache.set(key, promise);
    }
    return localImageCache.get(key);
  }

  function hydrateLocalImages(root) {
    if (!root || typeof api.previewFile !== "function") {
      return;
    }
    root.querySelectorAll("img[data-witty-src]").forEach((img) => {
      const raw = img.getAttribute("data-witty-src") || "";
      img.removeAttribute("data-witty-src");
      if (!raw) {
        return;
      }
      localImageData(raw)
        .then((got) => {
          img.src = got.src;
          img.classList.remove("md-img-pending");
          img.title = raw;
          img.style.cursor = "zoom-in";
          img.addEventListener("click", () => openLocalPath(got.path));
        })
        .catch(() => {
          // 越界 / 不存在 / 太大：退化成可点开的文件链接，别留裂图。
          const button = document.createElement("button");
          button.type = "button";
          button.className = "file-link";
          button.textContent = raw;
          button.title = "打开文件";
          button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            openLocalPath(raw);
          });
          img.replaceWith(button);
        });
    });
  }

  function wireFileLinks(root) {
    if (!root) {
      return;
    }
    hydrateLocalImages(root);
    root.querySelectorAll("code").forEach((code) => {
      const text = (code.textContent || "").trim();
      if (!looksLikeFilePath(text)) {
        return;
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-link";
      button.textContent = text;
      button.title = "打开文件";
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openLocalPath(text);
      });
      code.replaceWith(button);
    });
  }

  function drawPicker(host, rows, onPick) {
    host.replaceChildren();
    rows.forEach((row, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `picker-item${index === pickerIndex ? " active" : ""}`;
      button.setAttribute("role", "option");
      button.innerHTML = `${md.escapeHtml(row.title)}<small>${md.escapeHtml(row.detail || "")}</small>`;
      button.addEventListener("mousedown", (event) => {
        event.preventDefault();
        onPick(row);
      });
      host.appendChild(button);
    });
    host.hidden = rows.length === 0;
    const active = host.children[pickerIndex];
    if (active && typeof active.scrollIntoView === "function") {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function slashRows(query) {
    const needle = String(query || "").replace(/^\//, "").toLowerCase();
    return slashCommands
      .filter((item) => !needle || item.name.toLowerCase().startsWith(needle))
      .map((item) => ({
        title: `/${item.name}`,
        detail: item.description || "",
        value: `/${item.name} `,
      }));
  }

  function mentionRows(query) {
    const needle = String(query || "").replace(/^@/, "").toLowerCase();
    const files = workspaceFiles
      .map((full) => ({ full, rel: displayPath(full) }))
      .filter((item) => !needle || item.rel.toLowerCase().includes(needle) || item.full.toLowerCase().includes(needle))
      .sort((left, right) => {
        const dirFirst = Number(right.rel.endsWith("/")) - Number(left.rel.endsWith("/"));
        return dirFirst || left.rel.localeCompare(right.rel);
      })
      .slice(0, 40)
      .map((item) => ({
        title: item.rel,
        detail: item.full,
        value: fileRefToken(item.rel),
      }));
    files.push({ title: "选择文件…", detail: "从磁盘插入路径", value: "__pick__" });
    return files;
  }

  function syncComposerPickers() {
    const slash = slashToken();
    const mention = mentionToken();
    if (slash) {
      if (mentionPickerEl) {
        mentionPickerEl.hidden = true;
      }
      const rows = slashRows(slash);
      pickerIndex = Math.max(0, Math.min(pickerIndex, Math.max(0, rows.length - 1)));
      drawPicker(slashPickerEl, rows, (row) => {
        replaceToken(slash, row.value);
        hidePickers();
      });
      return;
    }
    if (mention) {
      if (slashPickerEl) {
        slashPickerEl.hidden = true;
      }
      if (!workspaceFiles.length) {
        refreshWorkspaceFiles();
      }
      const rows = mentionRows(mention);
      pickerIndex = Math.max(0, Math.min(pickerIndex, Math.max(0, rows.length - 1)));
      drawPicker(mentionPickerEl, rows, (row) => {
        if (row.value === "__pick__") {
          hidePickers();
          attachFiles();
          return;
        }
        replaceToken(mention, row.value);
        hidePickers();
      });
      return;
    }
    hidePickers();
  }

  function activePicker() {
    if (slashPickerEl && !slashPickerEl.hidden) {
      return slashPickerEl;
    }
    if (mentionPickerEl && !mentionPickerEl.hidden) {
      return mentionPickerEl;
    }
    return null;
  }

  function movePicker(delta) {
    const host = activePicker();
    if (!host) {
      return;
    }
    const count = host.children.length;
    if (!count) {
      return;
    }
    pickerIndex = (pickerIndex + delta + count) % count;
    Array.from(host.children).forEach((node, index) => {
      node.classList.toggle("active", index === pickerIndex);
    });
    const active = host.children[pickerIndex];
    if (active && typeof active.scrollIntoView === "function") {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function acceptPicker() {
    const host = activePicker();
    if (!host) {
      return false;
    }
    const active = host.children[pickerIndex] || host.children[0];
    if (!active) {
      return false;
    }
    active.dispatchEvent(new Event("mousedown"));
    return true;
  }

  function shouldSendOnEnter(event) {
    if (event.key !== "Enter" || event.shiftKey) {
      return false;
    }
    if (event.isComposing || event.keyCode === 229 || event.which === 229) {
      return false;
    }
    return true;
  }

  async function refreshCommands() {
    if (typeof api.listCommands !== "function") {
      slashCommands = FALLBACK_COMMANDS.slice();
      return;
    }
    try {
      const payload = await api.listCommands(sessionId);
      const rows = payload.commands || [];
      slashCommands = rows.length ? rows : FALLBACK_COMMANDS.slice();
    } catch {
      slashCommands = FALLBACK_COMMANDS.slice();
    }
  }

  async function refreshWorkspaceFiles(force) {
    const dir = workspaceEl.value.trim();
    if (!force && listedWorkspaceDir === dir) {
      renderArtifacts();
      return;
    }
    listedWorkspaceDir = dir;
    if (typeof api.listWorkspace !== "function") {
      workspaceFiles = [];
      renderContextMaterials();
      return;
    }
    try {
      workspaceFiles = (await api.listWorkspace(dir, sessionId)) || [];
    } catch {
      workspaceFiles = [];
    }
    renderContextMaterials();
    renderArtifacts();
    if (mentionToken()) {
      syncComposerPickers();
    }
  }

  async function refreshArtifactMeta() {
    if (artifactMetaPending || typeof api.statPaths !== "function") {
      return;
    }
    const wanted = sessionArtifacts.filter((full) => !artifactMeta.has(full));
    if (!wanted.length) {
      return;
    }
    artifactMetaPending = true;
    // 先占位：stat 拿不到就退回只有文件名的样子，产物栏不该整个空掉。占位还挡住了
    // 下面那个补跑判断——主进程少回一条时，没占位会被当成「还欠着」，转成死循环。
    wanted.forEach((full) => artifactMeta.set(full, { path: full, exists: true, size: 0, mtime: 0 }));
    try {
      const rows = (await api.statPaths(wanted)) || [];
      rows.forEach((row) => {
        if (row && row.path) {
          artifactMeta.set(row.path, row);
        }
      });
    } catch {
      // 占位已经写了，这里不用再兜。
    } finally {
      artifactMetaPending = false;
    }
    drawArtifacts();
    // 这一轮 stat 在飞的时候又写出了新文件，补一轮。
    if (sessionArtifacts.some((full) => !artifactMeta.has(full))) {
      refreshArtifactMeta().catch(() => {});
    }
  }

  function artifactRow(full) {
    const meta = artifactMeta.get(full) || null;
    const missing = Boolean(meta && meta.exists === false);
    const ext = pathExt(full);
    const origin = artifactOrigin(full);
    const row = document.createElement("div");
    row.className = `art-row${missing ? " missing" : ""}`;
    row.dataset.path = full;

    const open = document.createElement("button");
    open.type = "button";
    open.className = "art-open";
    // 整行是打开按钮，动作那几个键浮在右上角，不进这个按钮，免得嵌套 button。
    open.title = full;
    open.addEventListener("click", () => openLocalPath(full));

    const kind = document.createElement("span");
    kind.className = `art-kind kind-${ext || "file"}`;
    kind.textContent = (ext || "file").slice(0, 4).toUpperCase();
    open.appendChild(kind);

    const text = document.createElement("span");
    text.className = "art-text";
    // 文件名单独一行、不参与省略；目录那行才收缩。整条路径挤一行时先被切掉的
    // 恰恰是最该看到的文件名，这是用户报的那个「文件名称都看不到」。
    const name = document.createElement("strong");
    name.className = "art-name";
    name.textContent = pathBase(full);
    text.appendChild(name);

    const sub = document.createElement("span");
    sub.className = "art-sub";
    const where = document.createElement("em");
    where.className = `art-origin origin-${origin.key}`;
    where.textContent = origin.label;
    sub.appendChild(where);
    const dir = artifactDirLabel(full);
    if (dir) {
      // 目录拆成「前缀 + 最后一级」两段：宽度不够时省略号吃前缀，离文件最近的那一级
      // 始终看得见。整段用 text-overflow 的话，被切掉的正好是最有信息量的尾巴。
      const dirEl = document.createElement("span");
      dirEl.className = "art-dir";
      dirEl.title = pathDir(full);
      const cut = dir.lastIndexOf("/");
      const head = document.createElement("span");
      head.className = "art-dir-head";
      head.textContent = cut > 0 ? dir.slice(0, cut) : "";
      const tail = document.createElement("span");
      tail.className = "art-dir-tail";
      tail.textContent = cut > 0 ? dir.slice(cut) : dir;
      dirEl.append(head, tail);
      sub.appendChild(dirEl);
    }
    const facts = [];
    if (missing) {
      facts.push("已不在");
    } else if (meta) {
      if (meta.size) {
        facts.push(formatBytes(meta.size));
      }
      if (meta.mtime) {
        facts.push(sessionWhenLabel(meta.mtime));
      }
    }
    if (facts.length) {
      const factEl = document.createElement("span");
      factEl.className = "art-facts";
      factEl.textContent = `${dir ? "· " : ""}${facts.join(" · ")}`;
      sub.appendChild(factEl);
    }
    text.appendChild(sub);
    open.appendChild(text);
    row.appendChild(open);

    const actions = document.createElement("div");
    actions.className = "art-actions";
    if (typeof api.revealPath === "function" && !missing) {
      const reveal = document.createElement("button");
      reveal.type = "button";
      reveal.className = "art-act";
      reveal.title = "在文件管理器里显示";
      reveal.textContent = "定位";
      reveal.addEventListener("click", (event) => {
        event.stopPropagation();
        api.revealPath(full).catch(() => {});
      });
      actions.appendChild(reveal);
    }
    const cite = document.createElement("button");
    cite.type = "button";
    cite.className = "art-act";
    cite.title = "把路径填进输入框";
    cite.textContent = "引用";
    cite.addEventListener("click", (event) => {
      event.stopPropagation();
      insertComposerText(full);
    });
    actions.appendChild(cite);
    row.appendChild(actions);
    return row;
  }

  function drawArtifacts() {
    const host = document.getElementById("artifact-list");
    if (!host) {
      return;
    }
    const seen = new Set();
    const rows = [];
    sessionArtifacts.forEach((full) => {
      if (!seen.has(full)) {
        seen.add(full);
        rows.push(full);
      }
    });
    const count = document.getElementById("artifact-count");
    if (count) {
      count.textContent = rows.length ? String(rows.length) : "";
      count.hidden = !rows.length;
    }
    if (!rows.length) {
      host.className = "ctx-list art-list muted";
      host.textContent = "当前会话还没有产物文件。";
      return;
    }
    host.className = "ctx-list art-list";
    host.replaceChildren();
    // 新写出来的排前面。stat 没回来的按加入顺序兜底，别让列表在等 stat 时乱跳。
    const ordered = rows.slice(0, 24).sort((a, b) => {
      const ma = (artifactMeta.get(a) || {}).mtime || 0;
      const mb = (artifactMeta.get(b) || {}).mtime || 0;
      if (ma === mb) {
        return rows.indexOf(b) - rows.indexOf(a);
      }
      return mb - ma;
    });
    ordered.forEach((full) => host.appendChild(artifactRow(full)));
  }

  function renderArtifacts() {
    drawArtifacts();
    refreshArtifactMeta().catch(() => {});
  }

  function renderContextMaterials() {
    return;
  }

  function renderContextMemory() {
    return;
  }

  async function attachFiles() {
    if (typeof api.pickFiles === "function") {
      try {
        const paths = await api.pickFiles();
        (paths || []).forEach((item) => {
          rememberClip({
            token: fileRefToken(displayPath(item)),
            path: item,
            name: String(item).split(/[/\\]/).pop(),
          });
        });
        return;
      } catch {
        // fall through to hidden file input
      }
    }
    if (fileInputEl) {
      fileInputEl.click();
    }
  }

  function isImageFile(file) {
    if (!file) {
      return false;
    }
    if (file.type && /^image\//i.test(file.type)) {
      return true;
    }
    return /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name || file.path || "");
  }

  function collectTransferFiles(transfer) {
    const out = [];
    if (!transfer) {
      return out;
    }
    const seen = new Set();
    const push = (file) => {
      if (!file || seen.has(file)) {
        return;
      }
      seen.add(file);
      out.push(file);
    };
    Array.from(transfer.files || []).forEach(push);
    Array.from(transfer.items || []).forEach((item) => {
      if (item && item.kind === "file") {
        push(item.getAsFile());
      }
    });
    return out;
  }

  // dragover 阶段拿不到 File 对象（getAsFile 返回 null），只能看 kind/types。
  function transferHasFiles(transfer) {
    if (!transfer) {
      return false;
    }
    if (transfer.files && transfer.files.length) {
      return true;
    }
    if (Array.from(transfer.items || []).some((item) => item && item.kind === "file")) {
      return true;
    }
    return Array.from(transfer.types || []).includes("text/uri-list");
  }

  // Finder「拷贝文件」有时只给 text/uri-list 一种味道，不给 File 对象；
  // 不接住它，默认粘贴就把裸路径灌进输入框。
  function transferFilePaths(transfer) {
    const raw =
      transfer && typeof transfer.getData === "function" ? transfer.getData("text/uri-list") : "";
    return String(raw || "")
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#") && /^file:\/\//i.test(line))
      .map((line) => {
        try {
          return decodeURIComponent(line.replace(/^file:\/\/(localhost)?/i, ""));
        } catch {
          return "";
        }
      })
      .filter(Boolean);
  }

  function fileToDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("read failed"));
      reader.readAsDataURL(file);
    });
  }

  function rememberClip(item) {
    const token = item.token || fileRefToken(displayPath(item.path || ""));
    if (!token) {
      return;
    }
    if (!composerClips.some((row) => row.token === token)) {
      composerClips.push({
        token,
        path: item.path || "",
        preview: item.preview || "",
        name: item.name || token,
      });
    }
    // 令牌不进输入框：file: 路径是给后端的协议，不是给人看的。发送时由
    // takeClipTokens 拼进提示词，胶囊上的 × 就是撤销。
    renderComposerClips();
  }

  // 附件令牌在发送那一刻才并入提示词；已经手写在正文里的不重复。
  function takeClipTokens(text) {
    const body = String(text || "");
    return composerClips.map((row) => row.token).filter((token) => token && !body.includes(token));
  }

  function clearComposerClips() {
    composerClips = [];
    renderComposerClips();
  }

  function dropClip(token) {
    composerClips = composerClips.filter((row) => row.token !== token);
    if (promptEl && token) {
      promptEl.value = promptEl.value.split(token).join(" ").replace(/\s+/g, " ").trim();
      fitPrompt();
    }
    renderComposerClips();
  }

  function renderComposerClips() {
    if (!clipsEl) {
      return;
    }
    if (!composerClips.length) {
      clipsEl.hidden = true;
      clipsEl.replaceChildren();
      return;
    }
    clipsEl.hidden = false;
    clipsEl.replaceChildren();
    composerClips.forEach((item) => {
      const chip = document.createElement("div");
      chip.className = "composer-clip";
      if (item.preview) {
        const img = document.createElement("img");
        img.src = item.preview;
        img.alt = item.name || "";
        chip.appendChild(img);
      }
      const label = document.createElement("span");
      label.textContent = item.name || "文件";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "composer-clip-x";
      remove.textContent = "×";
      remove.title = "去掉这个附件";
      remove.addEventListener("click", () => dropClip(item.token));
      chip.append(label, remove);
      clipsEl.appendChild(chip);
    });
  }

  async function ingestClipFile(file) {
    if (!file) {
      return;
    }
    const image = isImageFile(file);
    if (file.path) {
      rememberClip({
        token: fileRefToken(displayPath(file.path)),
        path: file.path,
        preview: image ? URL.createObjectURL(file) : "",
        name: file.name || "文件",
      });
      return;
    }
    if (typeof api.saveInbox !== "function") {
      throw new Error("当前壳不能收文件");
    }
    const dataUrl = await fileToDataUrl(file);
    const comma = dataUrl.indexOf(",");
    const content = comma >= 0 ? dataUrl.slice(comma + 1) : "";
    const current = scope();
    const saved = await api.saveInbox({
      workspace_dir: current.workspace_dir,
      session_id: sessionId,
      filename: file.name || (image ? "paste.png" : "paste.bin"),
      mime: file.type || (image ? "image/png" : "application/octet-stream"),
      content_base64: content,
    });
    rememberClip({
      token: fileRefToken(saved.path || saved.token),
      path: saved.path,
      preview: image ? dataUrl : "",
      name: saved.name || file.name || "文件",
    });
  }

  async function ingestTransferFiles(transfer) {
    const files = collectTransferFiles(transfer);
    if (!files.length) {
      return 0;
    }
    for (const file of files) {
      await ingestClipFile(file);
    }
    return files.length;
  }

  // 胶囊里那截发布日期后缀是噪声，且长模型名在 11px 下正好顶到
  // 边框（12em 只放得下 128px 文字）。能去掉后缀就去掉，全名留在标题行和 title 里。
  function compactModelLabel(text) {
    return String(text || "").replace(/-\d{4,8}$/, "");
  }

  function modelPickLabels(models) {
    const full = models.map((item) => item.display_name || item.model_id || item.name);
    const short = full.map(compactModelLabel);
    // 两个模型只差日期后缀时压缩会撞名，那就都用全名。
    const collides = new Set(short).size !== short.length;
    return { full, short: collides ? full : short };
  }

  function fillModelPick(models, active) {
    if (!modelPickEl) {
      return;
    }
    const labels = modelPickLabels(models);
    modelPickEl.replaceChildren();
    models.forEach((item, index) => {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = labels.short[index] || item.name;
      option.title = labels.full[index] || item.name;
      if (item.name === active) {
        option.selected = true;
      }
      modelPickEl.appendChild(option);
    });
    const current = models.find((item) => item.name === active) || models[0];
    const currentIndex = models.indexOf(current);
    if (modelPickEl.parentElement && currentIndex >= 0) {
      modelPickEl.parentElement.title = `当前模型 · ${labels.full[currentIndex]}`;
    }
    if (chatSubEl && current) {
      const work = shortPath(workspaceEl.value.trim());
      chatSubEl.textContent = work ? `${current.model_id} · ${work}` : current.model_id;
    }
  }

  function fillModelForm(item) {
    editingModel = item ? item.name : "";
    if (modelFormTitleEl) {
      modelFormTitleEl.textContent = item ? `编辑 ${item.name}` : "新增模型";
    }
    if (modelNameEl) {
      modelNameEl.value = item ? item.name : "";
      modelNameEl.disabled = Boolean(item);
    }
    if (modelDisplayEl) {
      modelDisplayEl.value = item ? item.display_name || "" : "";
    }
    if (modelIdEl) {
      modelIdEl.value = item ? item.model_id || "" : "";
    }
    if (modelBaseEl) {
      modelBaseEl.value = item ? item.base_url || "" : "";
    }
    if (modelMaxEl) {
      modelMaxEl.value = item ? String(item.max_tokens || 2048) : "2048";
    }
    if (modelTimeoutEl) {
      modelTimeoutEl.value = item ? String(item.timeout_sec || 3600) : "3600";
    }
    if (modelKeyEl) {
      modelKeyEl.value = "";
      modelKeyEl.placeholder = item && item.has_key ? "已有钥匙，留空不改" : "填写 API Key";
    }
  }

  function drawModelList(models, active) {
    if (!modelListEl) {
      return;
    }
    modelListEl.replaceChildren();
    for (const item of models) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `model-chip${item.name === active ? " active" : ""}`;
      button.textContent = `${item.display_name || item.name}${item.has_key ? "" : " · 无钥匙"}`;
      button.addEventListener("click", () => fillModelForm(item));
      modelListEl.appendChild(button);
    }
  }

  async function refreshModel() {
    const current = scope();
    const fetcher = api.listModels || api.getModel;
    if (typeof fetcher !== "function") {
      return;
    }
    try {
      const payload = api.listModels
        ? await api.listModels(current.project_id, current.agent_id)
        : await api.getModel(current.project_id, current.agent_id);
      modelProfiles = payload.models || [];
      const active = payload.active || (modelProfiles.find((item) => item.active) || {}).name || "";
      fillModelPick(modelProfiles, active);
      drawModelList(modelProfiles, active);
      const selected = modelProfiles.find((item) => item.name === (editingModel || active)) || modelProfiles[0];
      if (selected) {
        fillModelForm(selected);
      }
      if (modelStatusEl) {
        const now = modelProfiles.find((item) => item.name === active);
        modelStatusEl.textContent = now
          ? `当前：${now.display_name || now.name} · ${now.model_id}${now.has_key ? "" : " · 缺钥匙"}`
          : "还没有模型，先在下面新增一套";
      }
    } catch (error) {
      if (modelStatusEl) {
        modelStatusEl.textContent = `读取失败：${error.message}`;
      }
    }
  }

  async function saveModelSettings() {
    const current = scope();
    const name = (modelNameEl && modelNameEl.value.trim()) || editingModel;
    if (!name) {
      throw new Error("先填目录名，例如 deepseek-flash");
    }
    const body = {
      project_id: current.project_id,
      agent_id: current.agent_id,
      name,
      model_id: modelIdEl ? modelIdEl.value.trim() : "",
      base_url: modelBaseEl ? modelBaseEl.value.trim() : "",
      display_name: modelDisplayEl ? modelDisplayEl.value.trim() : "",
      max_tokens: modelMaxEl ? Number(modelMaxEl.value || 2048) : 2048,
      timeout_sec: modelTimeoutEl ? Number(modelTimeoutEl.value || 3600) : 3600,
      activate: true,
    };
    if (modelKeyEl && modelKeyEl.value.trim()) {
      body.api_key = modelKeyEl.value.trim();
    }
    const saver = api.saveModelProfile || api.saveModel;
    await saver(body);
    if (modelKeyEl) {
      modelKeyEl.value = "";
    }
    editingModel = name;
    await refreshModel();
    await refreshHealth();
  }

  async function removeModelProfile() {
    if (!editingModel) {
      return;
    }
    const current = scope();
    await api.deleteModel(editingModel, current.project_id, current.agent_id);
    editingModel = "";
    await refreshModel();
  }

  function promptGroupOf(name) {
    return PROMPT_GROUPS.find((group) => group.test(name)) || PROMPT_GROUPS[PROMPT_GROUPS.length - 1];
  }

  function setWebStatus(text) {
    const el = document.getElementById("web-status");
    if (el) {
      el.textContent = text || "";
    }
  }

  function paintWebMode(mode) {
    const current = mode === "intranet" ? "intranet" : "public";
    document.querySelectorAll(".net-swatch").forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === current);
    });
  }

  async function loadWebSettings() {
    if (typeof api.getWeb !== "function") {
      setWebStatus("当前壳不支持改网络模式");
      return;
    }
    const payload = await api.getWeb();
    paintWebMode(payload.mode);
    setWebStatus(payload.mode === "intranet" ? "内网模式：不走公网" : "外网模式：默认可访问公网");
  }

  async function saveWebMode(mode) {
    if (typeof api.saveWeb !== "function") {
      setWebStatus("当前壳不支持改网络模式");
      return;
    }
    const payload = await api.saveWeb({ mode });
    paintWebMode(payload.mode);
    setWebStatus(payload.mode === "intranet" ? "已切到内网，新对话生效" : "已切到外网，新对话生效");
  }

  function showSettingsPanel(id, promptName) {
    settingsPanel = id;
    if (id === "prompt") {
      currentPromptName = String(promptName || currentPromptName || "");
    }
    const detail = document.getElementById("settings-detail");
    if (detail) {
      detail.classList.toggle("is-prompt", id === "prompt");
    }
    document.querySelectorAll("#settings-detail [data-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.panel !== id;
    });
    if (settingsNavEl) {
      settingsNavEl.querySelectorAll(".catalog-item").forEach((button) => {
        button.classList.toggle("active", button.dataset.panel === id);
      });
    }
    if (id === "prompt") {
      drawPromptNav();
    }
    if (id === "network") {
      loadWebSettings().catch((error) => setWebStatus(error.message));
    }
    if (id === "email") {
      loadMail().catch((error) => setEmailStatus(error.message));
    }
    if (id === "schedule") {
      loadSchedules().catch((error) => setScheduleStatus(error.message));
    }
  }

  function drawSettingsNav() {
    if (!settingsNavEl) {
      return;
    }
    settingsNavEl.replaceChildren();
    SETTINGS_SECTIONS.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "catalog-item";
      button.dataset.panel = item.id;
      const hint =
        item.id === "prompt" && promptRows.length
          ? `${promptRows.length} 条`
          : item.hint;
      button.innerHTML =
        `<span class="set-ico" aria-hidden="true">${item.icon || ""}</span>` +
        `<span class="set-copy">${md.escapeHtml(item.title)}<small>${md.escapeHtml(hint)}</small></span>`;
      button.addEventListener("click", () => {
        showSettingsPanel(item.id);
        if (item.id === "prompt" && !currentPromptName && promptRows[0]) {
          openPrompt(promptRows[0].name);
        }
      });
      settingsNavEl.appendChild(button);
    });
    settingsNavEl.querySelectorAll(".catalog-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.panel === settingsPanel);
    });
  }

  function drawPromptNav() {
    if (!promptNavEl) {
      return;
    }
    const needle = String((promptFilterEl && promptFilterEl.value) || "").trim().toLowerCase();
    const matched = promptRows.filter(
      (item) => !needle || `${item.name} ${item.preview}`.toLowerCase().includes(needle),
    );
    if (promptCountEl) {
      promptCountEl.textContent = needle ? `${matched.length} / ${promptRows.length}` : `${promptRows.length} 条`;
    }
    const buckets = new Map();
    PROMPT_GROUPS.forEach((group) => buckets.set(group.id, []));
    matched.forEach((item) => {
      buckets.get(promptGroupOf(item.name).id).push(item);
    });
    promptNavEl.replaceChildren();
    PROMPT_GROUPS.forEach((group) => {
      const rows = buckets.get(group.id) || [];
      if (!rows.length) {
        return;
      }
      const fold = document.createElement("details");
      fold.className = "catalog-fold";
      fold.dataset.group = group.id;
      fold.open = Boolean(
        needle || rows.some((item) => item.name === currentPromptName),
      );
      const summary = document.createElement("summary");
      summary.textContent = `${group.title} · ${rows.length}`;
      fold.appendChild(summary);
      rows.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "catalog-item";
        button.dataset.prompt = item.name;
        button.innerHTML = `${md.escapeHtml(item.name)}<small>${md.escapeHtml(item.preview || `${item.chars} 字`)}</small>`;
        button.classList.toggle("active", item.name === currentPromptName);
        button.addEventListener("click", () => openPrompt(item.name));
        fold.appendChild(button);
      });
      promptNavEl.appendChild(fold);
    });
  }

  async function loadPrompts() {
    if (typeof api.listPrompts !== "function") {
      return;
    }
    const payload = await api.listPrompts();
    promptRows = payload.prompts || [];
    drawSettingsNav();
    if (settingsPanel === "prompt") {
      drawPromptNav();
    }
  }

  async function openPrompt(name) {
    if (typeof api.getPrompt !== "function") {
      return;
    }
    currentPromptName = name;
    if (settingsPanel !== "prompt") {
      showSettingsPanel("prompt");
    } else {
      // 当前项变化时重画分组，让旧分组收起、当前分组保持展开。
      drawPromptNav();
    }
    if (promptStatusEl) {
      promptStatusEl.textContent = "读取中…";
    }
    const record = await api.getPrompt(name);
    currentPromptSaved = String(record.text || "");
    if (promptTitleEl) {
      promptTitleEl.textContent = record.name;
    }
    if (promptMetaEl) {
      promptMetaEl.textContent = `${record.chars} 字 · 发给模型的配置，保存后立即生效`;
    }
    if (promptBodyEl) {
      promptBodyEl.value = currentPromptSaved;
    }
    if (promptStatusEl) {
      promptStatusEl.textContent = "";
    }
  }

  async function saveCurrentPrompt() {
    if (!currentPromptName || typeof api.savePrompt !== "function") {
      return;
    }
    const text = promptBodyEl ? promptBodyEl.value : "";
    if (promptStatusEl) {
      promptStatusEl.textContent = "保存中…";
    }
    const saved = await api.savePrompt(currentPromptName, text);
    currentPromptSaved = String(saved.text || text);
    if (promptBodyEl) {
      promptBodyEl.value = currentPromptSaved;
    }
    if (promptMetaEl) {
      promptMetaEl.textContent = `${saved.chars} 字 · 已写入配置`;
    }
    if (promptStatusEl) {
      promptStatusEl.textContent = "已保存";
    }
    await loadPrompts();
    if (promptNavEl) {
      promptNavEl.querySelectorAll(".catalog-item").forEach((button) => {
        button.classList.toggle("active", button.dataset.prompt === currentPromptName);
      });
    }
  }

  function revertCurrentPrompt() {
    if (promptBodyEl) {
      promptBodyEl.value = currentPromptSaved;
    }
    if (promptStatusEl) {
      promptStatusEl.textContent = currentPromptName ? "已还原未保存的修改" : "";
    }
  }

  async function pickModel(name) {
    if (!name) {
      return;
    }
    const current = scope();
    await api.activateModel(name, current.project_id, current.agent_id);
    editingModel = name;
    await refreshModel();
    await refreshHealth();
  }

  async function removeSession(id) {
    if (typeof api.deleteSession !== "function") {
      return;
    }
    const current = scope();
    const previous = sessions.slice();
    sessions = sessions.filter((item) => item.session_id !== id);
    renderSessionList();
    if (sessionId === id) {
      sessionId = "";
      persistState();
      showHero();
    }
    try {
      await api.deleteSession(id, current.project_id, current.agent_id);
    } catch (err) {
      const message = String((err && err.message) || err || "");
      const gone = /not found|404/i.test(message);
      if (!gone) {
        sessions = previous;
        renderSessionList();
        setStatus(message || "删除失败", "bad");
        return;
      }
    }
    await refreshSessions();
  }

  async function createSession() {
    if (!(await refreshHealth())) {
      addBubble("meta", "先启动 Python API：uv run witty-agent serve");
      return;
    }
    const current = scope();
    const session = await api.createSession(current);
    sessionId = session.session_id;
    switchView("chat");
    sessionArtifacts = [];
    turnArtifacts = [];
    clearPromptQueue();
    persistState();
    logEl.replaceChildren();
    showHero();
    // 不再发「会话 UUID / 工作区 绝对路径」的 meta 气泡：一是信息噪音
    // （副标题和底部胶囊都有工作区，状态点有短 ID），二是 meta 气泡会
    // 建 turn 把 hero 顶掉，新会话页永远见不到欢迎标题。
    // 标题也不用 8 位随机 ID 充数，等服务端起好名再由 refreshSessions 同步。
    chatTitleEl.textContent = session.title || "新对话";
    chatSubEl.textContent = shortPath(session.workspace_dir) || "";
    renderTodos([]);
    renderPlan({ active: false });
    setStatus(`已连接 · ${sessionId.slice(0, 8)}`, "ok");
    await refreshSessions();
    await refreshCommands();
    await refreshWorkspaceFiles();
  }

  function latestTurnHasAssistant() {
    const turn = logEl.querySelector(".turn.live") || logEl.querySelector(".turn:last-of-type");
    if (!turn) {
      return false;
    }
    const node = turn.querySelector(":scope > .say.assistant .bubble.assistant, :scope > .bubble.assistant");
    return Boolean(node && bubbleCopyText(node));
  }

  async function stopGeneration() {
    if (sessionId && typeof api.abortSession === "function") {
      try {
        await api.abortSession(sessionId);
      } catch {
        // ignore a missing run
      }
    }
    setBusy(false);
    clearWaiting();
    addBubble("meta", "已停止生成");
    setStatus("已停止", "busy");
  }

  async function sendPrompt(event) {
    event.preventDefault();
    if (busy) {
      if (promptEl.value.trim() || composerClips.length) {
        enqueueFromComposer();
        return;
      }
      if (runIsLive()) {
        await stopGeneration();
        return;
      }
      genId += 1;
      setBusy(false);
      clearWaiting();
    }
    const text = promptEl.value.trim();
    // 附件令牌在这里才拼进出站提示词；只有附件没有字也算一条消息。
    const prompt = [text, ...takeClipTokens(text)].filter(Boolean).join(" ");
    if (!prompt) {
      return;
    }
    if (!sessionId) {
      showWaiting("正在建立会话…");
      try {
        await createSession();
      } catch (error) {
        clearWaiting();
        addBubble("meta", `新建会话失败：${error.message}`);
        return;
      }
      if (!sessionId) {
        clearWaiting();
        return;
      }
    }
    promptEl.value = "";
    clearComposerClips();
    fitPrompt();
    hidePickers();
    followLatest();
    addBubble("user", prompt);
    pendingMemoryReads = Object.create(null);
    loadedBrowseHits = [];
    const myGen = (genId += 1);
    setBusy(true);
    showWaiting("正在思考…");
    setStatus("正在生成…", "busy");
    lastSendError = "";
    lastFailedPrompt = prompt;
    approvalDock.replaceChildren();
    try {
      const reply = await completePrompt(sessionId, prompt);
      clearWaiting();
      if (myGen !== genId) {
        return;
      }
      if (reply.status === "error") {
        throw new Error(reply.error || "run error");
      }
      if (reply.text && !latestTurnHasAssistant()) {
        addBubble("assistant", reply.text);
      } else if (!String(reply.text || "").trim() && !latestTurnHasAssistant()) {
        addBubble("meta", "这一轮没有生成正文。请再发一次；如果刚点过停止，新一轮会重新调用模型。");
      }
      setStatus(`已连接 · ${sessionId.slice(0, 8)}`, "ok");
      await refreshSessions();
      if (currentView === "memory") {
        await loadMemory();
      }
    } catch (error) {
      lastSendError = error.message || String(error);
      clearWaiting();
      promptEl.value = prompt;
      fitPrompt();
      renderSendError(lastSendError, prompt);
      setStatus(lastSendError, "err");
    } finally {
      if (myGen === genId) {
        clearWaiting();
        setBusy(false);
        if (!lastSendError) {
          drainQueue();
        }
      }
    }
  }

  function highlightMemoryCells(ids) {
    const wanted = (ids || [])
      .map((item) => {
        if (item && typeof item === "object") {
          return { id: String(item.id || item.slug || ""), scope: String(item.scope || "") };
        }
        return { id: String(item || ""), scope: "" };
      })
      .filter((item) => item.id);
    document.querySelectorAll("#memory-lattice .memory-cell, #memory-taxonomy .memory-tax, #memory-workspace .memory-extra, #memory-archive .memory-archive").forEach((card) => {
      const id = card.dataset.id || "";
      const scope = card.dataset.scope || "";
      const on = wanted.some((item) => item.id === id && (!item.scope || item.scope === scope));
      card.classList.toggle("linked", on);
      if (on && wanted.length) {
        card.open = true;
      }
    });
  }

  function recallTargets(data) {
    const hits = (data && data.hits) || [];
    if (hits.length) {
      return hits
        .map((item) => ({ id: item.slug || item.id, scope: item.scope || "" }))
        .filter((item) => item.id);
    }
    const text = String((data && data.retrieved) || "");
    return Array.from(text.matchAll(/`([a-z0-9-]+)`/g)).map((item) => ({ id: item[1], scope: "" }));
  }

  function fillMemoryQuery(text) {
    const queryEl = document.getElementById("memory-query");
    if (!queryEl) {
      return;
    }
    queryEl.value = String(text || "").replace(/\s+/g, " ").trim().slice(0, 80);
  }

  function archiveQuerySeed(item) {
    const raw = String((item && (item.excerpt || item.title || item.id)) || "");
    return raw.replace(/^\d{4}-\d{2}-\d{2}\s+/, "").replace(/\s+/g, " ").trim().slice(0, 80);
  }

  function populatedHints(data) {
    const empty = (data && data.empty) || {};
    if (Array.isArray(empty.populated) && empty.populated.length) {
      return empty.populated.filter((item) => item && item.id);
    }
    const rows = [];
    const push = (item, scope) => {
      if (!item) {
        return;
      }
      const id = item.id || item.slug || "";
      const count = Number(item.count);
      const n = Number.isFinite(count)
        ? count
        : String(item.body || "").split("\n").filter((line) => line.trim()).length;
      if (!id || n <= 0) {
        return;
      }
      rows.push({ id, title: item.title || id, count: n, scope: item.scope || scope || "" });
    };
    (data.cells || []).forEach((item) => push(item, "user"));
    (data.taxonomy || []).forEach((item) => push(item, "user"));
    (data.workspace_topics || data.extras || []).forEach((item) => push(item, "workspace"));
    return rows.slice(0, 12);
  }

  const RECALL_PREVIEW = 72;

  function recallHitNode(hit) {
    const title = String((hit && (hit.title || hit.slug)) || "").trim();
    const text = String((hit && hit.text) || "").trim();
    const caption = recall.recallHitCaption(hit) || title;
    const mark = recall.recallScoreMark(hit);
    const target = { id: (hit && (hit.slug || hit.id)) || "", scope: (hit && hit.scope) || "" };
    const openCell = () => {
      highlightMemoryCells([target]);
      if (recall.recallIsWeak(hit)) {
        const hint =
          typeof recall.recallReadHint === "function" ? recall.recallReadHint(hit) : "read";
        insertComposerText(hint);
      }
    };
    const preview = text.length > RECALL_PREVIEW ? `${text.slice(0, RECALL_PREVIEW)}…` : text;
    if (text.length <= RECALL_PREVIEW) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "memory-link";
      if (mark) {
        button.dataset.score = String(recall.recallScore ? recall.recallScore(hit) : "");
        button.classList.toggle("recall-weak", mark.startsWith("弱"));
      }
      button.textContent = caption && text ? `${caption}: ${text}` : caption || text;
      button.addEventListener("click", openCell);
      return button;
    }
    const card = document.createElement("details");
    card.className = "memory-recall";
    card.dataset.id = target.id;
    card.dataset.scope = target.scope;
    if (mark) {
      card.dataset.score = String(recall.recallScore ? recall.recallScore(hit) : "");
      card.classList.toggle("recall-weak", mark.startsWith("弱"));
    }
    const summary = document.createElement("summary");
    summary.textContent = caption ? `${caption} · ${preview}` : preview;
    const body = document.createElement("button");
    body.type = "button";
    body.className = "memory-link memory-recall-body";
    body.textContent = text;
    body.addEventListener("click", openCell);
    card.addEventListener("toggle", () => {
      if (card.open) {
        openCell();
      }
    });
    card.append(summary, body);
    return card;
  }

  function renderRecalled(recalledEl, data) {
    const hits = data.hits || [];
    if (hits.length) {
      recalledEl.replaceChildren();
      if (typeof recall.recallHitsLayer === "function" && recall.recallHitsLayer(hits) === "mixed") {
        const note = document.createElement("p");
        note.className = "muted memory-mixed-note";
        note.textContent = "混层：工作集优先，旧笔记不要当当前偏好";
        recalledEl.appendChild(note);
      }
      hits.forEach((hit) => {
        recalledEl.appendChild(recallHitNode(hit));
      });
      const archiveHints = ((data.empty && data.empty.archive) || []).filter(
        (item) => item && item.overlap && (item.id || item.slug),
      );
      if (archiveHints.length) {
        const note = document.createElement("p");
        note.className = "muted memory-empty-note memory-archive-browse";
        note.textContent = "重叠归档可浏览，不是本轮源头";
        recalledEl.appendChild(note);
        archiveHints.forEach((item) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "memory-link memory-archive-hint";
          const excerpt = String(item.excerpt || "").trim();
          button.textContent = excerpt
            ? `${item.title || item.id} · ${excerpt}`
            : `${item.title || item.id} · ${item.count || 0} 条`;
          button.addEventListener("click", () => {
            fillMemoryQuery(archiveQuerySeed(item));
            highlightMemoryCells([{ id: item.id, scope: item.scope || "" }]);
          });
          recalledEl.appendChild(button);
        });
      }
      return;
    }
    const empty = data.empty || {};
    const query = String(data.query || "").trim();
    const tokens = empty.tokens || [];
    const reason = empty.reason || (query && !tokens.length ? "too_generic" : query ? "no_overlap" : "");
    recalledEl.replaceChildren();
    const note = document.createElement("p");
    note.className = "muted memory-empty-note";
    if (!query) {
      note.textContent = "输入关键词后显示 Recalled";
    } else if (reason === "too_generic") {
      note.textContent = "查询太泛，换格子里的具体词";
    } else {
      note.textContent = `没有与「${query}」重叠的条目`;
    }
    recalledEl.appendChild(note);
    const archived = Number(empty.archive_count);
    const archiveHints = Array.isArray(empty.archive) ? empty.archive : [];
    if (query && Number.isFinite(archived) && archived > 0) {
      const arch = document.createElement("p");
      arch.className = "muted memory-empty-note";
      arch.textContent = `归档还有 ${archived} 条`;
      recalledEl.appendChild(arch);
    }
    archiveHints.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "memory-link memory-archive-hint";
      const excerpt = String(item.excerpt || "").trim();
      button.textContent = excerpt
        ? `${item.title || item.id} · ${excerpt}`
        : `${item.title || item.id} · ${item.count || 0} 条`;
      button.addEventListener("click", () => {
        fillMemoryQuery(archiveQuerySeed(item));
        highlightMemoryCells([{ id: item.id, scope: item.scope || "" }]);
      });
      recalledEl.appendChild(button);
    });
    const hints = populatedHints(data);
    if (!hints.length) {
      return;
    }
    const label = document.createElement("p");
    label.className = "muted memory-empty-note";
    label.textContent = "有内容的格子";
    recalledEl.appendChild(label);
    hints.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "memory-link memory-empty-hint";
      button.textContent = `${item.title || item.id} · ${item.count || 0} 条`;
      button.addEventListener("click", () => highlightMemoryCells([{ id: item.id, scope: item.scope || "" }]));
      recalledEl.appendChild(button);
    });
  }

  function renderTimelineEvents(timeEl, data) {
    const events = data.timeline_events || [];
    if (!events.length) {
      timeEl.textContent = data.timeline || "还没有日期事件";
      return;
    }
    timeEl.replaceChildren();
    const groups = new Map();
    events.forEach((item) => {
      const day = item.date || "未标日期";
      if (!groups.has(day)) {
        groups.set(day, []);
      }
      groups.get(day).push(item);
    });
    groups.forEach((rows, day) => {
      const card = document.createElement("details");
      card.className = "memory-time";
      card.dataset.date = day;
      const summary = document.createElement("summary");
      summary.textContent = `${day} · ${rows.length} 条`;
      const list = document.createElement("div");
      list.className = "memory-time-list";
      rows.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "memory-link";
        button.textContent = item.text || day;
        button.addEventListener("click", () => {
          fillMemoryQuery(item.text || day);
          const cells = (data.cells || []).filter((cell) => {
            const body = String(cell.body || "");
            return Boolean(item.text) && body.includes(String(item.text).slice(0, 12));
          });
          const slugs = cells.map((cell) => cell.id).concat(recallTargets(data));
          if (item.date) {
            slugs.push("timeline");
          }
          highlightMemoryCells(slugs);
          const timeCard = document.querySelector(`#memory-timeline .memory-time[data-date="${day}"]`);
          if (timeCard) {
            timeCard.open = true;
            timeCard.classList.add("linked");
          }
        });
        list.appendChild(button);
      });
      card.append(summary, list);
      timeEl.appendChild(card);
    });
  }

  function isPlaceholderProfile(text) {
    const body = String(text || "").trim();
    if (!body || body === "尚未形成画像") {
      return true;
    }
    const lines = body.split(/\n+/).map((line) => line.replace(/^[-*]\s*/, "").trim()).filter(Boolean);
    return lines.every(
      (line) =>
        line.startsWith("#") ||
        line.startsWith("对话轮次") ||
        line.includes("尚未记录") ||
        line === "无" ||
        /：\s*(尚未记录|无)\s*$/.test(line),
    );
  }

  async function persistMemoryCell(slug, body, memScope, description) {
    if (!slug) {
      throw new Error("缺少记忆格");
    }
    if (window.__wittyTest && typeof window.__wittyTest.saveMemory === "function") {
      return window.__wittyTest.saveMemory({ slug, body, scope: memScope, description });
    }
    if (typeof api.saveMemory !== "function") {
      throw new Error("当前壳不能改记忆");
    }
    const current = scope();
    return api.saveMemory({
      project_id: current.project_id,
      agent_id: current.agent_id,
      workspace_dir: current.workspace_dir,
      slug,
      body,
      scope: memScope || "user",
      description: description || "",
    });
  }

  function attachMemoryEditor(card, item) {
    const editor = document.createElement("textarea");
    editor.className = "memory-cell-editor";
    editor.hidden = true;
    editor.rows = 6;
    editor.value = item.body || "";
    const actions = document.createElement("div");
    actions.className = "memory-cell-actions";
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "ghost quiet";
    editBtn.textContent = "修改";
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "ghost quiet";
    saveBtn.textContent = "保存";
    saveBtn.hidden = true;
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "ghost quiet";
    clearBtn.textContent = "清空";
    const status = document.createElement("span");
    status.className = "muted memory-cell-status";
    const stop = (event) => {
      event.preventDefault();
      event.stopPropagation();
    };
    const startEdit = () => {
      const body = card.querySelector(".memory-cell-body");
      if (body) {
        body.hidden = true;
      }
      editor.hidden = false;
      saveBtn.hidden = false;
      editor.focus();
    };
    editBtn.addEventListener("click", (event) => {
      stop(event);
      startEdit();
    });
    saveBtn.addEventListener("click", async (event) => {
      stop(event);
      status.textContent = "保存中…";
      try {
        await persistMemoryCell(item.slug, editor.value, item.scope, item.description);
        await loadMemory();
      } catch (error) {
        status.textContent = error && error.message ? error.message : "保存失败";
      }
    });
    clearBtn.addEventListener("click", async (event) => {
      stop(event);
      if (!window.confirm(`清空「${item.slug}」这一格？`)) {
        return;
      }
      status.textContent = "清空中…";
      try {
        await persistMemoryCell(item.slug, "", item.scope, item.description);
        await loadMemory();
      } catch (error) {
        status.textContent = error && error.message ? error.message : "清空失败";
      }
    });
    actions.append(editBtn, saveBtn, clearBtn, status);
    card.append(editor, actions);
  }

  function renderMemory(payload) {
    const data = payload || {};
    const profileEl = document.getElementById("memory-profile");
    const latticeEl = document.getElementById("memory-lattice");
    const taxEl = document.getElementById("memory-taxonomy");
    const timeEl = document.getElementById("memory-timeline");
    const linkEl = document.getElementById("memory-links");
    const recalledEl = document.getElementById("memory-recalled");
    if (profileEl) {
      const body = String(data.profile || "").trim() || "尚未形成画像";
      profileEl.textContent = body;
      const summary = document.getElementById("memory-profile-summary");
      const fold = document.getElementById("memory-profile-fold");
      const empty = isPlaceholderProfile(body);
      if (summary) {
        summary.textContent = empty ? "用户画像 · 空" : "用户画像";
      }
      if (fold) {
        if (!fold.dataset.wired) {
          fold.dataset.wired = "1";
          fold.addEventListener("toggle", (event) => {
            if (event.isTrusted) {
              fold.dataset.userToggled = "1";
            }
          });
        }
        if (!fold.dataset.userToggled) {
          fold.open = false;
        }
      }
    }
    if (timeEl) {
      renderTimelineEvents(timeEl, data);
    }
    if (recalledEl) {
      renderRecalled(recalledEl, data);
    }
    if (linkEl) {
      linkEl.replaceChildren();
      const links = data.links || [];
      if (!links.length) {
        linkEl.textContent = "还没有连边";
      } else {
        links.forEach((item) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "memory-link";
          button.textContent = `${item.from_title || item.from} ↔ ${item.to_title || item.to}`;
          button.addEventListener("click", () => highlightMemoryCells([item.from, item.to]));
          linkEl.appendChild(button);
        });
      }
    }
    if (latticeEl) {
      latticeEl.replaceChildren();
      (data.cells || []).forEach((cell) => {
        const card = document.createElement("details");
        card.className = "memory-cell";
        card.dataset.id = cell.id || "";
        card.dataset.scope = cell.scope || "user";
        const summary = document.createElement("summary");
        const count = Number(cell.count);
        const n = Number.isFinite(count) ? count : String(cell.body || "").split("\n").filter((line) => line.trim()).length;
        const title = document.createElement("span");
        title.textContent = cell.title || cell.id;
        const badge = document.createElement("span");
        badge.className = "cell-count";
        badge.textContent = String(n);
        summary.append(title, badge);
        card.dataset.empty = n ? "0" : "1";
        const preview = document.createElement("span");
        preview.className = "cell-preview";
        const raw = String(cell.body || "").replace(/\s+/g, " ").trim();
        preview.textContent = raw && raw !== "还是空的" ? raw.slice(0, 72) : "还没有记下内容";
        const body = document.createElement("pre");
        body.className = "memory-cell-body";
        body.textContent = cell.body || "还是空的";
        card.append(summary, preview, body);
        attachMemoryEditor(card, {
          slug: cell.id,
          scope: cell.scope || "user",
          description: cell.description || cell.title || cell.id,
          body: cell.body || "",
        });
        latticeEl.appendChild(card);
      });
    }
    if (taxEl) {
      const rows = data.taxonomy || [];
      taxEl.replaceChildren();
      if (!rows.length) {
        taxEl.textContent = "还没有归过类";
      } else {
        rows.forEach((item) => {
          const card = document.createElement("details");
          card.className = "memory-tax";
          card.dataset.id = item.id || "";
          card.dataset.scope = item.scope || "user";
          const summary = document.createElement("summary");
          const count = Number(item.count);
          const n = Number.isFinite(count)
            ? count
            : String(item.body || "").split("\n").filter((line) => line.trim()).length;
          summary.textContent = `${item.title || item.id} · ${n} 条`;
          const body = document.createElement("pre");
          body.className = "memory-cell-body";
          body.textContent = item.body || "（空）";
          card.append(summary, body);
          taxEl.appendChild(card);
        });
      }
    }
    const archiveEl = document.getElementById("memory-archive");
    if (archiveEl) {
      const rows = data.archive || [];
      archiveEl.replaceChildren();
      if (!rows.length) {
        archiveEl.textContent = "还没有归档";
      } else {
        rows.forEach((item) => {
          const card = document.createElement("details");
          card.className = "memory-archive";
          card.dataset.id = item.id || `archive/${item.slug || ""}`;
          card.dataset.scope = item.scope || "user";
          const summary = document.createElement("summary");
          summary.textContent = `${item.title || item.id} · ${item.count || 0} 条`;
          const body = document.createElement("pre");
          body.className = "memory-cell-body";
          body.textContent = item.body || "（空）";
          card.append(summary, body);
          archiveEl.appendChild(card);
        });
      }
    }
    const extraEl = document.getElementById("memory-workspace");
    if (extraEl) {
      const rows = data.workspace_topics || data.extras || [];
      extraEl.replaceChildren();
      if (!rows.length) {
        extraEl.textContent = "还没有本仓笔记";
      } else {
        rows.forEach((item) => {
          const card = document.createElement("details");
          card.className = "memory-extra";
          card.dataset.id = item.id || item.slug || "";
          card.dataset.scope = item.scope || "workspace";
          const summary = document.createElement("summary");
          const count = Number(item.count);
          const n = Number.isFinite(count)
            ? count
            : String(item.body || "").split("\n").filter((line) => line.trim()).length;
          const title = item.title || item.id || item.slug || "";
          summary.textContent = `工作区 · ${title} · ${n} 条`;
          const body = document.createElement("pre");
          body.className = "memory-cell-body";
          body.textContent = item.body || "（空）";
          card.append(summary, body);
          extraEl.appendChild(card);
        });
      }
    }
    const slugs = recallTargets(data);
    if (pendingMemoryFocus) {
      highlightMemoryCells([pendingMemoryFocus]);
    } else if (slugs.length) {
      highlightMemoryCells(slugs);
    }
  }

  async function fetchMemoryPayload() {
    if (window.__wittyTest && typeof window.__wittyTest.getMemory === "function") {
      return window.__wittyTest.getMemory();
    }
    if (typeof api.getMemory !== "function") {
      return null;
    }
    const current = scope();
    const queryEl = document.getElementById("memory-query");
    const recall = queryEl ? queryEl.value.trim() : "";
    return api.getMemory(current.project_id, current.agent_id, current.workspace_dir, recall);
  }

  async function refreshRecalled() {
    const recalledEl = document.getElementById("memory-recalled");
    if (!recalledEl) {
      return false;
    }
    try {
      const payload = await fetchMemoryPayload();
      if (!payload) {
        return false;
      }
      renderRecalled(
        recalledEl,
        attachLoadedHits(attachRelocatedHits(payload, pendingMemoryRelocated), loadedBrowseHits),
      );
      return true;
    } catch {
      return false;
    }
  }

  async function loadMemory() {
    const payload = await fetchMemoryPayload();
    if (!payload) {
      return;
    }
    renderMemory(attachLoadedHits(attachRelocatedHits(payload, pendingMemoryRelocated), loadedBrowseHits));
    pendingMemoryFocus = null;
  }

  function renderLinkDetail(item) {
    const box = document.getElementById("link-detail");
    if (!box) {
      return;
    }
    if (!item) {
      box.textContent = "点左边一条查看标题、别名和意图履历。";
      return;
    }
    const aliases = Array.isArray(item.aliases) ? item.aliases.join("、") : "";
    const history = Array.isArray(item.intents) ? item.intents.filter(Boolean).join("；") : "";
    box.textContent = [
      item.title || item.host || "未命名",
      item.url || "",
      aliases ? `别名：${aliases}` : "",
      item.intent ? `当前意图：${item.intent}` : "",
      history ? `履历：${history}` : "",
      item.note ? `备注：${item.note}` : "",
      `使用 ${item.hits || 1} 次`,
    ]
      .filter(Boolean)
      .join("\n");
  }

  function renderLinks(payload) {
    const box = document.getElementById("link-list");
    const habits = document.getElementById("link-habits");
    if (habits) {
      const text = (payload && (payload.habits || payload.text)) || "";
      habits.textContent = text || "（按使用次数）";
      habits.classList.toggle("muted", !text);
    }
    if (!box) {
      return;
    }
    const rows = (payload && payload.links) || [];
    box.innerHTML = "";
    if (!rows.length) {
      box.textContent = "还没有链接";
      box.classList.add("muted");
      renderLinkDetail(null);
      return;
    }
    box.classList.remove("muted");
    rows.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "habit-item";
      const title = item.title || item.host || item.url || "未命名";
      const intent = item.intent || "";
      const aliases = Array.isArray(item.aliases) ? item.aliases.join("、") : "";
      button.innerHTML = `<strong>${md.escapeHtml(title)}</strong><small>${md.escapeHtml(item.url || "")} · ${item.hits || 1} 次${aliases ? ` · 又称 ${md.escapeHtml(aliases)}` : ""}${intent ? ` · ${md.escapeHtml(intent)}` : ""}</small>`;
      button.addEventListener("click", () => {
        box.querySelectorAll(".habit-item").forEach((node) => {
          delete node.dataset.active;
        });
        button.dataset.active = "1";
        renderLinkDetail(item);
        const urlEl = document.getElementById("link-url");
        const titleEl = document.getElementById("link-title");
        const intentEl = document.getElementById("link-intent");
        const aliasEl = document.getElementById("link-alias");
        if (urlEl) {
          urlEl.value = item.url || "";
        }
        if (titleEl) {
          titleEl.value = title;
        }
        if (intentEl) {
          intentEl.value = intent;
        }
        if (aliasEl) {
          aliasEl.value = Array.isArray(item.aliases) ? item.aliases[0] || "" : "";
        }
      });
      if (index === 0) {
        button.dataset.active = "1";
        renderLinkDetail(item);
        button.click();
      }
      box.appendChild(button);
    });
  }

  function wikiWorkspace() {
    return (workspaceEl && workspaceEl.value.trim()) || "";
  }

  function setWikiStatus(text) {
    const el = document.getElementById("wiki-status");
    if (el) {
      el.textContent = text || "";
    }
  }

  function renderWiki(payload) {
    const list = document.getElementById("wiki-list");
    const detail = document.getElementById("wiki-detail");
    if (!list) {
      return;
    }
    const rows = (payload && payload.sources) || [];
    list.replaceChildren();
    if (!payload || payload.enabled === false) {
      list.textContent = (payload && payload.error) || "wiki 未启用";
      list.classList.add("muted");
      return;
    }
    if (!rows.length) {
      list.textContent = "还没有原文。选文件或贴网址，默认转入 wiki。";
      list.classList.add("muted");
      if (detail) {
        detail.textContent = payload.text || "点左边一条看路径。";
      }
      return;
    }
    list.classList.remove("muted");
    rows.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "habit-item";
      button.textContent = `${item.id || "-"} · ${item.kind || "-"}`;
      button.addEventListener("click", () => {
        if (detail) {
          detail.classList.remove("muted");
          detail.textContent = [
            item.id,
            item.path,
            item.origin,
            item.status,
            item.added,
          ]
            .filter(Boolean)
            .join("\n");
        }
      });
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "ghost quiet";
      drop.textContent = "删除";
      drop.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        try {
          const next = await api.removeWiki(item.id, wikiWorkspace());
          setWikiStatus(next.text || "已删除");
          renderWiki(next);
        } catch (error) {
          setWikiStatus(error.message);
        }
      });
      const row = document.createElement("div");
      row.className = "habit-row";
      row.append(button, drop);
      list.appendChild(row);
    });
    if (detail && payload.text) {
      detail.classList.add("muted");
      detail.textContent = `${rows.length} 条原文，待编译 ${payload.pending || 0}。`;
    }
  }

  async function loadWiki() {
    if (typeof api.getWiki !== "function") {
      setWikiStatus("当前壳不支持 wiki 页");
      return;
    }
    const payload = await api.getWiki(wikiWorkspace());
    renderWiki(payload);
    return payload;
  }

  async function addWikiSource(source) {
    const asked = String(source || "").trim();
    if (!asked) {
      setWikiStatus("给一个文件路径或网址");
      return;
    }
    const payload = await api.addWiki({ source: asked, workspace_dir: wikiWorkspace() });
    setWikiStatus(payload.text || "已转入 wiki");
    renderWiki(payload);
    return payload;
  }

  async function compileWiki() {
    let prompt = "用 llm-wiki 把工作区 raw/ 里尚未编译的原文 ingest 进 wiki。先读 SCHEMA 或 AGENTS.md 和 wiki/index.md。";
    try {
      const payload = await api.getWiki(wikiWorkspace());
      if (payload && payload.compile_prompt) {
        prompt = payload.compile_prompt;
      }
    } catch {
      /* 用上面的默认句 */
    }
    switchView("chat");
    promptEl.value = prompt;
    fitPrompt();
    promptEl.focus();
  }

  async function loadLinks(query) {
    const asked = query === undefined ? ((document.getElementById("link-query") || {}).value || "") : query;
    const payload = await api.getLinks(asked);
    renderLinks(payload);
    return payload;
  }

  function renderDiaryDays(days, current) {
    const box = document.getElementById("diary-days");
    if (!box) {
      return;
    }
    box.innerHTML = "";
    if (!days || !days.length) {
      box.textContent = "还没有日记";
      box.classList.add("muted");
      return;
    }
    box.classList.remove("muted");
    days.forEach((day) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "habit-item";
      button.textContent = day;
      if (day === current) {
        button.dataset.active = "1";
      }
      button.addEventListener("click", () => {
        loadDiary(day).catch((error) => {
          const status = document.getElementById("diary-status");
          if (status) {
            status.textContent = error.message;
          }
        });
      });
      box.appendChild(button);
    });
  }

  function renderDiary(payload) {
    const heading = document.getElementById("diary-heading");
    const body = document.getElementById("diary-body");
    if (heading) {
      heading.textContent = payload && payload.day ? payload.day : "当天";
    }
    if (body) {
      body.textContent = (payload && payload.body) || "（这一天还没有日记）";
    }
  }

  async function loadDiary(day) {
    const days = await api.getDiary("", true);
    const asked = day || ((document.getElementById("diary-day") || {}).value || "");
    const payload = await api.getDiary(asked, false);
    renderDiaryDays(days.days || [], payload.day);
    renderDiary(payload);
    const dateEl = document.getElementById("diary-day");
    if (dateEl && payload.day && payload.day !== "today" && /^\d{4}-\d{2}-\d{2}$/.test(payload.day)) {
      dateEl.value = payload.day;
    }
    return payload;
  }

  function emailFieldMap(prefix) {
    return {
      imap_host: document.getElementById(`${prefix}-imap-host`),
      imap_port: document.getElementById(`${prefix}-imap-port`),
      smtp_host: document.getElementById(`${prefix}-smtp-host`),
      smtp_port: document.getElementById(`${prefix}-smtp-port`),
      username: document.getElementById(`${prefix}-username`),
      mailbox: document.getElementById(`${prefix}-mailbox`),
      imap_password: document.getElementById(`${prefix}-imap-password`),
      smtp_password: document.getElementById(`${prefix}-smtp-password`),
      imap_ssl: document.getElementById(`${prefix}-imap-ssl`),
      smtp_ssl: document.getElementById(`${prefix}-smtp-ssl`),
      smtp_starttls: document.getElementById(`${prefix}-smtp-starttls`),
    };
  }

  function fillEmailForm(prefix, payload) {
    const fields = emailFieldMap(prefix);
    if (fields.imap_host) {
      fields.imap_host.value = payload.imap_host || "";
    }
    if (fields.imap_port) {
      fields.imap_port.value = String(payload.imap_port || 993);
    }
    if (fields.smtp_host) {
      fields.smtp_host.value = payload.smtp_host || "";
    }
    if (fields.smtp_port) {
      fields.smtp_port.value = String(payload.smtp_port || 465);
    }
    if (fields.username) {
      fields.username.value = payload.username || "";
    }
    if (fields.mailbox) {
      fields.mailbox.value = payload.mailbox || "INBOX";
    }
    if (fields.imap_password) {
      fields.imap_password.value = "";
      fields.imap_password.placeholder = payload.imap_password ? "已设，留空不改" : "未设";
    }
    if (fields.smtp_password) {
      fields.smtp_password.value = "";
      fields.smtp_password.placeholder = payload.smtp_password ? "已设，留空不改" : "不填则沿用 IMAP 密码";
    }
    if (fields.imap_ssl) {
      fields.imap_ssl.checked = payload.imap_ssl !== false;
    }
    if (fields.smtp_ssl) {
      fields.smtp_ssl.checked = payload.smtp_ssl !== false;
    }
    if (fields.smtp_starttls) {
      fields.smtp_starttls.checked = Boolean(payload.smtp_starttls);
    }
  }

  function collectEmailForm(prefix) {
    const fields = emailFieldMap(prefix);
    const current = scope();
    return {
      project_id: current.project_id,
      agent_id: current.agent_id,
      imap_host: fields.imap_host ? fields.imap_host.value.trim() : "",
      imap_port: fields.imap_port ? Number(fields.imap_port.value || 993) : 993,
      smtp_host: fields.smtp_host ? fields.smtp_host.value.trim() : "",
      smtp_port: fields.smtp_port ? Number(fields.smtp_port.value || 465) : 465,
      username: fields.username ? fields.username.value.trim() : "",
      mailbox: fields.mailbox ? fields.mailbox.value.trim() : "INBOX",
      imap_password: fields.imap_password ? fields.imap_password.value : "",
      smtp_password: fields.smtp_password ? fields.smtp_password.value : "",
      imap_ssl: fields.imap_ssl ? fields.imap_ssl.checked : true,
      smtp_ssl: fields.smtp_ssl ? fields.smtp_ssl.checked : true,
      smtp_starttls: fields.smtp_starttls ? fields.smtp_starttls.checked : false,
    };
  }

  function setEmailStatus(text) {
    ["email-form-status", "mail-form-status"].forEach((id) => {
      const node = document.getElementById(id);
      if (node) {
        node.textContent = text;
      }
    });
  }

  async function saveEmailFrom(prefix) {
    setEmailStatus("保存中…");
    const saved = await api.saveMail(collectEmailForm(prefix));
    fillEmailForm("email", saved);
    fillEmailForm("mail", saved);
    renderMail(saved);
    setEmailStatus(saved.configured ? "已保存。可以在对话里看收件箱。" : "已保存。主机或账号仍不完整，还不能收发。");
    return saved;
  }

  function renderMailStatus(payload) {
    const status = document.getElementById("mail-status");
    if (!status) {
      return;
    }
    if (!payload) {
      status.hidden = true;
      status.replaceChildren();
      return;
    }
    const ok = Boolean(payload.configured);
    const imap = payload.imap_host
      ? `${payload.imap_host}:${payload.imap_port || 993}`
      : "未填";
    const smtp = payload.smtp_host
      ? `${payload.smtp_host}:${payload.smtp_port || 465}`
      : "未填";
    const facts = [
      ["状态", ok ? "可以收发" : "还不能收发"],
      ["IMAP", imap],
      ["SMTP", smtp],
      ["账号", payload.username || "未填"],
      ["密码", payload.imap_password ? "已设" : "未设"],
      ["草稿", `${((payload.drafts || []).length)} 封`],
    ];
    status.hidden = false;
    status.className = ok ? "mail-status is-ready" : "mail-status";
    status.replaceChildren();
    const lead = document.createElement("p");
    lead.className = "mail-status-lead";
    lead.textContent = ok
      ? "通道已保存。在对话里让助手读收件箱或写信，发送前仍会问你。"
      : "还没配齐：需要内网主机、账号和 IMAP 密码。保存后才会真正收发。";
    const list = document.createElement("dl");
    facts.forEach(([name, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = name;
      const dd = document.createElement("dd");
      dd.textContent = value;
      list.append(dt, dd);
    });
    status.append(lead, list);
  }

  function renderMail(payload) {
    renderMailStatus(payload);
    const drafts = document.getElementById("mail-drafts");
    if (!drafts) {
      return;
    }
    const rows = (payload && payload.drafts) || [];
    drafts.innerHTML = "";
    if (!rows.length) {
      drafts.textContent = payload && payload.configured
        ? "还没有本地草稿。在对话里让助手写信后会出现在这里。"
        : "配好通道后，对话里写出的草稿会出现在这里。";
      drafts.classList.add("muted");
      return;
    }
    drafts.classList.remove("muted");
    rows.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "habit-item";
      const subject = item.subject || "(无主题)";
      const to = Array.isArray(item.to) ? item.to.join(", ") : "";
      button.innerHTML = `<strong>${md.escapeHtml(subject)}</strong><small>${md.escapeHtml(item.id || "")}${to ? ` · ${md.escapeHtml(to)}` : ""} · 附件 ${item.attachments || 0}</small>`;
      button.addEventListener("click", () => {
        promptEl.value = `继续处理草稿 ${item.id || ""}`;
        switchView("chat");
        promptEl.focus();
      });
      drafts.appendChild(button);
    });
  }

  function setScheduleStatus(text) {
    const el = document.getElementById("schedule-status");
    if (el) {
      el.textContent = text || "";
    }
  }

  function padClock(value) {
    return String(value).padStart(2, "0");
  }

  function toLocalInput(iso) {
    if (!iso) {
      return "";
    }
    const stamp = new Date(iso);
    if (Number.isNaN(stamp.getTime())) {
      return "";
    }
    return `${stamp.getFullYear()}-${padClock(stamp.getMonth() + 1)}-${padClock(stamp.getDate())}T${padClock(stamp.getHours())}:${padClock(stamp.getMinutes())}`;
  }

  function fromLocalInput(value) {
    const raw = String(value || "").trim();
    if (!raw) {
      return "";
    }
    const stamp = new Date(raw);
    if (Number.isNaN(stamp.getTime())) {
      return "";
    }
    return stamp.toISOString();
  }

  function formatNextFire(iso) {
    if (!iso) {
      return "无下次触发";
    }
    const stamp = new Date(iso);
    if (Number.isNaN(stamp.getTime())) {
      return iso;
    }
    return stamp.toLocaleString();
  }

  function defaultScheduleStart() {
    return toLocalInput(new Date(Date.now() + 5 * 60_000).toISOString());
  }

  function fillScheduleStartIfEmpty() {
    const startEl = document.getElementById("schedule-start");
    if (startEl && !startEl.value) {
      startEl.value = defaultScheduleStart();
    }
  }

  async function loadSchedules() {
    const host = document.getElementById("schedule-list");
    if (!host || typeof api.listSchedules !== "function") {
      return;
    }
    fillScheduleStartIfEmpty();
    const current = scope();
    const payload = await api.listSchedules(current.project_id, current.agent_id);
    const rows = (payload && payload.schedules) || [];
    host.replaceChildren();
    if (!rows.length) {
      host.className = "ctx-list muted";
      host.textContent = "还没有定时任务";
      return;
    }
    host.className = "ctx-list";
    rows.forEach((item) => {
      if (item.error) {
        const line = document.createElement("p");
        line.className = "muted";
        line.textContent = item.error;
        host.appendChild(line);
        return;
      }
      const row = document.createElement("div");
      row.className = "habit-item schedule-row";
      const period = item.period || "一次";
      const next = item.enabled ? formatNextFire(item.next_fire_at) : "已暂停";
      const end = item.end_at ? ` · 止 ${formatNextFire(item.end_at)}` : "";
      row.innerHTML = `<strong>${md.escapeHtml(item.name || "")}</strong><small>${item.enabled ? "启用" : "暂停"} · ${md.escapeHtml(item.status || "active")} · ${md.escapeHtml(period)}${md.escapeHtml(end)} · 下次 ${md.escapeHtml(next)}</small>`;
      const actions = document.createElement("div");
      actions.className = "schedule-actions";
      const pause = document.createElement("button");
      pause.type = "button";
      pause.className = "ghost quiet";
      pause.textContent = item.enabled ? "暂停" : "继续";
      pause.addEventListener("click", async () => {
        if (typeof api.setScheduleEnabled !== "function") {
          setScheduleStatus("当前壳没有暂停接口");
          return;
        }
        try {
          await api.setScheduleEnabled(item.name, !item.enabled, current.project_id, current.agent_id);
          setScheduleStatus(item.enabled ? `已暂停 ${item.name}` : `已继续 ${item.name}`);
          await loadSchedules();
        } catch (error) {
          setScheduleStatus(error.message);
        }
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "ghost quiet";
      del.textContent = "删除";
      del.addEventListener("click", async () => {
        try {
          await api.deleteSchedule(item.name, current.project_id, current.agent_id);
          setScheduleStatus(`已删除 ${item.name}`);
          await loadSchedules();
        } catch (error) {
          setScheduleStatus(error.message);
        }
      });
      actions.append(pause, del);
      row.appendChild(actions);
      host.appendChild(row);
    });
  }

  async function saveScheduleFromForm() {
    if (typeof api.saveSchedule !== "function") {
      setScheduleStatus("当前壳没有创建接口");
      return;
    }
    const name = String((document.getElementById("schedule-name") || {}).value || "").trim();
    const prompt = String((document.getElementById("schedule-prompt") || {}).value || "").trim();
    const startAt = fromLocalInput((document.getElementById("schedule-start") || {}).value || "");
    const endAt = fromLocalInput((document.getElementById("schedule-end") || {}).value || "");
    const period = String((document.getElementById("schedule-period") || {}).value || "");
    const enabled = Boolean((document.getElementById("schedule-enabled") || {}).checked);
    if (!/^[a-z0-9]+(?:[_-][a-z0-9]+)*$/.test(name)) {
      setScheduleStatus("短名用小写字母、数字、连字符");
      return;
    }
    if (!prompt) {
      setScheduleStatus("请填写提示或命令");
      return;
    }
    if (!startAt) {
      setScheduleStatus("请填写开始时间");
      return;
    }
    const current = scope();
    const body = {
      name,
      prompt,
      start_at: startAt,
      period,
      enabled,
      project_id: current.project_id,
      agent_id: current.agent_id,
      workspace: current.workspace || undefined,
    };
    if (endAt) {
      body.end_at = endAt;
    }
    const payload = await api.saveSchedule(body);
    const next = payload && payload.next_fire_at ? formatNextFire(payload.next_fire_at) : "无下次触发";
    setScheduleStatus(enabled ? `已创建 ${name} · 下次 ${next}` : `已保存 ${name}（暂停）`);
    await loadSchedules();
  }

  async function loadMail() {
    const current = scope();
    const payload = await api.getMail(current.project_id, current.agent_id);
    fillEmailForm("email", payload);
    fillEmailForm("mail", payload);
    renderMail(payload);
    return payload;
  }

  function switchView(name) {
    currentView = name;
    document.querySelector(".app").dataset.view = name;
    document.querySelectorAll(".rail-btn[data-view]").forEach((button) => {
      const on = button.dataset.view === name;
      button.classList.toggle("active", on);
      if (on) {
        const pack = button.closest(".rail-pack");
        if (pack) {
          pack.open = true;
        }
      }
    });
    document.querySelectorAll(".view").forEach((section) => {
      const active = section.id === `view-${name}`;
      section.classList.toggle("active", active);
      section.hidden = !active;
    });
    // 弹窗在 top layer，藏掉 #view-skills 不一定收得走，离开这页就显式关掉。
    closeSkillModal();
    if (name === "skills") {
      // 详情已经收进弹窗，加载失败要落到页头的状态行，不然报错跟着弹窗一起看不见。
      loadSkills().catch((error) => {
        setSkillInstallStatus(error.message);
      });
      if (skillWatchTimer) {
        clearInterval(skillWatchTimer);
      }
      skillWatchTimer = setInterval(() => {
        if (typeof api.getPlugins !== "function") {
          return;
        }
        api.getPlugins()
          .then((snap) => {
            const gen = snap && snap.skill_generation;
            if (typeof gen === "number" && gen !== skillGenSeen) {
              skillGenSeen = gen;
              // 磁盘上技能变了要重画，但别把用户正开着的详情窗顺手关掉。
              return loadSkills(skillOpenName || undefined);
            }
            return null;
          })
          .catch(() => {});
      }, 4000);
    } else if (skillWatchTimer) {
      clearInterval(skillWatchTimer);
      skillWatchTimer = 0;
    }
    if (name === "tools") {
      loadTools().catch((error) => {
        toolDetailEl.textContent = error.message;
      });
    }
    if (name === "settings") {
      // 先画静态导航（图标是本地的），提示词条数等 loadPrompts 回来再补。
      drawSettingsNav();
      refreshModel().catch(() => {});
      loadMail().catch((error) => setEmailStatus(error.message));
      loadPrompts().catch((error) => {
        if (promptStatusEl) {
          promptStatusEl.textContent = error.message;
        }
      });
    }
    if (name === "memory") {
      loadMemory().catch((error) => {
        const box = document.getElementById("memory-profile");
        if (box) {
          box.textContent = error.message;
        }
      });
    }
    if (name === "wiki") {
      loadWiki().catch((error) => {
        const box = document.getElementById("wiki-list");
        if (box) {
          box.textContent = error.message;
        }
        setWikiStatus(error.message);
      });
    }
    if (name === "links") {
      loadLinks().catch((error) => {
        const box = document.getElementById("link-list");
        if (box) {
          box.textContent = error.message;
        }
      });
    }
    if (name === "diary") {
      loadDiary().catch((error) => {
        const box = document.getElementById("diary-body");
        if (box) {
          box.textContent = error.message;
        }
      });
    }
    if (name === "mail") {
      loadMail().catch((error) => setEmailStatus(error.message));
    }
  }

  function clipCatalog(text, limit = 72) {
    const raw = String(text || "").replace(/\s+/g, " ").trim();
    if (raw.length <= limit) {
      return raw;
    }
    return `${raw.slice(0, Math.max(0, limit - 1))}…`;
  }

  function setSkillInstallStatus(text) {
    const el = document.getElementById("skill-install-status");
    if (!el) {
      return;
    }
    const msg = String(text || "").trim();
    el.textContent = msg;
    el.hidden = !msg;
  }

  async function pickLocalSkill() {
    if (typeof api.pickSkill === "function") {
      try {
        return await api.pickSkill();
      } catch {
        // browser file input
      }
    }
    return pickSkillFromBrowser();
  }

  async function confirmSkillOverwrite(name) {
    const message = `用户技能 ${name} 已存在，覆盖？`;
    if (typeof api.confirm === "function") {
      return api.confirm(message);
    }
    return window.confirm(message);
  }

  function postInstallSkill(picked, overwrite) {
    const current = scope();
    const body = {
      project_id: current.project_id,
      agent_id: current.agent_id,
      overwrite: Boolean(overwrite),
    };
    if (picked && picked.source) {
      body.source = picked.source;
    } else if (picked && picked.text) {
      body.text = picked.text;
    } else if (picked && picked.brief) {
      body.brief = picked.brief;
      if (picked.name) {
        body.name = picked.name;
      }
    } else if (typeof picked === "string") {
      body.source = picked;
    } else {
      return Promise.reject(new Error("没有选择 SKILL.md 或技能目录"));
    }
    return api.installSkill(body);
  }

  async function createDraftSkill() {
    const asked = window.prompt("用一句话说这个技能做什么（可先写短名，如 invoice-check 检查发票）", "");
    if (asked == null) {
      return;
    }
    const brief = String(asked).trim();
    if (!brief) {
      setSkillInstallStatus("先写一句话，例如：检查发票抬头和税号");
      return;
    }
    const picked = { brief };
    try {
      let installed;
      try {
        installed = await postInstallSkill(picked, false);
      } catch (err) {
        const conflict = (err && err.status === 409) || /已存在/.test(String((err && err.message) || ""));
        if (!conflict) {
          throw err;
        }
        if (!(await confirmSkillOverwrite((err && err.payload && err.payload.name) || brief))) {
          setSkillInstallStatus("已取消覆盖");
          return;
        }
        installed = await postInstallSkill(picked, true);
      }
      setSkillInstallStatus(`已新建 ${installed.name}`);
      await loadSkills(installed.name);
    } catch (err) {
      setSkillInstallStatus(err && err.message ? err.message : "新建失败");
    }
  }

  // details 菜单点完不会自己收，手动关一下；点空白处也要关。
  function closeHubMore() {
    document.querySelectorAll("details.hub-more[open]").forEach((node) => {
      node.removeAttribute("open");
    });
  }

  document.addEventListener("click", (event) => {
    if (!document.querySelector("details.hub-more[open]")) {
      return;
    }
    if (event.target instanceof Element && event.target.closest("details.hub-more")) {
      return;
    }
    closeHubMore();
  });

  async function installLocalSkill() {
    const addBtn = document.getElementById("skill-add");
    setSkillInstallStatus("选择本地 SKILL.md 或技能目录…");
    if (addBtn) {
      addBtn.disabled = true;
    }
    try {
      const picked = await pickLocalSkill();
      if (!picked || !(picked.source || picked.text || typeof picked === "string")) {
        setSkillInstallStatus("");
        return;
      }
      let installed;
      try {
        installed = await postInstallSkill(picked, false);
      } catch (err) {
        const conflict = (err && err.status === 409) || /已存在/.test(String((err && err.message) || ""));
        if (!conflict) {
          throw err;
        }
        const existing = (err && err.payload && err.payload.name) || "";
        const name = existing || String(err.message || "").replace(/^用户技能 /, "").split(" ")[0];
        if (!(await confirmSkillOverwrite(name))) {
          setSkillInstallStatus("已取消覆盖");
          return;
        }
        installed = await postInstallSkill(picked, true);
      }
      setSkillInstallStatus(`已安装 ${installed.name}`);
      await loadSkills(installed.name);
    } catch (err) {
      setSkillInstallStatus(err && err.message ? err.message : "安装失败");
    } finally {
      if (addBtn) {
        addBtn.disabled = false;
      }
    }
  }

  // 切页、目录轮询、装卸技能各会喊一次加载，彼此不等。并发跑的话后落地的那次
  // draw() 会 replaceChildren 把前一次的卡片整批换掉，刚点亮的那张就成了游离节点。
  // 排成队最省事，代价只是慢一拍。
  function loadSkills(keepName) {
    const next = skillLoadChain.catch(() => {}).then(() => loadSkillsOnce(keepName));
    skillLoadChain = next.catch(() => {});
    return next;
  }

  async function loadSkillsOnce(keepName) {
    const current = scope();
    const payload = await api.listSkills(current.project_id, current.agent_id);
    const system = payload.system || payload.skills || [];
    const user = payload.user || [];
    const userDir = payload.user_dir || "";
    const networkLabels = { intranet: "内网", public: "外网", general: "通用" };
    const skillNetwork = (item) => {
      const key = item.network || "general";
      return item.network_label || networkLabels[key] || "通用";
    };
    const cats = [
      { id: "all", label: "精选推荐", mark: "✦" },
      { id: "intranet", label: "内网", network: "intranet", mark: "⌂" },
      { id: "public", label: "外网", network: "public", mark: "◎" },
      { id: "general", label: "通用", network: "general", mark: "◇" },
      { id: "office", label: "文档办公", hit: /office|slides|ppt|document|公文|幻灯/i, mark: "▣" },
      { id: "write", label: "写作沟通", hit: /mail|diary|office|沟通|邮件|日记/i, mark: "✎" },
      { id: "research", label: "研究知识", hit: /benchmark|知识|评测/i, mark: "☰" },
      { id: "data", label: "数据分析", hit: /data|analysis|csv|统计|分析/i, mark: "▦" },
      { id: "dev", label: "开发自动化", hit: /software|engineer|port|skill-port|开发|工程/i, mark: "{}" },
      { id: "agent", label: "技能与工作流", hit: /agent|creation|optimization|evaluation|workflow/i, mark: "✱" },
    ];
    const catHost = document.getElementById("skill-cats");
    if (catHost) {
      catHost.replaceChildren();
      cats.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "hub-cat" + (item.id === skillCat ? " active" : "");
        button.textContent = `${item.mark || ""} ${item.label}`.trim();
        button.addEventListener("click", () => {
          skillCat = item.id;
          catHost.querySelectorAll(".hub-cat").forEach((node) => {
            node.classList.toggle("active", node === button);
          });
          draw();
        });
        catHost.appendChild(button);
      });
    }
    document.querySelectorAll("#skill-tabs .hub-tab").forEach((button) => {
      button.classList.toggle("active", (button.dataset.hub || "all") === skillHub);
      button.onclick = () => {
        skillHub = button.dataset.hub || "all";
        document.querySelectorAll("#skill-tabs .hub-tab").forEach((node) => {
          node.classList.toggle("active", node === button);
        });
        draw();
      };
    });
    const ico = (name) => {
      if (/ppt|slide/i.test(name)) {
        return "▣";
      }
      if (/mail/i.test(name)) {
        return "✉";
      }
      if (/diary|link/i.test(name)) {
        return "◈";
      }
      if (/data|analy/i.test(name)) {
        return "▦";
      }
      if (/agent/i.test(name)) {
        return "✶";
      }
      return "◇";
    };
    const draw = (needle) => {
      const q = (needle === undefined ? ((document.getElementById("skill-filter") || {}).value || "") : needle)
        .trim()
        .toLowerCase();
      skillListEl.replaceChildren();
      let rows = skillHub === "system" ? system : skillHub === "user" ? user : system.concat(user);
      const catRule = cats.find((item) => item.id === skillCat);
      if (catRule && catRule.network) {
        rows = rows.filter((item) => (item.network || "general") === catRule.network);
      } else if (catRule && catRule.hit) {
        rows = rows.filter((item) => catRule.hit.test(`${item.name} ${item.description || ""}`));
      }
      rows = rows.filter((item) => !q || `${item.name} ${item.description}`.toLowerCase().includes(q));
      rows.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "catalog-item skill-card";
        button.dataset.name = item.name;
        const origin = system.some((row) => row.name === item.name) ? "系统" : "用户";
        const net = item.network || "general";
        const enabled = item.enabled ? "已启用" : "未启用";
        // 卡头一行：图标 + 名字 + 右对齐状态。原来把 网络标签 塞进名字列当第二行，
        // 白占一行且让不同长度的名字后面状态位置参差。标签下移到页脚跟作者同排。
        button.innerHTML = `<div class="skill-card-top"><span class="skill-ico">${ico(item.name)}</span><strong>${md.escapeHtml(item.name)}</strong><em class="skill-status${item.enabled ? "" : " off"}">${enabled}</em></div><p>${md.escapeHtml(clipCatalog(item.description, 72))}</p><small class="skill-card-foot"><em class="skill-badge net-${md.escapeHtml(net)}">${md.escapeHtml(skillNetwork(item))}</em><span>作者 · ${origin}</span></small>`;
        button.addEventListener("click", () => showSkill(item.name));
        skillListEl.appendChild(button);
      });
      if (!skillListEl.children.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "没有匹配的技能";
        skillListEl.appendChild(empty);
      }
    };
    const filter = document.getElementById("skill-filter");
    if (filter) {
      filter.oninput = () => draw();
    }
    const addBtn = document.getElementById("skill-add");
    if (addBtn) {
      addBtn.onclick = () => installLocalSkill();
    }
    const newBtn = document.getElementById("skill-new");
    if (newBtn) {
      newBtn.onclick = () => createDraftSkill();
    }
    const mountBtn = document.getElementById("skill-mount");
    if (mountBtn) {
      mountBtn.onclick = () => {
        closeHubMore();
        mountSkillDir();
      };
    }
    const reloadBtn = document.getElementById("skill-reload");
    if (reloadBtn) {
      reloadBtn.onclick = () => {
        closeHubMore();
        reloadPluginSurface();
      };
    }
    draw();
    if (!keepName) {
      // 这次加载还在飞的时候可能已经有人开了详情窗（证据跳转就是），别顺手把它关掉。
      if (!skillOpenName) {
        closeSkillModal();
        skillDetailEl.innerHTML = "";
      }
      return;
    }
    try {
      await showSkill(keepName);
    } catch (error) {
      // 技能可能刚被别处删掉。关窗报一句就行，别让整次刷新连列表一起失败。
      closeSkillModal();
      skillDetailEl.innerHTML = "";
      setSkillInstallStatus(error && error.message ? error.message : `读不到技能 ${keepName}`);
    }
  }

  function openSkillModal() {
    if (!skillModalEl || skillModalEl.open) {
      return;
    }
    if (typeof skillModalEl.showModal === "function") {
      skillModalEl.showModal();
    } else {
      skillModalEl.setAttribute("open", "");
    }
  }

  function closeSkillModal() {
    // dialog 的 close 事件是排队派发的，这里同步清一次，好让并发的 loadSkills 看到真状态。
    skillOpenName = "";
    if (!skillModalEl || !skillModalEl.open) {
      return;
    }
    if (typeof skillModalEl.close === "function") {
      skillModalEl.close();
    } else {
      skillModalEl.removeAttribute("open");
    }
  }

  if (skillModalEl) {
    const skillModalCloseBtn = document.getElementById("skill-modal-close");
    if (skillModalCloseBtn) {
      skillModalCloseBtn.addEventListener("click", () => closeSkillModal());
    }
    // 点遮罩关掉：dialog 的遮罩不是独立节点，点上去 target 就是 dialog 本身。
    skillModalEl.addEventListener("click", (event) => {
      if (event.target === skillModalEl) {
        closeSkillModal();
      }
    });
    // Esc 走的是 dialog 自带的 cancel，收在 close 里一起把卡片高亮撤掉。
    skillModalEl.addEventListener("close", () => {
      // close 是排队派发的，队列排到之前可能已经开着另一个技能了，那就别动。
      if (skillModalEl.open) {
        return;
      }
      skillOpenName = "";
      skillListEl.querySelectorAll(".skill-card.active").forEach((node) => {
        node.classList.remove("active");
      });
    });
  }

  async function showSkill(name) {
    const detail = await api.getSkill(name);
    const current = scope();
    const origin = detail.origin === "user" ? "用户技能" : "系统技能";
    const net = detail.network || "general";
    const netLabel = detail.network_label || { intranet: "内网", public: "外网", general: "通用" }[net] || "通用";
    const extras = [
      detail.scripts ? "scripts" : "",
      detail.references ? "references" : "",
      detail.assets ? "assets" : "",
    ].filter(Boolean);
    const extraHtml = extras
      .map(
        (dir) =>
          `<details class="skill-extra"><summary>${dir}/</summary><p class="muted">相对 ${md.escapeHtml(detail.path || "")}</p></details>`,
      )
      .join("");
    skillDetailEl.innerHTML = `
      <h1>${md.escapeHtml(detail.name)} <span class="badge net-${md.escapeHtml(net)}">${md.escapeHtml(netLabel)}</span> <span class="badge">${origin}</span></h1>
      <p>${md.escapeHtml(detail.description || "")}</p>
      <label class="toggle"><input id="skill-enabled" type="checkbox" ${detail.enabled ? "checked" : ""} /> 对当前 Agent 启用</label>
      ${detail.origin === "user" ? `<button id="skill-uninstall" class="ghost" type="button">卸载</button>` : ""}
      <p class="muted">${md.escapeHtml(detail.path || "")}</p>
      <details class="skill-body">
        <summary>SKILL.md 正文</summary>
        <div class="md">${md.render(detail.body || "")}</div>
      </details>
      ${extraHtml}`;
    skillListEl.querySelectorAll(".catalog-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.name === name);
    });
    skillOpenName = name;
    openSkillModal();
    skillDetailEl.scrollTop = 0;
    const box = document.getElementById("skill-enabled");
    box.addEventListener("change", async () => {
      await api.setSkillEnabled(name, box.checked, current.project_id, current.agent_id);
      await loadSkills(name);
    });
    const drop = document.getElementById("skill-uninstall");
    if (drop) {
      drop.addEventListener("click", async () => {
        const ok = typeof api.confirm === "function"
          ? await api.confirm(`卸载用户技能 ${name}？`)
          : window.confirm(`卸载用户技能 ${name}？`);
        if (!ok) {
          return;
        }
        try {
          await api.uninstallSkill(name, current.project_id, current.agent_id);
          setSkillInstallStatus(`已卸载 ${name}`);
          await loadSkills();
        } catch (err) {
          setSkillInstallStatus(err && err.message ? err.message : "卸载失败");
        }
      });
    }
  }

  async function reloadPluginSurface() {
    try {
      const payload = await api.reloadPlugins();
      setSkillInstallStatus(`已重载，技能 ${payload.skills || 0}，工具 ${payload.tools || 0}`);
      await loadSkills();
    } catch (err) {
      setSkillInstallStatus(err && err.message ? err.message : "重载失败");
    }
  }

  async function mountSkillDir() {
    let picked = "";
    if (typeof api.pickDirectory === "function") {
      try {
        const result = await api.pickDirectory();
        picked = (result && (result.path || result)) || "";
      } catch {
        picked = "";
      }
    }
    if (!picked) {
      picked = window.prompt("技能目录绝对路径（含 SKILL.md 的父目录）", "") || "";
    }
    picked = String(picked).trim();
    if (!picked) {
      return;
    }
    try {
      const payload = await api.attachSkillPath(picked);
      setSkillInstallStatus(`已挂载 ${payload.path || picked}，不必重启`);
      await loadSkills();
    } catch (err) {
      setSkillInstallStatus(err && err.message ? err.message : "挂载失败");
    }
  }

  async function loadTools() {
    const current = scope();
    const payload = await api.listTools(current.project_id, current.agent_id);
    const items = payload.tools || [];
    const selected = (toolDetailEl.querySelector("h1") || {}).textContent || "";
    const draw = (needle) => {
      toolListEl.replaceChildren();
      const matched = items.filter(
        (item) => !needle || `${item.name} ${item.description}`.toLowerCase().includes(needle),
      );
      const groups = [
        { id: "kernel", title: "内核", rows: matched.filter((item) => item.kernel) },
        { id: "biz", title: "业务", rows: matched.filter((item) => !item.kernel && item.enabled) },
        { id: "off", title: "已关", rows: matched.filter((item) => !item.kernel && !item.enabled) },
      ];
      groups.forEach((group) => {
        if (!group.rows.length) {
          return;
        }
        const card = document.createElement("details");
        card.className = "catalog-fold";
        card.dataset.group = group.id;
        const hasActive = group.rows.some((item) => selected.includes(item.name));
        card.open = Boolean(needle || group.id === "kernel" || hasActive);
        const summary = document.createElement("summary");
        summary.textContent = `${group.title} · ${group.rows.length}`;
        card.appendChild(summary);
        group.rows.forEach((item) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "catalog-item";
          const mark = item.kernel ? "内核" : item.enabled ? "业务" : "已关";
          button.dataset.name = item.name;
          button.innerHTML = `${md.escapeHtml(item.name)} · ${mark}<small>${md.escapeHtml(clipCatalog(item.description))}</small>`;
          button.addEventListener("click", () => showTool(item));
          card.appendChild(button);
        });
        toolListEl.appendChild(card);
      });
      if (!toolListEl.children.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = needle ? "没有匹配的工具" : "没有工具。";
        toolListEl.appendChild(empty);
      }
    };
    document.getElementById("tool-filter").oninput = (event) => {
      draw(event.target.value.trim().toLowerCase());
    };
    draw("");
    const keep = items.find((item) => selected.includes(item.name)) || items[0];
    if (keep) {
      showTool(keep);
    } else {
      toolDetailEl.innerHTML = "<p class='muted'>没有工具。</p>";
    }
  }

  function showTool(item) {
    const current = scope();
    const props = (item.parameters && item.parameters.properties) || {};
    const rows = Object.keys(props)
      .map((key) => `<tr><td>${md.escapeHtml(key)}</td><td>${md.escapeHtml(props[key].type || "")}</td><td>${md.escapeHtml(props[key].description || "")}</td></tr>`)
      .join("");
    toolDetailEl.innerHTML = `
      <h1>${md.escapeHtml(item.name)} ${item.kernel ? '<span class="badge kernel">内核，不可关闭</span>' : ""}</h1>
      <p>${md.escapeHtml(item.description || "")}</p>
      <label class="toggle"><input id="tool-enabled" type="checkbox" ${item.enabled ? "checked" : ""} ${item.kernel ? "disabled" : ""} /> 对当前 Agent 启用</label>
      <div class="md-table"><table><thead><tr><th>参数</th><th>类型</th><th>说明</th></tr></thead><tbody>${rows || "<tr><td colspan='3'>无</td></tr>"}</tbody></table></div>
      <details class="skill-body tool-schema">
        <summary>JSON Schema</summary>
        <pre class="md-code"><code>${md.escapeHtml(JSON.stringify(item.parameters || {}, null, 2))}</code></pre>
      </details>`;
    toolListEl.querySelectorAll(".catalog-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.name === item.name);
    });
    const box = document.getElementById("tool-enabled");
    box.addEventListener("change", async () => {
      await api.setToolEnabled(item.name, box.checked, current.project_id, current.agent_id);
      await loadTools();
    });
  }

  const workspacePick = document.getElementById("workspace-pick");
  if (workspacePick) {
    workspacePick.addEventListener("click", () => {
      pickWorkspaceDir().catch((error) => setStatus(error.message || "选工作区失败", "busy"));
    });
  }
  document.querySelectorAll("[data-scene]").forEach((button) => {
    button.addEventListener("click", () => fillScene(button.dataset.scene));
  });
  document.getElementById("composer").addEventListener("submit", sendPrompt);
  newSessionBtn.addEventListener("click", async () => {
    if (busy) {
      await stopGeneration();
    }
    newSessionBtn.disabled = true;
    try {
      await createSession();
    } catch (error) {
      addBubble("meta", `新建会话失败：${error.message}`);
      setStatus(error.message, "err");
    } finally {
      newSessionBtn.disabled = false;
    }
  });
  function fitPrompt() {
    promptEl.style.height = "auto";
    const next = Math.min(160, Math.max(52, promptEl.scrollHeight));
    promptEl.style.height = `${next}px`;
  }

  promptEl.addEventListener("input", () => {
    fitPrompt();
    syncComposerPickers();
  });
  promptEl.addEventListener("keydown", (event) => {
    if (activePicker()) {
      if (event.key === "Escape") {
        event.preventDefault();
        hidePickers();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        movePicker(1);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        movePicker(-1);
        return;
      }
      if (event.key === "Tab" || shouldSendOnEnter(event)) {
        event.preventDefault();
        acceptPicker();
        return;
      }
    }
    if (shouldSendOnEnter(event)) {
      event.preventDefault();
      lastEnterSent = true;
      document.getElementById("composer").requestSubmit();
    }
  });
  if (attachBtn) {
    attachBtn.addEventListener("click", () => {
      attachFiles();
    });
  }
  if (fileInputEl) {
    fileInputEl.addEventListener("change", () => {
      Array.from(fileInputEl.files || []).forEach((file) => {
        ingestClipFile(file).catch((error) => {
          if (composerHintEl) {
            composerHintEl.textContent = error && error.message ? error.message : "文件没收下";
          }
        });
      });
      fileInputEl.value = "";
    });
  }
  const pasteHost = document.getElementById("view-chat") || document;
  function ingestTransfer(transfer) {
    // File 对象优先；只有 uri-list 味道时按路径收。两样都没有才放行默认粘贴（纯文本）。
    const files = collectTransferFiles(transfer);
    const paths = files.length ? [] : transferFilePaths(transfer);
    if (!files.length && !paths.length) {
      return false;
    }
    paths.forEach((full) => {
      rememberClip({
        token: fileRefToken(displayPath(full)),
        path: full,
        name: String(full).split(/[/\\]/).pop(),
      });
    });
    if (files.length) {
      ingestTransferFiles(transfer).catch((error) => {
        if (composerHintEl) {
          composerHintEl.textContent = error && error.message ? error.message : "文件没收下";
        }
      });
    }
    return true;
  }
  pasteHost.addEventListener("paste", (event) => {
    if (ingestTransfer(event.clipboardData)) {
      event.preventDefault();
    }
  });
  ["dragenter", "dragover"].forEach((name) => {
    pasteHost.addEventListener(name, (event) => {
      if (!transferHasFiles(event.dataTransfer)) {
        return;
      }
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    });
  });
  pasteHost.addEventListener("drop", (event) => {
    if (ingestTransfer(event.dataTransfer)) {
      event.preventDefault();
    }
  });
  if (thinkLevelEl) {
    thinkLevelEl.addEventListener("change", persistState);
  }
  if (themePickEl) {
    themePickEl.addEventListener("change", () => {
      applyTheme(themePickEl.value);
      persistState();
    });
  }
  document.querySelectorAll(".theme-swatch").forEach((button) => {
    button.addEventListener("click", () => {
      applyTheme(button.dataset.theme);
      persistState();
    });
  });
  document.querySelectorAll(".net-swatch").forEach((button) => {
    button.addEventListener("click", () => {
      saveWebMode(button.dataset.mode).catch((error) => setWebStatus(error.message));
    });
  });
  transcriptPort().addEventListener("scroll", () => {
    pinToBottom = isNearBottom(transcriptPort());
  });
  if (sessionSearchToggleEl) {
    sessionSearchToggleEl.addEventListener("click", () => {
      setSessionSearchOpen(Boolean(sessionSearchPanelEl && sessionSearchPanelEl.hidden));
    });
  }
  if (sessionSearchPanelEl && sessionFilterEl) {
    sessionSearchPanelEl.addEventListener("submit", (event) => {
      event.preventDefault();
      renderSessionList();
      setSessionSearchOpen(false);
      sessionSearchToggleEl?.focus();
    });
    sessionFilterEl.addEventListener("input", renderSessionList);
    sessionFilterEl.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setSessionSearchOpen(false);
        sessionSearchToggleEl?.focus();
      }
    });
    sessionFilterEl.addEventListener("focusout", () => {
      // 键盘 Tab 离开搜索框时也收起，避免只靠鼠标点击外部才能结束搜索。
      window.setTimeout(() => {
        if (!sessionSearchPanelEl.hidden && !sessionSearchPanelEl.contains(document.activeElement)) {
          setSessionSearchOpen(false);
        }
      }, 0);
    });
  }
  if (sessionSearchClearEl && sessionFilterEl) {
    sessionSearchClearEl.addEventListener("click", () => {
      sessionFilterEl.value = "";
      renderSessionList();
      setSessionSearchOpen(false);
      sessionSearchToggleEl?.focus();
    });
  }
  document.addEventListener("pointerdown", (event) => {
    if (
      !sessionSearchPanelEl ||
      sessionSearchPanelEl.hidden ||
      sessionSearchPanelEl.contains(event.target) ||
      sessionSearchToggleEl?.contains(event.target)
    ) {
      return;
    }
    setSessionSearchOpen(false);
  });
  const railToggle = document.getElementById("rail-toggle");
  if (railToggle) {
    railToggle.addEventListener("click", () => setRailOpen(!railOpen));
  }
  const sideToggle = document.getElementById("side-toggle");
  if (sideToggle) {
    sideToggle.addEventListener("click", () => setSideOpen(!sideOpen));
  }
  const sideClose = document.getElementById("side-close");
  if (sideClose) {
    sideClose.addEventListener("click", () => setSideOpen(false));
  }
  const artifactRefresh = document.getElementById("artifact-refresh");
  if (artifactRefresh) {
    artifactRefresh.addEventListener("click", () => {
      listedWorkspaceDir = "";
      // 刷新要能反映「文件后来被删了 / 又长大了」，所以缓存的 stat 一并丢掉重取。
      artifactMeta.clear();
      refreshWorkspaceFiles().catch(() => {});
    });
  }
  document.querySelectorAll(".rail-btn[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  const openMailDesk = document.getElementById("open-mail-desk");
  if (openMailDesk) {
    openMailDesk.addEventListener("click", () => switchView("mail"));
  }
  if (approvalModeEl) {
    approvalModeEl.addEventListener("change", syncModeHint);
  }
  if (saveModelBtn) {
    saveModelBtn.addEventListener("click", async () => {
      try {
        await saveModelSettings();
      } catch (error) {
        if (modelStatusEl) {
          modelStatusEl.textContent = `保存失败：${error.message}`;
        }
      }
    });
  }
  if (newModelBtn) {
    newModelBtn.addEventListener("click", () => fillModelForm(null));
  }
  if (deleteModelBtn) {
    deleteModelBtn.addEventListener("click", async () => {
      try {
        await removeModelProfile();
      } catch (error) {
        if (modelStatusEl) {
          modelStatusEl.textContent = `删除失败：${error.message}`;
        }
      }
    });
  }
  if (promptFilterEl) {
    promptFilterEl.addEventListener("input", drawPromptNav);
  }
  if (savePromptBtn) {
    savePromptBtn.addEventListener("click", async () => {
      try {
        await saveCurrentPrompt();
      } catch (error) {
        if (promptStatusEl) {
          promptStatusEl.textContent = `保存失败：${error.message}`;
        }
      }
    });
  }
  if (revertPromptBtn) {
    revertPromptBtn.addEventListener("click", revertCurrentPrompt);
  }
  if (modelPickEl) {
    modelPickEl.addEventListener("change", async () => {
      try {
        await pickModel(modelPickEl.value);
      } catch (error) {
        if (modelStatusEl) {
          modelStatusEl.textContent = `切换失败：${error.message}`;
        }
      }
    });
  }

  const memoryQueryEl = document.getElementById("memory-query");
  if (memoryQueryEl) {
    memoryQueryEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadMemory().catch(() => {});
      }
    });
    memoryQueryEl.addEventListener("change", () => {
      loadMemory().catch(() => {});
    });
  }

  const linkQueryEl = document.getElementById("link-query");
  if (linkQueryEl) {
    linkQueryEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadLinks().catch(() => {});
      }
    });
    linkQueryEl.addEventListener("change", () => {
      loadLinks().catch(() => {});
    });
  }
  const linkForm = document.getElementById("link-form");
  if (linkForm) {
    linkForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = document.getElementById("link-status");
      try {
        await api.addLink({
          url: (document.getElementById("link-url") || {}).value || "",
          title: (document.getElementById("link-title") || {}).value || "",
          intent: (document.getElementById("link-intent") || {}).value || "",
          alias: (document.getElementById("link-alias") || {}).value || "",
          source: "desktop",
        });
        if (status) {
          status.textContent = "已写入链接库";
        }
        ["link-url", "link-title", "link-intent", "link-alias"].forEach((id) => {
          const field = document.getElementById(id);
          if (field) {
            field.value = "";
          }
        });
        await loadLinks();
      } catch (error) {
        if (status) {
          status.textContent = error.message;
        }
      }
    });
  }
  const wikiForm = document.getElementById("wiki-form");
  if (wikiForm) {
    wikiForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const field = document.getElementById("wiki-source");
      try {
        await addWikiSource((field && field.value) || "");
        if (field) {
          field.value = "";
        }
      } catch (error) {
        setWikiStatus(error.message);
      }
    });
  }
  const wikiPick = document.getElementById("wiki-pick");
  if (wikiPick) {
    wikiPick.addEventListener("click", async () => {
      try {
        const picker = typeof api.pickFiles === "function" ? api.pickFiles : null;
        const paths = picker ? await picker() : [];
        if (!paths || !paths.length) {
          setWikiStatus("没有选到文件");
          return;
        }
        for (const item of paths) {
          await addWikiSource(item);
        }
      } catch (error) {
        setWikiStatus(error.message);
      }
    });
  }
  const wikiCompile = document.getElementById("wiki-compile");
  if (wikiCompile) {
    wikiCompile.addEventListener("click", () => {
      compileWiki().catch((error) => setWikiStatus(error.message));
    });
  }
  const diaryForm = document.getElementById("diary-form");
  if (diaryForm) {
    diaryForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = document.getElementById("diary-status");
      try {
        await api.writeDiary(
          (document.getElementById("diary-text") || {}).value || "",
          (document.getElementById("diary-day") || {}).value || "",
        );
        if (status) {
          status.textContent = "已写入日记";
        }
        const textEl = document.getElementById("diary-text");
        if (textEl) {
          textEl.value = "";
        }
        await loadDiary((document.getElementById("diary-day") || {}).value || "");
      } catch (error) {
        if (status) {
          status.textContent = error.message;
        }
      }
    });
  }
  const scheduleSave = document.getElementById("schedule-save");
  if (scheduleSave) {
    scheduleSave.addEventListener("click", () => {
      saveScheduleFromForm().catch((error) => setScheduleStatus(error.message));
    });
  }
  const scheduleRefresh = document.getElementById("schedule-refresh");
  if (scheduleRefresh) {
    scheduleRefresh.addEventListener("click", () => {
      loadSchedules().catch((error) => setScheduleStatus(error.message));
    });
  }
  const scheduleTick = document.getElementById("schedule-tick");
  if (scheduleTick) {
    scheduleTick.addEventListener("click", async () => {
      if (typeof api.tickSchedules !== "function") {
        setScheduleStatus("当前壳没有 tick 接口");
        return;
      }
      try {
        const payload = await api.tickSchedules();
        const count = (payload.fires || payload.jobs || []).length;
        setScheduleStatus(payload.ran ? `已入队 ${count} 条` : `到期 ${count} 条（未跑模型）`);
        await loadSchedules();
      } catch (error) {
        setScheduleStatus(error.message);
      }
    });
  }
  const mailOpenChat = document.getElementById("mail-open-chat");
  if (mailOpenChat) {
    mailOpenChat.addEventListener("click", () => {
      promptEl.value = "看一下收件箱";
      switchView("chat");
      promptEl.focus();
    });
  }
  const saveEmailBtn = document.getElementById("save-email");
  if (saveEmailBtn) {
    saveEmailBtn.addEventListener("click", () => {
      saveEmailFrom("email").catch((error) => setEmailStatus(error.message));
    });
  }
  const mailForm = document.getElementById("mail-form");
  if (mailForm) {
    mailForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveEmailFrom("mail").catch((error) => setEmailStatus(error.message));
    });
  }

  function wittyHitTest() {
    const spots = [
      ["rail", 36, 90],
      ["newbtn", 90, 150],
      ["mid", Math.round(window.innerWidth / 2), Math.round(window.innerHeight / 2)],
      ["composer", Math.round(window.innerWidth / 2), window.innerHeight - 90],
    ];
    const parts = spots.map(([name, x, y]) => {
      const stack = (document.elementsFromPoint(x, y) || []).slice(0, 4).map((el) => {
        const cls = String(el.className || "").replace(/\s+/g, ".").slice(0, 36);
        return `${el.tagName.toLowerCase()}#${el.id || "-"}.${cls}`;
      });
      return `${name}@${x},${y}=[${stack.join(" > ")}]`;
    });
    const neu = document.getElementById("new-session");
    const box = neu ? neu.getBoundingClientRect() : null;
    const extra = [
      `size=${window.innerWidth}x${window.innerHeight}`,
      `resizing=${document.body.classList.contains("is-resizing")}`,
      `js=${document.body.dataset.js || ""}`,
      `app=${document.querySelector(".app") ? document.querySelector(".app").className : "none"}`,
      box
        ? `newbox=${Math.round(box.x)},${Math.round(box.y)} ${Math.round(box.width)}x${Math.round(box.height)}`
        : "newbox=missing",
    ];
    console.log(`HITTEST ${parts.join(" | ")} || ${extra.join(" ")}`);
  }
  loadPersisted();
  applyTheme(currentTheme());
  document.body.classList.remove("is-resizing");
  window.setTimeout(wittyHitTest, 400);
  window.setTimeout(wittyHitTest, 1800);
  window.addEventListener("blur", () => endResize());
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      endResize();
    }
  });
  syncWorkspaceChip();
  bindAllSplits();
  syncModeHint();
  syncRunChrome();
  syncRailChrome();
  const boot = (async () => {
    showHero();
    await new Promise((resolve) => {
      window.requestAnimationFrame(() => resolve());
    });
    let ok = await refreshHealth();
    if (!ok) {
      // 原来靠标题栏的「启动 API」按钮手动兜底；按钮删了，改成开窗时自己拉起再等健康。
      try {
        await api.startServer();
        for (let i = 0; i < 40 && !ok; i += 1) {
          await new Promise((resolve) => setTimeout(resolve, 250));
          ok = await refreshHealth();
        }
      } catch {
        // 落到下面的提示
      }
    }
    if (!ok) {
      addBubble("meta", "API 未连接。请在仓库根目录运行 uv run witty-agent serve 后重开窗口。");
      return;
    }
    await refreshModel();
    await refreshSessions();
    await refreshCommands();
    refreshWorkspaceFiles().catch(() => {});
  })();
  setBusy(false);

  window.__wittyTest = {
    autoApprove: "",
    shouldSendOnEnter,
    mergeStreamPiece,
    applyEvent,
    setEvidence,
    // 截图夹具用：等 boot 走完再拍，否则拍到的是空列表。
    waitBoot: () => boot,
    // 同样是截图夹具用：产物栏平时是空的，塞几条才看得出排版。
    seedArtifacts: (paths) => {
      sessionArtifacts = (paths || []).slice();
      renderArtifacts();
    },
    // 截图夹具用：待办浮层只在模型建了清单后出现，塞一份才拍得到。
    seedTodos: (rows) => {
      renderTodos(rows || []);
    },
    async checkDesktopUi() {
      await boot;
      await refreshCommands();
      window.__wittyTest.streamStallMs = -1;
      const report = {
        ok: false,
        approvalEmpty: false,
        approvalFold: false,
        approvalFoldHeld: false,
        approvalEsc: false,
        askShown: false,
        askMulti: false,
        askFoldHeld: false,
        askPicksHeld: false,
        askNoId: false,
        askPreparing: false,
        streamStall: false,
        writeHeld: false,
        approvalNoId: false,
        scrollHolds: false,
        scrollFollows: false,
        imeBlocked: false,
        imeSends: false,
        pickerOpen: false,
        pickerEsc: false,
        pickerSelect: false,
        pathInserted: false,
        thinkPersisted: false,
        thinkShown: false,
        thinkFollow: false,
        toolsFolded: false,
        toolsStackHeld: false,
        toolsStackReopened: false,
        toolsFailed: false,
        toolsMiss: false,
        workProcessOutside: false,
        bubbleCopy: false,
        bubbleFork: false,
        bubbleRate: false,
        exportShown: false,
        bubbleRetry: false,
        artifactsSessionOnly: false,
        artifactNameFirst: false,
        artifactNoNoise: false,
        turnFilesShown: false,
        queueShown: false,
        sessionTitles: false,
        sessionCite: false,
        sessionForget: false,
        fileCite: false,
        fileDirCite: false,
        localImage: false,
        localImageFallback: false,
        skillFold: false,
        skillModalOpen: false,
        skillModalClose: false,
        skillModalLeave: false,
        toolGroup: false,
        memoryFold: false,
        memoryProfileFold: false,
        memoryRecallFold: false,
        memoryScore: false,
        memoryWeakRead: false,
        memoryRelocated: false,
        memoryRefresh: false,
        memoryLoaded: false,
        memoryWs: false,
        memoryScope: false,
        memoryTime: false,
        memoryEmpty: false,
        memoryArchive: false,
        linkShown: false,
        linkList: false,
        diaryShown: false,
        mailShown: false,
        mailStatus: false,
        diaryBody: false,
        evidenceClick: false,
        evidenceFold: false,
        evidenceFoldHeld: false,
        evidenceSkill: false,
        evidenceScope: false,
        evidenceBrowse: false,
        citeShown: false,
        citeMemory: false,
        weakCiteRead: false,
        evidenceRelocated: false,
        sealFold: false,
        retryShown: false,
        steerShown: false,
        steerAfterDone: false,
        planShown: false,
        todoShown: false,
        todoClick: false,
        todoEvent: false,
        todoScroll: false,
        todoFoldHeld: false,
        todoFoldAdvance: false,
        streamMerge: false,
        sseParse: false,
        sseWatch: false,
        sseFallback: false,
        themeApplied: false,
        glassTheme: false,
        settingsNavIcons: false,
        promptShown: false,
        promptGrouped: false,
        settingsWide: false,
        splitHandle: false,
        commands: slashCommands.map((item) => item.name),
      };
      report.streamMerge =
        mergeStreamPiece("hel", "hello") === "hello" &&
        mergeStreamPiece("hello", "!") === "hello!" &&
        mergeStreamPiece("你好", "你好") === "你好";
      const sseParsed = parseSseBuffer(
        'data: {"type":"text_delta","text":"hi"}\n\ndata: {"type":"todos","args":{"todos":[{"content":"sse-step","status":"in_progress"}]}}\n\npartial',
      );
      report.sseParse = Boolean(
        sseParsed.events.length === 2 &&
          sseParsed.events[0].text === "hi" &&
          sseParsed.events[1].type === "todos" &&
          sseParsed.rest === "partial",
      );
      const prevBusy = busy;
      const prevSkip = window.__wittyTest.skipStream;
      const prevOpen = window.__wittyTest.openRunStream;
      const prevGet = window.__wittyTest.getRun;
      busy = true;
      window.__wittyTest.skipStream = false;
      window.__wittyTest.openRunStream = async function* openSse() {
        yield { type: "text_delta", text: "from-sse", seq: 90 };
        yield {
          type: "todos",
          args: { todos: [{ content: "sse-step", status: "in_progress" }] },
          seq: 91,
        };
        yield { type: "done", text: "from-sse", seq: 92 };
      };
      window.__wittyTest.getRun = async () => {
        throw new Error("no snapshot");
      };
      const sseKinds = [];
      const sseLive = {
        node: null,
        text: "",
        reasoning: "",
        evidence: [],
        traceReason: "",
        sealed: "",
        seen: new Set(),
      };
      const streamed = await watchRun(
        "sse-ui",
        (item) => {
          sseKinds.push(item.type);
          applyEvent(item, sseLive);
        },
        0,
      );
      report.sseWatch = Boolean(
        sseKinds.includes("text_delta") &&
          sseKinds.includes("todos") &&
          sseKinds.includes("done") &&
          streamed &&
          streamed.status === "done" &&
          streamed.text === "from-sse" &&
          sseLive.text === "from-sse" &&
          todoDock &&
          /当前：sse-step/.test(todoDock.textContent || ""),
      );
      if (sseLive.node) {
        sseLive.node.remove();
      }
      renderTodos([]);
      window.__wittyTest.openRunStream = async function* failSse() {
        throw new Error("no stream");
      };
      window.__wittyTest.getRun = async () => ({
        status: "done",
        text: "from-poll",
        timeline: [],
        todos: [],
      });
      const polled = await watchRun("sse-fallback", () => {}, 0);
      report.sseFallback = Boolean(polled && polled.status === "done" && polled.text === "from-poll");
      window.__wittyTest.skipStream = prevSkip;
      if (prevOpen) {
        window.__wittyTest.openRunStream = prevOpen;
      } else {
        delete window.__wittyTest.openRunStream;
      }
      if (prevGet) {
        window.__wittyTest.getRun = prevGet;
      } else {
        delete window.__wittyTest.getRun;
      }
      busy = prevBusy;
      const dockWas = approvalDock.innerHTML;
      lastApproval = { decision: "", tool: "", callId: "" };
      const pending = promptApproval({ tool_name: "write", tool_call_id: "ui-check", args: { path: "x" } });
      const titleEl = approvalDock.querySelector(".approval-title");
      const argsFold = approvalDock.querySelector("details.approval-args");
      report.approvalFold = Boolean(
        /需要批准：write · x/.test((titleEl && titleEl.textContent) || "") &&
          argsFold &&
          argsFold.querySelector("pre") &&
          /"path"/.test((argsFold.querySelector("pre") || {}).textContent || ""),
      );
      const allowBtn = approvalDock.querySelector("button.primary");
      if (allowBtn) {
        allowBtn.click();
      }
      await pending;
      report.approvalEmpty = approvalDock.childElementCount === 0 && lastApproval.decision === "allow";
      const denied = promptApproval({ tool_name: "bash", tool_call_id: "ui-esc", args: { command: "ls" } });
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
      await denied;
      report.approvalEsc = lastApproval.decision === "deny" && lastApproval.callId === "ui-esc" && approvalDock.childElementCount === 0;
      promptApproval({
        tool_name: "write",
        tool_call_id: "fold-hold",
        args: { path: "held.txt", content: "x" },
      });
      const firstArgs = approvalDock.querySelector("details.approval-args");
      if (firstArgs) {
        firstArgs.open = false;
        firstArgs.dataset.userToggled = "1";
        approvalArgsFold = { callId: "fold-hold", open: false, userToggled: true };
      }
      const foldPending2 = promptApproval({
        tool_name: "write",
        tool_call_id: "fold-hold",
        args: { path: "held.txt", content: "x" },
      });
      const heldArgs = approvalDock.querySelector("details.approval-args");
      report.approvalFoldHeld = Boolean(
        heldArgs && heldArgs.open === false && heldArgs.dataset.userToggled === "1",
      );
      const cancelHeld = approvalDock.querySelector("button.deny");
      if (cancelHeld) {
        cancelHeld.click();
      }
      await foldPending2;
      lastAsk = { id: "", selected: "" };
      const asked = promptQuestion({
        questions: [
          {
            id: "q1",
            question: "OAuth2 还是 JWT？",
            options: [{ label: "OAuth2" }, { label: "JWT" }],
          },
        ],
      });
      const oauthBtn = Array.from(approvalDock.querySelectorAll("button")).find((item) =>
        /OAuth2/.test(item.textContent || ""),
      );
      if (oauthBtn) {
        oauthBtn.click();
      }
      const answers = await asked;
      report.askShown = Boolean(
        approvalDock.childElementCount === 0 &&
          lastAsk.id === "q1" &&
          lastAsk.selected === "OAuth2" &&
          answers &&
          answers[0] &&
          answers[0].selected &&
          answers[0].selected[0] === "OAuth2",
      );
      lastAsk = { id: "", selected: "" };
      const multiAsked = promptQuestion({
        questions: [
          {
            id: "q1",
            question: "A 还是 B？",
            options: [{ label: "A" }, { label: "B" }],
          },
          { id: "q2", question: "备注？" },
        ],
      });
      const aBtn = Array.from(approvalDock.querySelectorAll("button")).find((item) => item.dataset.label === "A");
      if (aBtn) {
        aBtn.click();
      }
      const customEl = approvalDock.querySelector(".question-item[data-qid=\"q2\"] .question-custom");
      if (customEl) {
        customEl.value = "自定义备注";
        customEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const submitBtn = approvalDock.querySelector(".question-submit");
      if (submitBtn) {
        submitBtn.click();
      }
      const multiAnswers = await multiAsked;
      report.askMulti = Boolean(
        approvalDock.childElementCount === 0 &&
          lastAsk.count === 2 &&
          multiAnswers &&
          multiAnswers[0] &&
          multiAnswers[0].selected &&
          multiAnswers[0].selected[0] === "A" &&
          multiAnswers[1] &&
          multiAnswers[1].custom === "自定义备注" &&
          approvalDock.querySelectorAll(".question-item").length === 0,
      );
      lastAsk = { id: "", selected: "" };
      const holdPayload = {
        questions: [
          {
            id: "q1",
            question: "A 还是 B？",
            options: [{ label: "A" }, { label: "B" }],
          },
          { id: "q2", question: "备注？" },
        ],
      };
      promptQuestion(holdPayload);
      const holdA = Array.from(approvalDock.querySelectorAll("button")).find((item) => item.dataset.label === "A");
      if (holdA) {
        holdA.click();
      }
      const holdCustom = approvalDock.querySelector(".question-item[data-qid=\"q2\"] .question-custom");
      if (holdCustom) {
        holdCustom.value = "保持备注";
        holdCustom.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const firstFold = approvalDock.querySelector("details.question-fold");
      if (firstFold) {
        firstFold.open = false;
        firstFold.dataset.userToggled = "1";
      }
      const askFoldPending2 = promptQuestion(holdPayload);
      const heldFold = approvalDock.querySelector("details.question-fold");
      const heldPick = approvalDock.querySelector("button.picked[data-label=\"A\"]");
      const heldCustom = approvalDock.querySelector(".question-item[data-qid=\"q2\"] .question-custom");
      report.askFoldHeld = Boolean(
        heldFold && heldFold.open === false && heldFold.dataset.userToggled === "1",
      );
      report.askPicksHeld = Boolean(
        heldPick && heldCustom && heldCustom.value === "保持备注",
      );
      const holdSubmit = approvalDock.querySelector(".question-submit");
      if (holdSubmit) {
        holdSubmit.click();
      }
      await askFoldPending2;
      const prevBusyAsk = busy;
      const prevSkipAsk = window.__wittyTest.skipStream;
      const prevGetAsk = window.__wittyTest.getRun;
      const prevSubmitAsk = window.__wittyTest.submitAnswer;
      const prevAutoAsk = window.__wittyTest.autoAnswer;
      let noIdAnswers = null;
      let noIdPolls = 0;
      busy = true;
      window.__wittyTest.skipStream = true;
      window.__wittyTest.autoAnswer = "是";
      window.__wittyTest.submitAnswer = async (_sid, answers) => {
        noIdAnswers = answers;
      };
      window.__wittyTest.getRun = async () => {
        noIdPolls += 1;
        if (noIdPolls === 1) {
          return {
            status: "awaiting_question",
            question: {
              questions: [
                {
                  question: "无编号还继续吗？",
                  options: [{ label: "是" }, { label: "否" }],
                },
              ],
            },
            timeline: [],
          };
        }
        return { status: "done", text: "noid-ok", timeline: [] };
      };
      const noIdRun = await watchRun("ask-noid", () => {}, 0);
      report.askNoId = Boolean(
        pendingQuestionKey({ questions: [{ question: "无编号还继续吗？" }] }) === "q:无编号还继续吗？" &&
          noIdRun &&
          noIdRun.status === "done" &&
          noIdAnswers &&
          noIdAnswers[0] &&
          noIdAnswers[0].selected &&
          noIdAnswers[0].selected[0] === "是",
      );
      const prepLive = { node: null, text: "", reasoning: "", evidence: [], traceReason: "", sealed: "", seen: new Set() };
      applyEvent({ type: "text_delta", text: "先确认两点", seq: 9201 }, prepLive);
      applyEvent({ type: "tool_preparing", tool_name: "ask_user_question", seq: 9202 }, prepLive);
      const prepText = (logEl.querySelector(".bubble.waiting") || {}).textContent || "";
      applyEvent(
        {
          type: "message_end",
          role: "assistant",
          text: "先确认两点",
          tool_calls: [{ id: "q1", name: "ask_user_question", arguments: { questions: [] } }],
          seq: 9203,
        },
        prepLive,
      );
      const gatedText = (logEl.querySelector(".bubble.waiting") || {}).textContent || "";
      report.askPreparing = Boolean(/正在准备选择题/.test(prepText) && /等待你选择/.test(gatedText));
      clearWaiting();
      const stallLive = { node: null, text: "", reasoning: "", evidence: [], traceReason: "", sealed: "", seen: new Set() };
      window.__wittyTest.streamStallMs = 1;
      runPhase = "idle";
      setBusy(true);
      applyEvent({ type: "text_delta", text: "第2章完成。现在写第3章。", seq: 9301 }, stallLive);
      const waitingBeforeStall = Boolean(logEl.querySelector(".bubble.waiting"));
      await new Promise((resolve) => setTimeout(resolve, 30));
      const stallText = (logEl.querySelector(".bubble.waiting") || {}).textContent || "";
      report.streamStall = Boolean(!waitingBeforeStall && /仍在生成/.test(stallText));
      applyEvent({ type: "tool_preparing", tool_name: "write", seq: 9302 }, stallLive);
      applyEvent(
        {
          type: "message_end",
          role: "assistant",
          text: "第2章完成。现在写第3章。",
          tool_calls: [{ id: "w1", name: "write", arguments: { path: "ch3.md" } }],
          seq: 9303,
        },
        stallLive,
      );
      const writeWait = (logEl.querySelector(".bubble.waiting") || {}).textContent || "";
      report.writeHeld = /正在调用 write/.test(writeWait);
      window.__wittyTest.streamStallMs = -1;
      clearWaiting();
      setBusy(false);
      busy = prevBusyAsk;
      window.__wittyTest.skipStream = prevSkipAsk;
      window.__wittyTest.autoAnswer = prevAutoAsk;
      if (prevGetAsk) {
        window.__wittyTest.getRun = prevGetAsk;
      } else {
        delete window.__wittyTest.getRun;
      }
      if (prevSubmitAsk) {
        window.__wittyTest.submitAnswer = prevSubmitAsk;
      } else {
        delete window.__wittyTest.submitAnswer;
      }
      const prevBusyAppr = busy;
      const prevSkipAppr = window.__wittyTest.skipStream;
      const prevGetAppr = window.__wittyTest.getRun;
      const prevSubmitAppr = window.__wittyTest.submitApproval;
      const prevAutoAppr = window.__wittyTest.autoApprove;
      let noIdDecision = "";
      let noIdApprPolls = 0;
      busy = true;
      window.__wittyTest.skipStream = true;
      window.__wittyTest.autoApprove = "allow";
      window.__wittyTest.submitApproval = async (_sid, callId, decision) => {
        noIdDecision = `${callId}:${decision}`;
      };
      window.__wittyTest.getRun = async () => {
        noIdApprPolls += 1;
        if (noIdApprPolls === 1) {
          return {
            status: "awaiting_approval",
            pending: { tool_name: "write", args: { path: "held.txt", content: "x" } },
            timeline: [],
          };
        }
        return { status: "done", text: "appr-noid-ok", timeline: [] };
      };
      const noIdApprRun = await watchRun("appr-noid", () => {}, 0);
      report.approvalNoId = Boolean(
        pendingApprovalKey({ tool_name: "write", args: { path: "held.txt" } }) === "a:write:held.txt" &&
          noIdApprRun &&
          noIdApprRun.status === "done" &&
          noIdDecision === ":allow",
      );
      busy = prevBusyAppr;
      window.__wittyTest.skipStream = prevSkipAppr;
      window.__wittyTest.autoApprove = prevAutoAppr;
      if (prevGetAppr) {
        window.__wittyTest.getRun = prevGetAppr;
      } else {
        delete window.__wittyTest.getRun;
      }
      if (prevSubmitAppr) {
        window.__wittyTest.submitApproval = prevSubmitAppr;
      } else {
        delete window.__wittyTest.submitApproval;
      }
      approvalDock.innerHTML = dockWas;

      const port = transcriptPort();
      const saved = logEl.innerHTML;
      logEl.replaceChildren();
      for (let i = 0; i < 36; i += 1) {
        const node = document.createElement("div");
        node.className = "bubble user";
        node.style.minHeight = "48px";
        node.textContent = `scroll-fixture-${i}\n\n`;
        logEl.appendChild(node);
      }
      port.scrollTop = 0;
      pinToBottom = false;
      const held = port.scrollTop;
      scrollThread();
      report.scrollHolds = port.scrollTop === held;
      port.scrollTop = port.scrollHeight;
      pinToBottom = true;
      scrollThread();
      report.scrollFollows = isNearBottom(port);
      logEl.innerHTML = saved;

      lastEnterSent = false;
      promptEl.value = "";
      promptEl.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, isComposing: true, keyCode: 229 }),
      );
      report.imeBlocked = lastEnterSent === false;
      promptEl.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, isComposing: false, keyCode: 13 }),
      );
      report.imeSends = lastEnterSent === true;

      hidePickers();
      promptEl.value = "/";
      promptEl.setSelectionRange(1, 1);
      promptEl.dispatchEvent(new Event("input", { bubbles: true }));
      report.pickerOpen = Boolean(
        slashPickerEl && !slashPickerEl.hidden && /plan|abort|loop|compact/.test(slashPickerEl.textContent || ""),
      );
      promptEl.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
      report.pickerEsc = Boolean(slashPickerEl && slashPickerEl.hidden);
      promptEl.value = "/";
      promptEl.setSelectionRange(1, 1);
      promptEl.dispatchEvent(new Event("input", { bubbles: true }));
      promptEl.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, keyCode: 13 }));
      report.pickerSelect = /^\//.test(promptEl.value) && promptEl.value.length > 1;

      promptEl.value = "";
      insertComposerText("src/witty_agent/session.py");
      report.pathInserted = promptEl.value.includes("src/witty_agent/session.py");
      const savedMentionFiles = workspaceFiles.slice();
      const savedMentionWs = workspaceEl ? workspaceEl.value : "";
      workspaceFiles = ["/tmp/ws/note.txt"];
      if (workspaceEl) {
        workspaceEl.value = "/tmp/ws";
      }
      promptEl.value = "@";
      promptEl.setSelectionRange(1, 1);
      promptEl.dispatchEvent(new Event("input", { bubbles: true }));
      const filePick = mentionPickerEl && mentionPickerEl.querySelector(".picker-item");
      if (filePick) {
        filePick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      }
      report.fileCite = Boolean(
        filePick && /(?:^|\s)file:note.txt(?:\s|$)/.test(promptEl.value || ""),
      );
      workspaceFiles = ["/tmp/ws/docs/", "/tmp/ws/note.txt"];
      promptEl.value = "@";
      promptEl.setSelectionRange(1, 1);
      promptEl.dispatchEvent(new Event("input", { bubbles: true }));
      const dirPick = mentionPickerEl && mentionPickerEl.querySelector(".picker-item");
      if (dirPick) {
        dirPick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      }
      report.fileDirCite = Boolean(
        dirPick && /(?:^|\s)file:docs\/(?:\s|$)/.test(promptEl.value || ""),
      );
      workspaceFiles = savedMentionFiles;
      if (workspaceEl) {
        workspaceEl.value = savedMentionWs;
      }
      promptEl.value = "";
      fitPrompt();

      // 本地图片水合的端到端：saveInbox 真落一张 1x1 png，气泡里的
      // ![](相对路径) 应换成 data: URL；不存在的图应退化成文件链接。
      try {
        const imageWs = "/tmp/witty-ui-check-img";
        const savedImgWs = workspaceEl ? workspaceEl.value : "";
        if (workspaceEl) {
          workspaceEl.value = imageWs;
        }
        const probePng =
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
        const savedProbe = await api.saveInbox({
          workspace_dir: imageWs,
          filename: "probe.png",
          mime: "image/png",
          content_base64: probePng,
        });
        const savedImgLog = logEl.innerHTML;
        const imgBubble = addBubble(
          "assistant",
          `![probe](${savedProbe.token})\n\n![missing](nope-missing.png)`,
        );
        for (let i = 0; i < 60; i += 1) {
          const hydrated = imgBubble.querySelector('img[src^="data:image/png"]');
          const fallback = Array.from(imgBubble.querySelectorAll("button.file-link")).find(
            (button) => (button.textContent || "").includes("nope-missing.png"),
          );
          report.localImage = Boolean(hydrated);
          report.localImageFallback = Boolean(fallback);
          if (report.localImage && report.localImageFallback) {
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
        logEl.innerHTML = savedImgLog;
        if (workspaceEl) {
          workspaceEl.value = savedImgWs;
        }
      } catch {
        // 报告位保持 false，最终 ok 会挡住
      }

      if (thinkLevelEl) {
        const previousThink = thinkLevelEl.value;
        thinkLevelEl.value = "long";
        thinkLevelEl.dispatchEvent(new Event("change"));
        try {
          report.thinkPersisted = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}").think_level === "long";
        } catch {
          report.thinkPersisted = false;
        }
        const savedLog = logEl.innerHTML;
        const probe = { node: null, text: "", reasoning: "", seen: new Set() };
        applyEvent({ type: "reasoning_delta", text: "because-two", seq: 9001 }, probe);
        report.thinkShown = Boolean(
          probe.node && probe.node.querySelector(".think") && /because-two/.test(probe.node.textContent || ""),
        );
        const thinkBody = probe.node && probe.node.querySelector(".think-body");
        if (thinkBody) {
          thinkBody.style.maxHeight = "48px";
          applyEvent({ type: "reasoning_delta", text: `\n${"line\n".repeat(40)}`, seq: 9002 }, probe);
          report.thinkFollow = thinkBody.scrollTop + thinkBody.clientHeight >= thinkBody.scrollHeight - 4;
        }
        if (probe.node) {
          probe.node.remove();
        }
        logEl.innerHTML = savedLog;
        thinkLevelEl.value = previousThink;
        persistState();
      }
      const savedSessions = sessions.slice();
      const savedSid = sessionId;
      const filterEl = document.getElementById("session-filter");
      const savedFilter = filterEl ? filterEl.value : "";
      if (filterEl) {
        filterEl.value = "";
      }
      sessions = [
        {
          session_id: "s-today",
          title: "today-chat",
          workspace_dir: "/tmp/today",
          updated_at: Date.now() / 1000,
        },
        {
          session_id: "s-old",
          title: "old-chat",
          workspace_dir: "/tmp/old",
          updated_at: Date.now() / 1000 - 12 * 86400,
        },
      ];
      sessionId = "s-today";
      renderSessionList();
      const sessionItems = Array.from(sessionListEl.querySelectorAll(".session-item"));
      const todayRow = sessionItems.find((node) => node.dataset.id === "s-today");
      const oldRow = sessionItems.find((node) => node.dataset.id === "s-old");
      const todayTopic = todayRow && todayRow.querySelector(".session-topic");
      const oldTopic = oldRow && oldRow.querySelector(".session-topic");
      const todayWhen = todayRow && todayRow.querySelector("time");
      const oldWhen = oldRow && oldRow.querySelector("time");
      report.sessionTitles = Boolean(
        !sessionListEl.querySelector("details.session-group") &&
          sessionItems.length === 2 &&
          todayTopic &&
          todayTopic.textContent === "today-chat" &&
          oldTopic &&
          oldTopic.textContent === "old-chat" &&
          todayWhen &&
          /^今天 /.test(todayWhen.textContent || "") &&
          oldWhen &&
          /\d+月\d+日/.test(oldWhen.textContent || "") &&
          (todayRow.textContent || "").indexOf("today-chat") < (todayRow.textContent || "").indexOf(todayWhen.textContent || "___"),
      );
      const savedPrompt = promptEl.value;
      promptEl.value = "";
      const citeBtn = oldRow && oldRow.querySelector(".session-cite");
      if (citeBtn) {
        citeBtn.click();
      }
      report.sessionCite = Boolean(
        citeBtn &&
          !todayRow.querySelector(".session-cite") &&
          /(?:^|\s)session:s-old(?:\s|$)/.test(promptEl.value || ""),
      );
      const forgetBtn = oldRow && oldRow.querySelector("button.forget");
      report.sessionForget = Boolean(
        oldRow &&
          oldRow.tagName !== "BUTTON" &&
          forgetBtn &&
          forgetBtn.tagName === "BUTTON",
      );
      promptEl.value = savedPrompt;
      fitPrompt();
      sessions = savedSessions;
      sessionId = savedSid;
      if (filterEl) {
        filterEl.value = savedFilter;
      }
      renderSessionList();
      const foldSaved = logEl.innerHTML;
      logEl.replaceChildren();
      mountTurn();
      addToolNode("t1", "read", { path: "note.txt" }, "1|hello", false);
      addToolNode("t1", "read", { path: "note.txt" }, "1|hello", true);
      addToolNode("t2", "grep", { pattern: "foo" }, "match", true);
      const stack = logEl.querySelector(".work-process");
      const labels = stack ? Array.from(stack.querySelectorAll(".node.tool summary")).map((item) => item.textContent) : [];
      report.toolsFolded = Boolean(
        stack &&
          stack.open === false &&
          stack.querySelectorAll(".node.tool").length === 2 &&
          /工作过程 · .*2/.test((stack.querySelector("summary") || {}).textContent || "") &&
          labels.some((text) => /已读取 note.txt/.test(text)) &&
          !stack.querySelector(".bubble.assistant"),
      );
      if (stack) {
        stack.open = true;
        stack.dataset.userToggled = "1";
        addToolNode("t2", "grep", { pattern: "foo" }, "match", true);
        report.toolsStackHeld = stack.open === true;
        stack.open = false;
        stack.dataset.userToggled = "1";
        stack.dataset.prevRunning = "0";
        addToolNode("t-new", "read", { path: "b.txt" }, "", false);
        report.toolsStackReopened = stack.open === true && stack.dataset.userToggled !== "1";
        addToolNode("t-new", "read", { path: "b.txt" }, "ok", true);
      }
      addToolNode("t-fail", "read", { path: "missing.txt" }, "not found", true, true);
      const failNode = logEl.querySelector("[data-call-id=\"t-fail\"]");
      const heldOpen = logEl.querySelector("[data-call-id=\"t1\"]");
      if (heldOpen) {
        heldOpen.open = true;
        heldOpen.dataset.userToggled = "1";
        addToolNode("t1", "read", { path: "note.txt" }, "1|hello", true, false);
      }
      report.toolsFailed = Boolean(
        failNode &&
          failNode.open === false &&
          failNode.classList.contains("error") &&
          /失败 · read · missing.txt/.test((failNode.querySelector("summary") || {}).textContent || "") &&
          heldOpen &&
          heldOpen.open === true,
      );
      addToolNode("t-miss", "grep", { pattern: "zzz" }, "(no matches)", true, false);
      const missNode = logEl.querySelector("[data-call-id=\"t-miss\"]");
      report.toolsMiss = Boolean(
        missNode &&
          missNode.open === false &&
          missNode.classList.contains("miss") &&
          /未命中 · grep · zzz/.test((missNode.querySelector("summary") || {}).textContent || ""),
      );
      const missEv = addBubble(
        "assistant",
        "not found",
        "",
        [{ source: "grep", locator: "zzz", excerpt: "(no matches)", ok: false }],
        "no source",
      );
      const wpAfter = logEl.querySelector(".work-process");
      report.workProcessOutside = Boolean(
        wpAfter &&
          missEv &&
          !wpAfter.contains(missEv) &&
          wpAfter.open === false &&
          wpAfter.contains(logEl.querySelector(".node.tool")),
      );
      const copyHost = addBubble("assistant", "copy-me-please");
      const copyBtn = actionRoot(copyHost).querySelector(".bubble-copy");
      const forkBtn = actionRoot(copyHost).querySelector(".bubble-fork");
      report.bubbleCopy = Boolean(
        copyBtn &&
          copyHost.dataset.raw === "copy-me-please" &&
          bubbleCopyText(copyHost) === "copy-me-please" &&
          !/copy-me-please/.test(copyBtn.textContent || ""),
      );
      report.bubbleFork = Boolean(
        forkBtn && (forkBtn.getAttribute("aria-label") || "").includes("分叉"),
      );
      const rateHost = addBubble("assistant", "rate-me-please");
      const upBtn = actionRoot(rateHost).querySelector(".bubble-up");
      const downBtn = actionRoot(rateHost).querySelector(".bubble-down");
      if (upBtn) {
        upBtn.click();
      }
      const rated = getMessageRate("rate-me-please");
      if (upBtn) {
        upBtn.click();
      }
      report.bubbleRate = Boolean(
        upBtn &&
          downBtn &&
          rated === "up" &&
          getMessageRate("rate-me-please") === "" &&
          !actionRoot(rateHost).querySelector(".bubble-up.on"),
      );
      if (rateHost) {
        rateHost.remove();
      }
      if (copyHost) {
        copyHost.remove();
      }
      const exportUser = addBubble("user", "export-me-please");
      const exportAsst = addBubble("assistant", "exported-ok");
      const exported = transcriptMarkdown();
      report.exportShown = Boolean(
        /^# /.test(exported) &&
          /## 用户/.test(exported) &&
          /export-me-please/.test(exported) &&
          /## 助手/.test(exported) &&
          /exported-ok/.test(exported),
      );
      if (exportUser) {
        exportUser.remove();
      }
      if (exportAsst) {
        exportAsst.remove();
      }
      const savedRetryQueue = promptQueue.slice();
      const savedRetryBusy = busy;
      const retryHost = addBubble("user", "resend-me-please");
      const retryBtn = actionRoot(retryHost).querySelector(".bubble-retry");
      promptQueue = [];
      setBusy(true);
      promptEl.value = "";
      if (retryBtn) {
        retryBtn.click();
      }
      report.bubbleRetry = Boolean(
        retryBtn &&
          retryBtn.getAttribute("aria-label") === "重发" &&
          promptQueue.some((item) => item.text === "resend-me-please"),
      );
      promptQueue = savedRetryQueue;
      renderQueue();
      setBusy(savedRetryBusy);
      if (retryHost) {
        retryHost.remove();
      }
      const savedArtifacts = sessionArtifacts.slice();
      const savedFiles = (workspaceFiles || []).slice();
      sessionArtifacts = ["/tmp/session-out.pptx"];
      workspaceFiles = ["/tmp/noise-workspace.pptx"];
      renderArtifacts();
      const artHost = document.getElementById("artifact-list");
      const artText = artHost ? artHost.textContent || "" : "";
      report.artifactsSessionOnly = Boolean(
        /session-out\.pptx/.test(artText) && !/noise-workspace\.pptx/.test(artText),
      );
      // 深路径产物：文件名得单独成行，不能被目录挤没（用户报的「文件名称都看不到」）。
      sessionArtifacts = ["/Users/me/.witty/data/sandbox/work/very/deep/place/季度汇报.pptx"];
      renderArtifacts();
      const deepRow = artHost && artHost.querySelector(".art-row");
      report.artifactNameFirst = Boolean(
        deepRow &&
          (deepRow.querySelector(".art-name") || {}).textContent === "季度汇报.pptx" &&
          (deepRow.querySelector(".art-kind") || {}).textContent === "PPTX" &&
          (deepRow.querySelector(".art-origin") || {}).textContent === "沙箱" &&
          (deepRow.querySelector(".art-dir-tail") || {}).textContent === "/place" &&
          deepRow.querySelectorAll(".art-actions .art-act").length >= 1,
      );
      // `ls -l` 的输出行以路径收尾，早先会被整行当成一个产物收进来。
      sessionArtifacts = [];
      turnArtifacts = [];
      noteArtifactsFromTool(null, "exit=0 -rw-r--r--@ 1 me staff 3018 8 27 14:11 /tmp/from-ls.png");
      report.artifactNoNoise = Boolean(
        sessionArtifacts.length === 1 && sessionArtifacts[0] === "/tmp/from-ls.png",
      );
      artifactMeta.clear();
      sessionArtifacts = savedArtifacts;
      workspaceFiles = savedFiles;
      renderArtifacts();
      const savedTurnFiles = turnArtifacts.slice();
      turnArtifacts = [];
      sessionArtifacts = [];
      const fileTurn = mountTurn();
      noteArtifactsFromTool({ path: "/tmp/session-out.pptx" }, "");
      noteArtifactsFromTool({ path: "/tmp/second-out.pdf" }, "");
      noteArtifactsFromTool({ path: "/tmp/third-out.docx" }, "");
      noteArtifactsFromTool({ path: "/tmp/fourth-out.xlsx" }, "");
      const fileAns = addBubble("assistant", "wrote four files");
      const fileBar = fileTurn.querySelector(".turn-files");
      const fileChips = fileBar ? Array.from(fileBar.querySelectorAll(".turn-file:not(.more)")) : [];
      const moreChip = fileBar && fileBar.querySelector(".turn-file.more");
      // addBubble 返回的是内层节点，回答外面还包了一层 .say.assistant，
      // 而产物条是插在那层外壳后面的（renderTurnFiles 里 answer.after(bar)）。
      const fileAnsHost = fileAns ? fileAns.closest(".say") || fileAns : null;
      report.turnFilesShown = Boolean(
        fileBar &&
          fileAns &&
          fileBar.previousElementSibling === fileAnsHost &&
          !fileAns.contains(fileBar) &&
          fileChips.length === 3 &&
          /session-out\.pptx/.test(fileChips[0].textContent || "") &&
          moreChip &&
          moreChip.textContent === "+1",
      );
      turnArtifacts = savedTurnFiles;
      sessionArtifacts = savedArtifacts;
      renderArtifacts();
      if (fileAns) {
        fileAns.remove();
      }
      if (fileBar) {
        fileBar.remove();
      }
      report.toolsMiss = Boolean(
        report.toolsMiss &&
          missEv &&
          !missEv.querySelector(".cite-chip") &&
          /未命中：grep · zzz/.test((missEv.querySelector(".evidence-item") || {}).textContent || ""),
      );
      if (missEv) {
        missEv.remove();
      }
      const evSavedPrompt = promptEl.value;
      const evNode = addBubble(
        "assistant",
        "from note.txt",
        "",
        [{ source: "read", locator: "note.txt", excerpt: "hello", ok: true }],
        "used read",
      );
      const evBtn = evNode.querySelector(".evidence-item");
      const citeChip = evNode.querySelector(".cite-chip");
      const citeInline = evNode.querySelector(".cite-inline");
      report.citeShown = Boolean(
        citeChip &&
          /read · note\.txt/.test(citeChip.textContent || "") &&
          citeInline &&
          citeInline.textContent === "note.txt",
      );
      const manyItems = Array.from({ length: 8 }, (_, index) => ({
        source: "read",
        locator: `f${index}.txt`,
        excerpt: "x".repeat(90),
        ok: true,
      }));
      const evFoldNode = addBubble("assistant", "many-sources", "", manyItems, "used 8 reads");
      const citeMore = evFoldNode.querySelector(".cite-more");
      const evidenceMore = evFoldNode.querySelector(".evidence-more");
      const excerptFold = evFoldNode.querySelector(".evidence-excerpt");
      report.evidenceFold = Boolean(
        evFoldNode.querySelectorAll(".cite-row > .cite-chip").length === 6 &&
          citeMore &&
          /还有 2 条/.test((citeMore.querySelector("summary") || {}).textContent || "") &&
          citeMore.open !== true &&
          evidenceMore &&
          /其余 4 条/.test((evidenceMore.querySelector("summary") || {}).textContent || "") &&
          evidenceMore.open !== true &&
          excerptFold &&
          excerptFold.open !== true &&
          evFoldNode.querySelectorAll(".evidence-body > .evidence-card, .evidence-body > .evidence-item").length === 4,
      );
      if (citeMore) {
        citeMore.open = true;
        citeMore.dataset.userToggled = "1";
      }
      if (evidenceMore) {
        evidenceMore.open = true;
        evidenceMore.dataset.userToggled = "1";
      }
      if (excerptFold) {
        excerptFold.open = true;
        excerptFold.dataset.userToggled = "1";
      }
      if (typeof window.__wittyTest.setEvidence === "function") {
        window.__wittyTest.setEvidence(evFoldNode, manyItems, "used 8 reads");
      }
      const heldCite = evFoldNode.querySelector(".cite-more");
      const heldMore = evFoldNode.querySelector(".evidence-more");
      const heldExcerpt = evFoldNode.querySelector(".evidence-excerpt");
      report.evidenceFoldHeld = Boolean(
        report.evidenceFold &&
          heldCite &&
          heldCite.open === true &&
          heldCite.dataset.userToggled === "1" &&
          heldMore &&
          heldMore.open === true &&
          heldMore.dataset.userToggled === "1" &&
          heldExcerpt &&
          heldExcerpt.open === true &&
          heldExcerpt.dataset.userToggled === "1",
      );
      if (evFoldNode) {
        evFoldNode.remove();
      }
      if (evBtn) {
        evBtn.click();
      }
      // 文件类依据现在先尝试用系统打开（openLocalPath），打不开才回填到输入框，
      // 而那步是 await api.openPath 之后才发生的。click 后同步读 value 必然是空。
      await new Promise((resolve) => window.setTimeout(resolve, 60));
      report.evidenceClick = Boolean(
        evNode.querySelector(".evidence") &&
          /源头：read · note.txt/.test((evBtn && evBtn.textContent) || "") &&
          /note\.txt/.test(promptEl.value || ""),
      );
      promptEl.value = evSavedPrompt;
      fitPrompt();
      if (evNode) {
        evNode.remove();
      }
      const evSkillNode = addBubble(
        "assistant",
        "from-skill",
        "",
        [{ kind: "skill", source: "skill", locator: "slides", excerpt: "做汇报", ok: true }],
        "loaded slides",
      );
      const evSkillBtn = evSkillNode.querySelector(".evidence-item");
      const viewBeforeSkill = currentView;
      if (evSkillBtn) {
        await useEvidence({ kind: "skill", source: "skill", locator: "slides", excerpt: "做汇报", ok: true });
      }
      report.evidenceSkill = Boolean(
        evSkillBtn &&
          /源头：skill · slides/.test((evSkillBtn && evSkillBtn.textContent) || "") &&
          currentView === "skills" &&
          /slides/.test((skillDetailEl.querySelector("h1") || {}).textContent || ""),
      );
      switchView(viewBeforeSkill || "chat");
      if (evSkillNode) {
        evSkillNode.remove();
      }
      const retrySavedPrompt = promptEl.value;
      const wasBusy = busy;
      busy = true;
      const errNode = renderSendError("boom", "retry-me");
      const sendRetryBtn = errNode.querySelector(".retry-send");
      if (sendRetryBtn) {
        sendRetryBtn.click();
      }
      report.retryShown = Boolean(
        sendRetryBtn &&
          /重试/.test(sendRetryBtn.textContent || "") &&
          /retry-me/.test(promptEl.value || ""),
      );
      const savedSteerSid = sessionId;
      const savedSteerBusy = busy;
      const savedSteerPrompt = promptEl.value;
      sessionId = savedSteerSid || "ui-steer";
      let steered = null;
      const prevHook = window.__wittyTest.steerSession;
      window.__wittyTest.steerSession = async (sid, text) => {
        steered = { sid, text };
        return { ok: true };
      };
      setBusy(true);
      promptEl.value = "改用短回复";
      await sendPrompt({ preventDefault() {} });
      const queuedSteerBtn = document.querySelector(".queue-steer");
      if (queuedSteerBtn) {
        queuedSteerBtn.click();
        await new Promise((resolve) => window.setTimeout(resolve, 20));
      }
      report.steerShown = Boolean(
        steered &&
          steered.sid === sessionId &&
          steered.text === "改用短回复" &&
          /改用短回复/.test(logEl.textContent || "") &&
          /调整方向/.test((document.querySelector(".queue-steer") || queuedSteerBtn || {}).textContent || "调整方向"),
      );
      if (prevHook) {
        window.__wittyTest.steerSession = prevHook;
      } else {
        delete window.__wittyTest.steerSession;
      }
      sessionId = savedSteerSid;
      setBusy(savedSteerBusy);
      promptEl.value = savedSteerPrompt;
      fitPrompt();
      const steerBubbles = Array.from(logEl.querySelectorAll(".bubble.user")).filter((node) =>
        /改用短回复/.test(node.textContent || ""),
      );
      steerBubbles.forEach((node) => node.remove());
      const waiting = logEl.querySelector(".bubble.waiting");
      if (waiting && /转向/.test(waiting.textContent || "")) {
        waiting.remove();
      }
      markRunPhase("done");
      busy = true;
      report.steerAfterDone = shouldSteer("我爱吃冰淇淋") === false;
      const savedQueue = promptQueue.slice();
      const savedQueuePrompt = promptEl.value;
      const savedQueueBusy = busy;
      const savedQueueSid = sessionId;
      promptQueue = [];
      sessionId = savedQueueSid || "ui-queue";
      setBusy(true);
      markRunPhase("streaming");
      promptEl.value = "queued-next";
      const queued = enqueueFromComposer();
      const queueHost = document.getElementById("queue-dock");
      const queueRow = queueHost && queueHost.querySelector(".queue-item");
      const queueSteer = queueHost && queueHost.querySelector(".queue-steer");
      const queueDel = queueHost && queueHost.querySelector(".queue-remove");
      report.queueShown = Boolean(
        queued &&
          queueHost &&
          queueHost.hidden === false &&
          queueRow &&
          /queued-next/.test(queueRow.textContent || "") &&
          queueSteer &&
          queueSteer.hidden === false &&
          /调整方向/.test(queueSteer.textContent || "") &&
          queueDel,
      );
      if (queueDel) {
        queueDel.click();
      }
      report.queueShown = Boolean(
        report.queueShown && queueHost && queueHost.hidden === true && promptQueue.length === 0,
      );
      promptQueue = savedQueue;
      renderQueue();
      sessionId = savedQueueSid;
      setBusy(savedQueueBusy);
      promptEl.value = savedQueuePrompt;
      fitPrompt();
      markRunPhase("idle");
      busy = false;
      renderTodos([
        { content: "读文件", status: "completed" },
        { content: "写摘要", status: "in_progress" },
      ]);
      const steerBtn = todoDock ? todoDock.querySelector(".todo-steer") : null;
      const beforeTodo = promptEl.value;
      promptEl.value = "";
      if (steerBtn) {
        steerBtn.click();
      }
      report.todoShown = Boolean(
        todoDock &&
          !todoDock.hidden &&
          /待办 · 1\/2 · 当前：写摘要/.test(todoDock.textContent || "") &&
          todoDock.querySelector("details.todo-panel") &&
          todoDock.querySelector('.todo-item.todo-in_progress[aria-current="step"]'),
      );
      report.todoClick = Boolean(
        steerBtn &&
          /写摘要/.test(steerBtn.textContent || "") &&
          todoDock.querySelectorAll(".todo-steer").length === 1 &&
          /继续：写摘要/.test(promptEl.value || ""),
      );
      promptEl.value = beforeTodo;
      renderTodos([]);
      applyEvent(
        {
          type: "todos",
          args: { todos: [{ content: "split tokens", status: "in_progress" }] },
        },
        {},
      );
      report.todoEvent = Boolean(
        todoDock &&
          !todoDock.hidden &&
          /split tokens/.test(todoDock.textContent || "") &&
          /待办 · 0\/1 · 当前：split tokens/.test(todoDock.textContent || "") &&
          todoDock.querySelector('.todo-item.todo-in_progress[data-current="1"]'),
      );
      const longTodos = Array.from({ length: 20 }, (_, index) => ({
        content: `步骤${index + 1}`,
        status: index === 0 ? "in_progress" : "pending",
      }));
      renderTodos(longTodos);
      renderTodos(
        longTodos.map((item, index) => ({
          content: item.content,
          status: index < 14 ? "completed" : index === 14 ? "in_progress" : "pending",
        })),
      );
      const todoList = todoDock ? todoDock.querySelector(".todo-list") : null;
      const nextRow = todoDock ? todoDock.querySelector('.todo-item.todo-in_progress[aria-current="step"]') : null;
      report.todoScroll = Boolean(
        todoDock &&
          !todoDock.hidden &&
          todoList &&
          todoList.scrollHeight > todoList.clientHeight + 8 &&
          todoList.scrollTop > 8 &&
          nextRow &&
          /步骤15/.test(nextRow.textContent || "") &&
          /待办 · 14\/20 · 当前：步骤15/.test(todoDock.textContent || "") &&
          todoCurrentVisible(todoList, nextRow),
      );
      renderTodos(longTodos);
      const folded = todoDock ? todoDock.querySelector("details.todo-panel") : null;
      if (folded) {
        folded.open = false;
        folded.dataset.userToggled = "1";
      }
      renderTodos(longTodos);
      const todoHeld = todoDock ? todoDock.querySelector("details.todo-panel") : null;
      report.todoFoldHeld = Boolean(
        todoHeld &&
          todoHeld.open === false &&
          todoHeld.dataset.userToggled === "1" &&
          todoHeld.dataset.currentKey === "步骤1",
      );
      renderTodos(
        longTodos.map((item, index) => ({
          content: item.content,
          status: index < 14 ? "completed" : index === 14 ? "in_progress" : "pending",
        })),
      );
      const advanced = todoDock ? todoDock.querySelector("details.todo-panel") : null;
      report.todoFoldAdvance = Boolean(
        advanced &&
          advanced.open === true &&
          advanced.dataset.userToggled !== "1" &&
          /当前：步骤15/.test(todoDock.textContent || ""),
      );
      renderTodos([]);
      renderPlan({ active: true });
      const planDockEl = document.getElementById("plan-dock");
      report.planShown = Boolean(
        planDockEl &&
          !planDockEl.hidden &&
          document.body.dataset.plan === "1" &&
          /\/plan off/.test(planDockEl.textContent || ""),
      );
      renderPlan({ active: false });
      busy = wasBusy;
      promptEl.value = retrySavedPrompt;
      fitPrompt();
      if (errNode) {
        errNode.remove();
      }
      logEl.innerHTML = foldSaved;
      const memorySaved = {
        lattice: (document.getElementById("memory-lattice") || {}).innerHTML,
        links: (document.getElementById("memory-links") || {}).innerHTML,
        recalled: (document.getElementById("memory-recalled") || {}).innerHTML,
        tax: (document.getElementById("memory-taxonomy") || {}).innerHTML,
        ws: (document.getElementById("memory-workspace") || {}).innerHTML,
        time: (document.getElementById("memory-timeline") || {}).innerHTML,
        query: (document.getElementById("memory-query") || {}).value || "",
      };
      renderMemory({
        query: "农配网",
        retrieved: "- 领域要点 (`domain`): 农配网台区",
        hits: [
          { slug: "domain", title: "领域要点", text: "农配网台区", score: 4 },
          { slug: "decisions", title: "已做决定", text: "OAuth2", score: 3, scope: "workspace" },
          {
            slug: "note-txt",
            title: "note-txt",
            text: `read note.txt: ${"alpha-source-line ".repeat(8).trim()}`,
            score: 2,
            scope: "workspace",
          },
          {
            slug: "pair",
            title: "两条路径",
            text: "read note.txt: alpha and read other.py: leftover",
            score: 3,
            scope: "workspace",
          },
          {
            slug: "solo-txt",
            title: "solo-txt",
            text: "read solo.txt: unique-body",
            score: 3,
            scope: "workspace",
            relocated: [{ from: "solo.txt", to: "notes/solo.txt" }],
          },
        ],
        timeline_events: [{ date: "2024-06-10", text: "农配网工程批复" }],
        cells: [
          { id: "who", title: "身份", body: "", count: 0 },
          { id: "goals", title: "目标", body: "", count: 0 },
          { id: "constraints", title: "红线", body: "", count: 0 },
          { id: "prefs", title: "偏好", body: "- 简短", count: 1 },
          { id: "domain", title: "领域", body: "- 农配网台区", count: 1 },
          { id: "assets", title: "资产", body: "", count: 0 },
          { id: "people", title: "关系", body: "", count: 0 },
          { id: "decisions", title: "决定", body: "- 用户决定用简体", count: 1 },
          { id: "followups", title: "跟进", body: "", count: 0 },
        ],
        links: [{ from: "prefs", to: "domain", from_title: "偏好", to_title: "领域" }],
        taxonomy: [{ id: "rural-distribution", title: "农配网项目", body: "- 台区", count: 1 }],
        workspace_topics: [
          { id: "decisions", title: "已做决定", body: "- 助手记录：已决定采用 OAuth2", count: 1 },
          { id: "note-txt", title: "note-txt", body: "- read note.txt: alpha-source-line", count: 1 },
        ],
      });
      report.memoryScope = Boolean(
        document.querySelector("#memory-workspace details.memory-extra[data-id=\"decisions\"][data-scope=\"workspace\"].linked") &&
          document.querySelector("#memory-workspace details.memory-extra[data-id=\"decisions\"][open]") &&
          !document.querySelector("#memory-lattice .memory-cell[data-id=\"decisions\"].linked"),
      );
      const evMem = addBubble(
        "assistant",
        "from-mem",
        "",
        [
          {
            source: "memory_read",
            locator: "decisions",
            excerpt: "OAuth2",
            ok: true,
            kind: "memory",
            scope: "workspace",
            score: 3,
          },
        ],
        "used memory",
      );
      const evMemBtn = evMem.querySelector(".evidence-item");
      const evMemSavedPrompt = promptEl.value;
      if (evMemBtn) {
        evMemBtn.click();
      }
      report.evidenceScope = Boolean(
        evMemBtn &&
          /源头：memory_read · decisions · workspace · 弱 · 3/.test(evMemBtn.textContent || "") &&
          document.querySelector("#memory-workspace details.memory-extra[data-id=\"decisions\"].linked") &&
          !document.querySelector("#memory-lattice .memory-cell[data-id=\"decisions\"].linked") &&
          /OAuth2/.test((document.getElementById("memory-query") || {}).value || "") &&
          !/^decisions$/.test(((document.getElementById("memory-query") || {}).value || "").trim()),
      );
      promptEl.value = evMemSavedPrompt;
      fitPrompt();
      const evPath = addBubble(
        "assistant",
        "from-path",
        "",
        [
          {
            source: "memory_read",
            locator: "note-txt",
            excerpt: "read note.txt: alpha-source-line",
            ok: true,
            kind: "memory",
            scope: "workspace",
            score: 3,
          },
        ],
        "used memory",
      );
      const evPathBtn = evPath.querySelector(".evidence-item");
      if (evPathBtn) {
        evPathBtn.click();
      }
      report.weakCiteRead = Boolean(
        evPathBtn && /(?:^|\s)read note\.txt(?:\s|$)/.test(promptEl.value || ""),
      );
      promptEl.value = evMemSavedPrompt;
      fitPrompt();
      if (evMem) {
        evMem.remove();
      }
      if (evPath) {
        evPath.remove();
      }
      const citeMem = addBubble(
        "assistant",
        "采用 OAuth2",
        "",
        [
          {
            source: "memory_read",
            locator: "decisions",
            excerpt: "2025-01-01 OAuth2",
            ok: true,
            kind: "memory",
            scope: "workspace",
            score: 7,
          },
        ],
        "used memory",
      );
      const citeMemInline = citeMem.querySelector(".cite-inline");
      const citeMemChip = citeMem.querySelector(".cite-chip");
      const citeMemQuery = document.getElementById("memory-query");
      if (citeMemQuery) {
        citeMemQuery.value = "decisions";
      }
      if (citeMemInline) {
        citeMemInline.click();
      }
      report.citeMemory = Boolean(
        citeMemInline &&
          citeMemInline.textContent === "OAuth2" &&
          citeMemChip &&
          /覆盖 · 7/.test(citeMemChip.textContent || "") &&
          citeMemQuery &&
          /OAuth2/.test(citeMemQuery.value || "") &&
          !/^decisions$/.test((citeMemQuery.value || "").trim()) &&
          document.querySelector("#memory-workspace details.memory-extra[data-id=\"decisions\"].linked"),
      );
      if (citeMem) {
        citeMem.remove();
      }
      const sealHost = addBubble("assistant", "里面是 42");
      setSeal(sealHost, "本轮没有工具或记忆依据。上一条助手结论应视为未核实，不能当源头。");
      report.sealFold = Boolean(
        sealHost.querySelector("details.seal") &&
          sealHost.dataset.sealed === "1" &&
          /未核实/.test((sealHost.querySelector(".seal summary") || {}).textContent || "") &&
          /不能当源头/.test((sealHost.querySelector(".seal-body") || {}).textContent || "") &&
          /里面是 42/.test((sealHost.querySelector(".md") || {}).textContent || "") &&
          !/不能当源头/.test((sealHost.querySelector(".md") || {}).textContent || ""),
      );
      if (sealHost) {
        sealHost.remove();
      }
      const linkBtn = document.querySelector("#memory-links .memory-link");
      if (linkBtn) {
        linkBtn.click();
      }
      const timeBtn = document.querySelector("#memory-timeline .memory-time .memory-link");
      if (timeBtn) {
        timeBtn.click();
      }
      report.memoryFold = Boolean(
        document.querySelectorAll("#memory-lattice details.memory-cell").length === 9 &&
          /领域要点/.test((document.getElementById("memory-recalled") || {}).textContent || "") &&
          document.querySelector("#memory-lattice .memory-cell[data-id=\"domain\"].linked") &&
          document.querySelector("#memory-lattice .memory-cell[data-id=\"domain\"][open]") &&
          document.querySelector("#memory-taxonomy details.memory-tax"),
      );
      const profileFold = document.getElementById("memory-profile-fold");
      const profileSummary = document.getElementById("memory-profile-summary");
      report.memoryProfileFold = Boolean(
        profileFold &&
          profileFold.open !== true &&
          /用户画像 · 空/.test((profileSummary && profileSummary.textContent) || "") &&
          /尚未形成画像/.test((document.getElementById("memory-profile") || {}).textContent || ""),
      );
      report.memoryWs = Boolean(
        document.querySelector("#memory-workspace details.memory-extra[data-id=\"decisions\"]") &&
          document.querySelector("#memory-workspace details.memory-extra[data-id=\"note-txt\"]"),
      );
      const recallFold = document.querySelector("#memory-recalled details.memory-recall");
      const recallSummary = recallFold && recallFold.querySelector("summary");
      const recallBody = recallFold && recallFold.querySelector(".memory-recall-body");
      report.memoryRecallFold = Boolean(
        recallFold &&
          recallSummary &&
          recallBody &&
          (recallSummary.textContent || "").includes("…") &&
          (recallSummary.textContent || "").length < (recallBody.textContent || "").length &&
          /alpha-source-line/.test(recallBody.textContent || "") &&
          recallFold.open !== true,
      );
      const domainHit = Array.from(document.querySelectorAll("#memory-recalled .memory-link")).find((node) =>
        /领域要点/.test(node.textContent || ""),
      );
      report.memoryScore = Boolean(
        domainHit &&
          /弱 · 4/.test(domainHit.textContent || "") &&
          domainHit.classList.contains("recall-weak") &&
          recallFold &&
          /弱 · 2/.test((recallSummary && recallSummary.textContent) || ""),
      );
      const pairHit = Array.from(document.querySelectorAll("#memory-recalled .memory-link")).find((node) =>
        /两条路径/.test(node.textContent || ""),
      );
      const savedRecallPrompt = promptEl.value;
      promptEl.value = "";
      if (pairHit) {
        pairHit.click();
      }
      const pairFilled = /read note\.txt and other\.py/.test(promptEl.value || "");
      promptEl.value = "";
      if (recallBody) {
        recallBody.click();
      }
      report.memoryWeakRead = Boolean(
        pairHit &&
          pairFilled &&
          /(?:^|\s)read note\.txt(?:\s|$)/.test(promptEl.value || ""),
      );
      const relocatedHit = Array.from(document.querySelectorAll("#memory-recalled .memory-link")).find((node) =>
        /已定位 notes\/solo\.txt/.test(node.textContent || ""),
      );
      promptEl.value = "";
      if (relocatedHit) {
        relocatedHit.click();
      }
      report.memoryRelocated = Boolean(
        relocatedHit && /(?:^|\s)read notes\/solo\.txt(?:\s|$)/.test(promptEl.value || ""),
      );
      const prevGetMemory = window.__wittyTest.getMemory;
      const recalledBox = document.getElementById("memory-recalled");
      window.__wittyTest.getMemory = async () => ({
        query: "unique",
        hits: [
          {
            slug: "solo-txt",
            title: "solo-txt",
            text: "read notes/solo.txt: unique-body",
            score: 3,
            scope: "workspace",
            relocated: [{ from: "solo.txt", to: "notes/solo.txt" }],
          },
        ],
      });
      if (recalledBox) {
        renderRecalled(recalledBox, {
          query: "unique",
          hits: [
            {
              slug: "solo-txt",
              title: "solo-txt",
              text: "read solo.txt: unique-body",
              score: 3,
              scope: "workspace",
            },
          ],
        });
      }
      const staleRecall = (recalledBox && recalledBox.textContent) || "";
      const refreshLive = applyEvent(
        { type: "message_end", role: "user", source: "plugin:recalled-verify", text: "relocated" },
        {},
      );
      if (refreshLive.memoryRefresh) {
        await refreshLive.memoryRefresh;
      }
      const freshRecall = (recalledBox && recalledBox.textContent) || "";
      report.memoryRefresh = Boolean(
        /read solo\.txt/.test(staleRecall) &&
          !/已定位/.test(staleRecall) &&
          /已定位 notes\/solo\.txt/.test(freshRecall),
      );
      const emptyRecallText = (() => {
        if (!recalledBox) {
          return "";
        }
        renderRecalled(recalledBox, {
          query: "个人偏好",
          hits: [],
          empty: { reason: "no_overlap", populated: [{ id: "prefs", title: "个人偏好", count: 1, scope: "user" }] },
        });
        return recalledBox.textContent || "";
      })();
      window.__wittyTest.getMemory = async () => ({
        query: "个人偏好",
        hits: [],
        empty: { reason: "no_overlap", populated: [{ id: "prefs", title: "个人偏好", count: 1, scope: "user" }] },
      });
      applyEvent(
        {
          type: "tool_execution_start",
          tool_name: "memory_read",
          tool_call_id: "browse-1",
          args: { slug: "prefs", scope: "user" },
        },
        {},
      );
      applyEvent(
        {
          type: "tool_execution_end",
          tool_name: "memory_read",
          tool_call_id: "browse-1",
          text: "- 我喜欢简短回复",
          is_error: false,
        },
        {},
      );
      const loadedLive = applyEvent(
        { type: "message_end", role: "user", source: "plugin:browse-read", text: "already read prefs" },
        {},
      );
      if (loadedLive.memoryRefresh) {
        await loadedLive.memoryRefresh;
      }
      report.memoryLoaded = Boolean(
        /没有与/.test(emptyRecallText) &&
          /已读/.test((recalledBox && recalledBox.textContent) || "") &&
          /简短回复/.test((recalledBox && recalledBox.textContent) || ""),
      );
      if (prevGetMemory) {
        window.__wittyTest.getMemory = prevGetMemory;
      } else {
        delete window.__wittyTest.getMemory;
      }
      promptEl.value = savedRecallPrompt;
      fitPrompt();
      report.memoryTime = Boolean(
        document.querySelector("#memory-timeline details.memory-time[data-date=\"2024-06-10\"]") &&
          /农配网工程批复/.test((document.getElementById("memory-query") || {}).value || "") &&
          document.querySelector("#memory-timeline details.memory-time.linked"),
      );
      renderMemory({
        query: "这个 需要 一下",
        retrieved: "",
        hits: [],
        empty: {
          reason: "too_generic",
          tokens: [],
          populated: [
            { id: "prefs", title: "偏好", count: 1, scope: "user" },
            { id: "domain", title: "领域", count: 1, scope: "user" },
          ],
          archive_count: 2,
          archive: [
            {
              id: "archive/domain",
              title: "归档·domain",
              count: 2,
              scope: "user",
              excerpt: "2025-01-01 旧施工图在柜里",
            },
          ],
        },
        archive: [
          { id: "archive/domain", title: "归档·domain", count: 2, body: "- 旧施工图", scope: "user" },
        ],
        cells: [
          { id: "who", title: "身份", body: "", count: 0 },
          { id: "goals", title: "目标", body: "", count: 0 },
          { id: "constraints", title: "红线", body: "", count: 0 },
          { id: "prefs", title: "偏好", body: "- 简短", count: 1 },
          { id: "domain", title: "领域", body: "- 农配网台区", count: 1 },
          { id: "assets", title: "资产", body: "", count: 0 },
          { id: "people", title: "关系", body: "", count: 0 },
          { id: "decisions", title: "决定", body: "", count: 0 },
          { id: "followups", title: "跟进", body: "", count: 0 },
        ],
      });
      const emptyHint = document.querySelector("#memory-recalled .memory-empty-hint");
      if (emptyHint) {
        emptyHint.click();
      }
      report.memoryEmpty = Boolean(
        /查询太泛/.test((document.getElementById("memory-recalled") || {}).textContent || "") &&
          /归档还有 2 条/.test((document.getElementById("memory-recalled") || {}).textContent || "") &&
          emptyHint &&
          /偏好/.test(emptyHint.textContent || "") &&
          document.querySelector("#memory-lattice .memory-cell[data-id=\"prefs\"][open]"),
      );
      const archHint = document.querySelector("#memory-recalled .memory-archive-hint");
      if (archHint) {
        archHint.click();
      }
      report.memoryArchive = Boolean(
        document.querySelector("#memory-archive .memory-archive[data-id=\"archive/domain\"]") &&
          archHint &&
          /归档·domain/.test(archHint.textContent || "") &&
          /旧施工图/.test(archHint.textContent || "") &&
          document.querySelector("#memory-archive .memory-archive[data-id=\"archive/domain\"][open]") &&
          /旧施工图在柜里/.test((document.getElementById("memory-query") || {}).value || "") &&
          !/2025-01-01/.test((document.getElementById("memory-query") || {}).value || ""),
      );
      if (document.getElementById("memory-lattice")) {
        document.getElementById("memory-lattice").innerHTML = memorySaved.lattice || "";
      }
      if (document.getElementById("memory-links")) {
        document.getElementById("memory-links").innerHTML = memorySaved.links || "";
      }
      if (document.getElementById("memory-recalled")) {
        document.getElementById("memory-recalled").innerHTML = memorySaved.recalled || "";
      }
      if (document.getElementById("memory-taxonomy")) {
        document.getElementById("memory-taxonomy").innerHTML = memorySaved.tax || "";
      }
      if (document.getElementById("memory-workspace")) {
        document.getElementById("memory-workspace").innerHTML = memorySaved.ws || "";
      }
      if (document.getElementById("memory-timeline")) {
        document.getElementById("memory-timeline").innerHTML = memorySaved.time || "";
      }
      if (document.getElementById("memory-query")) {
        document.getElementById("memory-query").value = memorySaved.query || "";
      }
      const evBrowseNode = addBubble(
        "assistant",
        "from-browse",
        "",
        [
          {
            kind: "browse",
            source: "memory_status",
            locator: "prefs",
            excerpt: "个人偏好 · 1",
            ok: true,
            scope: "user",
          },
        ],
        "No Recalled notes overlapped this turn.",
      );
      const evBrowseBtn = evBrowseNode.querySelector(".evidence-item");
      renderMemory({
        query: "",
        hits: [],
        cells: [
          { id: "who", title: "身份", body: "", count: 0 },
          { id: "prefs", title: "偏好", body: "- 简短", count: 1 },
        ],
      });
      const queryEl = document.getElementById("memory-query");
      if (queryEl) {
        queryEl.value = "prefs";
      }
      useEvidence({
        kind: "browse",
        source: "memory_status",
        locator: "prefs",
        excerpt: "个人偏好 · 1",
        ok: true,
        scope: "user",
      });
      report.evidenceBrowse = Boolean(
        evBrowseBtn &&
          /浏览：memory_status · prefs · user/.test(evBrowseBtn.textContent || "") &&
          // 依据面板的抬头统一是「依据 · N 条」；早先给浏览类单列的「可浏览 N 格」
          // 已经取消，纯浏览且无理由时 setEvidence 直接不出面板、只点亮记忆格。
          /依据 · 1 条/.test((evBrowseNode.querySelector(".evidence summary") || {}).textContent || "") &&
          queryEl &&
          queryEl.value === "" &&
          document.querySelector("#memory-lattice .memory-cell[data-id=\"prefs\"].linked"),
      );
      if (evBrowseNode) {
        evBrowseNode.remove();
      }
      const citeRelocSaved = promptEl.value;
      window.__wittyTest.getMemory = async () => ({
        query: "unique-body",
        hits: [
          {
            slug: "solo-txt",
            title: "solo-txt",
            text: "read solo.txt: unique-body",
            score: 3,
            scope: "workspace",
          },
        ],
      });
      await useEvidence({
        source: "memory_read",
        locator: "solo-txt",
        excerpt: "read solo.txt: unique-body",
        ok: true,
        kind: "memory",
        scope: "workspace",
        score: 3,
        relocated: [{ from: "solo.txt", to: "notes/solo.txt" }],
      });
      report.evidenceRelocated = Boolean(
        /已定位 notes\/solo\.txt/.test((document.getElementById("memory-recalled") || {}).textContent || "") &&
          /(?:^|\s)read notes\/solo\.txt(?:\s|$)/.test(promptEl.value || ""),
      );
      delete window.__wittyTest.getMemory;
      promptEl.value = citeRelocSaved;
      fitPrompt();
      const previousView = currentView;
      try {
        switchView("skills");
        await loadSkills();
        // 能力中心是卡片落地页，详情收在弹窗里。loadSkills() 不带 keepName 时不开窗，
        // 要检查折叠得先点开一张卡。
        const firstCard = skillListEl && skillListEl.querySelector(".skill-card");
        if (firstCard && firstCard.dataset.name) {
          await showSkill(firstCard.dataset.name);
        }
        const openedCard = firstCard
          ? skillListEl.querySelector(`.skill-card[data-name="${firstCard.dataset.name}"]`)
          : null;
        report.skillModalOpen = Boolean(
          skillModalEl &&
            skillModalEl.open &&
            skillModalEl.contains(skillDetailEl) &&
            openedCard &&
            openedCard.classList.contains("active"),
        );
        const skillBody = skillDetailEl.querySelector("details.skill-body");
        const dumpedMd = Array.from(skillDetailEl.children).some((node) => node.classList && node.classList.contains("md"));
        report.skillFold = Boolean(
          skillBody &&
            skillBody.open === false &&
            skillBody.querySelector(".md") &&
            !dumpedMd &&
            /SKILL\.md/.test((skillBody.querySelector("summary") || {}).textContent || ""),
        );
        const skillModalCloseBtn = document.getElementById("skill-modal-close");
        if (skillModalCloseBtn) {
          skillModalCloseBtn.click();
        }
        report.skillModalClose = Boolean(skillModalEl && !skillModalEl.open);
        // 弹窗在 top layer，藏掉 #view-skills 未必收得走，所以单独验一遍换页会关。
        if (firstCard && firstCard.dataset.name) {
          await showSkill(firstCard.dataset.name);
        }
      } catch {
        report.skillFold = false;
      }
      try {
        switchView("tools");
        report.skillModalLeave = Boolean(skillModalEl && !skillModalEl.open);
        await loadTools();
        report.toolGroup = Boolean(
          toolListEl.querySelector("details.catalog-fold[data-group=\"kernel\"][open]") &&
            toolListEl.querySelector("details.catalog-fold .catalog-item") &&
            !Array.from(toolListEl.children).some((node) => node.classList && node.classList.contains("catalog-item")),
        );
      } catch {
        report.toolGroup = false;
      }
      try {
        switchView("settings");
        await loadPrompts();
        showSettingsPanel("prompt");
        const firstPrompt = promptNavEl && promptNavEl.querySelector(".catalog-item[data-prompt]");
        if (firstPrompt) {
          await openPrompt(firstPrompt.dataset.prompt);
        }
        report.settingsWide = Boolean(document.querySelector("#view-settings .settings-shell"));
        report.splitHandle = Boolean(document.querySelector(".split-handle"));
        report.promptGrouped = Boolean(
          promptNavEl &&
            promptNavEl.querySelector("details.catalog-fold[data-group]") &&
            !settingsNavEl.querySelector(".catalog-item[data-prompt]"),
        );
        report.promptShown = Boolean(
          firstPrompt &&
            promptBodyEl &&
            promptBodyEl.value.trim() &&
            promptTitleEl &&
            promptTitleEl.textContent === firstPrompt.dataset.prompt &&
            document.querySelector('#settings-detail [data-panel="prompt"]:not([hidden])'),
        );
      } catch {
        report.promptShown = false;
        report.promptGrouped = false;
        report.settingsWide = Boolean(document.querySelector("#view-settings .settings-shell"));
      }
      try {
        switchView("links");
        try {
          await loadLinks();
        } catch {
          /* 脚本化 API 没有库时仍画夹具 */
        }
        renderLinks({
          links: [
            {
              url: "http://192.168.0.10/oa",
              title: "OA系统",
              intent: "报周报",
              hits: 3,
              aliases: ["OA"],
            },
          ],
        });
        report.linkShown = Boolean(
          document.querySelector('#view-links:not([hidden])') &&
            document.getElementById("link-form") &&
            document.querySelector(".rail-btn[data-view=\"links\"]"),
        );
        report.linkList = /OA系统/.test((document.getElementById("link-list") || {}).textContent || "") &&
          /报周报/.test((document.getElementById("link-detail") || {}).textContent || "");
        const firstLink = document.querySelector("#link-list .habit-item");
        if (firstLink) {
          firstLink.click();
        }
        report.linkFill = (document.getElementById("link-url") || {}).value === "http://192.168.0.10/oa";
        switchView("wiki");
        renderWiki({
          enabled: true,
          pending: 1,
          sources: [
            {
              id: "2026-08-20-paper",
              kind: "file",
              path: "raw/2026-08-20-paper.md",
              origin: "/tmp/paper.md",
              status: "pending",
              added: "2026-08-20",
            },
          ],
          text: "wiki 原文 1 条",
        });
        report.wikiShown = Boolean(
          document.querySelector('#view-wiki:not([hidden])') &&
            document.getElementById("wiki-form") &&
            document.querySelector(".rail-btn[data-view=\"wiki\"]"),
        );
        report.wikiList = /2026-08-20-paper/.test((document.getElementById("wiki-list") || {}).textContent || "");
        switchView("diary");
        try {
          await loadDiary();
        } catch {
          /* 没有日记时仍画夹具 */
        }
        renderDiaryDays(["2026-08-17"], "2026-08-17");
        renderDiary({ day: "2026-08-17", body: "- 13:00 · note · 今天下午开了验收会" });
        report.diaryShown = Boolean(
          document.querySelector('#view-diary:not([hidden])') &&
            document.getElementById("diary-form") &&
            document.querySelector(".rail-btn[data-view=\"diary\"]"),
        );
        report.diaryBody = /验收会/.test((document.getElementById("diary-body") || {}).textContent || "");
        switchView("mail");
        renderMail({
          configured: false,
          text: "IMAP 主机 （未配置）",
          drafts: [{ id: "d-abcd1234", to: ["lead@grid.local"], subject: "周报草稿", attachments: 1 }],
        });
        report.mailShown = Boolean(
          document.querySelector('#view-mail:not([hidden])') &&
            document.getElementById("mail-open-chat") &&
            document.getElementById("open-mail-desk"),
        );
        report.mailStatus = /周报草稿/.test((document.getElementById("mail-drafts") || {}).textContent || "");
      } catch {
        report.linkShown = false;
        report.linkList = false;
        report.linkFill = false;
        report.diaryShown = false;
        report.diaryBody = false;
        report.mailShown = false;
        report.mailStatus = false;
      }
      switchView(previousView || "chat");
      const previousTheme = currentTheme();
      applyTheme("day");
      report.themeApplied = document.documentElement.dataset.theme === "day";
      applyTheme("glass");
      report.glassTheme =
        document.documentElement.dataset.theme === "glass" &&
        /blur/.test(getComputedStyle(document.querySelector(".rail")).backdropFilter || "");
      applyTheme(previousTheme);
      report.settingsNavIcons = Boolean(
        settingsNavEl && settingsNavEl.querySelector(".catalog-item .set-ico svg"),
      );
      persistState();
      report.hasPlanAbort = report.commands.includes("plan") && report.commands.includes("abort");
      report.ok = Boolean(
        report.approvalEmpty &&
          report.approvalFold &&
          report.approvalFoldHeld &&
          report.approvalEsc &&
          report.askShown &&
          report.askMulti &&
          report.askFoldHeld &&
          report.askPicksHeld &&
          report.askNoId &&
          report.askPreparing &&
          report.streamStall &&
          report.writeHeld &&
          report.approvalNoId &&
          report.scrollHolds &&
          report.scrollFollows &&
          report.imeBlocked &&
          report.imeSends &&
          report.pickerOpen &&
          report.pickerEsc &&
          report.pickerSelect &&
          report.pathInserted &&
          report.thinkPersisted &&
          report.thinkShown &&
          report.thinkFollow &&
          report.toolsFolded &&
          report.toolsStackHeld &&
          report.toolsStackReopened &&
          report.toolsFailed &&
          report.toolsMiss &&
          report.workProcessOutside &&
          report.bubbleCopy &&
          report.bubbleFork &&
          report.bubbleRate &&
          report.exportShown &&
          report.bubbleRetry &&
          report.artifactsSessionOnly &&
          report.artifactNameFirst &&
          report.artifactNoNoise &&
          report.turnFilesShown &&
          report.queueShown &&
          report.sessionTitles &&
          report.sessionCite &&
          report.sessionForget &&
          report.fileCite &&
          report.fileDirCite &&
          report.localImage &&
          report.localImageFallback &&
          report.skillFold &&
          report.skillModalOpen &&
          report.skillModalClose &&
          report.skillModalLeave &&
          report.toolGroup &&
          report.memoryFold &&
          report.memoryProfileFold &&
          report.memoryRecallFold &&
          report.memoryScore &&
          report.memoryWeakRead &&
          report.memoryRelocated &&
          report.memoryRefresh &&
          report.memoryLoaded &&
          report.memoryWs &&
          report.memoryScope &&
          report.memoryTime &&
          report.memoryEmpty &&
          report.memoryArchive &&
          report.evidenceClick &&
          report.evidenceSkill &&
          report.evidenceScope &&
          report.evidenceBrowse &&
          report.citeShown &&
          report.evidenceFold &&
          report.evidenceFoldHeld &&
          report.citeMemory &&
          report.weakCiteRead &&
          report.evidenceRelocated &&
          report.sealFold &&
          report.retryShown &&
          report.steerShown &&
          report.steerAfterDone &&
          report.planShown &&
          report.todoShown &&
          report.todoClick &&
          report.todoEvent &&
          report.todoScroll &&
          report.todoFoldHeld &&
          report.todoFoldAdvance &&
          report.streamMerge &&
          report.sseParse &&
          report.sseWatch &&
          report.sseFallback &&
          report.themeApplied &&
          report.glassTheme &&
          report.settingsNavIcons &&
          report.hasPlanAbort &&
          report.promptShown &&
          report.promptGrouped &&
          report.settingsWide &&
          report.splitHandle &&
          report.linkShown &&
          report.linkList &&
          report.linkFill &&
          report.diaryShown &&
          report.diaryBody &&
          report.mailShown &&
          report.mailStatus,
      );
      hidePickers();
      return report;
    },
    async runChat(text, options) {
      await boot;
      const opts = options || {};
      window.__wittyTest.autoApprove = opts.approve || "";
      if (opts.workspace) {
        workspaceEl.value = opts.workspace;
      }
      const prompt = String(text || "hello-window");
      if (!(await refreshHealth())) {
        return { ok: false, error: "api down" };
      }
      await createSession();
      if (!sessionId) {
        return { ok: false, error: "no session" };
      }
      persistState();
      promptEl.value = prompt;
      await sendPrompt({ preventDefault() {} });
      const users = logEl.querySelectorAll(".bubble.user");
      const assistants = logEl.querySelectorAll(".bubble.assistant");
      const user = users[users.length - 1];
      const assistant = assistants[assistants.length - 1];
      return {
        ok: Boolean(user && assistant && assistant.textContent),
        sessionId,
        reply: assistant ? assistant.dataset.raw || assistant.textContent : "",
        userText: user ? user.textContent : "",
        approved: Boolean(lastApproval.decision),
        decision: lastApproval.decision || "",
        approvalDockEmpty: approvalDock.childElementCount === 0,
        error: lastSendError,
        persisted: (() => {
          try {
            return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}").session_id === sessionId;
          } catch {
            return false;
          }
        })(),
      };
    },
  };
})();
