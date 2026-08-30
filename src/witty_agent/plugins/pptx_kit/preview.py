"""同一套语义稿的 HTML 预览。只读展示，真正可编辑的是 PPTX。"""

from __future__ import annotations

import json
import math
import re
from html import escape
from pathlib import Path

from witty_agent.plugins.pptx_kit.assets import asset_data_uri
from witty_agent.plugins.pptx_kit.chrome import (
    BAND_BOTTOM,
    BAND_H,
    COVER_BAND_H,
    COVER_BAND_Y,
    COVER_DIVIDER_X,
    COVER_GOLD_H,
    COVER_KICKER_PT,
    COVER_META_PT,
    COVER_SUB_PT,
    COVER_TITLE_PT,
    MARGIN_X,
    RULE_H,
    RULE_LEFT,
    SECTION_BAND_H,
    SECTION_BAND_Y,
    SECTION_BLOCK_TINT,
    SECTION_BLOCK_W,
    SECTION_SUB_PT,
    SECTION_TEXT_GAP,
    SECTION_TITLE_PT,
    cover_logo_rect,
    cover_text_x,
    emblem_rect,
    is_dark,
    logo_ground,
    logo_rect,
    logo_stem,
    section_mark_pt,
    shade,
    wants_band,
    wants_logo,
)
from witty_agent.plugins.pptx_kit.metrics import PT_PER_INCH, TEXT_PAD, line_factor
from witty_agent.plugins.pptx_kit.schema import SHAPE_KINDS, Box, ChartSeries, Deck, Slide
from witty_agent.plugins.pptx_kit.shapes import POLY_KINDS, clip_path
from witty_agent.plugins.pptx_kit.themes import Theme, color_of, css_hex, resolve_theme

_NAV = """
(function () {
  function slides() { return Array.prototype.slice.call(document.querySelectorAll('.slide')); }
  function current() {
    var list = slides();
    var mid = window.innerHeight / 2;
    for (var i = 0; i < list.length; i++) {
      var box = list[i].getBoundingClientRect();
      if (box.bottom > mid) return i;
    }
    return Math.max(0, list.length - 1);
  }
  function goTo(i) {
    var list = slides();
    var n = Math.min(list.length - 1, Math.max(0, i));
    if (!list[n]) return;
    list[n].scrollIntoView({ behavior: 'smooth', block: 'start' });
    history.replaceState(null, '', '#' + String(n + 1));
  }
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var i = current();
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === ' ' || e.key === 'PageDown') i += 1;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'PageUp') i -= 1;
    else if (e.key === 'Home') i = 0;
    else if (e.key === 'End') i = slides().length - 1;
    else return;
    e.preventDefault();
    goTo(i);
  });
  document.addEventListener('DOMContentLoaded', function () {
    var n = parseInt(String(location.hash).replace('#', ''), 10);
    if (n > 1) goTo(n - 1);
  });
})();
"""


def _li(items: list[str]) -> str:
    if not items:
        return ""
    inner = "".join(f"<li>{escape(item)}</li>" for item in items)
    return f"<ul class='items'>{inner}</ul>"


_CANVAS_W = 13.333
_CANVAS_H = 7.5
# 盒内边距，和 render.Painter.text 的 0.06 英寸对齐
_PAD_X = TEXT_PAD / 2 / _CANVAS_W * 100
_PAD_Y = 0.04 / _CANVAS_H * 100


def _chart_colors(box: Box, theme: Theme) -> list[str]:
    """图表系列色与成稿同源：先解析盒子里写的色（含 accent2 这类主题名），
    没写就用主题调色板——跟 render._chart_palette 一个顺序，预览和 PPTX 不许两个样。"""
    if box.colors:
        return [css_hex(color_of(theme, item, theme.accent)) for item in box.colors]
    return [css_hex(c) for c in (theme.accent, theme.accent2, theme.bar, theme.ink, theme.muted)]


def _svg_wrap(inner: str) -> str:
    return (
        "<svg viewBox='0 0 400 240' preserveAspectRatio='xMidYMid meet' "
        "xmlns='http://www.w3.org/2000/svg'>"
        "<rect x='0' y='0' width='400' height='240' fill='none'/>"
        f"{inner}</svg>"
    )


