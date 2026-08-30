"""顶栏色带与标识的几何。渲染和预览共用一份，免得两边画得不一样。

尺寸是对照两份数字化审计培训类企业原稿量出来的：
顶部一条 0.95 英寸的白带，带下压一条 0.075 英寸的细线，细线左段 2.05 英寸走深色、
右段走浅灰一直到右缘；标识摆在带内右上角。
"""

from __future__ import annotations

from witty_agent.plugins.pptx_kit.assets import asset_path, asset_size
from witty_agent.plugins.pptx_kit.schema import CANVAS_W, Box
from witty_agent.plugins.pptx_kit.themes import Theme, color_of

MARGIN_X = 0.62

BAND_H = 0.95
RULE_H = 0.075
RULE_LEFT = 2.05
BAND_BOTTOM = BAND_H + RULE_H

LOGO_W = 1.90
LOGO_TOP = 0.24
LOGO_NAME = "witty-logo"
BAND_NAME = "witty-band"

# 封面标识更大：两份原稿封面右上分别是 2.82 / 3.40 英寸宽
COVER_LOGO_W = 2.60
COVER_LOGO_TOP = 0.30

# 封面通栏色带：带内左徽标、右标题，带底压一条金线
COVER_BAND_Y = 2.00
COVER_BAND_H = 3.35
COVER_GOLD_H = 0.07
COVER_EMBLEM_X = 0.98
COVER_EMBLEM_W = 1.75
COVER_DIVIDER_X = 3.12
COVER_TEXT_X = 3.52

# 章节通栏色带：左边切一块浅一档的同色放大号数（原稿左块 #0D8176 比主带 #006F6B 浅）
SECTION_BAND_Y = 2.35
SECTION_BAND_H = 2.45
SECTION_BLOCK_W = 3.30
SECTION_TEXT_GAP = 0.40
SECTION_BLOCK_TINT = 1.16

# 封面章节的字号（pt）。这几号字是压在固定高度的色带里的，成稿按 pt 排、预览按
# cqw 排，两边必须同源，不然色带里的文字组一边居中一边溢出。
COVER_KICKER_PT = 14
COVER_TITLE_PT = 34
COVER_SUB_PT = 17
COVER_META_PT = 13
SECTION_MARK_PT = 52
SECTION_MARK_LONG_PT = 30
SECTION_TITLE_PT = 32
SECTION_SUB_PT = 16


def section_mark_pt(mark: str) -> int:
    """号数字号：两位数放到最大，写成「第一章」这类长标记就降一档免得撑破色块。"""
    return SECTION_MARK_PT if len(mark.strip()) <= 2 else SECTION_MARK_LONG_PT


# 标识落在右上角，正文压过来就让位
_FOREGROUND = frozenset({"text", "bullets", "table", "chart", "image"})
_GROUND = frozenset({"rect", "round"})


def is_dark(color) -> bool:
    """深底判定。渲染里选白字/白标识都走这一条线，阈值只在这里改。"""
    return (color[0] * 299 + color[1] * 587 + color[2] * 114) / 1000 < 148


def shade(color, factor: float) -> tuple[int, int, int]:
    """同色系加深/提亮一档。用来在通栏色带里再切出一块，不引入第二个色。"""
    return tuple(max(0, min(255, int(round(channel * factor)))) for channel in color)  # type: ignore[return-value]


def logo_stem(theme: Theme) -> str:
    return (theme.logo or "").strip()


def logo_asset(theme: Theme, *, dark: bool) -> str:
    stem = logo_stem(theme)
    if not stem:
        return ""
    if dark:
        return asset_path(f"{stem}-white") or asset_path(stem)
    return asset_path(stem)


def emblem_asset(theme: Theme, *, dark: bool) -> str:
    stem = (theme.emblem or "").strip()
    if not stem:
        return ""
    if dark:
        return asset_path(f"{stem}-white") or asset_path(stem)
    return asset_path(stem)


