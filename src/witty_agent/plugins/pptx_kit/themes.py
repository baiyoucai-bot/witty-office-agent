"""本地主题目录。颜色和字体都是 token，不走公网模板。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from witty_agent.prompts import get_prompt

Color = tuple[int, int, int]


@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    font: str
    bg: Color
    surface: Color
    ink: Color
    muted: Color
    accent: Color
    accent2: Color
    on_accent: Color
    cover_bg: Color
    cover_ink: Color
    cover_muted: Color
    bar: Color
    card: Color
    card_ink: Color
    line: Color
    cover: str
    # 内页顶栏样式：bar=左侧竖条，band=顶部白带+压线（企业模板的样子）
    chrome: str = "bar"
    # 内置标识名，空=不摆。深底自动换 <名字>-white
    logo: str = ""
    # 封面用的大徽标名，空=不摆
    emblem: str = ""


def _rgb(r: int, g: int, b: int) -> Color:
    return (r, g, b)


THEMES: dict[str, Theme] = {
    "custom": Theme(
        id="custom",
        label="空白底稿，颜色和版式由本次稿现场决定",
        font="Microsoft YaHei",
        bg=_rgb(0xFF, 0xFF, 0xFF),
        surface=_rgb(0xF4, 0xF4, 0xF4),
        ink=_rgb(0x1C, 0x1C, 0x22),
        muted=_rgb(0x66, 0x66, 0x66),
        accent=_rgb(0x1C, 0x1C, 0x22),
        accent2=_rgb(0x1C, 0x1C, 0x22),
        on_accent=_rgb(0xFF, 0xFF, 0xFF),
        cover_bg=_rgb(0xFF, 0xFF, 0xFF),
        cover_ink=_rgb(0x1C, 0x1C, 0x22),
        cover_muted=_rgb(0x66, 0x66, 0x66),
        bar=_rgb(0x1C, 0x1C, 0x22),
        card=_rgb(0xF7, 0xF7, 0xF7),
        card_ink=_rgb(0x1C, 0x1C, 0x22),
        line=_rgb(0xDD, 0xDD, 0xDD),
        cover="minimal",
    ),
    "grid": Theme(
        id="grid",
        label="青绿金线企业风：白带压线+青绿通栏，对照数字化审计培训稿",
        font="Microsoft YaHei",
        bg=_rgb(0xF2, 0xF3, 0xF3),
        surface=_rgb(0xFF, 0xFF, 0xFF),
        ink=_rgb(0x22, 0x30, 0x36),
        muted=_rgb(0x5E, 0x6C, 0x6B),
        accent=_rgb(0x01, 0x70, 0x6C),
        accent2=_rgb(0x07, 0x66, 0x55),
        on_accent=_rgb(0xFF, 0xFF, 0xFF),
        cover_bg=_rgb(0x01, 0x70, 0x6C),
        cover_ink=_rgb(0xFF, 0xFF, 0xFF),
        cover_muted=_rgb(0xBD, 0xD5, 0xD0),
        bar=_rgb(0xFF, 0xC2, 0x0A),
        card=_rgb(0xFF, 0xFF, 0xFF),
        card_ink=_rgb(0x22, 0x30, 0x36),
        line=_rgb(0xC9, 0xD4, 0xD2),
        cover="grid",
        chrome="band",
        logo="",
        emblem="",
    ),
    "grid-navy": Theme(
        id="grid-navy",
        label="藏青+金线，公文汇报",
        font="Microsoft YaHei",
        bg=_rgb(0xF4, 0xF6, 0xF8),
        surface=_rgb(0xFF, 0xFF, 0xFF),
        ink=_rgb(0x1C, 0x1C, 0x22),
        muted=_rgb(0x5C, 0x5C, 0x68),
        accent=_rgb(0xC4, 0xA3, 0x5A),
        accent2=_rgb(0x0B, 0x3D, 0x6E),
        on_accent=_rgb(0x0B, 0x3D, 0x6E),
        cover_bg=_rgb(0x0B, 0x3D, 0x6E),
        cover_ink=_rgb(0xFF, 0xFF, 0xFF),
        cover_muted=_rgb(0xC4, 0xA3, 0x5A),
        bar=_rgb(0xC4, 0xA3, 0x5A),
        card=_rgb(0xFF, 0xFF, 0xFF),
        card_ink=_rgb(0x1C, 0x1C, 0x22),
        line=_rgb(0xD5, 0xD8, 0xDE),
        cover="bar",
    ),
    "paper-ink": Theme(
        id="paper-ink",
        label="暖纸+暗红，杂志感",
        font="Microsoft YaHei",
        bg=_rgb(0xF5, 0xF1, 0xE8),
        surface=_rgb(0xFF, 0xFB, 0xF4),
        ink=_rgb(0x1B, 0x16, 0x12),
        muted=_rgb(0x6A, 0x5E, 0x54),
        accent=_rgb(0x8B, 0x1E, 0x3F),
        accent2=_rgb(0x1B, 0x16, 0x12),
        on_accent=_rgb(0xFF, 0xFB, 0xF4),
        cover_bg=_rgb(0xF5, 0xF1, 0xE8),
        cover_ink=_rgb(0x1B, 0x16, 0x12),
        cover_muted=_rgb(0x8B, 0x1E, 0x3F),
        bar=_rgb(0x8B, 0x1E, 0x3F),
        card=_rgb(0xFF, 0xFB, 0xF4),
        card_ink=_rgb(0x1B, 0x16, 0x12),
        line=_rgb(0xE0, 0xD6, 0xC8),
        cover="minimal",
    ),
    "swiss-red": Theme(
        id="swiss-red",
        label="白底+一点红，瑞士国际主义",
        font="Microsoft YaHei",
        bg=_rgb(0xFF, 0xFF, 0xFF),
        surface=_rgb(0xF4, 0xF4, 0xF4),
        ink=_rgb(0x11, 0x11, 0x11),
        muted=_rgb(0x5A, 0x5A, 0x5A),
        accent=_rgb(0xE3, 0x06, 0x13),
        accent2=_rgb(0x11, 0x11, 0x11),
        on_accent=_rgb(0xFF, 0xFF, 0xFF),
        cover_bg=_rgb(0xFF, 0xFF, 0xFF),
        cover_ink=_rgb(0x11, 0x11, 0x11),
        cover_muted=_rgb(0xE3, 0x06, 0x13),
        bar=_rgb(0xE3, 0x06, 0x13),
        card=_rgb(0xF4, 0xF4, 0xF4),
        card_ink=_rgb(0x11, 0x11, 0x11),
        line=_rgb(0xDD, 0xDD, 0xDD),
        cover="mark",
    ),
    "night-cyan": Theme(
        id="night-cyan",
        label="暗底青绿，科技汇报",
        font="Microsoft YaHei",
        bg=_rgb(0x12, 0x18, 0x1E),
        surface=_rgb(0x1B, 0x25, 0x2E),
        ink=_rgb(0xE8, 0xF1, 0xF5),
        muted=_rgb(0x8A, 0x9A, 0xA6),
        accent=_rgb(0x2E, 0xC4, 0xB6),
        accent2=_rgb(0x4D, 0x9D, 0xE0),
        on_accent=_rgb(0x0B, 0x14, 0x18),
        cover_bg=_rgb(0x0B, 0x12, 0x18),
        cover_ink=_rgb(0xE8, 0xF1, 0xF5),
        cover_muted=_rgb(0x2E, 0xC4, 0xB6),
        bar=_rgb(0x2E, 0xC4, 0xB6),
        card=_rgb(0x1B, 0x25, 0x2E),
        card_ink=_rgb(0xE8, 0xF1, 0xF5),
        line=_rgb(0x2A, 0x38, 0x44),
        cover="bar",
    ),
    "forest": Theme(
        id="forest",
        label="深绿封面+米纸内页",
        font="Microsoft YaHei",
        bg=_rgb(0xF3, 0xF0, 0xE7),
        surface=_rgb(0xFF, 0xFC, 0xF5),
        ink=_rgb(0x1A, 0x2E, 0x22),
        muted=_rgb(0x5B, 0x6B, 0x60),
        accent=_rgb(0xC4, 0xA3, 0x5A),
        accent2=_rgb(0x1F, 0x3D, 0x2B),
        on_accent=_rgb(0x1A, 0x2E, 0x22),
        cover_bg=_rgb(0x1F, 0x3D, 0x2B),
        cover_ink=_rgb(0xF3, 0xF0, 0xE7),
        cover_muted=_rgb(0xC4, 0xA3, 0x5A),
        bar=_rgb(0xC4, 0xA3, 0x5A),
        card=_rgb(0xFF, 0xFC, 0xF5),
        card_ink=_rgb(0x1A, 0x2E, 0x22),
        line=_rgb(0xD8, 0xD2, 0xC4),
        cover="bar",
    ),
    "dawn": Theme(
        id="dawn",
        label="浅底珊瑚带，轻汇报",
        font="Microsoft YaHei",
        bg=_rgb(0xFB, 0xF7, 0xF3),
        surface=_rgb(0xFF, 0xFF, 0xFF),
        ink=_rgb(0x2C, 0x24, 0x20),
        muted=_rgb(0x7A, 0x6A, 0x62),
        accent=_rgb(0xC4, 0x5C, 0x4A),
        accent2=_rgb(0x2C, 0x24, 0x20),
        on_accent=_rgb(0xFF, 0xFF, 0xFF),
        cover_bg=_rgb(0xFB, 0xF7, 0xF3),
        cover_ink=_rgb(0x2C, 0x24, 0x20),
        cover_muted=_rgb(0xC4, 0x5C, 0x4A),
        bar=_rgb(0xC4, 0x5C, 0x4A),
        card=_rgb(0xFF, 0xFF, 0xFF),
        card_ink=_rgb(0x2C, 0x24, 0x20),
        line=_rgb(0xE8, 0xDC, 0xD4),
        cover="band",
    ),
    "mono": Theme(
        id="mono",
        label="黑白分割，极简",
        font="Microsoft YaHei",
        bg=_rgb(0xFA, 0xFA, 0xFA),
        surface=_rgb(0xFF, 0xFF, 0xFF),
        ink=_rgb(0x11, 0x11, 0x11),
        muted=_rgb(0x66, 0x66, 0x66),
        accent=_rgb(0x11, 0x11, 0x11),
        accent2=_rgb(0x11, 0x11, 0x11),
        on_accent=_rgb(0xFF, 0xFF, 0xFF),
        cover_bg=_rgb(0x11, 0x11, 0x11),
        cover_ink=_rgb(0xFF, 0xFF, 0xFF),
        cover_muted=_rgb(0xBB, 0xBB, 0xBB),
        bar=_rgb(0x11, 0x11, 0x11),
        card=_rgb(0xFF, 0xFF, 0xFF),
        card_ink=_rgb(0x11, 0x11, 0x11),
        line=_rgb(0xDD, 0xDD, 0xDD),
        cover="split",
    ),
}

ALIASES = {
    "青绿": "grid",
    "数字化审计": "grid",
    "审计培训": "grid",
    "藏青": "grid-navy",
    "公文": "grid-navy",
    "默认": "grid-navy",
    "navy": "grid-navy",
    "gold": "grid-navy",
    "杂志": "paper-ink",
    "纸感": "paper-ink",
    "暖纸": "paper-ink",
    "editorial": "paper-ink",
    "瑞士": "swiss-red",
    "极简红": "swiss-red",
    "国际主义": "swiss-red",
    "暗色": "night-cyan",
    "科技": "night-cyan",
    "深色": "night-cyan",
    "dark": "night-cyan",
    "森林": "forest",
    "绿色": "forest",
    "coral": "dawn",
    "浅色": "dawn",
    "柔和": "dawn",
    "黑白": "mono",
    "极简": "mono",
    "mono": "mono",
}

_COLOR_FIELDS = {
    "bg",
    "surface",
    "ink",
    "muted",
    "accent",
    "accent2",
    "on_accent",
    "cover_bg",
    "cover_ink",
    "cover_muted",
    "bar",
    "card",
    "card_ink",
    "line",
}

_COVER_STYLES = frozenset({"bar", "split", "band", "minimal", "mark", "grid"})
_CHROME_STYLES = frozenset({"bar", "band", "none"})
# 关标识/徽标的写法。写 off 才是真的关掉，空串会被当成「没提这一项」。
_OFF = frozenset({"off", "none", "no", "无", "关"})


def parse_hex(value: str) -> Color:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(get_prompt("pptx_bad_color", value=value))
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError as exc:
        raise ValueError(get_prompt("pptx_bad_color", value=value)) from exc


def css_hex(color: Color) -> str:
    return f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"


def _luma_channel(value: int) -> float:
    scaled = value / 255.0
    if scaled <= 0.04045:
        return scaled / 12.92
    return ((scaled + 0.055) / 1.055) ** 2.4


def relative_luma(color: Color) -> float:
    return 0.2126 * _luma_channel(color[0]) + 0.7152 * _luma_channel(color[1]) + 0.0722 * _luma_channel(color[2])


def contrast_ratio(left: Color, right: Color) -> float:
    """WCAG 对比度。1:1 是同色，21:1 是纯黑纯白。"""
    l1 = relative_luma(left)
    l2 = relative_luma(right)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def match_theme_id(name: str) -> str:
    key = (name or "").strip()
    if not key:
        return "custom"
    folded = key.casefold()
    if folded in THEMES:
        return folded
    if folded in ALIASES:
        return ALIASES[folded]
    for alias, theme_id in ALIASES.items():
        if alias in folded or alias in key:
            return theme_id
    return "custom"


def color_of(theme: Theme, value: str, fallback: Color) -> Color:
    text = (value or "").strip()
    if not text:
        return fallback
    if hasattr(theme, text):
        token = getattr(theme, text)
        if isinstance(token, tuple) and len(token) == 3:
            return token
    if not text.startswith("#"):
        text = "#" + text
    return parse_hex(text)


def resolve_theme(theme_id: str, overrides: dict[str, str] | None = None) -> Theme:
    matched = match_theme_id(theme_id)
    theme = THEMES.get(matched) or THEMES["custom"]
    if matched == "custom":
        theme = replace(theme, id=(theme_id or "").strip() or "custom")
    if not overrides:
        return theme
    changes: dict[str, object] = {}
    for key, raw in overrides.items():
        name = str(key).strip()
        value = str(raw).strip()
        if not value:
            continue
        if name in _COLOR_FIELDS:
            changes[name] = parse_hex(value)
        elif name == "font":
            changes["font"] = value
        elif name == "cover" and value in _COVER_STYLES:
            changes["cover"] = value
        elif name == "chrome" and value in _CHROME_STYLES:
            changes["chrome"] = value
        elif name in {"logo", "emblem"}:
            changes[name] = "" if value.casefold() in _OFF else value
    if not changes:
        return theme
    return replace(theme, **changes)


def list_themes() -> list[Theme]:
    return list(THEMES.values())


def themes_text() -> str:
    lines = []
    for item in THEMES.values():
        extra = f"  标识={item.logo}" if item.logo else ""
        lines.append(f"{item.id}  {item.label}  cover={item.cover}  chrome={item.chrome}{extra}")
    lines.append(get_prompt("pptx_theme_custom_hint"))
    return "\n".join(lines)


_FONT_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Microsoft YaHei",
        (
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\msyh.ttf",
            "/Library/Fonts/Microsoft YaHei.ttf",
            "/Library/Fonts/msyh.ttc",
        ),
    ),
    (
        "PingFang SC",
        ("/System/Library/Fonts/PingFang.ttc",),
    ),
    (
        "Hiragino Sans GB",
        (
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Hiragino Sans GB.ttc",
        ),
    ),
    (
        "Songti SC",
        ("/System/Library/Fonts/Supplemental/Songti.ttc",),
    ),
    (
        "Noto Sans CJK SC",
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        ),
    ),
)


def resolve_installed_font(preferred: str) -> str:
    wanted = (preferred or "").strip() or "Microsoft YaHei"
    for name, paths in _FONT_FILES:
        if name.casefold() == wanted.casefold() and any(Path(item).is_file() for item in paths):
            return name
    if os.name == "nt":
        return wanted
    for name, paths in _FONT_FILES:
        if any(Path(item).is_file() for item in paths):
            return name
    return wanted


def theme_payload(theme: Theme) -> dict[str, str]:
    # 空的 logo/emblem 要写成 off：回读时空串会被当成没提，会把种子里的标识又捡回来。
    payload = {
        "id": theme.id,
        "font": theme.font,
        "cover": theme.cover,
        "chrome": theme.chrome,
        "logo": theme.logo or "off",
        "emblem": theme.emblem or "off",
    }
    for key in _COLOR_FIELDS:
        payload[key] = css_hex(getattr(theme, key))
    return payload


def theme_from_payload(payload: dict[str, str]) -> Theme:
    theme_id = str(payload.get("id") or "custom").strip() or "custom"
    overrides = {str(key): str(value) for key, value in payload.items() if key != "id" and value}
    return resolve_theme(theme_id, overrides)
