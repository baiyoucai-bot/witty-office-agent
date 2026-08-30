"""可编辑 PPTX 的本地稿引擎：语义稿 → 原生形状 + HTML 预览。"""

from witty_agent.plugins.pptx_kit.html_parse import parse_html
from witty_agent.plugins.pptx_kit.lint import format_issues, lint_deck, lint_slide
from witty_agent.plugins.pptx_kit.preview import append_html, render_html, write_html
from witty_agent.plugins.pptx_kit.render import (
    append_slide,
    append_slides,
    insert_slide,
    replace_slide,
    write_pptx,
)
from witty_agent.plugins.pptx_kit.schema import (
    SLIDE_KINDS,
    Box,
    Card,
    Deck,
    Metric,
    Slide,
    Step,
    parse_deck,
    parse_slide,
)
from witty_agent.plugins.pptx_kit.themes import list_themes, resolve_theme, themes_text

__all__ = [
    "SLIDE_KINDS",
    "Box",
    "Card",
    "Deck",
    "Metric",
    "Slide",
    "Step",
    "append_html",
    "append_slide",
    "append_slides",
    "insert_slide",
    "format_issues",
    "lint_deck",
    "lint_slide",
    "list_themes",
    "parse_deck",
    "parse_html",
    "parse_slide",
    "render_html",
    "replace_slide",
    "resolve_theme",
    "themes_text",
    "write_html",
    "write_pptx",
]
