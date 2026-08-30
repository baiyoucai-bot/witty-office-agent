"""版式检查：溢出、叠字、对比度、空腔。只报问题，不改稿。"""

from __future__ import annotations

from dataclasses import dataclass

from witty_agent.plugins.pptx_kit.chrome import LOGO_NAME, logo_zone_clash
from witty_agent.plugins.pptx_kit.metrics import bullets_height, text_height
from witty_agent.plugins.pptx_kit.schema import CANVAS_H, CANVAS_W, SHAPE_KINDS, Box, Deck, Slide
from witty_agent.plugins.pptx_kit.shapes import text_inset
from witty_agent.plugins.pptx_kit.themes import (
    Theme,
    color_of,
    contrast_ratio,
    css_hex,
    resolve_theme,
)
from witty_agent.prompts import get_prompt

_EPS = 0.05
_OVERLAP = 0.08
_MARGIN = 0.28
# 页脚带上沿。内容下沿越过这里就会压页脚。
_CONTENT_BOTTOM = 6.80
# 卡片里文字占不到这个比例就是空腔
_SLACK_RATIO = 0.55
_SLACK_MIN_H = 2.2
# 整页可视内容只到这里以上就是下半页空着
_LOW_PAGE = 4.90
_MIN_BODY_SIZE = 12
_FOREGROUND = frozenset({"text", "bullets", "table", "chart", "image"})
_BACKGROUND = frozenset({"line"} | SHAPE_KINDS)
_FLOW = frozenset({"table", "chart", "image"})


def _labeled(box: Box) -> bool:
    """自带文字的形状。流程带、圆点这些是内容不是装饰，得按前景查对比度和留白。"""
    return box.kind in SHAPE_KINDS and bool(box.text)


@dataclass(frozen=True)
class Issue:
    page: int
    code: str
    message: str


def _area(a: Box, b: Box) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.w, b.x + b.w)
    y2 = min(a.y + a.h, b.y + b.h)
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _label(box: Box, index: int) -> str:
    return box.name or get_prompt("pptx_issue_box_label", n=str(index + 1), kind=box.kind)


def _contains(outer: Box, inner: Box) -> bool:
    return (
        outer.x - _EPS <= inner.x
        and outer.y - _EPS <= inner.y
        and outer.x + outer.w + _EPS >= inner.x + inner.w
        and outer.y + outer.h + _EPS >= inner.y + inner.h
    )


def _full_bleed(box: Box) -> bool:
    return box.w >= CANVAS_W - 0.12 and box.h >= CANVAS_H - 0.12


def _panel(box: Box) -> bool:
    """通栏色块：横跨整幅或纵贯整幅。这是装饰底，不是卡片，不按空腔算。"""
    return box.w >= CANVAS_W - 0.12 or box.h >= CANVAS_H - 0.12


def _slide_bg(spec: Slide, theme: Theme) -> tuple[int, int, int]:
    return color_of(theme, spec.bg, theme.bg)


def _text_fill(box: Box, earlier: list[Box], slide_bg, theme: Theme) -> tuple[int, int, int]:
    if box.fill:
        return color_of(theme, box.fill, slide_bg)
    for other in reversed(earlier):
        if other.kind in _BACKGROUND and other.fill and _contains(other, box):
            return color_of(theme, other.fill, slide_bg)
    return slide_bg


def content_height(box: Box, font: str = "") -> float:
    """盒子里的文字大约要多高。非文字盒子返回 0。给了字体名就按真实字宽量。"""
    family = box.font or font
    if box.kind == "text":
        return text_height(box.text, box.w, box.size, family)
    if box.kind == "bullets":
        items = box.items or ([box.text] if box.text else [])
        return bullets_height(items, box.w, box.size, family)
    if _labeled(box):
        return text_height(box.text, max(box.w - text_inset(box.kind) * 2, 0.2), box.size, family)
    return 0.0


