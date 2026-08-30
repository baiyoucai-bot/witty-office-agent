"use strict";

const assert = require("assert");
const path = require("path");
const recall = require(path.join(__dirname, "..", "renderer", "recall.js"));

assert.strictEqual(recall.recallScore({ score: 7 }), 7);
assert.strictEqual(recall.recallScore({ score: "3" }), 3);
assert.strictEqual(recall.recallScore({}), 0);
assert.strictEqual(recall.recallScoreMark({ score: 7 }), "覆盖 · 7");
assert.strictEqual(recall.recallScoreMark({ score: 4 }), "弱 · 4");
assert.strictEqual(recall.recallScoreMark({ score: 5 }), "覆盖 · 5");
assert.strictEqual(recall.recallScoreMark({ score: 3 }, 5), "弱 · 3");
assert.strictEqual(recall.recallScoreMark({}), "");
assert.strictEqual(recall.recallHitCaption({ title: "领域要点", score: 4 }), "领域要点 · 弱 · 4");
assert.strictEqual(recall.recallHitCaption({ title: "个人偏好", score: 7 }), "个人偏好 · 覆盖 · 7");
assert.strictEqual(recall.recallIsWeak({ score: 3 }), true);
assert.strictEqual(recall.recallIsWeak({ score: 7 }), false);
assert.strictEqual(recall.recallIsWeak({}), false);
assert.deepStrictEqual(recall.recallExcerptPaths("2026-08-17 read note.txt: alpha-source-line"), [
  "note.txt",
]);
assert.deepStrictEqual(recall.recallExcerptPaths("read notes/solo.txt: unique-body"), ["notes/solo.txt"]);
assert.deepStrictEqual(recall.recallExcerptPaths("OAuth2 采用决定"), []);
assert.strictEqual(
  recall.recallReadHint({ excerpt: "read note.txt: alpha-source-line", score: 3 }),
  "read note.txt",
);
assert.strictEqual(recall.recallReadHint({ excerpt: "OAuth2", score: 3 }), "read");
assert.strictEqual(
  recall.recallReadHint({ excerpt: "read note.txt: alpha and read other.py: leftover", score: 3 }),
  "read note.txt and other.py",
);
assert.deepStrictEqual(recall.recallRelocatedPaths({ relocated: [{ from: "solo.txt", to: "notes/solo.txt" }] }), [
  "notes/solo.txt",
]);
assert.strictEqual(
  recall.recallReadHint({
    excerpt: "read solo.txt: unique-body",
    score: 3,
    relocated: [{ from: "solo.txt", to: "notes/solo.txt" }],
  }),
  "read notes/solo.txt",
);
assert.strictEqual(
  recall.recallReadHint({ excerpt: "read solo.txt and notes/solo.txt: unique-body", score: 3 }),
  "read notes/solo.txt",
);
assert.strictEqual(
  recall.recallHitCaption({
    title: "solo-txt",
    score: 3,
    relocated: [{ from: "solo.txt", to: "notes/solo.txt" }],
  }),
  "solo-txt · 弱 · 3 · 已定位 notes/solo.txt",
);
assert.strictEqual(recall.recallHitCaption({ title: "prefs", loaded: true }), "prefs · 已读");
assert.strictEqual(recall.recallIsArchive({ slug: "archive/prefs", layer: "archive" }), true);
assert.strictEqual(recall.recallIsArchive({ locator: "archive/prefs" }), true);
assert.strictEqual(recall.recallIsArchive({ slug: "prefs" }), false);
assert.strictEqual(recall.recallLayerMark({ slug: "archive/prefs" }), "旧笔记");
assert.strictEqual(recall.recallLayerMark({ locator: "archive/prefs" }), "旧笔记");
assert.strictEqual(recall.recallHitsLayer([{ slug: "prefs" }, { slug: "archive/prefs" }]), "mixed");
assert.strictEqual(recall.recallHitsLayer([{ slug: "archive/prefs", layer: "archive" }]), "archive");
assert.strictEqual(
  recall.recallHitCaption({ title: "个人偏好", slug: "archive/prefs", layer: "archive", score: 6 }),
  "个人偏好 · 覆盖 · 6 · 旧笔记",
);
process.stdout.write("recall-ok\n");