def _bar_svg(categories: list[str], series: list[ChartSeries], colors: list[str], *, horizontal: bool) -> str:
    peak = max((max(item.values) if item.values else 0.0) for item in series) or 1.0
    n_cat = max(len(categories), 1)
    n_ser = max(len(series), 1)
    parts: list[str] = []
    if horizontal:
        row_h = 180 / n_cat
        bar_h = max(4.0, (row_h - 8) / n_ser)
        for c_i, label in enumerate(categories):
            y0 = 20 + c_i * row_h
            parts.append(f"<text x='8' y='{y0 + row_h / 2:.1f}' font-size='10' fill='#5C6B70'>{escape(label)}</text>")
            for s_i, item in enumerate(series):
                value = item.values[c_i] if c_i < len(item.values) else 0.0
                width = max(0.0, 250 * (value / peak))
                y = y0 + 4 + s_i * bar_h
                color = colors[s_i % len(colors)]
                parts.append(
                    f"<rect x='80' y='{y:.1f}' width='{width:.1f}' height='{bar_h - 2:.1f}' fill='{escape(color)}'/>"
                )
    else:
        col_w = 320 / n_cat
        bar_w = max(4.0, (col_w - 10) / n_ser)
        parts.append("<line x1='40' y1='200' x2='380' y2='200' stroke='#D9D1C3' stroke-width='1'/>")
        for c_i, label in enumerate(categories):
            x0 = 40 + c_i * col_w
            parts.append(
                f"<text x='{x0 + col_w / 2:.1f}' y='220' text-anchor='middle' font-size='10' fill='#5C6B70'>{escape(label)}</text>"
            )
            for s_i, item in enumerate(series):
                value = item.values[c_i] if c_i < len(item.values) else 0.0
                height = max(0.0, 170 * (value / peak))
                x = x0 + 6 + s_i * bar_w
                color = colors[s_i % len(colors)]
                parts.append(
                    f"<rect x='{x:.1f}' y='{200 - height:.1f}' width='{bar_w - 2:.1f}' height='{height:.1f}' fill='{escape(color)}'/>"
                )
    return _svg_wrap("".join(parts))


def _line_svg(categories: list[str], series: list[ChartSeries], colors: list[str]) -> str:
    peak = max((max(item.values) if item.values else 0.0) for item in series) or 1.0
    n_cat = max(len(categories), 1)
    parts = ["<line x1='40' y1='200' x2='380' y2='200' stroke='#D9D1C3' stroke-width='1'/>"]
    for c_i, label in enumerate(categories):
        x = 40 + (320 * c_i / max(n_cat - 1, 1))
        parts.append(f"<text x='{x:.1f}' y='220' text-anchor='middle' font-size='10' fill='#5C6B70'>{escape(label)}</text>")
    for s_i, item in enumerate(series):
        color = colors[s_i % len(colors)]
        pts = []
        for c_i in range(n_cat):
            value = item.values[c_i] if c_i < len(item.values) else 0.0
            x = 40 + (320 * c_i / max(n_cat - 1, 1))
            y = 200 - 170 * (value / peak)
            pts.append(f"{x:.1f},{y:.1f}")
        parts.append(f"<polyline fill='none' stroke='{escape(color)}' stroke-width='2.5' points='{' '.join(pts)}'/>")
    return _svg_wrap("".join(parts))


def _pie_svg(categories: list[str], values: list[float], colors: list[str], *, hole: bool) -> str:
    total = sum(values) or 1.0
    cx, cy, r = 200.0, 110.0, 80.0
    inner = 42.0 if hole else 0.0
    angle = -90.0
    parts: list[str] = []
    for index, (label, value) in enumerate(zip(categories, values)):
        sweep = 360.0 * (value / total)
        start = math.radians(angle)
        end = math.radians(angle + sweep)
        x1, y1 = cx + r * math.cos(start), cy + r * math.sin(start)
        x2, y2 = cx + r * math.cos(end), cy + r * math.sin(end)
        large = 1 if sweep > 180 else 0
        color = colors[index % len(colors)]
        if inner:
            ix1, iy1 = cx + inner * math.cos(start), cy + inner * math.sin(start)
            ix2, iy2 = cx + inner * math.cos(end), cy + inner * math.sin(end)
            d = (
                f"M {x1:.1f} {y1:.1f} A {r:.1f} {r:.1f} 0 {large} 1 {x2:.1f} {y2:.1f} "
                f"L {ix2:.1f} {iy2:.1f} A {inner:.1f} {inner:.1f} 0 {large} 0 {ix1:.1f} {iy1:.1f} Z"
            )
        else:
            d = f"M {cx:.1f} {cy:.1f} L {x1:.1f} {y1:.1f} A {r:.1f} {r:.1f} 0 {large} 1 {x2:.1f} {y2:.1f} Z"
        parts.append(f"<path d='{d}' fill='{escape(color)}'><title>{escape(label)}</title></path>")
        angle += sweep
    return _svg_wrap("".join(parts))


def _chart_svg(box: Box, theme: Theme) -> str:
    if not box.categories or not box.series:
        return escape(box.text)
    colors = _chart_colors(box, theme)
    kind = (box.chart or "column").lower()
    if kind in {"pie", "doughnut"}:
        values = list(box.series[0].values)
        if len(values) < len(box.categories):
            values.extend([0.0] * (len(box.categories) - len(values)))
        return _pie_svg(box.categories, values, colors, hole=kind == "doughnut")
    if kind == "line":
        return _line_svg(box.categories, box.series, colors)
    return _bar_svg(box.categories, box.series, colors, horizontal=kind == "bar")


def _chart_payload(box: Box) -> str:
    payload = {
        "chart": box.chart or "column",
        "categories": box.categories,
        "series": [{"name": item.name, "values": item.values} for item in box.series],
        "colors": box.colors,
    }
    return json.dumps(payload, ensure_ascii=False)


