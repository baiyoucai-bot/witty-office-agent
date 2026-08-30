"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("witty", {
  health: () => ipcRenderer.invoke("api:health"),
  createSession: (options) => ipcRenderer.invoke("api:createSession", options),
  forkSession: (sessionId) => ipcRenderer.invoke("api:forkSession", sessionId),
  sendPrompt: (sessionId, prompt) => ipcRenderer.invoke("api:sendPrompt", sessionId, prompt),
  startPrompt: (sessionId, prompt, approvalMode, thinkLevel) =>
    ipcRenderer.invoke("api:startPrompt", sessionId, prompt, approvalMode, thinkLevel),
  abortSession: (sessionId) => ipcRenderer.invoke("api:abortSession", sessionId),
  steerSession: (sessionId, text) => ipcRenderer.invoke("api:steerSession", sessionId, text),
  getRun: (sessionId) => ipcRenderer.invoke("api:getRun", sessionId),
  submitApproval: (sessionId, toolCallId, decision) =>
    ipcRenderer.invoke("api:submitApproval", sessionId, toolCallId, decision),
  submitAnswer: (sessionId, answers) => ipcRenderer.invoke("api:submitAnswer", sessionId, answers),
  getMessages: (sessionId) => ipcRenderer.invoke("api:getMessages", sessionId),
  listSessions: (projectId, agentId) => ipcRenderer.invoke("api:listSessions", projectId, agentId),
  listSkills: (projectId, agentId) => ipcRenderer.invoke("api:listSkills", projectId, agentId),
  getSkill: (name) => ipcRenderer.invoke("api:getSkill", name),
  setSkillEnabled: (name, enabled, projectId, agentId) =>
    ipcRenderer.invoke("api:setSkillEnabled", name, enabled, projectId, agentId),
  installSkill: (body) => ipcRenderer.invoke("api:installSkill", body),
  uninstallSkill: (name, projectId, agentId) =>
    ipcRenderer.invoke("api:uninstallSkill", name, projectId, agentId),
  reloadPlugins: () => ipcRenderer.invoke("api:reloadPlugins"),
  getPlugins: () => ipcRenderer.invoke("api:getPlugins"),
  attachSkillPath: (path) => ipcRenderer.invoke("api:attachSkillPath", path),
  detachSkillPath: (path) => ipcRenderer.invoke("api:detachSkillPath", path),
  attachToolPackage: (name, path) => ipcRenderer.invoke("api:attachToolPackage", name, path),
  detachToolPackage: (name) => ipcRenderer.invoke("api:detachToolPackage", name),
  pickSkill: () => ipcRenderer.invoke("shell:pickSkill"),
  confirm: (message) => ipcRenderer.invoke("shell:confirm", message),
  listTools: (projectId, agentId) => ipcRenderer.invoke("api:listTools", projectId, agentId),
  setToolEnabled: (name, enabled, projectId, agentId) =>
    ipcRenderer.invoke("api:setToolEnabled", name, enabled, projectId, agentId),
  listCommands: (sessionId) => ipcRenderer.invoke("api:listCommands", sessionId),
  getMemory: (projectId, agentId, workspaceDir, recall) =>
    ipcRenderer.invoke("api:getMemory", projectId, agentId, workspaceDir, recall),
  saveMemory: (body) => ipcRenderer.invoke("api:saveMemory", body),
  saveInbox: (body) => ipcRenderer.invoke("api:saveInbox", body),
  previewFile: (workspaceDir, target) => ipcRenderer.invoke("api:previewFile", workspaceDir, target),
  pickFiles: () => ipcRenderer.invoke("shell:pickFiles"),
  pickDirectory: () => ipcRenderer.invoke("shell:pickDirectory"),
  listWorkspace: (dir) => ipcRenderer.invoke("shell:listWorkspace", dir),
  openPath: (target) => ipcRenderer.invoke("shell:openPath", target),
  statPaths: (targets) => ipcRenderer.invoke("shell:statPaths", targets),
  revealPath: (target) => ipcRenderer.invoke("shell:revealPath", target),
  deleteSession: (sessionId, projectId, agentId) =>
    ipcRenderer.invoke("api:deleteSession", sessionId, projectId, agentId),
  getWeb: () => ipcRenderer.invoke("api:getWeb"),
  saveWeb: (body) => ipcRenderer.invoke("api:saveWeb", body),
  getModel: (projectId, agentId) => ipcRenderer.invoke("api:getModel", projectId, agentId),
  saveModel: (body) => ipcRenderer.invoke("api:saveModel", body),
  listModels: (projectId, agentId) => ipcRenderer.invoke("api:listModels", projectId, agentId),
  saveModelProfile: (body) => ipcRenderer.invoke("api:saveModelProfile", body),
  deleteModel: (name, projectId, agentId) =>
    ipcRenderer.invoke("api:deleteModel", name, projectId, agentId),
  activateModel: (name, projectId, agentId) =>
    ipcRenderer.invoke("api:activateModel", name, projectId, agentId),
  startServer: () => ipcRenderer.invoke("api:startServer"),
  apiBase: () => ipcRenderer.invoke("api:base"),
  listPrompts: () => ipcRenderer.invoke("api:listPrompts"),
  getPrompt: (name) => ipcRenderer.invoke("api:getPrompt", name),
  savePrompt: (name, text) => ipcRenderer.invoke("api:savePrompt", name, text),
  getMail: (projectId, agentId) => ipcRenderer.invoke("api:getMail", projectId, agentId),
  saveMail: (body) => ipcRenderer.invoke("api:saveMail", body),
  listSchedules: (projectId, agentId) => ipcRenderer.invoke("api:listSchedules", projectId, agentId),
  saveSchedule: (body) => ipcRenderer.invoke("api:saveSchedule", body),
  setScheduleEnabled: (name, enabled, projectId, agentId) =>
    ipcRenderer.invoke("api:setScheduleEnabled", name, enabled, projectId, agentId),
  deleteSchedule: (name, projectId, agentId) =>
    ipcRenderer.invoke("api:deleteSchedule", name, projectId, agentId),
  tickSchedules: () => ipcRenderer.invoke("api:tickSchedules"),
  getWiki: (workspaceDir) => ipcRenderer.invoke("api:getWiki", workspaceDir),
  addWiki: (body) => ipcRenderer.invoke("api:addWiki", body),
  removeWiki: (sourceId, workspaceDir) => ipcRenderer.invoke("api:removeWiki", sourceId, workspaceDir),
  getLinks: (query) => ipcRenderer.invoke("api:getLinks", query),
  addLink: (body) => ipcRenderer.invoke("api:addLink", body),
  getDiary: (day, list) => ipcRenderer.invoke("api:getDiary", day, list),
  writeDiary: (text, day) => ipcRenderer.invoke("api:writeDiary", text, day),
});
