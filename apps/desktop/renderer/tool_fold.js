(function (root) {
  "use strict";

  var LOCATOR_KEYS = ["path", "file", "url", "slug", "query", "pattern", "command", "prompt"];

  function toolLocator(args) {
    var obj = args && typeof args === "object" ? args : {};
    for (var i = 0; i < LOCATOR_KEYS.length; i += 1) {
      var value = obj[LOCATOR_KEYS[i]];
      if (value !== undefined && value !== null && String(value).trim()) {
        return String(value).replace(/\s+/g, " ").trim().slice(0, 80);
      }
    }
    return "";
  }

  function isEmptyLookup(text) {
    return /^\((?:no matches|no hits|empty(?: fanout)?|the index is empty[^)]*)\)$/i.test(
      String(text || "").trim(),
    );
  }

  function toolLabel(name, args, done, failed, missed) {
    var status = failed ? "失败" : missed ? "未命中" : done ? "完成" : "运行";
    var tool = String(name || "工具");
    var locator = toolLocator(args);
    return locator ? status + " · " + tool + " · " + locator : status + " · " + tool;
  }

  function stackOpen(runningCount, userToggled, currentOpen, prevRunning) {
    var running = Number(runningCount) > 0;
    var wasRunning = Number(prevRunning || 0) > 0;
    if (running && !wasRunning) {
      return true;
    }
    if (userToggled) {
      return Boolean(currentOpen);
    }
    return running;
  }

  function clipResult(text, limit) {
    var body = String(text || "");
    var cap = limit || 2000;
    if (body.length <= cap) {
      return body;
    }
    return body.slice(0, cap) + "\n…";
  }

  var api = {
    toolLocator: toolLocator,
    toolLabel: toolLabel,
    clipResult: clipResult,
    isEmptyLookup: isEmptyLookup,
    stackOpen: stackOpen,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.wittyToolFold = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