def _box_html(box: Box, theme: Theme) -> str:
    left = box.x / _CANVAS_W * 100
    top = box.y / _CANVAS_H * 100
    width = box.w / _CANVAS_W * 100
    height = box.h / _CANVAS_H * 100
    # 字号换成页宽百分比（cqw），页缩小字跟着缩，和 PPTX 里的 pt 一一对应。
    size_cqw = box.size / (_CANVAS_W * PT_PER_INCH) * 100
    style = (
        f"left:{left:.3f}%;top:{top:.3f}%;width:{width:.3f}%;height:{height:.3f}%;"
        f"font-size:{size_cqw:.3f}cqw;line-height:{line_factor(box.size):.2f};"
        f"padding:{_PAD_Y:.2f}% {_PAD_X:.2f}%;"
    )
    if box.kind in {"text", "bullets"}:
        anchor = {"middle": "center", "bottom": "flex-end"}.get(box.anchor, "flex-start")
        style += f"display:flex;flex-direction:column;justify-content:{anchor};"
    if box.kind in SHAPE_KINDS and box.kind not in {"rect", "round"}:
        # 形状里的字居中，跟成稿和快照一致
        style += "display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;"
    if box.kind == "oval":
        style += "border-radius:50%;"
    elif box.kind in POLY_KINDS:
        path = clip_path(box.kind, box.w, box.h, box.point or "right")
        if path:
            style += f"clip-path:{path};"
    if box.stroke and box.stroke_w > 0:
        edge = css_hex(color_of(theme, box.stroke, theme.line))
        if box.kind in POLY_KINDS:
            # clip-path 会连 border 一起裁掉，多边形只能用四向 drop-shadow 描边。
            # 滤镜作用在不透明像素上，所以没给填充时先垫一层卡片底，否则描不出来。
            if not box.fill:
                style += "background:var(--card);"
            off = max(box.stroke_w * 0.6, 1.0)
            style += (
                f"filter:drop-shadow({off:.1f}px 0 0 {edge}) drop-shadow(-{off:.1f}px 0 0 {edge})"
                f" drop-shadow(0 {off:.1f}px 0 {edge}) drop-shadow(0 -{off:.1f}px 0 {edge});"
            )
        else:
            stroke_cqw = box.stroke_w / (_CANVAS_W * PT_PER_INCH) * 100
            style += f"border:{stroke_cqw:.3f}cqw solid {edge};box-sizing:border-box;"
    if box.font:
        style += f"font-family:\"{escape(box.font, quote=True)}\",var(--font);"
    if box.radius > 0:
        style += f"border-radius:{box.radius / _CANVAS_W * 100:.3f}cqw;"
    # fill/color 写的是主题色名（accent2、card…），得先过 color_of 换成真的十六进制。
    # 直接塞进 CSS 浏览器认不出来，会把整条声明丢掉——盒子就变透明、字就变默认墨色。
    if box.fill:
        style += f"background:{css_hex(color_of(theme, box.fill, theme.surface))};"
    if box.color:
        style += f"color:{css_hex(color_of(theme, box.color, theme.ink))};"
    if box.bold:
        style += "font-weight:700;"
    if box.align == "center":
        style += "text-align:center;"
    elif box.align == "right":
        style += "text-align:right;"
    attrs = (
        f"class='box' data-box='{escape(box.kind)}' data-x='{box.x}' data-y='{box.y}' "
        f"data-w='{box.w}' data-h='{box.h}' data-size='{box.size}' data-align='{escape(box.align)}'"
    )
    if box.anchor and box.anchor != "top":
        attrs += f" data-anchor='{escape(box.anchor, quote=True)}'"
    if box.radius > 0:
        attrs += f" data-radius='{box.radius}'"
    if box.shadow is not None:
        attrs += f" data-shadow='{1 if box.shadow else 0}'"
    if box.fill:
        attrs += f" data-fill='{escape(box.fill, quote=True)}'"
    if box.color:
        attrs += f" data-color='{escape(box.color, quote=True)}'"
    if box.bold:
        attrs += " data-bold='1'"
    if box.italic:
        attrs += " data-italic='1'"
    if box.name:
        attrs += f" data-name='{escape(box.name, quote=True)}'"
    if box.font:
        attrs += f" data-font='{escape(box.font, quote=True)}'"
    if box.stroke:
        attrs += f" data-stroke='{escape(box.stroke, quote=True)}'"
    if box.stroke_w:
        attrs += f" data-stroke-w='{box.stroke_w}'"
    if box.point:
        attrs += f" data-point='{escape(box.point, quote=True)}'"
    if box.kind == "chart":
        attrs += f" data-chart='{escape(box.chart or 'column', quote=True)}'"
        if box.text:
            attrs += f" data-text='{escape(box.text, quote=True)}'"
    if box.kind == "bullets":
        inner = _li(box.items)
    elif box.kind == "chart":
        inner = (
            _chart_svg(box, theme)
            + f"<script type='application/json' class='chart-data'>{escape(_chart_payload(box))}</script>"
        )
    elif box.kind == "table":
        headers = box.headers or (box.rows[0] if box.rows else [])
        body_rows = box.rows if box.headers else box.rows[1:]
        thead = "".join(f"<th>{escape(h)}</th>" for h in headers)
        tbody = "".join(
            "<tr>" + "".join(f"<td>{escape(row[i] if i < len(row) else '')}</td>" for i in range(len(headers))) + "</tr>"
            for row in body_rows
        )
        inner = f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"
    elif box.kind == "image" and box.image:
        inner = f"<img data-image='{escape(box.image, quote=True)}' alt=''>"
    else:
        inner = escape(box.text)
    return f"<div {attrs} style='{style}'>{inner}</div>"


