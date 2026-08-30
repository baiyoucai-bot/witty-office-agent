"use strict";

const assert = require("assert");
const path = require("path");
const md = require(path.join(__dirname, "..", "renderer", "markdown.js"));

function html(source) {
  return md.render(source);
}

function assertFence(source, needle) {
  const out = html(source);
  assert.ok(out.includes("md-code") || out.includes("md-diagram"), source);
  assert.ok(out.includes(needle), `${needle} in ${out}`);
  assert.ok(!out.includes("```"), `raw fence leaked: ${out}`);
}

assertFence("```python title=\"demo\"\nprint(1)\n```", "print(1)");
assertFence("``` python\nx = 1\n```", "x = 1");
assertFence("```c++\nint x = 0;\n```", "int x = 0;");
assertFence("```c#\nvar x = 1;\n```", "var x = 1;");
assertFence("~~~js\nconst a = 1;\n~~~", "const a = 1;");
assertFence("    ```bash\necho hi\n    ```", "echo hi");
assertFence("代码如下：```js\n1 + 1\n```", "1 + 1");
const nested = html("````md\n```js\n1\n```\n````");
assert.ok(nested.includes("md-code") && nested.includes("```js"));
assert.ok(html("｀｀｀python\npass\n｀｀｀").includes("pass"));

const graph = html("```mermaid\ngraph TD\nA[开始] --> B[结束]\n```");
assert.ok(graph.includes("md-diagram") && graph.includes("<svg") && graph.includes("开始"));

const pie = html('```mermaid\npie title Pets\n"Dogs" : 386\n"Cats" : 85\n```');
assert.ok(pie.includes("md-diagram") && pie.includes("Dogs"));

const seq = html("```mermaid\nsequenceDiagram\nAlice->>Bob: Hello\nBob-->>Alice: Hi\n```");
assert.ok(seq.includes("md-diagram") && seq.includes("Hello"));

const xy = html('```mermaid\nxychart-beta\n    title "Sales"\n    x-axis [Q1, Q2, Q3]\n    bar [10, 20, 15]\n    line [5, 18, 12]\n```');
assert.ok(xy.includes("md-diagram") && xy.includes("Sales") && xy.includes("polyline"));

const table = html("| a | b |\n| --- | --- |\n| 1 | 2 |");
assert.ok(table.includes("<table") && table.includes("<th>"));

const unclosed = html("```python\nprint(1)");
assert.ok(unclosed.includes("print(1)") && unclosed.includes("md-code"));

// 本地图片出占位符（data-witty-src），不能直接当 src——CSP 禁 file:。
const localImg = html("画好了 ![对比图](sandbox/plot.png)");
assert.ok(localImg.includes('data-witty-src="sandbox/plot.png"'), localImg);
assert.ok(localImg.includes("md-img-pending"), localImg);
assert.ok(!/\ssrc=/.test(localImg), localImg);

const absImg = html("![图](/tmp/out/趋势.PNG)");
assert.ok(absImg.includes('data-witty-src="/tmp/out/趋势.PNG"'), absImg);

const fileTokenImg = html("![x](file:.witty-inbox/a.webp)");
assert.ok(fileTokenImg.includes('data-witty-src="file:.witty-inbox/a.webp"'), fileTokenImg);

// http(s) 图仍旧直接上 src；非图片后缀的本地路径不出 img。
const httpImg = html("![x](https://example.com/a.png)");
assert.ok(httpImg.includes('src="https://example.com/a.png"') && !httpImg.includes("data-witty-src"), httpImg);
const notImg = html("![x](notes.txt)");
assert.ok(!notImg.includes("<img"), notImg);

process.stdout.write("markdown-ok\n");
