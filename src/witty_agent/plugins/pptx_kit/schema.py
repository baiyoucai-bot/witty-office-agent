"""语义幻灯片稿。这是 HTML 预览和可编辑 PPTX 的唯一结构。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from witty_agent.prompts import get_prompt

SLIDE_KINDS = frozenset(
    {
        "cover",
        "section",
        "bullets",
        "two_col",
        "kpi",
        "cards",
        "table",
        "process",
        "compare",
        "quote",
        "closing",
        "picture",
    }
)

KIND_ALIASES = {
    "title": "cover",
    "toc": "section",
    "divider": "section",
    "bullet": "bullets",
    "list": "bullets",
    "two-col": "two_col",
    "twocol": "two_col",
    "cols": "two_col",
    "kpi-grid": "kpi",
    "metrics": "kpi",
    "card": "cards",
    "image": "picture",
    "photo": "picture",
    "comparison": "compare",
    "vs": "compare",
    "steps": "process",
    "flow": "process",
    "end": "closing",
    "thanks": "closing",
    "quote-block": "quote",
}

# 画布尺寸的唯一出处。chrome / render / lint 都从这里取，免得四份常量各自漂移。
CANVAS_W = 13.333
CANVAS_H = 7.5

MAX_ITEMS = 7
MAX_METRICS = 4
MAX_CARDS = 3
MAX_STEPS = 5
MAX_TABLE_COLS = 6
MAX_TABLE_ROWS = 8
MAX_SLIDES = 40
# flex 树一页能解出几十个盒子（一张卡就是底 + 图标 + 标题 + 正文四个），
# 上限按「排得满的一页」放宽；手写 boxes 的老稿远够不到。
MAX_BOXES = 220
MAX_CHART_CATS = 16
MAX_CHART_SERIES = 8
BOX_KINDS = frozenset(
    {
        "rect",
        "round",
        "text",
        "bullets",
        "table",
        "image",
        "line",
        "chart",
        # 矢量层：画流程图、时间轴、架构图用。都是 PowerPoint 原生形状，WPS 里可编辑。
        "oval",
        "diamond",
        "triangle",
        "chevron",
        "pentagon",
        "arrow",
    }
)
# 能填色、能描边、能在里面写字的形状。text/bullets/table/chart/image 不算。
SHAPE_KINDS = frozenset(
    {"rect", "round", "oval", "diamond", "triangle", "chevron", "pentagon", "arrow"}
)
_KIND_ALIASES_BOX = {
    "circle": "oval",
    "ellipse": "oval",
    "rhombus": "diamond",
    "tri": "triangle",
    "banner": "chevron",
    "home": "pentagon",
}
CHART_TYPES = frozenset({"bar", "column", "col", "line", "pie", "doughnut", "donut"})
_CHART_ALIASES = {
    "col": "column",
    "bar-clustered": "bar",
    "column-clustered": "column",
    "horizontal": "bar",
    "donut": "doughnut",
    "line-markers": "line",
}


@dataclass
class Metric:
    label: str
    value: str
    note: str = ""


@dataclass
class Card:
    title: str
    body: str = ""


@dataclass
class Step:
    title: str
    body: str = ""


@dataclass
class ChartSeries:
    name: str
    values: list[float] = field(default_factory=list)


@dataclass
class Box:
    kind: str
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 0.4
    text: str = ""
    items: list[str] = field(default_factory=list)
    fill: str = ""
    color: str = ""
    size: int = 18
    bold: bool = False
    italic: bool = False
    align: str = "left"
    anchor: str = "top"
    font: str = ""
    name: str = ""
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    image: str = ""
    chart: str = ""
    categories: list[str] = field(default_factory=list)
    series: list[ChartSeries] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    shadow: bool | None = None
    radius: float = 0.0
    # 描边：架构图那种「白底 + 主色框」靠这个。0 宽等于不描。
    stroke: str = ""
    stroke_w: float = 0.0
    # chevron / pentagon / arrow 的朝向，也决定 line 画不画箭头
    point: str = ""


@dataclass
class Slide:
    kind: str = "custom"
    title: str = ""
    subtitle: str = ""
    kicker: str = ""
    meta: str = ""
    items: list[str] = field(default_factory=list)
    left_title: str = ""
    left: list[str] = field(default_factory=list)
    right_title: str = ""
    right: list[str] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    quote: str = ""
    by: str = ""
    image: str = ""
    notes: str = ""
    bg: str = ""
    boxes: list[Box] = field(default_factory=list)


@dataclass
class Deck:
    title: str
    theme: str = "grid-navy"
    theme_overrides: dict[str, str] = field(default_factory=dict)
    footer: str = ""
    slides: list[Slide] = field(default_factory=list)


def _clip(items: list[str], limit: int) -> list[str]:
    cleaned = [item.strip() for item in items if str(item).strip()]
    return cleaned[:limit]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _clip(value.splitlines(), MAX_ITEMS)
    if isinstance(value, list):
        return _clip([str(item) for item in value], MAX_ITEMS)
    return _clip([str(value)], MAX_ITEMS)


def _normalize_kind(raw: str) -> str:
    key = (raw or "").strip().lower().replace(" ", "_")
    kind = KIND_ALIASES.get(key, key)
    if kind not in SLIDE_KINDS:
        raise ValueError(get_prompt("pptx_bad_kind", kind=raw or "(空)"))
    return kind


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chart_number(value: Any) -> float:
    text = str(value).strip().replace(",", "").replace("%", "").replace("，", "")
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _normalize_chart(raw: str) -> str:
    key = (raw or "").strip().lower().replace("_", "-")
    key = _CHART_ALIASES.get(key, key)
    if key in {"bar", "column", "line", "pie", "doughnut"}:
        return key
    return "column"


def _parse_series(raw: Any) -> list[ChartSeries]:
    series: list[ChartSeries] = []
    if not isinstance(raw, list):
        return series
    for item in raw[:MAX_CHART_SERIES]:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("label") or "").strip() or f"系列{len(series) + 1}"
            values = [_chart_number(v) for v in (item.get("values") or item.get("data") or [])][:MAX_CHART_CATS]
            series.append(ChartSeries(name=name, values=values))
        elif isinstance(item, list):
            series.append(
                ChartSeries(name=f"系列{len(series) + 1}", values=[_chart_number(v) for v in item][:MAX_CHART_CATS])
            )
    return series


def _series_from_rows(headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[ChartSeries]]:
    if not rows:
        return [], []
    categories = _clip([row[0] if row else "" for row in rows], MAX_CHART_CATS)
    width = max((len(row) for row in rows), default=1)
    series: list[ChartSeries] = []
    for col in range(1, min(width, MAX_CHART_SERIES + 1)):
        name = headers[col] if col < len(headers) else f"系列{col}"
        values = [_chart_number(row[col] if col < len(row) else 0) for row in rows[:MAX_CHART_CATS]]
        series.append(ChartSeries(name=name or f"系列{col}", values=values))
    if not series and categories:
        series.append(ChartSeries(name="数值", values=[1.0] * len(categories)))
    return categories, series


def parse_box(raw: dict[str, Any]) -> Box | None:
    kind = str(raw.get("kind") or "text").strip().lower()
    kind = _KIND_ALIASES_BOX.get(kind, kind)
    chart = str(raw.get("chart") or raw.get("chart_type") or "").strip().lower()
    if kind in CHART_TYPES:
        # `kind: "column"` 这类是图表的简写。但 "line" 同时是「一条色条 / 连接线」的盒子，
        # 简写会把所有分隔线都吞成折线图——只有真带了数据才当图表。
        has_data = bool(raw.get("categories") or raw.get("series") or raw.get("rows") or raw.get("headers"))
        if kind != "line" or has_data:
            chart = kind
            kind = "chart"
    if kind not in BOX_KINDS:
        kind = "text"
    if kind == "chart":
        chart = _normalize_chart(chart)
    try:
        size = int(float(raw.get("size", 18)))
    except (TypeError, ValueError):
        size = 18
    rows: list[list[str]] = []
    for row in raw.get("rows") or []:
        if isinstance(row, list):
            rows.append([str(cell) for cell in row][:MAX_TABLE_COLS])
        else:
            rows.append([str(row)])
        if len(rows) >= MAX_TABLE_ROWS:
            break
    items = raw.get("items")
    if isinstance(items, list):
        parsed_items = _clip([str(item) for item in items], 16)
    else:
        parsed_items = _as_list(items)
    headers = _clip([str(h) for h in (raw.get("headers") or [])], MAX_TABLE_COLS)
    categories = _clip([str(c) for c in (raw.get("categories") or [])], MAX_CHART_CATS)
    series = _parse_series(raw.get("series"))
    if kind == "chart" and not categories and rows:
        categories, series = _series_from_rows(headers, rows)
    colors = [str(c).strip() for c in (raw.get("colors") or []) if str(c).strip()][:MAX_CHART_SERIES]
    box = Box(
        kind=kind,
        x=_num(raw.get("x")),
        y=_num(raw.get("y")),
        w=_num(raw.get("w") if raw.get("w") is not None else raw.get("width"), 1.0),
        h=_num(raw.get("h") if raw.get("h") is not None else raw.get("height"), 0.4),
        text=str(raw.get("text") or "").strip(),
        items=parsed_items,
        fill=str(raw.get("fill") or "").strip(),
        color=str(raw.get("color") or "").strip(),
        size=max(8, min(size, 96)),
        bold=bool(raw.get("bold")),
        italic=bool(raw.get("italic")),
        align=str(raw.get("align") or "left").strip() or "left",
        anchor=str(raw.get("anchor") or "top").strip() or "top",
        font=str(raw.get("font") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        headers=headers,
        rows=rows,
        image=str(raw.get("image") or "").strip(),
        chart=chart,
        categories=categories,
        series=series,
        colors=colors,
        shadow=None if raw.get("shadow") is None else bool(raw.get("shadow")),
        radius=max(0.0, min(_num(raw.get("radius"), 0.0), 0.5)),
        stroke=str(raw.get("stroke") or "").strip(),
        stroke_w=max(0.0, min(_num(raw.get("stroke_w") if raw.get("stroke_w") is not None else raw.get("stroke_width"), 0.0), 6.0)),
        point=str(raw.get("point") or raw.get("dir_point") or "").strip().lower(),
    )
    if box.w <= 0 or box.h <= 0:
        return None
    return box


def _layout_boxes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """声明式 layout 树 → 绝对坐标盒子。这里晚 import，免得 flex 与 schema 打环。"""
    layout = raw.get("layout")
    if not isinstance(layout, dict):
        return []
    from witty_agent.plugins.pptx_kit.flex import solve_layout

    return solve_layout(layout, CANVAS_W, CANVAS_H)


def parse_slide(raw: dict[str, Any]) -> Slide:
    boxes: list[Box] = []
    # 手写 boxes 在前、layout 解出的在后：前者当底衬，后者压在上面。
    raw_boxes = [item for item in (raw.get("boxes") or []) if isinstance(item, dict)]
    for item in raw_boxes + _layout_boxes(raw):
        box = parse_box(item)
        if box is not None:
            boxes.append(box)
        if len(boxes) >= MAX_BOXES:
            break
    kind_raw = str(raw.get("kind") or "").strip()
    if boxes:
        kind = kind_raw or "custom"
    else:
        kind = _normalize_kind(kind_raw)
    metrics: list[Metric] = []
    for item in raw.get("metrics") or []:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if label or value:
                metrics.append(
                    Metric(label=label, value=value, note=str(item.get("note") or "").strip())
                )
        elif str(item).strip():
            metrics.append(Metric(label=str(item).strip(), value=""))
        if len(metrics) >= MAX_METRICS:
            break
    cards: list[Card] = []
    for item in raw.get("cards") or []:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("label") or "").strip()
            body = str(item.get("body") or item.get("text") or "").strip()
            if title or body:
                cards.append(Card(title=title, body=body))
        elif str(item).strip():
            cards.append(Card(title=str(item).strip()))
        if len(cards) >= MAX_CARDS:
            break
    steps: list[Step] = []
    for item in raw.get("steps") or []:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("label") or "").strip()
            body = str(item.get("body") or item.get("text") or "").strip()
            if title or body:
                steps.append(Step(title=title, body=body))
        elif str(item).strip():
            steps.append(Step(title=str(item).strip()))
        if len(steps) >= MAX_STEPS:
            break
    headers = _clip([str(h) for h in (raw.get("headers") or [])], MAX_TABLE_COLS)
    rows: list[list[str]] = []
    for row in raw.get("rows") or []:
        if isinstance(row, list):
            cells = [str(cell).strip() for cell in row][: MAX_TABLE_COLS or 1]
        else:
            cells = [str(row).strip()]
        if any(cells):
            rows.append(cells)
        if len(rows) >= MAX_TABLE_ROWS:
            break
    return Slide(
        kind=kind,
        title=str(raw.get("title") or "").strip(),
        subtitle=str(raw.get("subtitle") or "").strip(),
        kicker=str(raw.get("kicker") or "").strip(),
        meta=str(raw.get("meta") or "").strip(),
        items=_as_list(raw.get("items") or raw.get("bullets")),
        left_title=str(raw.get("left_title") or "").strip(),
        left=_as_list(raw.get("left")),
        right_title=str(raw.get("right_title") or "").strip(),
        right=_as_list(raw.get("right")),
        metrics=metrics,
        cards=cards,
        headers=headers,
        rows=rows,
        steps=steps,
        quote=str(raw.get("quote") or "").strip(),
        by=str(raw.get("by") or "").strip(),
        image=str(raw.get("image") or raw.get("image_path") or "").strip(),
        notes=str(raw.get("notes") or "").strip(),
        bg=str(raw.get("bg") or "").strip(),
        boxes=boxes,
    )


def parse_deck(raw: str | dict[str, Any]) -> Deck:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(get_prompt("pptx_bad_json"))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(get_prompt("pptx_bad_json")) from exc
    else:
        payload = raw
    if not isinstance(payload, dict):
        raise ValueError(get_prompt("pptx_bad_json"))
    slides_raw = payload.get("slides")
    if not isinstance(slides_raw, list) or not slides_raw:
        raise ValueError(get_prompt("pptx_no_slides"))
    slides = [parse_slide(item if isinstance(item, dict) else {"kind": "bullets", "items": [str(item)]}) for item in slides_raw[:MAX_SLIDES]]
    overrides = payload.get("theme_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    title = str(payload.get("title") or "").strip()
    if not title:
        for slide in slides:
            if slide.title:
                title = slide.title
                break
            for box in slide.boxes:
                if box.name == "witty-title" and box.text:
                    title = box.text
                    break
            if title:
                break
    if not title:
        title = "未命名"
    theme_raw = payload.get("theme")
    merged = {str(k): str(v) for k, v in overrides.items() if v is not None}
    if isinstance(theme_raw, dict):
        theme_id = str(theme_raw.get("id") or "custom").strip() or "custom"
        for key, value in theme_raw.items():
            if key == "id" or value is None:
                continue
            merged.setdefault(str(key), str(value))
    elif isinstance(theme_raw, str) and theme_raw.strip():
        theme_id = theme_raw.strip()
    else:
        theme_id = "custom" if merged else "grid-navy"
    return Deck(
        title=title,
        theme=theme_id,
        theme_overrides=merged,
        footer=str(payload.get("footer") or "").strip(),
        slides=slides,
    )
