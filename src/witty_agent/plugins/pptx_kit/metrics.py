"""量文字：一段话在给定宽度里占几行、几英寸高。

渲染和自检共用这一份行距与字宽算法，免得「画的」和「查的」两套结果。
字宽先问 fonts 模块拿本机字体的真实 advance width（fontTools 读 hmtx），
本机没装该字体就回落「中文全宽、拉丁半宽」的启发式（误差 ±10% 量级）。
"""

from __future__ import annotations

import math

from witty_agent.plugins.pptx_kit.fonts import char_em as _measured_em

PT_PER_INCH = 72.0
# 文本框左右内边距（render.Painter.text 用 0.06 英寸）
TEXT_PAD = 0.12
# 项目符号悬挂缩进
BULLET_INDENT = 0.24


def line_factor(size: int) -> float:
    """行距倍数。大字收紧，正文放松，标题才不会松垮。"""
    if size >= 30:
        return 1.06
    if size >= 22:
        return 1.14
    if size >= 15:
        return 1.28
    return 1.34


def bullet_gap(size: int) -> float:
    """两条要点之间的额外留白，单位 pt。"""
    return max(6.0, size * 0.55)


def _char_em(ch: str) -> float:
    code = ord(ch)
    if ch in {"\t"}:
        return 2.0
    if ch == " ":
        return 0.3
    if code < 0x2E80:
        # 拉丁、数字、半角标点
        if ch.isupper() or ch.isdigit():
            return 0.62
        if ch.isalpha():
            return 0.53
        return 0.42
    if 0x2E80 <= code <= 0xA4CF or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFF60:
        # 中日韩、全角标点
        return 1.0
    return 0.9


def char_em(ch: str, font: str = "") -> float:
    """单字宽度（em）。给了字体名且本机装了，就用真实宽度；否则启发式。"""
    if font:
        measured = _measured_em(ch, font)
        if measured is not None:
            return measured
    return _char_em(ch)


def text_em(text: str, font: str = "") -> float:
    """一行文字的宽度，单位 em（1 em = 字号）。"""
    return sum(char_em(ch, font) for ch in text)


def wrap_lines(text: str, width: float, size: int, font: str = "") -> int:
    """text 在 width 英寸内、size pt 下大约折几行。显式换行按段算。"""
    if not text.strip():
        return 0
    usable = max(width, 0.2)
    per_line_em = usable * PT_PER_INCH / max(size, 1)
    if per_line_em <= 0:
        return 1
    total = 0
    for chunk in text.replace("\r\n", "\n").split("\n"):
        if not chunk.strip():
            total += 1
            continue
        total += max(1, math.ceil(text_em(chunk, font) / per_line_em - 1e-6))
    return total


def text_height(text: str, width: float, size: int, font: str = "") -> float:
    """一个 text 盒子里的文字大约占多高，单位英寸。"""
    lines = wrap_lines(text, max(width - TEXT_PAD, 0.2), size, font)
    if not lines:
        return 0.0
    return lines * size * line_factor(size) / PT_PER_INCH + 0.08


def bullets_height(items: list[str], width: float, size: int, font: str = "") -> float:
    """一组要点大约占多高，单位英寸。含悬挂缩进和条间距。"""
    lines = [wrap_lines(item, max(width - TEXT_PAD - BULLET_INDENT, 0.2), size, font) for item in items if item.strip()]
    if not lines:
        return 0.0
    body = sum(lines) * size * line_factor(size) / PT_PER_INCH
    return body + bullet_gap(size) * max(len(lines) - 1, 0) / PT_PER_INCH + 0.08


def table_row_height(size: int) -> float:
    """一行表格的舒服高度，单位英寸。"""
    return max(0.34, size * 2.6 / PT_PER_INCH)


def table_height(rows: int, size: int) -> float:
    """表头 + rows 行数据的贴合高度，单位英寸。"""
    unit = table_row_height(size)
    return unit * 1.18 + unit * max(rows, 0)
