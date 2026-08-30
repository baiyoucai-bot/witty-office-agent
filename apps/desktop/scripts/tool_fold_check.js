"use strict";

const assert = require("assert");
const path = require("path");
const fold = require(path.join(__dirname, "..", "renderer", "tool_fold.js"));

assert.strictEqual(fold.toolLocator({ path: "note.txt" }), "note.txt");
assert.strictEqual(fold.toolLocator({ pattern: "foo bar" }), "foo bar");
assert.strictEqual(fold.toolLocator({}), "");
assert.strictEqual(fold.toolLabel("read", { path: "note.txt" }, true), "完成 · read · note.txt");
assert.strictEqual(fold.toolLabel("bash", { command: "ls" }, false), "运行 · bash · ls");
assert.strictEqual(fold.toolLabel("read", { path: "missing.txt" }, true, true), "失败 · read · missing.txt");
assert.strictEqual(fold.toolLabel("grep", { pattern: "zzz" }, true, false, true), "未命中 · grep · zzz");
assert.ok(fold.isEmptyLookup("(no matches)"));
assert.ok(fold.isEmptyLookup("(no hits)"));
assert.ok(!fold.isEmptyLookup("1|hello"));
assert.ok(fold.clipResult("x".repeat(2500)).endsWith("…"));
assert.strictEqual(fold.clipResult("short"), "short");
assert.strictEqual(fold.stackOpen(1, false, false), true);
assert.strictEqual(fold.stackOpen(0, false, true), false);
assert.strictEqual(fold.stackOpen(0, true, true), true);
assert.strictEqual(fold.stackOpen(2, true, false, 2), false);
assert.strictEqual(fold.stackOpen(1, true, false, 0), true);
assert.strictEqual(fold.stackOpen(0, true, true, 1), true);
process.stdout.write("tool-fold-ok\n");
