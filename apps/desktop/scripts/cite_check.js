"use strict";

const assert = require("assert");
const path = require("path");
const cite = require(path.join(__dirname, "..", "renderer", "cite.js"));

const rows = [
  { kind: "tool", source: "read", locator: "src/note.txt", ok: true },
  { kind: "browse", source: "memory_status", locator: "prefs", ok: true },
  { kind: "memory", source: "memory_read", locator: "decisions", excerpt: "OAuth2", ok: true, score: 3 },
];
const visible = cite.visibleCites(rows);
assert.strictEqual(visible.length, 2);
assert.strictEqual(cite.citeNeedle(rows[0]), "src/note.txt");
assert.deepStrictEqual(cite.citeNeedles(rows[0]), ["src/note.txt"]);
assert.strictEqual(cite.citeLabel(rows[0]), "note.txt");
assert.strictEqual(cite.citeChipText(rows[0]), "read · note.txt");
assert.strictEqual(cite.citeChipText(rows[2]), "memory_read · decisions · 弱 · 3");
assert.strictEqual(
  cite.citeChipText({
    kind: "memory",
    source: "memory_read",
    locator: "archive/prefs",
    excerpt: "喜欢吃桃子",
    ok: true,
    score: 6,
    layer: "archive",
  }),
  "memory_read · prefs · 覆盖 · 6 · 旧笔记",
);
assert.strictEqual(cite.citeNeedle({ locator: "ab" }), "");
assert.strictEqual(cite.citeNeedle(rows[2]), "OAuth2");
assert.deepStrictEqual(cite.citeNeedles(rows[2]), ["OAuth2", "decisions"]);
assert.strictEqual(
  cite.citeNeedle({ kind: "memory", source: "memory_read", locator: "decisions", excerpt: "2025-01-01 OAuth2" }),
  "OAuth2",
);
assert.strictEqual(cite.visibleCites([{ kind: "browse", locator: "prefs" }]).length, 0);
assert.strictEqual(
  cite.visibleCites([{ kind: "tool", source: "grep", locator: "zzz", ok: false, excerpt: "(no matches)" }]).length,
  0,
);
const many = Array.from({ length: 8 }, (_, index) => ({
  kind: "tool",
  source: "read",
  locator: `f${index}.txt`,
  ok: true,
  excerpt: "x".repeat(90),
}));
assert.strictEqual(cite.citePreview(many).length, 6);
assert.strictEqual(cite.citeRest(many).length, 2);
assert.strictEqual(cite.citeMoreLabel(2), "还有 2 条");
assert.strictEqual(cite.evidencePreview(many).length, 4);
assert.strictEqual(cite.evidenceRest(many).length, 4);
assert.strictEqual(cite.evidenceMoreLabel(4), "其余 4 条");
assert.strictEqual(cite.excerptNeedsFold("short"), false);
assert.strictEqual(cite.excerptNeedsFold("x".repeat(90)), true);
assert.strictEqual(cite.excerptNeedsFold("line\nline"), true);
assert.ok(cite.clipExcerpt("x".repeat(90)).endsWith("…"));
assert.ok(cite.clipExcerpt("x".repeat(90)).length < 90);
process.stdout.write("cite-ok\n");