def _slide_body(spec: Slide, theme: Theme, page: int) -> str:
    if spec.boxes:
        return "".join(_box_html(box, theme) for box in spec.boxes)
    kind = spec.kind
    if kind == "section":
        # 号数没写就顶页码，字号按标记长短挑——两处都照抄 render._section_band 的规则。
        mark = (spec.kicker or f"{page:02d}").strip()
        size = section_mark_pt(mark) / (_CANVAS_W * PT_PER_INCH) * 100
        return "".join(
            [
                f"<p class='kicker' style='font-size:{size:.3f}cqw'>{escape(mark)}</p>",
                f"<h1 class='title'>{escape(spec.title)}</h1>" if spec.title else "",
                f"<p class='subtitle'>{escape(spec.subtitle)}</p>" if spec.subtitle else "",
            ]
        )
    if kind == "cover":
        parts = [
            f"<p class='kicker'>{escape(spec.kicker)}</p>" if spec.kicker else "",
            f"<h1 class='title'>{escape(spec.title)}</h1>" if spec.title else "",
            f"<p class='subtitle'>{escape(spec.subtitle)}</p>" if spec.subtitle else "",
            f"<p class='meta'>{escape(spec.meta)}</p>" if spec.meta else "",
        ]
        return "".join(parts)
    head = ""
    if spec.kicker:
        head += f"<p class='kicker'>{escape(spec.kicker)}</p>"
    if spec.title:
        head += f"<h2 class='title'>{escape(spec.title)}</h2>"
    if kind == "bullets":
        return head + _li(spec.items)
    if kind == "two_col":
        return (
            head
            + "<div class='cols'>"
            + f"<div class='col' data-side='left'><h3>{escape(spec.left_title)}</h3>{_li(spec.left)}</div>"
            + f"<div class='col' data-side='right'><h3>{escape(spec.right_title)}</h3>{_li(spec.right)}</div>"
            + "</div>"
        )
    if kind == "kpi":
        cards = "".join(
            "<div class='metric' data-value='{value}' data-note='{note}'><span class='label'>{label}</span>"
            "<span class='value'>{value}</span><span class='note'>{note}</span></div>".format(
                label=escape(item.label),
                value=escape(item.value),
                note=escape(item.note),
            )
            for item in spec.metrics
        )
        return head + f"<div class='metrics n{len(spec.metrics)}'>{cards}</div>"
    if kind == "cards":
        cards = "".join(
            f"<article class='card'><h3>{escape(item.title)}</h3><p>{escape(item.body)}</p></article>"
            for item in spec.cards
        )
        return head + f"<div class='cards n{len(spec.cards)}'>{cards}</div>"
    if kind == "table":
        headers = spec.headers or (spec.rows[0] if spec.rows else [])
        body_rows = spec.rows if spec.headers else spec.rows[1:]
        thead = "".join(f"<th>{escape(h)}</th>" for h in headers)
        tbody = "".join(
            "<tr>" + "".join(f"<td>{escape(row[i] if i < len(row) else '')}</td>" for i in range(len(headers))) + "</tr>"
            for row in body_rows
        )
        return head + f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"
    if kind == "process":
        items = "".join(
            f"<li data-title='{escape(step.title)}'><h3>{escape(step.title)}</h3><p>{escape(step.body)}</p></li>"
            for step in spec.steps
        )
        return head + f"<ol class='steps'>{items}</ol>"
    if kind == "compare":
        return (
            head
            + "<div class='cols compare'>"
            + f"<div class='col' data-side='left'><h3>{escape(spec.left_title)}</h3>{_li(spec.left)}</div>"
            + f"<div class='col' data-side='right'><h3>{escape(spec.right_title)}</h3>{_li(spec.right)}</div>"
            + "</div>"
        )
    if kind == "quote":
        return (
            f"<blockquote class='quote'><p>{escape(spec.quote or spec.title)}</p>"
            + (f"<cite>{escape(spec.by)}</cite>" if spec.by else "")
            + "</blockquote>"
        )
    if kind == "closing":
        return head + _li(spec.items)
    if kind == "picture":
        src = escape(spec.image, quote=True)
        return head + (f"<img data-image='{src}' alt=''>" if spec.image else "")
    return head