def _lint_edges(page: int, name: str, box: Box) -> list[Issue]:
    issues: list[Issue] = []
    if box.x < -_EPS or box.y < -_EPS or box.x + box.w > CANVAS_W + _EPS or box.y + box.h > CANVAS_H + _EPS:
        issues.append(Issue(page, "overflow", get_prompt("pptx_issue_overflow", name=name)))
    if box.kind not in _FOREGROUND and not _labeled(box):
        return issues
    right = box.x + box.w
    if box.x > _EPS and box.x < _MARGIN:
        issues.append(Issue(page, "edge", get_prompt("pptx_issue_edge_left", name=name, v=f"{box.x:.2f}")))
    if right < CANVAS_W - _EPS and right > CANVAS_W - _MARGIN:
        issues.append(Issue(page, "edge", get_prompt("pptx_issue_edge_right", name=name, v=f"{CANVAS_W - right:.2f}")))
    if box.y + box.h > _CONTENT_BOTTOM:
        issues.append(
            Issue(
                page,
                "footer_band",
                get_prompt("pptx_issue_footer_band", name=name, v=f"{box.y + box.h:.2f}", limit=f"{_CONTENT_BOTTOM:.2f}"),
            )
        )
    return issues


def _lint_box(page: int, index: int, box: Box, earlier: list[Box], spec: Slide, theme: Theme) -> list[Issue]:
    issues: list[Issue] = _lint_edges(page, _label(box, index), box)
    name = _label(box, index)
    if box.kind == "chart" and (not box.categories or not box.series):
        issues.append(Issue(page, "empty_chart", get_prompt("pptx_issue_empty_chart", name=name)))
    if box.kind == "table" and not box.headers and not box.rows:
        issues.append(Issue(page, "empty_table", get_prompt("pptx_issue_empty_table", name=name)))
    if box.kind == "text" and box.size >= 22 and not box.bold and box.name == "witty-title":
        issues.append(Issue(page, "title_weight", get_prompt("pptx_issue_title_weight", name=name)))
    if box.kind in {"text", "bullets"} or _labeled(box):
        if box.size < _MIN_BODY_SIZE:
            issues.append(
                Issue(page, "tiny_text", get_prompt("pptx_issue_tiny_text", name=name, size=str(box.size), min=str(_MIN_BODY_SIZE)))
            )
        need = content_height(box, theme.font)
        if need > box.h + 0.06:
            issues.append(
                Issue(
                    page,
                    "text_overflow",
                    get_prompt("pptx_issue_text_overflow", name=name, need=f"{need:.2f}", have=f"{box.h:.2f}"),
                )
            )
        ink = color_of(theme, box.color, theme.ink)
        fill = _text_fill(box, earlier, _slide_bg(spec, theme), theme)
        ratio = contrast_ratio(ink, fill)
        limit = 3.0 if box.size >= 14 or box.bold else 4.5
        if ratio < limit:
            issues.append(
                Issue(
                    page,
                    "contrast",
                    get_prompt(
                        "pptx_issue_contrast",
                        name=name,
                        ink=css_hex(ink),
                        fill=css_hex(fill),
                        ratio=f"{ratio:.1f}",
                        need=f"{limit:.1f}",
                    ),
                )
            )
    if box.kind in _FOREGROUND:
        for other_i, other in enumerate(earlier):
            if other.kind not in _FOREGROUND:
                # 形状是自己的底，字画在里面，压着不算撞车
                continue
            if _area(box, other) >= _OVERLAP:
                issues.append(
                    Issue(page, "overlap", get_prompt("pptx_issue_overlap", name=name, other=_label(other, other_i)))
                )
    return issues


def _lint_cards(page: int, boxes: list[Box], theme: Theme) -> list[Issue]:
    """卡片空腔：卡子高高的，里面只有一两行字。"""
    issues: list[Issue] = []
    for index, card in enumerate(boxes):
        if card.kind not in {"rect", "round"} or not card.fill or card.text:
            continue
        if card.h < _SLACK_MIN_H or card.w < 1.2 or _panel(card):
            continue
        inside = [
            item
            for item in boxes
            if item is not card and (item.kind in _FOREGROUND or _labeled(item)) and _contains(card, item)
        ]
        if not inside or any(item.kind in _FLOW for item in inside):
            continue
        used = sum(content_height(item, theme.font) for item in inside) + 0.44
        if used < card.h * _SLACK_RATIO:
            issues.append(
                Issue(
                    page,
                    "slack",
                    get_prompt("pptx_issue_slack", name=_label(card, index), have=f"{card.h:.2f}", used=f"{used:.2f}"),
                )
            )
    return issues


