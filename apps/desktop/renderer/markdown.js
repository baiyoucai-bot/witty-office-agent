"use strict";

/** Small Markdown renderer for chat. No extra runtime. */
(function (root) {
  const COLORS = ["#5b7cfa", "#3aa76d", "#d97706", "#c2410c", "#7c3aed", "#0f766e"];

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function normalizeSource(source) {
    return String(source || "")
      .replace(/\uFEFF/g, "")
      .replace(/｀/g, "`")
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");
  }

  function inline(text) {
    const raw = String(text || "");
    if (raw.length > 4000) {
      return escapeHtml(raw);
    }
    let out = escapeHtml(raw);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/!\[([^\]]*)\]\((https?:[^)\s]+)\)/g, '<img alt="$1" src="$2" />');
    // 本地图片（绝对路径 / 工作区相对 / sandbox/… / file:…）不能直接当 src：
    // CSP 禁 file:，改出占位符，由 app.js 找后端换成 data: URL 再点亮。
    out = out.replace(
      /!\[([^\]]*)\]\(((?:file:)?[^)\s]+\.(?:png|jpe?g|gif|webp|bmp))\)/gi,
      '<img class="md-img-pending" alt="$1" data-witty-src="$2" />',
    );
    out = out.replace(
      /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>',
    );
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^\w*])\*([^*\n]{1,400})\*(?!\*)/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^\w_])_([^_\n]{1,400})_(?!_)/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~]{1,400})~~/g, "<del>$1</del>");
    return out;
  }

  function looksLikeTable(lines) {
    if (lines.length < 2) {
      return false;
    }
    return (
      lines[0].includes("|") &&
      /^\s*\|?[\s:|-]+\|[\s:|-]+/.test(lines[1])
    );
  }

  function renderTable(lines) {
    const cells = (line) =>
      line
        .trim()
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map((cell) => cell.trim());
    const head = cells(lines[0]);
    const body = lines.slice(2).map(cells);
    const thead = `<thead><tr>${head.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead>`;
    const tbody = `<tbody>${body
      .map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`)
      .join("")}</tbody>`;
    return `<div class="md-table"><table>${thead}${tbody}</table></div>`;
  }

  function renderList(items, ordered) {
    const tag = ordered ? "ol" : "ul";
    const body = items
      .map((item) => {
        const task = item.match(/^\[( |x|X)\]\s+([\s\S]*)$/);
        if (task) {
          const checked = task[1].toLowerCase() === "x" ? " checked" : "";
          return `<li class="task"><input type="checkbox" disabled${checked} /> ${inline(task[2])}</li>`;
        }
        return `<li>${inline(item)}</li>`;
      })
      .join("");
    return `<${tag}>${body}</${tag}>`;
  }

  function parseFenceOpen(line) {
    const match = String(line || "").match(/^\s*(`{3,}|~{3,})([^\n`~]*)$/);
    if (!match) {
      return null;
    }
    const mark = match[1][0];
    const length = match[1].length;
    const info = String(match[2] || "").trim();
    const lang = (info.split(/[\s{,]/)[0] || "").replace(/[^A-Za-z0-9_+#.-]/g, "");
    return { mark, length, lang };
  }

  function splitFenceLine(line) {
    const direct = parseFenceOpen(line);
    if (direct) {
      return { prefix: "", fence: direct };
    }
    const match = String(line || "").match(/^(.*?)(`{3,}|~{3,})([^\n]*)$/);
    if (!match) {
      return null;
    }
    const prefix = match[1];
    if (!/[:：]\s*$/.test(prefix) && !/(代码|如下|示例|example|code)\s*$/i.test(prefix.trim())) {
      return null;
    }
    const fence = parseFenceOpen(match[2] + match[3]);
    if (!fence) {
      return null;
    }
    return { prefix: prefix.trim(), fence };
  }

  function isFenceClose(line, open) {
    const match = String(line || "").match(/^\s*(`{3,}|~{3,})(.*)$/);
    if (!match || !open) {
      return false;
    }
    if (match[1][0] !== open.mark || match[1].length < open.length) {
      return false;
    }
    const rest = String(match[2] || "").trim();
    return !rest;
  }

  function codeBlock(lang, body) {
    const label = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
    return `<pre class="md-code"${label}><code>${escapeHtml(body)}</code></pre>`;
  }

  function wrapDiagram(svg) {
    return svg ? `<div class="md-diagram">${svg}</div>` : "";
  }

  function parseNodeToken(token) {
    const match = String(token || "")
      .trim()
      .match(/^([A-Za-z][\w]*)(\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}|"([^"]*)")?$/);
    if (!match) {
      return null;
    }
    return { id: match[1], label: match[3] || match[4] || match[5] || match[6] || match[1] };
  }

  function mermaidGraph(src) {
    const lines = String(src || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (!lines.length) {
      return "";
    }
    const head = lines[0];
    const dirMatch = head.match(/^(?:graph|flowchart)\s+(TD|TB|BT|DT|LR|RL)\b/i);
    const plain = /^(?:graph|flowchart)\b/i.test(head);
    if (!dirMatch && !plain) {
      return "";
    }
    let dir = dirMatch ? dirMatch[1].toUpperCase() : "TD";
    if (dir === "TB" || dir === "DT") {
      dir = "TD";
    }
    const nodes = new Map();
    const edges = [];
    const remember = (node) => {
      if (!node) {
        return;
      }
      if (!nodes.has(node.id) || node.label !== node.id) {
        nodes.set(node.id, node.label);
      }
    };
    const edgeRe =
      /^([A-Za-z][\w]*(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|"[^"]*")?)\s*(-->|---|==>|-\.->)\s*(?:\|([^|]+)\|)?\s*([A-Za-z][\w]*(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|"[^"]*")?)\s*$/;
    const labeledRe =
      /^([A-Za-z][\w]*(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\})?)\s*--\s*([^-]+?)\s*-->\s*([A-Za-z][\w]*(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\})?)\s*$/;
    for (const line of lines.slice(1)) {
      if (/^subgraph\b/i.test(line) || /^end$/i.test(line) || line.startsWith("%%")) {
        continue;
      }
      const labeled = line.match(labeledRe);
      if (labeled) {
        const from = parseNodeToken(labeled[1]);
        const to = parseNodeToken(labeled[3]);
        remember(from);
        remember(to);
        if (from && to) {
          edges.push({ from: from.id, to: to.id, label: labeled[2].trim() });
        }
        continue;
      }
      const edge = line.match(edgeRe);
      if (edge) {
        const from = parseNodeToken(edge[1]);
        const to = parseNodeToken(edge[4]);
        remember(from);
        remember(to);
        if (from && to) {
          edges.push({ from: from.id, to: to.id, label: (edge[3] || "").trim() });
        }
        continue;
      }
      const lone = parseNodeToken(line);
      if (lone) {
        remember(lone);
      }
    }
    if (!nodes.size) {
      return "";
    }
    const ids = Array.from(nodes.keys());
    const incoming = new Map(ids.map((id) => [id, 0]));
    edges.forEach((edge) => {
      incoming.set(edge.to, (incoming.get(edge.to) || 0) + 1);
    });
    const rank = new Map();
    const queue = ids.filter((id) => !incoming.get(id));
    if (!queue.length) {
      queue.push(ids[0]);
    }
    queue.forEach((id) => rank.set(id, 0));
    const seen = new Set(queue);
    while (queue.length) {
      const current = queue.shift();
      const base = rank.get(current) || 0;
      edges
        .filter((edge) => edge.from === current)
        .forEach((edge) => {
          const next = Math.max(rank.get(edge.to) || 0, base + 1);
          rank.set(edge.to, next);
          if (!seen.has(edge.to)) {
            seen.add(edge.to);
            queue.push(edge.to);
          }
        });
    }
    ids.forEach((id) => {
      if (!rank.has(id)) {
        rank.set(id, 0);
      }
    });
    const buckets = new Map();
    ids.forEach((id) => {
      const key = rank.get(id) || 0;
      if (!buckets.has(key)) {
        buckets.set(key, []);
      }
      buckets.get(key).push(id);
    });
    const vertical = dir === "TD" || dir === "BT";
    const boxW = 120;
    const boxH = 40;
    const gapX = 36;
    const gapY = 48;
    const pos = new Map();
    Array.from(buckets.keys())
      .sort((a, b) => a - b)
      .forEach((key) => {
        const row = buckets.get(key) || [];
        row.forEach((id, index) => {
          const x = vertical ? index * (boxW + gapX) : key * (boxW + gapX);
          const y = vertical ? key * (boxH + gapY) : index * (boxH + gapY);
          pos.set(id, { x: x + 16, y: y + 16 });
        });
      });
    let maxX = 0;
    let maxY = 0;
    pos.forEach((point) => {
      maxX = Math.max(maxX, point.x + boxW);
      maxY = Math.max(maxY, point.y + boxH);
    });
    const linesSvg = edges
      .map((edge) => {
        const from = pos.get(edge.from);
        const to = pos.get(edge.to);
        if (!from || !to) {
          return "";
        }
        const x1 = from.x + boxW / 2;
        const y1 = vertical ? from.y + boxH : from.y + boxH / 2;
        const x2 = to.x + boxW / 2;
        const y2 = vertical ? to.y : to.y + boxH / 2;
        const midX = (x1 + x2) / 2;
        const midY = (y1 + y2) / 2;
        const label = edge.label
          ? `<text x="${midX}" y="${midY - 4}" text-anchor="middle" class="md-diagram-label">${escapeHtml(edge.label)}</text>`
          : "";
        return `<line class="md-diagram-edge" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" />${label}`;
      })
      .join("");
    const nodesSvg = ids
      .map((id) => {
        const point = pos.get(id);
        const label = String(nodes.get(id) || id);
        return `<g transform="translate(${point.x},${point.y})">
          <rect width="${boxW}" height="${boxH}" rx="8" />
          <text x="${boxW / 2}" y="${boxH / 2 + 4}" text-anchor="middle">${escapeHtml(label)}</text>
        </g>`;
      })
      .join("");
    return `<svg class="md-diagram-svg" viewBox="0 0 ${maxX + 16} ${maxY + 16}" role="img">${linesSvg}${nodesSvg}</svg>`;
  }

  function mermaidPie(src) {
    const lines = String(src || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (!lines.length || !/^pie\b/i.test(lines[0])) {
      return "";
    }
    const titleMatch = lines[0].match(/^pie(?:\s+showdata)?(?:\s+title\s+(.+))?$/i);
    const title = (titleMatch && titleMatch[1]) || "";
    const slices = [];
    lines.slice(1).forEach((line) => {
      const match = line.match(/^"([^"]+)"\s*:\s*([0-9.]+)\s*$/) || line.match(/^([^:]+):\s*([0-9.]+)\s*$/);
      if (match) {
        slices.push({ label: match[1].trim().replace(/^"|"$/g, ""), value: Number(match[2]) });
      }
    });
    if (!slices.length) {
      return "";
    }
    const total = slices.reduce((sum, item) => sum + item.value, 0) || 1;
    const cx = 90;
    const cy = 90;
    const r = 70;
    let angle = -Math.PI / 2;
    const paths = slices
      .map((item, index) => {
        const sweep = (item.value / total) * Math.PI * 2;
        const x1 = cx + r * Math.cos(angle);
        const y1 = cy + r * Math.sin(angle);
        angle += sweep;
        const x2 = cx + r * Math.cos(angle);
        const y2 = cy + r * Math.sin(angle);
        const large = sweep > Math.PI ? 1 : 0;
        return `<path d="M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z" fill="${COLORS[index % COLORS.length]}" />`;
      })
      .join("");
    const legend = slices
      .map((item, index) => {
        const y = 24 + index * 18;
        return `<rect x="190" y="${y - 10}" width="10" height="10" fill="${COLORS[index % COLORS.length]}" />
          <text x="206" y="${y}" class="md-diagram-label">${escapeHtml(item.label)} (${item.value})</text>`;
      })
      .join("");
    const caption = title
      ? `<text x="90" y="12" text-anchor="middle" class="md-diagram-title">${escapeHtml(title)}</text>`
      : "";
    const height = Math.max(180, 20 + slices.length * 18);
    return `<svg class="md-diagram-svg" viewBox="0 0 360 ${height}" role="img">${caption}${paths}${legend}</svg>`;
  }

  function mermaidSequence(src) {
    const lines = String(src || "")
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("%%"));
    if (!lines.length || !/^sequenceDiagram\b/i.test(lines[0])) {
      return "";
    }
    const actors = [];
    const actorIndex = new Map();
    const addActor = (id, label) => {
      const key = String(id || "").trim();
      if (!key) {
        return;
      }
      if (actorIndex.has(key)) {
        if (label && label !== key) {
          actors[actorIndex.get(key)].label = label;
        }
        return;
      }
      actorIndex.set(key, actors.length);
      actors.push({ id: key, label: label || key });
    };
    const msgs = [];
    lines.slice(1).forEach((line) => {
      const named = line.match(/^(?:participant|actor)\s+([^\s]+)(?:\s+as\s+(.+))?$/i);
      if (named) {
        addActor(named[1], (named[2] || named[1]).trim());
        return;
      }
      const msg = line.match(/^([^\s-]+)\s*(-->>|->>|->|-->|--x|-x|-\)\)?)\s*([^\s:]+)\s*:\s*(.+)$/);
      if (msg) {
        addActor(msg[1]);
        addActor(msg[3]);
        msgs.push({
          from: msg[1],
          to: msg[3],
          text: msg[4].trim(),
          dash: String(msg[2]).includes("--"),
        });
      }
    });
    if (!actors.length) {
      return "";
    }
    const colW = 140;
    const top = 44;
    const rowH = 40;
    const width = Math.max(280, actors.length * colW + 20);
    const height = top + 16 + Math.max(msgs.length, 1) * rowH + 28;
    const xOf = (id) => 70 + (actorIndex.get(id) || 0) * colW;
    const heads = actors
      .map((actor, index) => {
        const x = 70 + index * colW;
        return `<rect x="${x - 50}" y="8" width="100" height="28" rx="6" />
          <text x="${x}" y="27" text-anchor="middle">${escapeHtml(actor.label)}</text>
          <line class="md-diagram-edge" x1="${x}" y1="36" x2="${x}" y2="${height - 8}" />`;
      })
      .join("");
    const arrows = msgs
      .map((msg, index) => {
        const y = top + 12 + index * rowH;
        const x1 = xOf(msg.from);
        const x2 = xOf(msg.to);
        const dash = msg.dash ? ' stroke-dasharray="4 3"' : "";
        const dir = x2 >= x1 ? 1 : -1;
        return `<line class="md-diagram-edge" x1="${x1}" y1="${y}" x2="${x2}" y2="${y}"${dash} />
          <polygon points="${x2},${y} ${x2 - 8 * dir},${y - 4} ${x2 - 8 * dir},${y + 4}" />
          <text x="${(x1 + x2) / 2}" y="${y - 6}" text-anchor="middle" class="md-diagram-label">${escapeHtml(msg.text)}</text>`;
      })
      .join("");
    return `<svg class="md-diagram-svg" viewBox="0 0 ${width} ${height}" role="img">${heads}${arrows}</svg>`;
  }

  function splitList(raw) {
    return String(raw || "")
      .split(",")
      .map((item) => item.trim().replace(/^["']|["']$/g, ""))
      .filter((item) => item.length);
  }

  function mermaidXy(src) {
    const lines = String(src || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (!lines.length || !/^xychart(?:-beta)?\b/i.test(lines[0])) {
      return "";
    }
    let title = "";
    let labels = [];
    let ymin = 0;
    let ymax = 0;
    const series = [];
    lines.slice(1).forEach((line) => {
      const titled = line.match(/^title\s+"?([^"]+)"?$/i);
      if (titled) {
        title = titled[1].trim();
        return;
      }
      const xa = line.match(/^x-axis(?:\s+"[^"]*")?\s*\[([^\]]+)\]/i);
      if (xa) {
        labels = splitList(xa[1]);
        return;
      }
      const ya = line.match(/^y-axis(?:\s+"[^"]*")?\s*([0-9.]+)\s*-->\s*([0-9.]+)/i);
      if (ya) {
        ymin = Number(ya[1]);
        ymax = Number(ya[2]);
        return;
      }
      const bar = line.match(/^bar(?:\s+"[^"]*")?\s*\[([^\]]+)\]/i);
      if (bar) {
        series.push({ type: "bar", values: splitList(bar[1]).map(Number) });
        return;
      }
      const ln = line.match(/^line(?:\s+"[^"]*")?\s*\[([^\]]+)\]/i);
      if (ln) {
        series.push({ type: "line", values: splitList(ln[1]).map(Number) });
      }
    });
    const n = Math.max(labels.length, ...series.map((item) => item.values.length), 1);
    while (labels.length < n) {
      labels.push(String(labels.length + 1));
    }
    const allVals = series.flatMap((item) => item.values.filter((value) => Number.isFinite(value)));
    if (!ymax) {
      ymax = Math.max(1, ...allVals, 0);
    }
    const left = 40;
    const top = title ? 28 : 12;
    const bottom = 28;
    const width = Math.max(280, n * 48 + left + 16);
    const height = 180;
    const plotW = width - left - 16;
    const plotH = height - top - bottom;
    const span = ymax - ymin || 1;
    const yAt = (value) => top + plotH - ((value - ymin) / span) * plotH;
    const xAt = (index) => left + (plotW / n) * (index + 0.5);
    const axis = `<line class="md-diagram-edge" x1="${left}" y1="${top}" x2="${left}" y2="${top + plotH}" />
      <line class="md-diagram-edge" x1="${left}" y1="${top + plotH}" x2="${width - 12}" y2="${top + plotH}" />`;
    const ticks = labels
      .map((label, index) => `<text x="${xAt(index)}" y="${height - 8}" text-anchor="middle" class="md-diagram-label">${escapeHtml(label)}</text>`)
      .join("");
    const caption = title
      ? `<text x="${width / 2}" y="16" text-anchor="middle" class="md-diagram-title">${escapeHtml(title)}</text>`
      : "";
    const bars = series
      .filter((item) => item.type === "bar")
      .flatMap((item, seriesIndex) => {
        const barW = Math.max(8, (plotW / n) * 0.5);
        return item.values.map((value, index) => {
          const x = xAt(index) - barW / 2 + seriesIndex * 4;
          const y = yAt(value);
          const h = Math.max(0, top + plotH - y);
          return `<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="3" fill="${COLORS[seriesIndex % COLORS.length]}" stroke="none" />`;
        });
      })
      .join("");
    const linesSvg = series
      .filter((item) => item.type === "line")
      .map((item, seriesIndex) => {
        const pts = item.values
          .map((value, index) => `${xAt(index)},${yAt(value)}`)
          .join(" ");
        return `<polyline class="md-diagram-line" points="${pts}" fill="none" stroke="${COLORS[(seriesIndex + 2) % COLORS.length]}" />`;
      })
      .join("");
    return `<svg class="md-diagram-svg" viewBox="0 0 ${width} ${height}" role="img">${caption}${axis}${ticks}${bars}${linesSvg}</svg>`;
  }

  function looksMermaid(lang, body) {
    const kind = String(lang || "").toLowerCase();
    if (
      kind === "mermaid" ||
      kind === "flowchart" ||
      kind === "pie" ||
      kind === "sequence" ||
      kind === "sequencediagram" ||
      kind === "xychart" ||
      kind === "xychart-beta"
    ) {
      return true;
    }
    const first = String(body || "")
      .trim()
      .split("\n")[0] || "";
    return /^(graph|flowchart|pie|sequenceDiagram|xychart(?:-beta)?)\b/i.test(first);
  }

  function renderFence(lang, body) {
    if (looksMermaid(lang, body)) {
      const svg = mermaidPie(body) || mermaidSequence(body) || mermaidXy(body) || mermaidGraph(body);
      if (svg) {
        return wrapDiagram(svg);
      }
    }
    return codeBlock(lang, body);
  }

  function isFenceStart(line) {
    return Boolean(splitFenceLine(line));
  }

  function render(source) {
    const text = normalizeSource(source);
    if (!text.trim()) {
      return "";
    }
    if (text.length > 24000) {
      return `<pre class="md-code"><code>${escapeHtml(text.slice(0, 24000))}\n…</code></pre>`;
    }
    const parts = [];
    const lines = text.split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const split = splitFenceLine(line);
      if (split) {
        if (split.prefix) {
          parts.push(`<p>${inline(split.prefix)}</p>`);
        }
        const buf = [];
        i += 1;
        while (i < lines.length && !isFenceClose(lines[i], split.fence)) {
          buf.push(lines[i]);
          i += 1;
        }
        if (i < lines.length) {
          i += 1;
        }
        parts.push(renderFence(split.fence.lang, buf.join("\n")));
        continue;
      }
      if (/^\s*---+\s*$/.test(line) || /^\s*\*\*\*+\s*$/.test(line)) {
        parts.push("<hr />");
        i += 1;
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        parts.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        i += 1;
        continue;
      }
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        parts.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`);
        continue;
      }
      if (looksLikeTable(lines.slice(i, i + 3))) {
        const buf = [];
        while (i < lines.length && lines[i].includes("|")) {
          buf.push(lines[i]);
          i += 1;
        }
        if (buf.length >= 2) {
          parts.push(renderTable(buf));
          continue;
        }
      }
      const ul = line.match(/^\s*[-*+]\s+(.+)$/);
      const ol = line.match(/^\s*\d+\.\s+(.+)$/);
      if (ul || ol) {
        const ordered = Boolean(ol);
        const buf = [];
        while (i < lines.length) {
          const next = ordered
            ? lines[i].match(/^\s*\d+\.\s+(.+)$/)
            : lines[i].match(/^\s*[-*+]\s+(.+)$/);
          if (!next) {
            break;
          }
          buf.push(next[1]);
          i += 1;
        }
        parts.push(renderList(buf, ordered));
        continue;
      }
      if (!line.trim()) {
        i += 1;
        continue;
      }
      const buf = [line];
      i += 1;
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(#{1,6})\s+/.test(lines[i]) &&
        !isFenceStart(lines[i]) &&
        !/^\s*[-*+]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i]) &&
        !/^\s*>/.test(lines[i])
      ) {
        buf.push(lines[i]);
        i += 1;
      }
      parts.push(`<p>${buf.map(inline).join("<br />")}</p>`);
    }
    return parts.join("");
  }

  const api = { render, escapeHtml };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.wittyMarkdown = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