def _brand_css(theme: Theme) -> str:
    """标识、顶栏白带、封面章节通栏带的样式。

    几何全部取自 chrome.py 的同一批常量，换算成百分比——预览和成稿走同一份数字，
    不会出现「预览挺好、导出走形」。图内联成 data URI，预览是单文件、离线也能看。
    """
    stem = logo_stem(theme)
    light = asset_data_uri(stem)
    emblem = asset_data_uri(f"{(theme.emblem or '').strip()}-white") or asset_data_uri((theme.emblem or "").strip())

    def vh(inches: float) -> str:
        return f"{inches / _CANVAS_H * 100:.3f}%"

    def vw(inches: float) -> str:
        return f"{inches / _CANVAS_W * 100:.3f}%"

    def pt(size: float) -> str:
        """pt 换成页宽百分比。和盒子那边同一个换算，成稿改字号预览跟着改。"""
        return f"{size / (_CANVAS_W * PT_PER_INCH) * 100:.3f}cqw"

    band = f"""
.chrome {{
  position: absolute; left: 0; top: 0; width: 100%; height: {vh(BAND_H)};
  background: var(--surface); z-index: 3;
}}
.chrome::after {{
  content: ""; position: absolute; left: 0; top: 100%; width: 100%; height: {RULE_H / BAND_H * 100:.3f}%;
  background: linear-gradient(to right,
    var(--accent2) 0 {vw(RULE_LEFT)}, var(--line) {vw(RULE_LEFT)} 100%);
}}
.slide.band {{ padding-top: {vh(BAND_BOTTOM + 0.26)}; }}
.slide.band::before {{ display: none; }}
.slide.band .title {{ color: var(--accent2); }}
/* 封面和章节页：内边距刚好框住通栏色带，再靠 justify-content 把文字组在带里居中，
   和成稿那边「先量标题高度再居中」是同一个结果。 */
.slide.band[data-kind="cover"], .slide.band[data-kind="section"] {{
  background: var(--bg); color: var(--cover-ink); justify-content: center;
}}
.slide.band[data-kind="cover"] > p, .slide.band[data-kind="cover"] > h1,
.slide.band[data-kind="section"] > p, .slide.band[data-kind="section"] > h1 {{
  position: relative; z-index: 2;
}}
.slide.band[data-kind="cover"] {{
  padding: {vh(COVER_BAND_Y)} {vw(MARGIN_X)} {vh(_CANVAS_H - COVER_BAND_Y - COVER_BAND_H)} {vw(cover_text_x(theme))};
}}
/* 金线用渐变最后一段画，不用 border——border-width 不吃百分比，写进去整条声明会被丢掉。
   色块高度连金线一起算，渐变分界按自身高度取百分比。 */
.slide.band[data-kind="cover"]::after {{
  content: ""; position: absolute; left: 0; top: {vh(COVER_BAND_Y)}; width: 100%;
  height: {vh(COVER_BAND_H + COVER_GOLD_H)}; z-index: 1;
  background: linear-gradient(to bottom,
    var(--cover) 0 {COVER_BAND_H / (COVER_BAND_H + COVER_GOLD_H) * 100:.3f}%,
    var(--bar) {COVER_BAND_H / (COVER_BAND_H + COVER_GOLD_H) * 100:.3f}% 100%);
}}
.slide.band[data-kind="cover"] .kicker {{ color: var(--cover-muted); font-size: {pt(COVER_KICKER_PT)}; }}
.slide.band[data-kind="cover"] .title {{
  color: var(--cover-ink); font-size: {pt(COVER_TITLE_PT)}; max-width: 20ch;
}}
.slide.band[data-kind="cover"] .subtitle {{
  color: var(--cover-muted); font-size: {pt(COVER_SUB_PT)}; max-width: 42cqw; margin: {vh(0.2)} 0 0;
}}
.slide.band[data-kind="cover"] .meta {{
  position: absolute; left: {vw(MARGIN_X)}; top: {vh(6.02)}; margin: 0; color: var(--muted);
  font-size: {pt(COVER_META_PT)}; z-index: 2;
}}
.slide.band[data-kind="section"] {{
  padding: {vh(SECTION_BAND_Y)} {vw(MARGIN_X)} {vh(_CANVAS_H - SECTION_BAND_Y - SECTION_BAND_H)}
    {vw(SECTION_BLOCK_W + SECTION_TEXT_GAP)};
}}
.slide.band[data-kind="section"]::after {{
  content: ""; position: absolute; left: 0; top: {vh(SECTION_BAND_Y)}; width: 100%;
  height: {vh(SECTION_BAND_H)}; z-index: 1;
  background: linear-gradient(to right,
    {css_hex(shade(theme.cover_bg, SECTION_BLOCK_TINT))} 0 {vw(SECTION_BLOCK_W)}, var(--cover) {vw(SECTION_BLOCK_W)} 100%);
}}
.slide.band[data-kind="section"] .kicker {{
  position: absolute; left: 0; top: {vh(SECTION_BAND_Y)}; width: {vw(SECTION_BLOCK_W)};
  height: {vh(SECTION_BAND_H)}; margin: 0; display: flex; align-items: center; justify-content: center;
  letter-spacing: 0; color: var(--cover-ink); z-index: 2;
}}
.slide.band[data-kind="section"] .title {{
  color: var(--cover-ink); font-size: {pt(SECTION_TITLE_PT)}; margin: 0;
}}
.slide.band[data-kind="section"] .subtitle {{
  position: absolute; left: {vw(MARGIN_X)}; top: {vh(SECTION_BAND_Y + SECTION_BAND_H + 0.44)};
  margin: 0; color: var(--muted); font-size: {pt(SECTION_SUB_PT)}; z-index: 2;
}}
.slide.band[data-kind="cover"] .foot, .slide.band[data-kind="section"] .foot {{ color: var(--muted); z-index: 2; }}
/* boxes 页的白带垫在内容底下 */
.slide.free .chrome {{ z-index: 0; }}
"""
    if emblem:
        ex, ey, ew, eh = emblem_rect(theme)
        band += f"""
.slide.band[data-kind="cover"] .emblem {{
  position: absolute; left: {vw(ex)}; top: {vh(ey)}; width: {vw(ew)}; height: {vh(eh)}; z-index: 2;
  background: url("{emblem}") no-repeat center / contain;
}}
.slide.band[data-kind="cover"] .emblem::after {{
  content: ""; position: absolute; left: {(COVER_DIVIDER_X - ex) / ew * 100:.3f}%;
  top: {(COVER_BAND_Y + 0.66 - ey) / eh * 100:.3f}%; width: {0.02 / ew * 100:.3f}%;
  height: {(COVER_BAND_H - 1.32) / eh * 100:.3f}%; background: var(--cover-muted);
}}
"""
    if not light:
        return band
    _x, top, width, height = logo_rect(theme)
    _cx, cover_top, cover_w, cover_h = cover_logo_rect(theme)
    dark = asset_data_uri(f"{stem}-white") or light
    return band + f"""
/* 标识永远压最上层 */
.logo {{
  position: absolute; right: {vw(MARGIN_X)}; top: {vh(top)};
  width: {vw(width)}; height: {vh(height)}; z-index: 4;
  background: url("{light}") no-repeat center / contain;
}}
.slide[data-kind="cover"] .logo {{
  top: {vh(cover_top)}; width: {vw(cover_w)}; height: {vh(cover_h)};
}}
.logo[data-dark='1'] {{ background-image: url("{dark}"); }}
"""


