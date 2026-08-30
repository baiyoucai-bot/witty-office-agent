"""纯 Python 光栅预览：语义稿直接画成 PNG，不开浏览器、不装 LibreOffice。

用途有二：一是视觉回归——模板改动跑测试时对图上的关键像素采样，模板画坏了
（白带没了、标识丢了、色带错位）当场红；二是 agent 自查——生成完调 pptx_snapshot
把稿子画成图，自己看一眼再交稿。

保真契约（诚实说清楚，别拿它当成稿）：
- 几何、配色、chrome（白带/压线/标识/色带/页脚）与成稿同源，坐标一致；
- 文字用本机字体画，折行按真实字宽算，但不是 PowerPoint 的排版引擎，
  行位可能差半行；本机连一个 CJK 字体都没有时文字会缺字形，几何仍然可靠；
- 图表/表格是简化画法，数据形状对，细节样式（阴影、渐变）不追求一致。
"""

from __future__ import annotations

from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.plugins.pptx_kit.chrome import (
    BAND_H,
    COVER_BAND_H,
    COVER_BAND_Y,
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
    emblem_asset,
    emblem_rect,
    is_dark,
    logo_asset,
    logo_rect,
    section_mark_pt,
    shade,
    wants_band,
    wants_logo,
)
from witty_agent.plugins.pptx_kit.fonts import char_em, fallback_font_file, find_font
from witty_agent.plugins.pptx_kit.metrics import bullet_gap, line_factor, text_height
from witty_agent.plugins.pptx_kit.schema import CANVAS_H, CANVAS_W, SHAPE_KINDS, Box, Deck, Slide
from witty_agent.plugins.pptx_kit.shapes import POLY_KINDS, normalize_point, scaled_points
from witty_agent.plugins.pptx_kit.themes import Theme, color_of, resolve_theme

logger = get_logger("pptx")

# 96 px/英寸：一页 1280x720，看清版式够了，文件也不大
SCALE = 96
PAGE_W = round(CANVAS_W * SCALE)
PAGE_H = round(CANVAS_H * SCALE)
SHEET_GAP = 24
SHEET_BG = (34, 34, 34)

FOOTER_TOP = 6.88


def _px(inches: float) -> int:
    return round(inches * SCALE)


def _pt2px(pt: float) -> int:
    return max(1, round(pt * SCALE / 72.0))