def _lint_corners(page: int, boxes: list[Box]) -> list[Issue]:
    """直角色条贴着圆角卡片同边界，圆角处会露出尖角。"""
    issues: list[Issue] = []
    rounds = [(i, item) for i, item in enumerate(boxes) if item.kind == "round"]
    for index, bar in enumerate(boxes):
        if bar.kind != "rect" or not bar.fill or bar.w > 0.35:
            continue
        for other_i, card in rounds:
            same_left = abs(bar.x - card.x) < 0.02 or abs(bar.x + bar.w - card.x - card.w) < 0.02
            same_span = abs(bar.y - card.y) < 0.02 and abs(bar.h - card.h) < 0.02
            if same_left and same_span:
                issues.append(
                    Issue(page, "corner", get_prompt("pptx_issue_corner", name=_label(bar, index), other=_label(card, other_i)))
                )
    return issues


def _lint_balance(page: int, boxes: list[Box]) -> list[Issue]:
    """下半页整块空着。可视盒子（有填充的底 + 前景内容）都不到腰线以下。"""
    visual = [
        item
        for item in boxes
        if not _full_bleed(item) and (item.kind in _FOREGROUND or item.fill or item.stroke)
    ]
    if len(visual) < 2:
        return []
    bottom = max(item.y + item.h for item in visual)
    if bottom >= _LOW_PAGE:
        return []
    return [Issue(page, "low_page", get_prompt("pptx_issue_low_page", bottom=f"{bottom:.2f}"))]


def _safe_fonts() -> frozenset[str]:
    from witty_agent.runtime import pptx_settings

    return frozenset(item.lower() for item in pptx_settings()["safe_fonts"])


def _lint_fonts(page: int, spec: Slide, theme: Theme) -> list[Issue]:
    """稿里用了域内清单之外的字体：放映机没装就跑版。清单在 runtime.toml [pptx]。"""
    safe = _safe_fonts()
    used: dict[str, str] = {}
    if page == 1 and theme.font and theme.font.lower() not in safe:
        used[theme.font.lower()] = theme.font
    for box in spec.boxes:
        if box.font and box.font.lower() not in safe:
            used.setdefault(box.font.lower(), box.font)
    return [
        Issue(page, "font_risk", get_prompt("pptx_issue_font_risk", font=name))
        for name in used.values()
    ]


def _lint_logo_zone(page: int, boxes: list[Box], theme: Theme) -> list[Issue]:
    """内容压进右上角标识区。标识一定会摆（不再静默让位），压住的是内容自己。"""
    if any((box.name or "") == LOGO_NAME for box in boxes):
        return []
    return [
        Issue(page, "logo_zone", get_prompt("pptx_issue_logo_zone", name=_label(box, index)))
        for index, box in enumerate(boxes)
        if logo_zone_clash(theme, box)
    ]


def lint_slide(spec: Slide, theme: Theme, page: int) -> list[Issue]:
    issues: list[Issue] = _lint_fonts(page, spec, theme)
    if not spec.boxes:
        # 封面/章节的宏版式是引擎按参考稿排的模板页，不算偷懒；内容页才要求 boxes。
        if spec.kind not in {"cover", "section"}:
            issues.append(Issue(page, "no_boxes", get_prompt("pptx_issue_no_boxes")))
        return issues
    if not any(box.name == "witty-title" or (box.kind == "text" and box.size >= 24) for box in spec.boxes):
        issues.append(Issue(page, "no_title", get_prompt("pptx_issue_no_title")))
    earlier: list[Box] = []
    for index, box in enumerate(spec.boxes):
        issues.extend(_lint_box(page, index, box, earlier, spec, theme))
        earlier.append(box)
    issues.extend(_lint_cards(page, spec.boxes, theme))
    issues.extend(_lint_corners(page, spec.boxes))
    issues.extend(_lint_balance(page, spec.boxes))
    issues.extend(_lint_logo_zone(page, spec.boxes, theme))
    return issues


def lint_deck(deck: Deck, theme: Theme | None = None) -> list[Issue]:
    resolved = theme or resolve_theme(deck.theme, deck.theme_overrides)
    issues: list[Issue] = []
    for page, spec in enumerate(deck.slides, start=1):
        issues.extend(lint_slide(spec, resolved, page))
    return issues


def format_issues(issues: list[Issue]) -> str:
    if not issues:
        return ""
    return "\n".join(get_prompt("pptx_issue_line", page=str(item.page), code=item.code, message=item.message) for item in issues)
