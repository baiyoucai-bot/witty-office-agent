"""把本引擎写出的受约束 HTML 读回语义稿。不是通用网页转换器。"""

from __future__ import annotations

import json
import re
from html import unescape

from witty_agent.plugins.pptx_kit.schema import Deck, Slide, parse_slide
from witty_agent.prompts import get_prompt

_SECTION = re.compile(r"<section\b([^>]*)>(.*?)</section>", re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r'([:\w-]+)\s*=\s*["\']([^"\']*)["\']')
_TAG = re.compile(r"<[^>]+>")


def _attrs(blob: str) -> dict[str, str]:
    return {key.casefold(): unescape(value) for key, value in _ATTR.findall(blob or "")}


def _text(html: str) -> str:
    cleaned = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"</(p|h1|h2|h3|li|div|tr)>", "\n", cleaned, flags=re.IGNORECASE)
    return unescape(_TAG.sub("", cleaned)).replace("\xa0", " ").strip()


def _pick(html: str, tag: str, cls: str = "") -> str:
    if cls:
        pattern = rf"<{tag}\b[^>]*class=['\"][^'\"]*{re.escape(cls)}[^'\"]*['\"][^>]*>(.*?)</{tag}>"
    else:
        pattern = rf"<{tag}\b[^>]*>(.*?)</{tag}>"
    match = re.search(pattern, html or "", re.IGNORECASE | re.DOTALL)
    return _text(match.group(1)) if match else ""


def _items(html: str) -> list[str]:
    return [_text(item) for item in re.findall(r"<li\b[^>]*>(.*?)</li>", html or "", re.I | re.S) if _text(item)]


def _col(html: str, side: str) -> tuple[str, list[str]]:
    match = re.search(
        rf"<div\b[^>]*data-side=['\"]{side}['\"][^>]*>(.*?)</div>",
        html or "",
        re.I | re.S,
    )
    if not match:
        return "", []
    block = match.group(1)
    return _pick(block, "h3"), _items(block)