def _chrome_html(theme: Theme, spec: Slide, ground) -> tuple[str, bool]:
    """返回 (顶栏 HTML, 这页是否套 band 宏版式)。

    白带只压内容页（宏页和 boxes 页同一套模板），封面章节按原稿不压白带；
    boxes 页的白带垫在内容底下。band 主题浅底标识用墨版，深底换反白版。
    """
    macro = not spec.boxes
    band = theme.chrome == "band" and macro
    band_div = theme.chrome == "band" and (
        (macro and spec.kind not in {"cover", "section"})
        or (not macro and wants_band(theme, spec.kind, spec.boxes, ground))
    )
    dark_page = macro and not band and spec.kind in {"cover", "section"}
    parts = "<div class='chrome'></div>" if band_div else ""
    if band and spec.kind == "cover" and (theme.emblem or "").strip():
        parts += "<i class='emblem'></i>"
    if logo_stem(theme):
        if spec.boxes and not wants_logo(theme, spec.boxes):
            return parts, band
        dark = dark_page or (spec.boxes and is_dark(logo_ground(theme, spec.boxes, ground)))
        parts += f"<i class='logo' data-dark='{1 if dark else 0}'></i>"
    return parts, band


def _css(theme: Theme) -> str:
    return f"""
:root {{
  --bg: {css_hex(theme.bg)};
  --surface: {css_hex(theme.surface)};
  --ink: {css_hex(theme.ink)};
  --muted: {css_hex(theme.muted)};
  --accent: {css_hex(theme.accent)};
  --accent2: {css_hex(theme.accent2)};
  --bar: {css_hex(theme.bar)};
  --on-accent: {css_hex(theme.on_accent)};
  --cover: {css_hex(theme.cover_bg)};
  --cover-ink: {css_hex(theme.cover_ink)};
  --cover-muted: {css_hex(theme.cover_muted)};
  --card: {css_hex(theme.card)};
  --card-ink: {css_hex(theme.card_ink)};
  --line: {css_hex(theme.line)};
  --font: "{theme.font}", "PingFang SC", "Noto Sans SC", sans-serif;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: #16181a; font-family: var(--font); color: var(--ink); }}
/* 页是一叠 16:9 的块，正常滚。字号跟着页宽（cqw）缩，不跟窗口高。 */
.deck {{ width: 100%; padding: 2.4vh 0; display: flex; flex-direction: column; align-items: center; gap: 2.4vh; }}
.slide {{
  container-type: inline-size;
  position: relative; flex: none; overflow: hidden;
  width: min(96vw, calc((100vh - 6vh) * {_CANVAS_W / _CANVAS_H:.4f}));
  aspect-ratio: {_CANVAS_W:.3f} / {_CANVAS_H:.3f};
  padding: 5.6% 4.7% 8%;
  display: flex; flex-direction: column;
  background: var(--bg);
  border-radius: 4px; box-shadow: 0 6px 28px rgba(0, 0, 0, 0.42);
  scroll-margin-top: 2.4vh;
}}
.slide.free {{ padding: 0; display: block; }}
.slide.free::before {{ display: none; }}
.slide.free .box {{ position: absolute; overflow: hidden; box-sizing: border-box; }}
.slide.free .box .items {{ margin: 0; padding-left: 0; }}
.slide.free .box .items li {{ font-size: 1em; line-height: inherit; padding: 0 0 .34em 1.35em; }}
.slide.free .box .items li:last-child {{ padding-bottom: 0; }}
.slide.free .box .items li::before {{ top: .48em; width: .3em; height: .3em; border-radius: 999px; background: currentColor; opacity: .75; }}
.slide.free .box table {{ font-size: 1em; }}
.slide.free .box th, .slide.free .box td {{ padding: .32em .5em; }}
.slide.free .box[data-box="round"] {{ border-radius: 1cqw; box-shadow: 0 1cqw 2.4cqw rgba(26, 36, 40, 0.12); }}
.slide.free .box[data-box="chart"] svg {{ width: 100%; height: 100%; display: block; }}
.slide.free .box[data-box="image"] img {{ width: 100%; height: 100%; object-fit: contain; max-height: none; }}
.slide[data-kind="cover"], .slide[data-kind="section"] {{
  background: var(--cover); color: var(--cover-ink);
}}
.slide.cover-minimal[data-kind="cover"], .slide.cover-mark[data-kind="cover"], .slide.cover-band[data-kind="cover"] {{
  background: var(--bg); color: var(--ink);
}}
.slide::before {{
  content: ""; position: absolute; left: 0; top: 0; width: 0.75cqw; height: 100%;
  background: var(--accent2);
}}
.slide[data-kind="cover"].cover-bar::before, .slide[data-kind="section"]::before {{
  background: var(--accent); width: 1.05cqw;
}}
.slide.cover-mark::before, .slide.cover-minimal::before, .slide.cover-band::before {{ display: none; }}
.kicker {{ margin: 0 0 .5cqw; font-size: 1.35cqw; letter-spacing: .12em; color: var(--accent); font-weight: 700; }}
.slide[data-kind="cover"] .kicker, .slide[data-kind="section"] .kicker {{ color: var(--cover-muted); }}
.title {{ margin: 0 0 .8cqw; font-size: 3.2cqw; line-height: 1.2; font-weight: 700; }}
.slide[data-kind="cover"] .title {{ font-size: 4.6cqw; max-width: 18ch; }}
.subtitle, .meta {{ color: var(--muted); font-size: 1.55cqw; max-width: 56cqw; }}
.slide[data-kind="cover"] .subtitle, .slide[data-kind="cover"] .meta {{ color: var(--cover-muted); }}
.items {{ margin: 1.35cqw 0 0; padding: 0; list-style: none; }}
.items li {{ position: relative; padding: .48cqw 0 .48cqw 1.6cqw; font-size: 1.68cqw; line-height: 1.45; }}
.items li::before {{ content: ""; position: absolute; left: 0; top: 1.15cqw; width: .6cqw; height: .6cqw; background: var(--accent); border-radius: 1px; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.6cqw; flex: 1; min-height: 0; }}
.col, .card, .metric {{ background: var(--card); color: var(--card-ink); padding: 1.5cqw 1.6cqw; border-radius: .75cqw; }}
.col h3, .card h3 {{ margin: 0 0 .8cqw; font-size: 1.42cqw; color: var(--accent); }}
.metrics, .cards {{ display: grid; gap: 1.35cqw; flex: 1; }}
.metrics.n2, .metrics.n3, .cards.n2, .cards.n3 {{ grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); }}
.metrics.n4 {{ grid-template-columns: 1fr 1fr; }}
.metric .label {{ display: block; color: var(--muted); font-size: 1.2cqw; }}
.metric .value {{ display: block; font-size: 3.2cqw; font-weight: 700; margin: .48cqw 0; }}
.metric .note {{ color: var(--accent); font-size: 1.2cqw; }}
table {{ width: 100%; border-collapse: collapse; font-size: 1.35cqw; }}
th, td {{ padding: .88cqw 1.08cqw; text-align: left; border-bottom: 1px solid var(--line); }}
th {{ background: var(--accent2); color: #fff; font-weight: 700; }}
.steps {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); gap: 1.35cqw; list-style: none; padding: 0; counter-reset: step; }}
.steps li {{ counter-increment: step; text-align: center; font-size: 1.55cqw; }}
.steps li::before {{
  content: counter(step); display: inline-flex; width: 2.7cqw; height: 2.7cqw; align-items: center; justify-content: center;
  background: var(--accent); color: var(--on-accent); border-radius: 999px; font-weight: 700; margin-bottom: .8cqw;
}}
blockquote.quote {{ margin: auto 0; font-size: 2.7cqw; line-height: 1.35; }}
blockquote.quote cite {{ display: block; margin-top: 1.35cqw; color: var(--muted); font-size: 1.35cqw; font-style: normal; }}
img {{ max-width: 100%; max-height: 56cqh; object-fit: contain; }}
.foot {{ position: absolute; left: 4.7%; right: 4.7%; bottom: 3.4%; color: var(--muted); font-size: 1.15cqw; display: flex; justify-content: space-between; }}
.slide[data-kind="cover"] .foot, .slide[data-kind="section"] .foot {{ color: var(--cover-muted); }}
@media print {{
  html, body {{ background: #fff; }}
  .deck {{ padding: 0; gap: 0; }}
  .slide {{ width: 100%; box-shadow: none; border-radius: 0; break-after: page; }}
}}
"""