class _Text:
    """字体加载和折行画字。一个光栅任务共用一套字体缓存。"""

    def __init__(self, family: str) -> None:
        self.family = family
        self.file = find_font(family) or fallback_font_file()
        # 主字体画不出的字（Arial 遇到汉字）改用本机 CJK 字体逐字兜底。PowerPoint 自己
        # 会做字体回退，快照不做就会画出一排豆腐块，让人以为稿子坏了。
        alt = fallback_font_file()
        self.alt_file = alt if alt and alt != self.file else ""
        self._cache: dict[tuple[int, bool, bool], object] = {}
        self._missing: dict[str, bool] = {}

    def _load(self, path: str, size_pt: int, alt: bool, bold: bool):
        from PIL import ImageFont

        key = (size_pt, bold, alt)
        if key not in self._cache:
            loaded = None
            if path:
                try:
                    loaded = ImageFont.truetype(path, _pt2px(size_pt))
                except OSError:
                    loaded = None
            self._cache[key] = loaded or ImageFont.load_default(_pt2px(size_pt))
        return self._cache[key]

    def font(self, size_pt: int, bold: bool):
        return self._load(self.file, size_pt, False, bold)

    def _lacks(self, ch: str) -> bool:
        """主字体的 cmap 里没有这个字。ASCII 一律当有，避免为标点查表。"""
        if not self.alt_file or ch.isascii():
            return False
        hit = self._missing.get(ch)
        if hit is None:
            hit = char_em(ch, self.family) is None
            self._missing[ch] = hit
        return hit

    def segments(self, line: str, size_pt: int, bold: bool):
        """把一行按「主字体画不画得出」切成若干段，每段配好字体。"""
        if not line:
            return []
        runs: list[list] = []
        for ch in line:
            alt = self._lacks(ch)
            if runs and runs[-1][1] == alt:
                runs[-1][0] += ch
            else:
                runs.append([ch, alt])
        return [
            (chunk, self._load(self.alt_file if alt else self.file, size_pt, alt, bold))
            for chunk, alt in runs
        ]

    def measure(self, draw, line: str, size_pt: int, bold: bool) -> float:
        return sum(draw.textlength(chunk, font=font) for chunk, font in self.segments(line, size_pt, bold))

    def draw_line(self, draw, xy, line: str, size_pt: int, bold: bool, color) -> None:
        x, y = xy
        for chunk, font in self.segments(line, size_pt, bold):
            draw.text((x, y), chunk, font=font, fill=tuple(color))
            x += draw.textlength(chunk, font=font)

    def wrap(self, draw, text: str, width_px: int, size_pt: int, bold: bool = False) -> list[str]:
        """按像素宽折行。逐字试探，CJK 无空格也能折。"""
        lines: list[str] = []
        for chunk in text.replace("\r\n", "\n").split("\n"):
            if not chunk:
                lines.append("")
                continue
            current = ""
            for ch in chunk:
                trial = current + ch
                if current and self.measure(draw, trial, size_pt, bold) > width_px:
                    lines.append(current)
                    current = ch
                else:
                    current = trial
            lines.append(current)
        return lines

    def block(
        self,
        draw,
        rect_px: tuple[int, int, int, int],
        text: str,
        *,
        size: int,
        color,
        bold: bool = False,
        align: str = "left",
        anchor: str = "top",
    ) -> None:
        """在矩形里画一段折行文字，支持水平对齐和垂直锚点。"""
        if not text.strip():
            return
        x, y, w, h = rect_px
        pad = _px(0.06)
        lines = self.wrap(draw, text, max(w - pad * 2, 10), size, bold)
        step = round(_pt2px(size) * line_factor(size))
        total = step * len(lines)
        top = y + _px(0.03)
        if anchor == "middle":
            top = y + max((h - total) // 2, 0)
        elif anchor == "bottom":
            top = y + max(h - total - _px(0.03), 0)
        for line in lines:
            if line:
                lw = self.measure(draw, line, size, bold)
                lx = x + pad
                if align == "center":
                    lx = x + max((w - lw) // 2, 0)
                elif align == "right":
                    lx = x + max(w - lw - pad, 0)
                self.draw_line(draw, (lx, top), line, size, bold, color)
            top += step


def _paste_png(img, path: str, x: float, y: float, w: float) -> None:
    """按宽等比贴 PNG，带透明通道。缺图静默跳过，和成稿一个态度。"""
    if not path or not Path(path).is_file():
        return
    from PIL import Image

    logo = Image.open(path).convert("RGBA")
    ratio = logo.height / max(logo.width, 1)
    box_w = _px(w)
    box_h = max(round(box_w * ratio), 1)
    logo = logo.resize((box_w, box_h), Image.LANCZOS)
    img.paste(logo, (_px(x), _px(y)), logo)


def _chart_palette(theme: Theme, box: Box) -> list[tuple[int, int, int]]:
    """和 render._chart_palette、preview._chart_colors 同一个顺序。"""
    if box.colors:
        return [color_of(theme, item, theme.accent) for item in box.colors]
    return [theme.accent, theme.accent2, theme.bar, theme.ink, theme.muted]


def _draw_chart(draw, box: Box, theme: Theme, text: _Text) -> None:
    if not box.categories or not box.series:
        return
    colors = _chart_palette(theme, box)
    x, y = _px(box.x) + _px(0.2), _px(box.y) + _px(0.2)
    w, h = _px(box.w) - _px(0.4), _px(box.h) - _px(0.55)
    kind = (box.chart or "column").lower()
    peak = max((max(s.values) if s.values else 0.0) for s in box.series) or 1.0
    n_cat = max(len(box.categories), 1)
    n_ser = max(len(box.series), 1)
    if kind in {"pie", "doughnut"}:
        values = list(box.series[0].values)[: len(box.categories)]
        total = sum(values) or 1.0
        d = min(w, h)
        cx, cy = x + w // 2, y + h // 2
        bbox = (cx - d // 2, cy - d // 2, cx + d // 2, cy + d // 2)
        angle = -90.0
        for index, value in enumerate(values):
            sweep = 360.0 * value / total
            draw.pieslice(bbox, angle, angle + sweep, fill=tuple(colors[index % len(colors)]))
            angle += sweep
        if kind == "doughnut":
            hole = d // 3
            draw.ellipse((cx - hole, cy - hole, cx + hole, cy + hole), fill=(255, 255, 255))
        return
    if kind == "line":
        for s_i, series in enumerate(box.series):
            points = []
            for c_i in range(n_cat):
                value = series.values[c_i] if c_i < len(series.values) else 0.0
                px_x = x + (c_i + 0.5) / n_cat * w
                px_y = y + h - value / peak * h
                points.append((px_x, px_y))
            if len(points) > 1:
                draw.line(points, fill=tuple(colors[s_i % len(colors)]), width=3)
            for point in points:
                draw.ellipse((point[0] - 4, point[1] - 4, point[0] + 4, point[1] + 4), fill=tuple(colors[s_i % len(colors)]))
        for c_i, label in enumerate(box.categories):
            text.draw_line(draw, (x + (c_i + 0.5) / n_cat * w - 10, y + h + 6), label, 11, False, theme.muted)
        return
    if kind == "bar":  # 水平条
        row_h = h / n_cat
        for c_i, label in enumerate(box.categories):
            for s_i, series in enumerate(box.series):
                value = series.values[c_i] if c_i < len(series.values) else 0.0
                bar_h = max(row_h / (n_ser + 0.6), 6)
                top = y + c_i * row_h + s_i * bar_h + row_h * 0.18
                draw.rectangle((x + _px(0.9), top, x + _px(0.9) + value / peak * (w - _px(0.9)), top + bar_h), fill=tuple(colors[s_i % len(colors)]))
            text.draw_line(draw, (x, y + c_i * row_h + row_h * 0.3), label, 11, False, theme.muted)
        return
    col_w = w / n_cat  # 竖柱
    for c_i, label in enumerate(box.categories):
        for s_i, series in enumerate(box.series):
            value = series.values[c_i] if c_i < len(series.values) else 0.0
            bar_w = max(col_w / (n_ser + 0.8), 6)
            left = x + c_i * col_w + s_i * bar_w + col_w * 0.16
            bar_top = y + h - value / peak * h
            draw.rectangle((left, bar_top, left + bar_w, y + h), fill=tuple(colors[s_i % len(colors)]))
        text.draw_line(draw, (x + c_i * col_w + col_w * 0.3, y + h + 6), label, 11, False, theme.muted)


def _draw_table(draw, box: Box, theme: Theme, text: _Text) -> None:
    headers = box.headers or (box.rows[0] if box.rows else [])
    rows = box.rows if box.headers else box.rows[1:]
    if not headers:
        return
    cols = max(len(headers), 1)
    x, y, w = _px(box.x), _px(box.y), _px(box.w)
    col_w = w // cols
    header_h = _px(0.42)
    row_h = _px(0.4)
    draw.rectangle((x, y, x + w, y + header_h), fill=tuple(theme.accent2))
    for index, head in enumerate(headers):
        text.block(draw, (x + index * col_w + _px(0.08), y + _px(0.06), col_w, header_h), str(head), size=box.size or 14, color=(255, 255, 255), bold=True)
    top = y + header_h
    for row in rows:
        for index in range(cols):
            cell = str(row[index]) if index < len(row) else ""
            text.block(draw, (x + index * col_w + _px(0.08), top + _px(0.05), col_w, row_h), cell, size=box.size or 14, color=theme.ink)
        draw.line((x, top + row_h, x + w, top + row_h), fill=tuple(theme.line), width=1)
        top += row_h


def _arrow_head(draw, x2: float, y2: float, facing: str, size: float, color) -> None:
    """连接线末端的实心小三角。跟成稿那边的 a:tailEnd 对应。"""
    if facing == "right":
        pts = [(x2, y2), (x2 - size, y2 - size * 0.6), (x2 - size, y2 + size * 0.6)]
    elif facing == "left":
        pts = [(x2, y2), (x2 + size, y2 - size * 0.6), (x2 + size, y2 + size * 0.6)]
    elif facing == "down":
        pts = [(x2, y2), (x2 - size * 0.6, y2 - size), (x2 + size * 0.6, y2 - size)]
    else:
        pts = [(x2, y2), (x2 - size * 0.6, y2 + size), (x2 + size * 0.6, y2 + size)]
    draw.polygon(pts, fill=tuple(color))


def _draw_connector(draw, box: Box, theme: Theme, x, y, w, h) -> None:
    facing = normalize_point(box.point)
    color = color_of(theme, box.fill or box.stroke or box.color, theme.accent)
    weight = max(round(_pt2px(box.stroke_w or 1.5)), 1)
    if facing in {"left", "right"}:
        mid = y + h // 2
        x1, x2 = (x, x + w) if facing == "right" else (x + w, x)
        draw.line((x1, mid, x2, mid), fill=tuple(color), width=weight)
        _arrow_head(draw, x2, mid, facing, max(weight * 3.0, _px(0.07)), color)
    else:
        mid = x + w // 2
        y1, y2 = (y, y + h) if facing == "down" else (y + h, y)
        draw.line((mid, y1, mid, y2), fill=tuple(color), width=weight)
        _arrow_head(draw, mid, y2, facing, max(weight * 3.0, _px(0.07)), color)


def _draw_box(img, draw, box: Box, theme: Theme, slide_bg, text: _Text) -> None:
    x, y, w, h = _px(box.x), _px(box.y), _px(box.w), _px(box.h)
    fill = color_of(theme, box.fill, slide_bg) if box.fill else None
    stroke = color_of(theme, box.stroke, theme.line) if box.stroke else None
    edge = tuple(stroke) if stroke and box.stroke_w > 0 else None
    pen = max(round(_pt2px(box.stroke_w)), 1) if edge else 0
    if box.kind in POLY_KINDS:
        pts = scaled_points(box.kind, x, y, w, h, box.point or "right")
        draw.polygon(pts, fill=tuple(fill) if fill else None, outline=edge, width=pen)
    elif box.kind == "oval":
        draw.ellipse((x, y, x + w, y + h), fill=tuple(fill) if fill else None, outline=edge, width=pen)
    elif box.kind == "rect" and (fill or edge):
        draw.rectangle((x, y, x + w, y + h), fill=tuple(fill) if fill else None, outline=edge, width=pen)
    elif box.kind == "round" and (fill or edge):
        radius = _px(box.radius) if box.radius > 0 else _px(0.12)
        draw.rounded_rectangle(
            (x, y, x + w, y + h), radius=radius, fill=tuple(fill) if fill else None, outline=edge, width=pen
        )
    elif box.kind == "line":
        if box.point:
            _draw_connector(draw, box, theme, x, y, w, h)
        else:
            color = color_of(theme, box.fill or box.color, theme.line)
            if h <= w:
                draw.line((x, y + h // 2, x + w, y + h // 2), fill=tuple(color), width=max(h, 1))
            else:
                draw.line((x + w // 2, y, x + w // 2, y + h), fill=tuple(color), width=max(w, 1))
    elif box.kind == "image":
        _paste_png(img, box.image, box.x, box.y, box.w)
    elif box.kind == "chart":
        if box.fill:
            draw.rectangle((x, y, x + w, y + h), fill=tuple(color_of(theme, box.fill, slide_bg)))
        _draw_chart(draw, box, theme, text)
    elif box.kind == "table":
        _draw_table(draw, box, theme, text)
    elif box.kind == "text":
        if fill:
            draw.rectangle((x, y, x + w, y + h), fill=tuple(fill))
        ink = color_of(theme, box.color, theme.ink)
        text.block(draw, (x, y, w, h), box.text, size=box.size, color=ink, bold=box.bold, align=box.align, anchor=box.anchor)
    elif box.kind == "bullets":
        if fill:
            draw.rectangle((x, y, x + w, y + h), fill=tuple(fill))
        ink = color_of(theme, box.color, theme.ink)
        indent = _px(0.24)
        top = y + _px(0.05)
        step = round(_pt2px(box.size) * line_factor(box.size))
        gap = round(bullet_gap(box.size) * SCALE / 72.0)
        for item in box.items or ([box.text] if box.text else []):
            if not str(item).strip():
                continue
            lines = text.wrap(draw, str(item), max(w - indent - _px(0.12), 10), box.size)
            dot = _px(0.055)
            dot_y = top + step // 2 - dot
            draw.rectangle((x + _px(0.04), dot_y, x + _px(0.04) + dot * 2, dot_y + dot * 2), fill=tuple(theme.accent))
            for line in lines:
                if line:
                    text.draw_line(draw, (x + indent, top), line, box.size, False, ink)
                top += step
            top += gap
    if box.kind in SHAPE_KINDS and box.text:
        # 形状里的字：跟成稿一样默认居中。带尖角的形状左右各让出一点，字不压到尖上。
        inset = _px(0.10) if box.kind in {"chevron", "pentagon", "arrow"} else 0
        text.block(
            draw,
            (x + inset, y, max(w - inset * 2, 10), h),
            box.text,
            size=box.size,
            color=color_of(theme, box.color, theme.ink),
            bold=box.bold,
            align=box.align if box.align != "left" else "center",
            anchor="middle",
        )


def _footer(draw, theme: Theme, slide_bg, footer: str, page: int, text: _Text) -> None:
    tint = theme.cover_muted if is_dark(slide_bg) else theme.muted
    label = f"{footer}  ·  {page}" if footer else str(page)
    text.block(draw, (_px(MARGIN_X), _px(FOOTER_TOP), _px(10.6), _px(0.32)), label, size=10, color=tint)
    text.block(draw, (_px(11.4), _px(FOOTER_TOP), _px(1.3), _px(0.32)), f"{page:02d}", size=10, color=tint, align="right")


def _band_chrome(draw, theme: Theme) -> None:
    draw.rectangle((0, 0, PAGE_W, _px(BAND_H)), fill=tuple(theme.surface))
    draw.rectangle((0, _px(BAND_H), _px(RULE_LEFT), _px(BAND_H + RULE_H)), fill=tuple(theme.accent2))
    draw.rectangle((_px(RULE_LEFT), _px(BAND_H), PAGE_W, _px(BAND_H + RULE_H)), fill=tuple(theme.line))


def _bar_chrome(draw, theme: Theme) -> None:
    """通用主题的内页顶栏：顶边一条 + 左侧竖条，与 render.Painter.chrome 同源。"""
    draw.rectangle((0, 0, PAGE_W, _px(0.08)), fill=tuple(theme.accent2))
    draw.rectangle((0, 0, _px(0.12), PAGE_H), fill=tuple(theme.accent2))


def _on(theme: Theme, bg) -> tuple[int, int, int]:
    return (255, 255, 255) if is_dark(bg) else theme.ink


def _rect(draw, x: float, y: float, w: float, h: float, color) -> None:
    draw.rectangle((_px(x), _px(y), _px(x + w), _px(y + h)), fill=tuple(color))


def _cover_grid_raster(img, draw, spec: Slide, theme: Theme, page: int, footer: str, text: _Text) -> None:
    _paste_png(img, logo_asset(theme, dark=False), *cover_logo_rect(theme)[:3])
    band_y, band_h = COVER_BAND_Y, COVER_BAND_H
    draw.rectangle((0, _px(band_y), PAGE_W, _px(band_y + band_h)), fill=tuple(theme.cover_bg))
    draw.rectangle((0, _px(band_y + band_h), PAGE_W, _px(band_y + band_h + COVER_GOLD_H)), fill=tuple(theme.bar))
    text_x = MARGIN_X
    emblem = emblem_asset(theme, dark=True)
    if emblem:
        ex, ey, ew, _eh = emblem_rect(theme)
        _paste_png(img, emblem, ex, ey, ew)
        text_x = cover_text_x(theme)
    width = CANVAS_W - text_x - MARGIN_X
    title = spec.title or " "
    title_h = max(text_height(title, width, COVER_TITLE_PT, theme.font), 0.62)
    subtitle_h = text_height(spec.subtitle, width, COVER_SUB_PT, theme.font) if spec.subtitle else 0.0
    block_h = title_h + (subtitle_h + 0.30 if subtitle_h else 0.0)
    title_y = band_y + (band_h - block_h) / 2 + (0.16 if spec.kicker else 0.0)
    if spec.kicker:
        text.block(draw, (_px(text_x), _px(title_y - 0.46), _px(width), _px(0.36)), spec.kicker, size=COVER_KICKER_PT, color=theme.cover_muted)
    text.block(draw, (_px(text_x), _px(title_y), _px(width), _px(title_h)), title, size=COVER_TITLE_PT, color=theme.cover_ink, bold=True)
    if spec.subtitle:
        text.block(
            draw,
            (_px(text_x), _px(title_y + title_h + 0.30), _px(width), _px(subtitle_h)),
            spec.subtitle,
            size=COVER_SUB_PT,
            color=theme.cover_muted,
        )
    if spec.meta:
        text.block(draw, (_px(MARGIN_X), _px(6.02), _px(12.0), _px(0.40)), spec.meta, size=COVER_META_PT, color=theme.muted)
    _footer(draw, theme, theme.bg, footer, page, text)


def _section_raster(draw, spec: Slide, theme: Theme, page: int, footer: str, text: _Text) -> None:
    band_y, band_h, block_w = SECTION_BAND_Y, SECTION_BAND_H, SECTION_BLOCK_W
    draw.rectangle((0, _px(band_y), PAGE_W, _px(band_y + band_h)), fill=tuple(theme.cover_bg))
    draw.rectangle((0, _px(band_y), _px(block_w), _px(band_y + band_h)), fill=tuple(shade(theme.cover_bg, SECTION_BLOCK_TINT)))
    mark = (spec.kicker or f"{page:02d}").strip()
    text.block(draw, (0, _px(band_y), _px(block_w), _px(band_h)), mark, size=section_mark_pt(mark), color=theme.cover_ink, bold=True, align="center", anchor="middle")
    text.block(
        draw,
        (_px(block_w + SECTION_TEXT_GAP), _px(band_y), _px(CANVAS_W - block_w - SECTION_TEXT_GAP - MARGIN_X), _px(band_h)),
        spec.title or " ",
        size=SECTION_TITLE_PT,
        color=theme.cover_ink,
        bold=True,
        anchor="middle",
    )
    if spec.subtitle:
        text.block(draw, (_px(MARGIN_X), _px(band_y + band_h + 0.44), _px(12.0), _px(0.80)), spec.subtitle, size=SECTION_SUB_PT, color=theme.muted)
    _footer(draw, theme, theme.bg, footer, page, text)


def _cover_plain_raster(draw, spec: Slide, theme: Theme, page: int, footer: str, text: _Text):
    """grid 之外的五种封面版式，坐标照抄 render._cover。返回页底色供页脚定色。

    别把这些当"非 grid 就不用管"——换企业模板就是走这条路，画不准等于自查瞎了。
    """
    style = theme.cover
    slide_bg = theme.cover_bg if style == "bar" else theme.bg

    def line(x, y, w, h, body, size, color, *, bold=False):
        if body:
            text.block(draw, (_px(x), _px(y), _px(w), _px(h)), body, size=size, color=color, bold=bold)

    if style == "bar":
        _rect(draw, 0, 0, 0.16, CANVAS_H, theme.bar)
        line(MARGIN_X, 1.55, 11.8, 0.36, spec.kicker, 14, theme.cover_muted)
        _rect(draw, MARGIN_X, 2.05, 2.15, 0.055, theme.bar)
        line(MARGIN_X, 2.25, 11.8, 1.35, spec.title or " ", 40, theme.cover_ink, bold=True)
        line(MARGIN_X, 3.75, 11.8, 1.1, spec.subtitle, 18, theme.cover_muted)
        line(MARGIN_X, 6.15, 11.8, 0.4, spec.meta, 13, theme.cover_muted)
    elif style == "split":
        _rect(draw, 0, 0, 5.15, CANVAS_H, theme.cover_bg)
        line(0.55, 2.15, 4.3, 0.4, spec.kicker, 13, theme.cover_muted)
        line(0.55, 2.6, 4.3, 2.2, spec.title or " ", 32, theme.cover_ink, bold=True)
        line(5.7, 2.7, 6.8, 2.4, spec.subtitle, 18, theme.ink)
        line(5.7, 6.15, 6.8, 0.4, spec.meta, 13, theme.muted)
    elif style == "band":
        _rect(draw, 0, 0, CANVAS_W, 1.42, theme.accent)
        line(MARGIN_X, 0.48, 12.0, 0.55, spec.kicker or footer, 16, _on(theme, theme.accent))
        line(MARGIN_X, 2.15, 12.0, 1.4, spec.title or " ", 40, theme.ink, bold=True)
        line(MARGIN_X, 3.7, 12.0, 1.1, spec.subtitle, 18, theme.muted)
        line(MARGIN_X, 6.15, 12.0, 0.4, spec.meta, 13, theme.muted)
    elif style == "mark":
        _rect(draw, MARGIN_X, 2.05, 0.55, 0.55, theme.accent)
        line(MARGIN_X, 1.35, 12.0, 0.4, spec.kicker, 13, theme.accent)
        line(MARGIN_X, 2.75, 12.0, 1.5, spec.title or " ", 42, theme.ink, bold=True)
        line(MARGIN_X, 4.4, 12.0, 1.1, spec.subtitle, 18, theme.muted)
        line(MARGIN_X, 6.15, 12.0, 0.4, spec.meta, 13, theme.muted)
    else:
        line(MARGIN_X, 1.7, 12.0, 0.4, spec.kicker, 13, theme.accent)
        line(MARGIN_X, 2.25, 12.0, 1.5, spec.title or " ", 40, theme.ink, bold=True)
        _rect(draw, MARGIN_X, 3.9, 1.6, 0.05, theme.accent)
        line(MARGIN_X, 4.15, 12.0, 1.1, spec.subtitle, 18, theme.muted)
        line(MARGIN_X, 6.15, 12.0, 0.4, spec.meta, 13, theme.muted)
    _footer(draw, theme, slide_bg, footer, page, text)
    return slide_bg


def _section_bar_raster(draw, spec: Slide, theme: Theme, page: int, footer: str, text: _Text) -> None:
    """非 band 主题的章节页：深色满版 + 左侧竖条，照抄 render._section。"""
    _rect(draw, 0, 0, 0.16, CANVAS_H, theme.bar)
    text.block(draw, (_px(MARGIN_X), _px(2.35), _px(12.0), _px(0.4)), spec.kicker or " ", size=14, color=theme.cover_muted)
    text.block(draw, (_px(MARGIN_X), _px(2.85), _px(12.0), _px(1.6)), spec.title or " ", size=36, color=theme.cover_ink, bold=True)
    if spec.subtitle:
        text.block(draw, (_px(MARGIN_X), _px(4.6), _px(12.0), _px(0.8)), spec.subtitle, size=16, color=theme.cover_muted)
    _footer(draw, theme, theme.cover_bg, footer, page, text)


def _macro_raster(draw, spec: Slide, theme: Theme, page: int, footer: str, text: _Text) -> None:
    """cover/section 之外的宏页：只画标题和条目的粗排。技能要求内容页走 boxes，
    这些宏本就不该出现在正式稿里；画个诚实的骨架，别装成成稿。"""
    y = 0.42
    if theme.chrome == "band":
        _band_chrome(draw, theme)
        y = BAND_H + RULE_H + 0.26
    elif theme.chrome == "bar" and not is_dark(theme.bg):
        # chrome 还有第三个值 none，那是「顶栏全关」，别顺手画成 bar
        _bar_chrome(draw, theme)
        y = 0.50
    if spec.kicker:
        text.block(draw, (_px(MARGIN_X), _px(y), _px(12.0), _px(0.32)), spec.kicker, size=12, color=theme.accent, bold=True)
        y += 0.34
    text.block(draw, (_px(MARGIN_X), _px(y), _px(12.0), _px(0.62)), spec.title or " ", size=26, color=theme.accent2 if theme.chrome == "band" else theme.ink, bold=True)
    y += 0.80
    lines = spec.items or spec.left or ([spec.subtitle] if spec.subtitle else [])
    body = Box(kind="bullets", x=MARGIN_X, y=y, w=12.05, h=6.6 - y, items=[str(item) for item in lines], size=18)
    if lines:
        _draw_box(None, draw, body, theme, theme.bg, text)
    _footer(draw, theme, theme.bg, footer, page, text)


def render_slide_image(spec: Slide, theme: Theme, page: int, footer: str):
    """单页画成 PIL Image。几何与成稿同源。"""
    from PIL import Image, ImageDraw

    text = _Text(theme.font)
    slide_bg = color_of(theme, spec.bg, theme.bg)
    if spec.kind == "cover" and not spec.boxes:
        if theme.cover == "grid":
            img = Image.new("RGB", (PAGE_W, PAGE_H), tuple(theme.bg))
            _cover_grid_raster(img, ImageDraw.Draw(img), spec, theme, page, footer, text)
            return img
        base = theme.cover_bg if theme.cover == "bar" else theme.bg
        img = Image.new("RGB", (PAGE_W, PAGE_H), tuple(base))
        draw = ImageDraw.Draw(img)
        cover_bg = _cover_plain_raster(draw, spec, theme, page, footer, text)
        if wants_logo(theme, []):
            _paste_png(img, logo_asset(theme, dark=is_dark(cover_bg)), *cover_logo_rect(theme)[:3])
        return img
    if spec.kind == "section" and not spec.boxes:
        if theme.chrome == "band":
            img = Image.new("RGB", (PAGE_W, PAGE_H), tuple(theme.bg))
            draw = ImageDraw.Draw(img)
            _section_raster(draw, spec, theme, page, footer, text)
            _paste_png(img, logo_asset(theme, dark=False), *logo_rect(theme)[:3])
            return img
        img = Image.new("RGB", (PAGE_W, PAGE_H), tuple(theme.cover_bg))
        draw = ImageDraw.Draw(img)
        _section_bar_raster(draw, spec, theme, page, footer, text)
        if wants_logo(theme, []):
            _paste_png(img, logo_asset(theme, dark=is_dark(theme.cover_bg)), *logo_rect(theme)[:3])
        return img
    img = Image.new("RGB", (PAGE_W, PAGE_H), tuple(slide_bg))
    draw = ImageDraw.Draw(img)
    if not spec.boxes:
        _macro_raster(draw, spec, theme, page, footer, text)
        if wants_logo(theme, []):
            dark = is_dark(slide_bg)
            _paste_png(img, logo_asset(theme, dark=dark), *logo_rect(theme)[:3])
        return img
    if wants_band(theme, spec.kind, spec.boxes, slide_bg):
        _band_chrome(draw, theme)
    elif theme.chrome == "bar" and not is_dark(slide_bg):
        _bar_chrome(draw, theme)
    for box in spec.boxes:
        _draw_box(img, draw, box, theme, slide_bg, text)
    if wants_logo(theme, spec.boxes):
        dark = is_dark(slide_bg)
        _paste_png(img, logo_asset(theme, dark=dark), *(cover_logo_rect(theme) if spec.kind == "cover" else logo_rect(theme))[:3])
    _footer(draw, theme, slide_bg, footer, page, text)
    return img


def render_deck_png(deck: Deck, path: str, theme: Theme | None = None, *, cols: int = 2) -> str:
    """整稿画成一张联络表 PNG，页码顺序从左到右、从上到下。"""
    from PIL import Image

    resolved = theme or resolve_theme(deck.theme, deck.theme_overrides)
    footer = deck.footer or deck.title
    images = [render_slide_image(spec, resolved, page, footer) for page, spec in enumerate(deck.slides, start=1)]
    if not images:
        raise ValueError("空稿画不了快照")
    cols = max(1, min(cols, len(images)))
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new(
        "RGB",
        (cols * PAGE_W + (cols + 1) * SHEET_GAP, rows * PAGE_H + (rows + 1) * SHEET_GAP),
        SHEET_BG,
    )
    for index, img in enumerate(images):
        col, row = index % cols, index // cols
        sheet.paste(img, (SHEET_GAP + col * (PAGE_W + SHEET_GAP), SHEET_GAP + row * (PAGE_H + SHEET_GAP)))
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp~")
    sheet.save(tmp, format="PNG")
    tmp.replace(target)
    logger.info("光栅快照 path=%s pages=%s theme=%s", target, len(images), resolved.id)
    return str(target)


def page_origin(index: int, total: int, *, cols: int = 2) -> tuple[int, int]:
    """联络表里第 index 页（0 起）的左上角像素坐标。测试采样用。"""
    cols = max(1, min(cols, total))
    col, row = index % cols, index // cols
    return SHEET_GAP + col * (PAGE_W + SHEET_GAP), SHEET_GAP + row * (PAGE_H + SHEET_GAP)