def _parse_section(attr_blob: str, body: str) -> Slide:
    attrs = _attrs(attr_blob)
    classes = attrs.get("class", "")
    kind = attrs.get("data-kind") or ""
    if not kind:
        for token in classes.split():
            if token not in {"slide", "is-on"} and not token.startswith("cover-"):
                kind = token
                break
    raw: dict = {"kind": kind or "bullets", "title": _pick(body, "h1", "title") or _pick(body, "h2", "title")}
    if attrs.get("data-bg"):
        raw["bg"] = attrs["data-bg"]
    boxes = []
    for match in re.finditer(r"<div\b([^>]*\bdata-box\b[^>]*)>(.*?)</div>", body, re.I | re.S):
        battrs = _attrs(match.group(1))
        inner = match.group(2)
        box: dict = {
            "kind": battrs.get("data-box") or "text",
            "x": battrs.get("data-x") or 0,
            "y": battrs.get("data-y") or 0,
            "w": battrs.get("data-w") or 1,
            "h": battrs.get("data-h") or 0.4,
            "size": battrs.get("data-size") or 18,
            "align": battrs.get("data-align") or "left",
            "fill": battrs.get("data-fill") or "",
            "color": battrs.get("data-color") or "",
            "bold": battrs.get("data-bold") == "1",
            "italic": battrs.get("data-italic") == "1",
            "name": battrs.get("data-name") or "",
            "font": battrs.get("data-font") or "",
            "text": _text(inner),
            "items": _items(inner),
        }
        if battrs.get("data-anchor"):
            box["anchor"] = battrs["data-anchor"]
        if battrs.get("data-radius"):
            box["radius"] = battrs["data-radius"]
        if battrs.get("data-shadow"):
            box["shadow"] = battrs["data-shadow"] == "1"
        if battrs.get("data-stroke"):
            box["stroke"] = battrs["data-stroke"]
        if battrs.get("data-stroke-w"):
            box["stroke_w"] = battrs["data-stroke-w"]
        if battrs.get("data-point"):
            box["point"] = battrs["data-point"]
        if box["items"]:
            # 要点已经进 items，别再把同一段文字塞进 text，否则回读会画两遍
            box["text"] = ""
        if "<table" in inner.lower():
            box["headers"] = [_text(h) for h in re.findall(r"<th\b[^>]*>(.*?)</th>", inner, re.I | re.S)]
            box["rows"] = []
            for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", re.split(r"<tbody", inner, flags=re.I)[-1], re.I | re.S):
                cells = [_text(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.I | re.S)]
                if cells:
                    box["rows"].append(cells)
            box["text"] = ""
        img = re.search(r"data-image=['\"]([^'\"]+)", inner)
        if img:
            box["image"] = unescape(img.group(1))
        if (box.get("kind") == "chart") or battrs.get("data-chart"):
            box["kind"] = "chart"
            box["chart"] = battrs.get("data-chart") or "column"
            box["text"] = battrs.get("data-text") or ""
            script = re.search(
                r"<script\b[^>]*class=['\"]chart-data['\"][^>]*>(.*?)</script>",
                inner,
                re.I | re.S,
            )
            if script:
                try:
                    payload = json.loads(unescape(script.group(1)))
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict):
                    box["chart"] = payload.get("chart") or box["chart"]
                    box["categories"] = payload.get("categories") or []
                    box["series"] = payload.get("series") or []
                    box["colors"] = payload.get("colors") or []
        boxes.append(box)
    if boxes:
        raw["boxes"] = boxes
        raw["kind"] = kind or "custom"
        notes = re.search(r"<script\b[^>]*class=['\"]notes['\"][^>]*>(.*?)</script>", body, re.I | re.S)
        if notes:
            raw["notes"] = _text(notes.group(1))
        return parse_slide(raw)
    raw["kicker"] = _pick(body, "p", "kicker")
    raw["subtitle"] = _pick(body, "p", "subtitle")
    raw["meta"] = _pick(body, "p", "meta")
    raw["items"] = _items(body)
    if "kpi" in (kind, classes) or "metrics" in body:
        metrics = []
        for match in re.finditer(r"<div\b([^>]*)class=['\"][^'\"]*metric[^'\"]*['\"][^>]*>(.*?)</div>", body, re.I | re.S):
            mattrs = _attrs(match.group(1) + " " + match.group(0)[:180])
            metrics.append(
                {
                    "label": _pick(match.group(2), "span", "label") or _text(match.group(2)),
                    "value": mattrs.get("data-value") or _pick(match.group(2), "span", "value"),
                    "note": mattrs.get("data-note") or _pick(match.group(2), "span", "note"),
                }
            )
        if metrics:
            raw["kind"] = "kpi"
            raw["metrics"] = metrics
    if re.search(r"class=['\"][^'\"]*cards", body, re.I):
        cards = []
        for match in re.finditer(r"<article\b[^>]*>(.*?)</article>", body, re.I | re.S):
            cards.append({"title": _pick(match.group(1), "h3"), "body": _pick(match.group(1), "p")})
        if cards:
            raw["kind"] = "cards"
            raw["cards"] = cards
    if "<table" in body.lower():
        headers = [_text(h) for h in re.findall(r"<th\b[^>]*>(.*?)</th>", body, re.I | re.S)]
        rows = []
        for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", re.split(r"<tbody", body, flags=re.I)[-1], re.I | re.S):
            cells = [_text(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.I | re.S)]
            if cells:
                rows.append(cells)
        if headers or rows:
            raw["kind"] = "table"
            raw["headers"] = headers
            raw["rows"] = rows
    if re.search(r"<ol\b[^>]*class=['\"][^'\"]*steps", body, re.I):
        steps = []
        for match in re.finditer(r"<li\b([^>]*)>(.*?)</li>", body, re.I | re.S):
            lattrs = _attrs(match.group(1))
            steps.append(
                {
                    "title": lattrs.get("data-title") or _pick(match.group(2), "h3"),
                    "body": _pick(match.group(2), "p"),
                }
            )
        if steps:
            raw["kind"] = "process"
            raw["steps"] = steps
    if "quote" in (kind, classes) or "<blockquote" in body.lower():
        raw["kind"] = "quote"
        raw["quote"] = _pick(body, "p") or _text(body)
        raw["by"] = _pick(body, "cite")
    left_title, left = _col(body, "left")
    right_title, right = _col(body, "right")
    if left or right:
        raw["left_title"] = left_title
        raw["left"] = left
        raw["right_title"] = right_title
        raw["right"] = right
        if "compare" in classes or "compare" in body[:80].lower() or kind == "compare":
            raw["kind"] = "compare"
        elif kind not in {"compare"}:
            raw["kind"] = "two_col"
    img = re.search(r"<img\b([^>]*)>", body, re.I)
    if img:
        iattrs = _attrs(img.group(1))
        raw["image"] = iattrs.get("data-image") or iattrs.get("src") or ""
        raw["kind"] = "picture"
    notes = re.search(r"<script\b[^>]*class=['\"]notes['\"][^>]*>(.*?)</script>", body, re.I | re.S)
    if notes:
        raw["notes"] = _text(notes.group(1))
    return parse_slide(raw)


def parse_html(html: str) -> Deck:
    text = html or ""
    if "data-witty-deck" not in text and 'class="deck"' not in text and "class='deck'" not in text:
        raise ValueError(get_prompt("pptx_html_unrecognized"))
    sections = _SECTION.findall(text)
    if not sections:
        raise ValueError(get_prompt("pptx_html_unrecognized"))
    html_attrs = {}
    head = re.search(r"<html\b([^>]*)>", text, re.I)
    if head:
        html_attrs = _attrs(head.group(1))
    slides = [_parse_section(attr, body) for attr, body in sections]
    overrides = {}
    for key, value in html_attrs.items():
        if key.startswith("data-") and key not in {"data-witty-deck", "data-theme", "data-title", "data-footer"}:
            overrides[key[5:].replace("-", "_")] = value
    title = html_attrs.get("data-title") or next((s.title for s in slides if s.title), "未命名")
    return Deck(
        title=title,
        theme=html_attrs.get("data-theme") or "grid-navy",
        theme_overrides=overrides,
        footer=html_attrs.get("data-footer") or "",
        slides=slides,
    )