def _slide_section(spec: Slide, resolved: Theme, index: int, footer: str) -> str:
    """一页的 <section>。整稿渲染和分块追加都走这里，免得两条路排出来不一样。"""
    cover_class = f"cover-{resolved.cover}"
    extra = "free" if spec.boxes else (cover_class if spec.kind in {"cover", "section"} else "")
    ground = color_of(resolved, spec.bg, resolved.bg)
    # 页底色同理：data-bg 留原始写法给回读用，style 里必须是解析后的色值。
    bg = f" data-bg='{escape(spec.bg, quote=True)}' style='background:{css_hex(ground)}'" if spec.bg else ""
    notes = f"<script type='application/json' class='notes'>{escape(spec.notes)}</script>" if spec.notes else ""
    chrome, band = _chrome_html(resolved, spec, ground)
    if band:
        extra += " band"
    return (
        f"<section class='slide {extra}' data-kind='{escape(spec.kind)}'{bg}>"
        f"{chrome}"
        f"{_slide_body(spec, resolved, index)}"
        f"<div class='foot'><span>{footer}</span><span>{index:02d}</span></div>"
        f"{notes}"
        "</section>"
    )


def render_sections(specs: list[Slide], resolved: Theme, footer: str, start: int = 1) -> str:
    """从第 start 页起渲染若干页。footer 由调用方给，此处按已转义的字面量用。"""
    return "".join(_slide_section(spec, resolved, start + offset, footer) for offset, spec in enumerate(specs))


