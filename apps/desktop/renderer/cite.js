"use strict";

/** Citation chips from protocol evidence. Shell-only; does not invent sources. */
(function (root) {
  function visibleCites(items) {
    return (Array.isArray(items) ? items : []).filter(
      (item) =>
        item &&
        item.kind !== "browse" &&
        item.ok !== false &&
        (item.locator || item.source),
    );
  }

  function stripCiteDate(text) {
    return String(text || "")
      .replace(/^\d{4}-\d{2}-\d{2}\s+/, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isMemoryCite(item) {
    const kind = String((item && item.kind) || "");
    const source = String((item && item.source) || "");
    return kind === "memory" || source === "memory_read";
  }

  function citeNeedles(item) {
    const needles = [];
    const push = (raw) => {
      const text = stripCiteDate(raw).slice(0, 80);
      if (text.length >= 3 && !needles.includes(text)) {
        needles.push(text);
      }
    };
    if (isMemoryCite(item)) {
      const excerpt = String((item && item.excerpt) || "");
      push(excerpt);
      const tokens = stripCiteDate(excerpt).match(/[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}/g) || [];
      tokens
        .slice()
        .sort((a, b) => b.length - a.length)
        .forEach(push);
    }
    push((item && item.locator) || "");
    return needles;
  }

  function citeNeedle(item) {
    return citeNeedles(item)[0] || "";
  }

  function citeLabel(item) {
    const locator = String((item && item.locator) || "").trim();
    const source = String((item && item.source) || "").trim();
    const parts = locator.split(/[/\\]/).filter(Boolean);
    const name = parts[parts.length - 1] || locator || source || "source";
    return name.slice(0, 48);
  }

  function recallMark(item) {
    if (!isMemoryCite(item)) {
      return "";
    }
    let api = root.wittyRecall;
    if (!api && typeof require === "function") {
      try {
        api = require("./recall.js");
      } catch (_error) {
        api = null;
      }
    }
    const score = api && typeof api.recallScoreMark === "function" ? api.recallScoreMark(item) : "";
    const layer = api && typeof api.recallLayerMark === "function" ? api.recallLayerMark(item) : "";
    return [score, layer].filter(Boolean).join(" · ");
  }

  function citeChipText(item) {
    const label = citeLabel(item);
    const source = String((item && item.source) || "").trim();
    const base = source && label && label !== source ? `${source} · ${label}` : label || source;
    const mark = recallMark(item);
    return mark ? `${base} · ${mark}` : base;
  }

  const CITE_PREVIEW = 6;
  const EVIDENCE_PREVIEW = 4;
  const EXCERPT_PREVIEW = 72;

  function citePreview(items) {
    return visibleCites(items).slice(0, CITE_PREVIEW);
  }

  function citeRest(items) {
    return visibleCites(items).slice(CITE_PREVIEW);
  }

  function citeMoreLabel(count) {
    const n = Number(count) || 0;
    return n > 0 ? `还有 ${n} 条` : "";
  }

  function clipExcerpt(text, limit) {
    const compact = String(text || "").replace(/\s+/g, " ").trim();
    const cap = Number(limit) > 0 ? Number(limit) : EXCERPT_PREVIEW;
    if (compact.length <= cap) {
      return compact;
    }
    return compact.slice(0, cap - 1) + "…";
  }

  function excerptNeedsFold(text, limit) {
    const raw = String(text || "").trim();
    if (!raw) {
      return false;
    }
    if (raw.includes("\n")) {
      return true;
    }
    const cap = Number(limit) > 0 ? Number(limit) : EXCERPT_PREVIEW;
    return raw.length > cap;
  }

  function evidencePreview(items) {
    return (Array.isArray(items) ? items : []).slice(0, EVIDENCE_PREVIEW);
  }

  function evidenceRest(items) {
    return (Array.isArray(items) ? items : []).slice(EVIDENCE_PREVIEW);
  }

  function evidenceMoreLabel(count) {
    const n = Number(count) || 0;
    return n > 0 ? `其余 ${n} 条` : "";
  }

  const api = {
    CITE_PREVIEW,
    EVIDENCE_PREVIEW,
    EXCERPT_PREVIEW,
    visibleCites,
    citeNeedles,
    citeNeedle,
    citeLabel,
    citeChipText,
    citePreview,
    citeRest,
    citeMoreLabel,
    clipExcerpt,
    excerptNeedsFold,
    evidencePreview,
    evidenceRest,
    evidenceMoreLabel,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.wittyCite = api;
})(typeof window !== "undefined" ? window : globalThis);
