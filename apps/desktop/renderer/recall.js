"use strict";

/** Recalled hit labels. Shell-only; uses protocol score, does not invent hits. */
(function (root) {
  const DEFAULT_COVER_MIN = 5;

  function recallScore(hit) {
    const score = Number(hit && hit.score);
    return Number.isFinite(score) && score > 0 ? Math.trunc(score) : 0;
  }

  function recallCoverMin(coverMin) {
    const min = Number(coverMin);
    return Number.isFinite(min) && min > 0 ? min : DEFAULT_COVER_MIN;
  }

  function recallIsWeak(hit, coverMin) {
    const score = recallScore(hit);
    return Boolean(score) && score < recallCoverMin(coverMin);
  }

  function recallScoreMark(hit, coverMin) {
    const score = recallScore(hit);
    if (!score) {
      return "";
    }
    return score >= recallCoverMin(coverMin) ? `覆盖 · ${score}` : `弱 · ${score}`;
  }

  function recallIsArchive(hit) {
    const layer = String((hit && hit.layer) || "");
    const slug = String((hit && (hit.slug || hit.id || hit.locator)) || "").trim();
    return layer === "archive" || slug.startsWith("archive/");
  }

  function recallHitsLayer(hits) {
    let archive = false;
    let working = false;
    (hits || []).forEach((hit) => {
      const slug = String((hit && (hit.slug || hit.id)) || "").trim();
      if (!slug) {
        return;
      }
      if (recallIsArchive(hit)) {
        archive = true;
      } else {
        working = true;
      }
    });
    if (archive && working) {
      return "mixed";
    }
    if (archive) {
      return "archive";
    }
    return "working";
  }

  function recallLayerMark(hit) {
    return recallIsArchive(hit) ? "旧笔记" : "";
  }

  function recallHitCaption(hit, coverMin) {
    const title = String((hit && (hit.title || hit.slug)) || "").trim();
    const mark = recallScoreMark(hit, coverMin);
    const layer = recallLayerMark(hit);
    const found = recallRelocatedPaths(hit).join("、");
    const parts = [title, mark, layer];
    if (hit && hit.loaded) {
      parts.push("已读");
    }
    if (found) {
      parts.push(`已定位 ${found}`);
    }
    return parts.filter(Boolean).join(" · ");
  }

  const PATHISH =
    /(?:[\w.-]+\/[\w./\\-]+)|(?:[./~][\w./\\-]+)|(?:[\w-]+\.[A-Za-z0-9]{1,8})|\b(?:README|LICENSE|TODO|CONTRIBUTING|NOTICE|CHANGELOG|AGENTS)\b/gi;

  function recallExcerptPaths(text) {
    const found = String(text || "").match(PATHISH) || [];
    const seen = new Set();
    const out = [];
    found.forEach((item) => {
      const key = String(item).toLowerCase();
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      out.push(item);
    });
    return out;
  }

  function recallRelocatedPaths(hit) {
    const listed = Array.isArray(hit && hit.relocated) ? hit.relocated : [];
    const out = [];
    const seen = new Set();
    listed.forEach((row) => {
      const dest = String((row && (row.to || row.found)) || "").trim();
      const key = dest.toLowerCase();
      if (!dest || seen.has(key)) {
        return;
      }
      seen.add(key);
      out.push(dest);
    });
    return out;
  }

  function recallPreferPaths(paths) {
    const rows = (paths || []).map((item) => String(item || "").trim()).filter(Boolean);
    const norm = (item) => item.replace(/\\/g, "/").toLowerCase();
    return rows.filter((item) => {
      const name = norm(item).split("/").pop();
      return !rows.some((other) => {
        if (other === item) {
          return false;
        }
        const path = norm(other);
        return path !== norm(item) && (path.endsWith(`/${name}`) || path.endsWith(`\\${name}`));
      });
    });
  }

  function recallReadHint(item) {
    const relocated = recallRelocatedPaths(item);
    const extracted = recallExcerptPaths(item && (item.excerpt || item.text || ""));
    const paths = recallPreferPaths(relocated.length ? relocated : extracted).slice(0, 3);
    if (!paths.length) {
      return "read";
    }
    if (paths.length === 1) {
      return `read ${paths[0]}`;
    }
    return `read ${paths.join(" and ")}`;
  }

  const api = {
    DEFAULT_COVER_MIN,
    recallScore,
    recallIsWeak,
    recallScoreMark,
    recallIsArchive,
    recallHitsLayer,
    recallLayerMark,
    recallHitCaption,
    recallExcerptPaths,
    recallRelocatedPaths,
    recallPreferPaths,
    recallReadHint,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.wittyRecall = api;
})(typeof window !== "undefined" ? window : globalThis);