def render_html(deck: Deck, theme: Theme | None = None) -> str:
    resolved = theme or resolve_theme(deck.theme, deck.theme_overrides)
    footer = escape(deck.footer or deck.title)
    body = render_sections(deck.slides, resolved, footer)
    title = escape(deck.title)
    overrides = " ".join(
        f'data-{escape(key).replace("_", "-")}="{escape(str(value))}"' for key, value in deck.theme_overrides.items()
    )
    return (
        "<!DOCTYPE html><html lang='zh-CN' data-witty-deck='1' "
        f"data-theme='{escape(resolved.id)}' data-title='{title}' data-footer='{footer}' {overrides}>"
        f"<head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{_css(resolved)}{_brand_css(resolved)}</style></head>"
        f"<body><article class='deck'>{body}</article><script>{_NAV}</script></body></html>"
    )


def _atomic_write(target: Path, text: str) -> None:
    """临时文件 + 原子换名，和 PPTX 落盘一个纪律：预览要么旧要么新，不烂尾。"""
    tmp = target.with_name(target.name + ".tmp~")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def write_html(deck: Deck, path: str, theme: Theme | None = None) -> str:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, render_html(deck, theme))
    return str(target)


def append_html(path: str, specs: list[Slide], theme: Theme) -> str:
    """把新页拼进已有预览，页码接着排。

    只在末尾插 section，不回读重排旧页——旧页的 HTML 保持字节不动，
    分块生成到第几批都不会把前面的页搞坏。旧版单页预览（display:none 翻页那代，
    滚轮翻不了页）不能硬拼，整稿解析后重写成现行滚动版式。认不出的文件返回空串。
    """
    target = Path(path).expanduser()
    if not specs or not target.is_file():
        return ""
    html = target.read_text(encoding="utf-8")
    close = html.rfind("</article>")
    if "data-witty-deck='1'" not in html or close < 0:
        return ""
    if "aspect-ratio" not in html:
        return _rewrite_legacy(target, html, specs, theme)
    footer = ""
    found = re.search(r"data-footer='([^']*)'", html)
    if found:
        footer = found.group(1)
    start = html.count("<section class='slide") + 1
    _atomic_write(target, html[:close] + render_sections(specs, theme, footer, start) + html[close:])
    return str(target)


def _rewrite_legacy(target: Path, html: str, specs: list[Slide], theme: Theme) -> str:
    from witty_agent.plugins.pptx_kit.html_parse import parse_html

    try:
        deck = parse_html(html)
    except ValueError:
        return ""
    deck.slides.extend(specs)
    return write_html(deck, str(target), theme)
