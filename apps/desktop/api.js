"use strict";

/** HTTP client for the Python witty-agent API. Does not define a second protocol. */

function apiBase() {
  return (process.env.WITTY_API_BASE || "http://127.0.0.1:8765").replace(/\/$/, "");
}

async function request(method, path, body) {
  const headers = { Accept: "application/json" };
  const options = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`${apiBase()}${path}`, options);
  let payload = {};
  const raw = await response.text();
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = { error: raw };
    }
  }
  if (!response.ok) {
    const error = new Error(payload.error || `${response.status} ${response.statusText}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function health() {
  return request("GET", "/v1/health");
}

function createAgent(projectId, agentId) {
  return request("POST", "/v1/agents", {
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
}

function forkSession(sessionId) {
  return request("POST", `/v1/sessions/${encodeURIComponent(sessionId)}/fork`, {});
}

function createSession(options) {
  const body = {
    project_id: options.project_id || "default_project",
    agent_id: options.agent_id || "default_agent",
  };
  if (options.workspace_dir) {
    body.workspace_dir = options.workspace_dir;
  }
  return request("POST", "/v1/sessions", body);
}

function sendPrompt(sessionId, prompt, approvalMode) {
  return request("POST", `/v1/sessions/${sessionId}/messages`, {
    prompt,
    approval_mode: approvalMode || "allow-all",
  });
}

function abortSession(sessionId) {
  return request("POST", `/v1/sessions/${sessionId}/abort`, {});
}

function steerSession(sessionId, text) {
  return request("POST", `/v1/sessions/${sessionId}/steer`, { text });
}

function startPrompt(sessionId, prompt, approvalMode, thinkLevel) {
  return request("POST", `/v1/sessions/${sessionId}/messages`, {
    prompt,
    approval_mode: approvalMode || "always-ask",
    think_level: thinkLevel || "short",
    wait: false,
  });
}

function deleteSession(sessionId, projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("DELETE", `/v1/sessions/${encodeURIComponent(sessionId)}?project_id=${project}&agent_id=${agent}`);
}

function getWeb() {
  return request("GET", "/v1/web");
}

function saveWeb(body) {
  return request("PUT", "/v1/web", body || {});
}

function getModel(projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("GET", `/v1/model?project_id=${project}&agent_id=${agent}`);
}

function saveModel(body) {
  return request("PUT", "/v1/model", body || {});
}

function listModels(projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("GET", `/v1/models?project_id=${project}&agent_id=${agent}`);
}

function saveModelProfile(body) {
  return request("PUT", "/v1/models", body || {});
}

function deleteModel(name, projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("DELETE", `/v1/models/${encodeURIComponent(name)}?project_id=${project}&agent_id=${agent}`);
}

function activateModel(name, projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("POST", `/v1/models/${encodeURIComponent(name)}/activate?project_id=${project}&agent_id=${agent}`, {
    name,
  });
}

function getRun(sessionId) {
  return request("GET", `/v1/sessions/${sessionId}/run`);
}

function submitApproval(sessionId, toolCallId, decision) {
  return request("POST", `/v1/sessions/${sessionId}/approval`, {
    tool_call_id: toolCallId,
    decision,
  });
}

function submitAnswer(sessionId, answers) {
  return request("POST", `/v1/sessions/${sessionId}/answer`, { answers });
}

function getMessages(sessionId) {
  return request("GET", `/v1/sessions/${sessionId}/messages`);
}

function listSessions(projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("GET", `/v1/sessions?project_id=${project}&agent_id=${agent}`);
}

function listSkills(projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("GET", `/v1/skills?project_id=${project}&agent_id=${agent}`);
}

function getSkill(name) {
  return request("GET", `/v1/skills/${encodeURIComponent(name)}`);
}

function setSkillEnabled(name, enabled, projectId, agentId) {
  return request("PUT", `/v1/skills/${encodeURIComponent(name)}`, {
    enabled,
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
}

function installSkill(body) {
  return request("POST", "/v1/skills", body || {});
}

function uninstallSkill(name, projectId, agentId) {
  return request("DELETE", `/v1/skills/${encodeURIComponent(name)}`, {
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
}

function reloadPlugins() {
  return request("POST", "/v1/plugins/reload", {});
}

function getPlugins() {
  return request("GET", "/v1/plugins");
}

function attachSkillPath(path) {
  return request("POST", "/v1/plugins/paths", { path });
}

function detachSkillPath(path) {
  return request("DELETE", "/v1/plugins/paths", { path });
}

function attachToolPackage(name, path) {
  return request("POST", "/v1/plugins/packages", { package: name, path: path || "" });
}

function detachToolPackage(name) {
  return request("DELETE", "/v1/plugins/packages", { package: name });
}

function listTools(projectId, agentId) {
  const project = encodeURIComponent(projectId || "default_project");
  const agent = encodeURIComponent(agentId || "default_agent");
  return request("GET", `/v1/tools?project_id=${project}&agent_id=${agent}`);
}

function listCommands(sessionId) {
  const suffix = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return request("GET", `/v1/commands${suffix}`);
}

function openPath() {
  return Promise.resolve({ ok: false, error: "browser" });
}

function listWorkspace(dir, sessionId) {
  const query = new URLSearchParams();
  if (dir) {
    query.set("dir", dir);
  }
  if (sessionId) {
    query.set("session_id", sessionId);
  }
  return request("GET", `/v1/workspace?${query.toString()}`).then((body) => body.paths || []);
}

function getMail(projectId, agentId) {
  const query = new URLSearchParams({
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
  return request("GET", `/v1/mail?${query.toString()}`);
}

function saveMail(body) {
  return request("PUT", "/v1/mail", body || {});
}

function listSchedules(projectId, agentId) {
  const query = new URLSearchParams({
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
  return request("GET", `/v1/schedules?${query.toString()}`);
}

function saveSchedule(body) {
  return request("PUT", "/v1/schedules", body || {});
}

function setScheduleEnabled(name, enabled, projectId, agentId) {
  const query = new URLSearchParams({
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
  return request("PATCH", `/v1/schedules/${encodeURIComponent(name)}?${query.toString()}`, {
    enabled: Boolean(enabled),
  });
}

function deleteSchedule(name, projectId, agentId) {
  const query = new URLSearchParams({
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
  return request("DELETE", `/v1/schedules/${encodeURIComponent(name)}?${query.toString()}`);
}

function tickSchedules() {
  return request("POST", "/v1/schedules/tick", {});
}

function getWiki(workspaceDir) {
  const query = new URLSearchParams();
  if (workspaceDir) {
    query.set("workspace_dir", workspaceDir);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request("GET", `/v1/wiki${suffix}`);
}

function addWiki(body) {
  return request("POST", "/v1/wiki", body || {});
}

function removeWiki(sourceId, workspaceDir) {
  const query = new URLSearchParams();
  if (sourceId) {
    query.set("id", sourceId);
  }
  if (workspaceDir) {
    query.set("workspace_dir", workspaceDir);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request("DELETE", `/v1/wiki${suffix}`);
}

function getLinks(query) {
  const asked = String(query || "").trim();
  const suffix = asked ? `?q=${encodeURIComponent(asked)}` : "";
  return request("GET", `/v1/links${suffix}`);
}

function addLink(body) {
  return request("POST", "/v1/links", body || {});
}

function getDiary(day, list) {
  const query = new URLSearchParams();
  if (list) {
    query.set("list", "1");
  }
  if (day) {
    query.set("day", day);
  }
  const suffix = query.toString() ? `?${query}` : "";
  return request("GET", `/v1/diary${suffix}`);
}

function writeDiary(text, day) {
  return request("POST", "/v1/diary", { text, day: day || "" });
}

function saveInbox(body) {
  return request("POST", "/v1/inbox", body || {});
}

function previewFile(workspaceDir, target) {
  const query = new URLSearchParams({ path: String(target || "") });
  if (workspaceDir) {
    query.set("workspace_dir", workspaceDir);
  }
  return request("GET", `/v1/file-preview?${query.toString()}`);
}

function getMemory(projectId, agentId, workspaceDir, recall) {
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
}

function saveMemory(body) {
  return request("POST", "/v1/memory", body || {});
}

function listPrompts() {
  return request("GET", "/v1/prompts");
}

function getPrompt(name) {
  return request("GET", `/v1/prompts/${encodeURIComponent(name)}`);
}

function savePrompt(name, text) {
  return request("PUT", `/v1/prompts/${encodeURIComponent(name)}`, { text });
}

function setToolEnabled(name, enabled, projectId, agentId) {
  return request("PUT", `/v1/tools/${encodeURIComponent(name)}`, {
    enabled,
    project_id: projectId || "default_project",
    agent_id: agentId || "default_agent",
  });
}

module.exports = {
  apiBase,
  health,
  createAgent,
  createSession,
  forkSession,
  sendPrompt,
  startPrompt,
  abortSession,
  steerSession,
  getRun,
  submitApproval,
  submitAnswer,
  getMessages,
  listSessions,
  listSkills,
  getSkill,
  setSkillEnabled,
  installSkill,
  uninstallSkill,
  reloadPlugins,
  getPlugins,
  attachSkillPath,
  detachSkillPath,
  attachToolPackage,
  detachToolPackage,
  listTools,
  listCommands,
  getMemory,
  saveMemory,
  saveInbox,
  previewFile,
  setToolEnabled,
  deleteSession,
  getWeb,
  saveWeb,
  getModel,
  saveModel,
  listModels,
  saveModelProfile,
  deleteModel,
  activateModel,
  listPrompts,
  getPrompt,
  savePrompt,
  openPath,
  listWorkspace,
  getMail,
  saveMail,
  listSchedules,
  saveSchedule,
  setScheduleEnabled,
  deleteSchedule,
  tickSchedules,
  getWiki,
  addWiki,
  removeWiki,
  getLinks,
  addLink,
  getDiary,
  writeDiary,
};
