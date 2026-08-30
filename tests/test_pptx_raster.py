"""光栅快照的视觉回归：不依赖 LibreOffice、不开浏览器、不比字体反锯齿。

断言策略是「关键像素采样」：白带、压线、色带、标识这些纯色几何元素的位置和颜色
是确定的，字体渲染差异碰不到它们。模板画坏（白带丢了、标识没了、色带错位）当场红，
字体不同的机器上跑也不会假红。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.plugins.pptx_kit import parse_deck
from witty_agent.plugins.pptx_kit.chrome import BAND_H, RULE_H, RULE_LEFT
from witty_agent.plugins.pptx_kit.raster import (
    PAGE_H,
    PAGE_W,
    SCALE,
    page_origin,
    render_deck_png,
)
from witty_agent.plugins.pptx_kit.themes import resolve_theme


def _grid_deck() -> dict:
    return {
        "title": "青绿快照回归",
        "footer": "某某有限公司",
        "theme": "青绿",
        "slides": [
            {"kind": "cover", "title": "数字化审计培训", "subtitle": "审计部", "kicker": "内部培训"},
            {"kind": "section", "title": "平台总体介绍", "kicker": "01"},
            {
                "kind": "custom",
                "title": "要点",
                "boxes": [
                    {"kind": "text", "x": 0.7, "y": 0.42, "w": 9.8, "h": 0.62, "text": "要点", "size": 28, "bold": True, "color": "accent2", "name": "witty-title"},
                    {"kind": "round", "x": 0.7, "y": 1.5, "w": 5.9, "h": 3.2, "fill": "card"},
                    {"kind": "bullets", "x": 1.05, "y": 1.85, "w": 5.2, "h": 2.6, "items": ["覆盖全业务", "数据集中"], "size": 16, "name": "witty-body"},
                ],
            },
            {
                "kind": "custom",
                "title": "深底页",
                "bg": "accent2",
                "boxes": [
                    {"kind": "text", "x": 0.7, "y": 2.8, "w": 12.0, "h": 1.2, "text": "深底不压白带", "size": 30, "bold": True, "color": "#FFFFFF", "name": "witty-title"},
                ],
            },
        ],
    }


def _px(inches: float) -> int:
    return round(inches * SCALE)


class PptxRasterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PIL import Image

        cls.theme = resolve_theme("grid")
        cls.deck = parse_deck(_grid_deck())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = str(Path(cls.tmp.name) / "sheet.png")
        render_deck_png(cls.deck, cls.path, cls.theme)
        cls.sheet = Image.open(cls.path).convert("RGB")
        cls.total = len(cls.deck.slides)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sheet.close()
        cls.tmp.cleanup()

    def _pixel(self, page_index: int, x_in: float, y_in: float) -> tuple[int, int, int]:
        ox, oy = page_origin(page_index, self.total)
        return self.sheet.getpixel((ox + _px(x_in), oy + _px(y_in)))

    def _region_has_ink(self, page_index: int, x_in: float, y_in: float, w_in: float, h_in: float, bg) -> bool:
        """区域里是否有明显异于底色的像素（标识贴上去了就有）。"""
        ox, oy = page_origin(page_index, self.total)
        crop = self.sheet.crop((ox + _px(x_in), oy + _px(y_in), ox + _px(x_in + w_in), oy + _px(y_in + h_in)))
        for pixel in crop.getdata():
            if sum(abs(a - b) for a, b in zip(pixel, bg)) > 90:
                return True
        return False

    def test_sheet_size_and_pages(self) -> None:
        """联络表尺寸按页数排布：2 列 x 2 行，页与页之间有留缝。"""
        from witty_agent.plugins.pptx_kit.raster import SHEET_GAP

        self.assertEqual(self.sheet.width, 2 * PAGE_W + 3 * SHEET_GAP)
        self.assertEqual(self.sheet.height, 2 * PAGE_H + 3 * SHEET_GAP)

    def test_cover_band_and_gold_line(self) -> None:
        """封面：通栏青绿带、带底金线都在。这是 grid 模板的身份件。"""
        theme = self.theme
        # 色带内部（避开文字区，取带的右侧空当）
        self.assertEqual(self._pixel(0, 12.9, 3.0), tuple(theme.cover_bg))
        # 金线在带底
        gold_y = 2.00 + 3.35 + 0.03
        self.assertEqual(self._pixel(0, 6.0, gold_y), tuple(theme.bar))
        # 开源版主题不带标识：封面右上标识区必须干净
        self.assertFalse(self._region_has_ink(0, 10.2, 0.3, 2.4, 0.7, tuple(theme.bg)))

    def test_section_band_and_number_block(self) -> None:
        """章节页：通栏色带 + 左侧浅一档号数块，两块颜色必须不同。"""
        theme = self.theme
        band = self._pixel(1, 8.0, 3.5)
        block = self._pixel(1, 1.0, 3.5)
        self.assertEqual(band, tuple(theme.cover_bg))
        self.assertNotEqual(block, band)

    def test_content_band_and_rule(self) -> None:
        """内容页：顶部白带、带下压线（左段深、右段浅）；无标识主题右上区干净。"""
        theme = self.theme
        self.assertEqual(self._pixel(2, 6.0, 0.15), tuple(theme.surface))
        rule_y = BAND_H + RULE_H / 2
        self.assertEqual(self._pixel(2, RULE_LEFT / 2, rule_y), tuple(theme.accent2))
        self.assertEqual(self._pixel(2, RULE_LEFT + 3.0, rule_y), tuple(theme.line))
        # 带下面是页底色，不是白带铺满全页
        self.assertEqual(self._pixel(2, 12.9, 6.7), tuple(theme.bg))
        self.assertFalse(self._region_has_ink(2, 10.8, 0.24, 1.9, 0.6, tuple(theme.surface)))

    def test_dark_page_skips_band(self) -> None:
        """深底页不压白带：顶部就是深底色；无标识主题右上区也是干净深底。"""
        theme = self.theme
        self.assertEqual(self._pixel(3, 6.0, 0.15), tuple(theme.accent2))
        self.assertFalse(self._region_has_ink(3, 10.8, 0.24, 1.9, 0.6, tuple(theme.accent2)))

    def test_snapshot_tool_writes_png(self) -> None:
        """pptx_snapshot 工具：跟随 deck_path 落 PNG，回执里带路径。"""
        import json

        from witty_agent.plugins.pptx import pptx_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            deck_file = Path(tmp) / "deck.json"
            deck_file.write_text(json.dumps(_grid_deck(), ensure_ascii=False), encoding="utf-8")
            message = pptx_snapshot(deck_path=str(deck_file))
            out = Path(tmp) / "deck.png"
            self.assertTrue(out.is_file())
            self.assertIn(str(out), message)
            # 内联稿必须给 out
            with self.assertRaises(ValueError):
                pptx_snapshot(deck=json.dumps(_grid_deck(), ensure_ascii=False))


class PptxRasterNonGridTests(unittest.TestCase):
    """非 grid 主题也必须画对。

    曾经踩过：光栅器只认 `cover=grid` 和 `chrome=band`，别的主题一律掉进「宏页粗排」
    分支——自编主题的封面色块、章节页、左侧竖条全没画出来，成稿其实是对的。这类假象
    比不画还坏：agent 自查时会以为版式坏了，回去改一份本来就没病的稿。
    """

    def _sheet(self, theme_payload):
        from PIL import Image

        deck = parse_deck(
            {
                "title": "换主题",
                "footer": "某某公司",
                "theme": theme_payload,
                "slides": [
                    {"kind": "cover", "title": "标题", "subtitle": "副题", "kicker": "演示"},
                    {"kind": "section", "title": "第一部分", "kicker": "01"},
                    {
                        "kind": "custom",
                        "title": "内容",
                        "boxes": [
                            {"kind": "text", "x": 0.7, "y": 1.45, "w": 11.9, "h": 0.6, "text": "内容", "size": 30, "bold": True, "name": "witty-title"},
                        ],
                    },
                ],
            }
        )
        theme = resolve_theme(deck.theme, deck.theme_overrides)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "s.png")
        render_deck_png(deck, path, theme)
        img = Image.open(path).convert("RGB")
        self.addCleanup(img.close)
        return img, theme, len(deck.slides)

    @staticmethod
    def _at(img, total, page_index, x_in, y_in):
        ox, oy = page_origin(page_index, total)
        return img.getpixel((ox + _px(x_in), oy + _px(y_in)))

    def test_split_cover_paints_left_panel(self) -> None:
        """split 封面：左 5.15 英寸是 cover_bg 深块，右侧是页底色。"""
        payload = {"id": "custom", "bg": "#FBF7F0", "ink": "#241A14", "cover_bg": "#241A14",
                   "cover_ink": "#FBF7F0", "accent": "#C2410C", "cover": "split"}
        img, theme, total = self._sheet(payload)
        self.assertEqual(self._at(img, total, 0, 2.0, 5.8), tuple(theme.cover_bg))
        self.assertEqual(self._at(img, total, 0, 8.0, 5.8), tuple(theme.bg))

    def test_bar_section_and_chrome(self) -> None:
        """非 band 主题：章节页满版 cover_bg + 左侧 0.16 竖条；内容页有左竖条。"""
        payload = {"id": "custom", "bg": "#FFFFFF", "ink": "#111111", "cover_bg": "#0B3D6E",
                   "cover_ink": "#FFFFFF", "accent": "#C4A35A", "accent2": "#0B3D6E",
                   "bar": "#C4A35A", "cover": "bar"}
        img, theme, total = self._sheet(payload)
        self.assertEqual(self._at(img, total, 1, 8.0, 3.5), tuple(theme.cover_bg))
        self.assertEqual(self._at(img, total, 1, 0.08, 3.5), tuple(theme.bar))
        self.assertEqual(self._at(img, total, 2, 0.05, 3.5), tuple(theme.accent2))

    def test_mark_cover_draws_square(self) -> None:
        """mark 封面：MARGIN_X 处 0.55 见方的强调色块必须在。"""
        img, theme, total = self._sheet("swiss-red")
        self.assertEqual(self._at(img, total, 0, 0.8, 2.3), tuple(theme.accent))

    def test_chrome_none_draws_nothing(self) -> None:
        """chrome=none 是「顶栏全关」，内容页左上角必须是干净底色，不能冒出 bar。"""
        payload = {"id": "custom", "bg": "#FFFFFF", "ink": "#111111", "accent": "#E30613",
                   "accent2": "#111111", "chrome": "none"}
        img, theme, total = self._sheet(payload)
        self.assertEqual(self._at(img, total, 2, 0.05, 3.5), tuple(theme.bg))
        self.assertEqual(self._at(img, total, 2, 6.0, 0.04), tuple(theme.bg))

    def test_custom_theme_logo_zone_is_clean(self) -> None:
        """自编主题不带 logo：右上角标识区应当干干净净，不能漏任何标识进去。"""
        payload = {"id": "custom", "bg": "#FBF7F0", "ink": "#241A14", "cover_bg": "#241A14", "cover": "split"}
        img, theme, total = self._sheet(payload)
        ox, oy = page_origin(2, total)
        crop = img.crop((ox + _px(10.8), oy + _px(0.24), ox + _px(12.7), oy + _px(0.84)))
        self.assertEqual(set(crop.getdata()), {tuple(theme.bg)})


class PptxFontTests(unittest.TestCase):
    def test_measured_width_falls_back(self) -> None:
        """量得到就用真实宽度（CJK 恒为 1em 上下），量不到回落启发式。"""
        from witty_agent.plugins.pptx_kit.fonts import fallback_font_file, find_font
        from witty_agent.plugins.pptx_kit.metrics import char_em, text_em

        self.assertGreater(text_em("测试 Audit 42"), 0)
        # 不存在的字体：回落启发式，不报错
        self.assertAlmostEqual(char_em("测", "不存在的字体"), 1.0, places=2)
        # 本机任一 CJK 字体：汉字宽度必须接近 1em（真实度量生效的证据）
        fallback = fallback_font_file()
        if fallback:
            family = Path(fallback).stem
            found = find_font(family)
            if found:
                self.assertAlmostEqual(char_em("测", family), 1.0, delta=0.1)

    def test_latin_font_falls_back_for_cjk(self) -> None:
        """主题字体是拉丁字体时，汉字要逐字换 CJK 字体画，不能出豆腐块。

        PowerPoint 自己会做字体回退，所以 font=Arial 的中文稿成稿是好的；快照不回退
        就会画出一排方框，让 agent 以为稿子坏了回去瞎改。
        """
        from witty_agent.plugins.pptx_kit.fonts import char_em, fallback_font_file, find_font
        from witty_agent.plugins.pptx_kit.raster import _Text

        if not find_font("Arial") or not fallback_font_file():
            self.skipTest("本机缺 Arial 或 CJK 字体，回退无从谈起")
        if char_em("测", "Arial") is not None:
            self.skipTest("本机 Arial 自带 CJK 字形")
        runs = _Text("Arial").segments("A测B", 16, False)
        self.assertEqual([chunk for chunk, _ in runs], ["A", "测", "B"])
        # 汉字那段换了字体，两段拉丁仍共用主字体
        self.assertIsNot(runs[0][1], runs[1][1])
        self.assertIs(runs[0][1], runs[2][1])

    def test_font_risk_lint(self) -> None:
        """清单外字体报 font_risk；清单内不报。清单在 runtime.toml [pptx].safe_fonts。"""
        from witty_agent.plugins.pptx_kit.lint import lint_deck

        deck = parse_deck(
            {
                "title": "字体检查",
                "theme": "青绿",
                "slides": [
                    {
                        "kind": "custom",
                        "title": "页",
                        "boxes": [
                            {"kind": "text", "x": 0.7, "y": 1.5, "w": 8.0, "h": 0.8, "text": "标题", "size": 28, "bold": True, "name": "witty-title"},
                            {"kind": "text", "x": 0.7, "y": 2.6, "w": 8.0, "h": 0.8, "text": "怪字体", "size": 16, "font": "Papyrus"},
                        ],
                    }
                ],
            }
        )
        codes = [issue.code for issue in lint_deck(deck)]
        self.assertIn("font_risk", codes)
        # 主题字体微软雅黑在清单内：只有那一条 font_risk
        self.assertEqual(codes.count("font_risk"), 1)


if __name__ == "__main__":
    unittest.main()