def asset_ratio(stem: str, fallback: float = 3.0) -> float:
    width, height = asset_size((stem or "").strip())
    return width / height if width and height else fallback


def logo_rect(theme: Theme) -> tuple[float, float, float, float]:
    """标识宽度定死，高度按图自身比例算，换图不会拉变形。"""
    height = LOGO_W / max(asset_ratio(logo_stem(theme)), 0.1)
    return CANVAS_W - MARGIN_X - LOGO_W, LOGO_TOP, LOGO_W, height


def cover_logo_rect(theme: Theme) -> tuple[float, float, float, float]:
    """封面标识：同在右上角，按原稿放大一号。"""
    height = COVER_LOGO_W / max(asset_ratio(logo_stem(theme)), 0.1)
    return CANVAS_W - MARGIN_X - COVER_LOGO_W, COVER_LOGO_TOP, COVER_LOGO_W, height


def emblem_rect(theme: Theme) -> tuple[float, float, float, float]:
    """封面大徽标：横向定在带内左侧，纵向在色带里居中。"""
    height = COVER_EMBLEM_W / max(asset_ratio((theme.emblem or "").strip(), 1.0), 0.1)
    return (
        COVER_EMBLEM_X,
        COVER_BAND_Y + (COVER_BAND_H - height) / 2,
        COVER_EMBLEM_W,
        height,
    )


def cover_text_x(theme: Theme) -> float:
    """封面正文起始横坐标：摆了徽标就让到徽标右侧，没摆就贴左页边。"""
    return COVER_TEXT_X if emblem_asset(theme, dark=True) else MARGIN_X


def _overlap(rect: tuple[float, float, float, float], box: Box) -> float:
    x, y, w, h = rect
    dx = min(x + w, box.x + box.w) - max(x, box.x)
    dy = min(y + h, box.y + box.h) - max(y, box.y)
    return max(0.0, dx) * max(0.0, dy)


def _covers(box: Box, rect: tuple[float, float, float, float]) -> bool:
    x, y, w, h = rect
    return (
        box.x - 0.05 <= x
        and box.y - 0.05 <= y
        and box.x + box.w + 0.05 >= x + w
        and box.y + box.h + 0.05 >= y + h
    )


def wants_logo(theme: Theme, boxes: list[Box]) -> bool:
    """主题带标识就一定摆（用户底线：主题标识必须在）。
    页里自己写了 witty-logo 盒子的，按作者的摆法走，引擎不再补。
    内容压到标识区不再静默藏标识——那是版式错误，交给 lint 报出来改版式。
    """
    if not logo_stem(theme):
        return False
    return not any((box.name or "") == LOGO_NAME for box in boxes)


def logo_zone_clash(theme: Theme, box: Box) -> bool:
    """前景内容压进右上角标识区。lint 用它报问题，让作者挪内容而不是丢标识。"""
    if not logo_stem(theme):
        return False
    return box.kind in _FOREGROUND and _overlap(logo_rect(theme), box) > 0.02


def wants_band(theme: Theme, kind: str, boxes: list[Box], slide_bg) -> bool:
    """boxes 页要不要自动压顶部白带：band 主题的内容页都压，和宏页一个模板。
    封面/章节页、深底页、作者自画 witty-band 的页不压。
    """
    if theme.chrome != "band" or kind in {"cover", "section"}:
        return False
    if is_dark(slide_bg):
        return False
    return not any((box.name or "") == BAND_NAME for box in boxes)


def logo_ground(theme: Theme, boxes: list[Box], slide_bg) -> tuple[int, int, int]:
    """标识身后压着什么颜色：最后一个盖住它的色块，否则页底色。"""
    rect = logo_rect(theme)
    ground = slide_bg
    for box in boxes:
        if box.kind in _GROUND and box.fill and _covers(box, rect):
            ground = color_of(theme, box.fill, ground)
    return ground
