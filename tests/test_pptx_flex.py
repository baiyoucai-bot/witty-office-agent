"""flex 排版层：声明式 row / column 树解算成绝对坐标盒子。"""

from __future__ import annotations

import json
import unittest

from witty_agent.plugins.pptx_kit import parse_deck
from witty_agent.plugins.pptx_kit.flex import solve_layout
from witty_agent.plugins.pptx_kit.schema import CANVAS_H, CANVAS_W

EPS = 1e-3


def _by_text(boxes: list[dict], text: str) -> dict:
    for box in boxes:
        if box.get("text") == text:
            return box
    raise AssertionError(f"没解出文字为 {text!r} 的盒子")


class FlexRowTest(unittest.TestCase):
    def test_row_without_flex_splits_evenly(self) -> None:
        """行里既没给宽也没给 flex 的孩子等分主轴——写四个就是四等分。"""
        boxes = solve_layout(
            {
                "dir": "row",
                "gap": 0.2,
                "children": [{"kind": "rect", "fill": "#111", "text": str(i)} for i in range(4)],
            },
            CANVAS_W,
            CANVAS_H,
        )
        widths = [box["w"] for box in boxes]
        self.assertEqual(len(widths), 4)
        for width in widths:
            self.assertAlmostEqual(width, widths[0], delta=EPS)
        used = sum(widths) + 0.2 * 3
        self.assertAlmostEqual(used, CANVAS_W, delta=EPS)

    def test_row_honours_flex_weights(self) -> None:
        boxes = solve_layout(
            {
                "dir": "row",
                "gap": 0.0,
                "children": [
                    {"kind": "rect", "fill": "#111", "flex": 3, "text": "wide"},
                    {"kind": "rect", "fill": "#222", "flex": 1, "text": "narrow"},
                ],
            },
            CANVAS_W,
            CANVAS_H,
        )
        wide = _by_text(boxes, "wide")
        narrow = _by_text(boxes, "narrow")
        self.assertAlmostEqual(wide["w"], narrow["w"] * 3, delta=EPS)
        self.assertAlmostEqual(wide["w"] + narrow["w"], CANVAS_W, delta=EPS)

    def test_fixed_width_child_keeps_size_and_siblings_take_rest(self) -> None:
        boxes = solve_layout(
            {
                "dir": "row",
                "gap": 0.1,
                "children": [
                    {"kind": "rect", "fill": "#111", "w": 2.0, "text": "fixed"},
                    {"kind": "rect", "fill": "#222", "text": "rest"},
                ],
            },
            CANVAS_W,
            CANVAS_H,
        )
        fixed = _by_text(boxes, "fixed")
        rest = _by_text(boxes, "rest")
        self.assertAlmostEqual(fixed["w"], 2.0, delta=EPS)
        self.assertAlmostEqual(rest["w"], CANVAS_W - 2.0 - 0.1, delta=EPS)
        self.assertAlmostEqual(rest["x"], 2.1, delta=EPS)


class FlexColumnTest(unittest.TestCase):
    def test_column_stacks_with_gap_and_padding(self) -> None:
        boxes = solve_layout(
            {
                "pad": 0.5,
                "gap": 0.25,
                "children": [
                    {"kind": "rect", "fill": "#111", "h": 1.0, "text": "a"},
                    {"kind": "rect", "fill": "#222", "h": 1.0, "text": "b"},
                ],
            },
            CANVAS_W,
            CANVAS_H,
        )
        first = _by_text(boxes, "a")
        second = _by_text(boxes, "b")
        self.assertAlmostEqual(first["x"], 0.5, delta=EPS)
        self.assertAlmostEqual(first["y"], 0.5, delta=EPS)
        self.assertAlmostEqual(first["w"], CANVAS_W - 1.0, delta=EPS)
        self.assertAlmostEqual(second["y"], 0.5 + 1.0 + 0.25, delta=EPS)

    def test_flex_child_absorbs_remaining_height(self) -> None:
        boxes = solve_layout(
            {
                "gap": 0.0,
                "children": [
                    {"kind": "rect", "fill": "#111", "h": 1.5, "text": "head"},
                    {"kind": "rect", "fill": "#222", "flex": 1, "text": "body"},
                ],
            },
            CANVAS_W,
            CANVAS_H,
        )
        body = _by_text(boxes, "body")
        self.assertAlmostEqual(body["y"], 1.5, delta=EPS)
        self.assertAlmostEqual(body["h"], CANVAS_H - 1.5, delta=EPS)

    def test_push_pins_child_to_bottom(self) -> None:
        """push 吃掉它前面的富余，等价于 margin-top:auto，用来把尾巴钉底。"""
        boxes = solve_layout(
            {
                "h": 6.0,
                "gap": 0.0,
                "children": [
                    {"kind": "rect", "fill": "#111", "h": 1.0, "text": "top"},
                    {"kind": "rect", "fill": "#222", "h": 0.5, "push": True, "text": "foot"},
                ],
            },
            CANVAS_W,
            CANVAS_H,
        )
        foot = _by_text(boxes, "foot")
        self.assertAlmostEqual(foot["y"] + foot["h"], 6.0, delta=EPS)

    def test_justify_center_centres_the_stack(self) -> None:
        boxes = solve_layout(
            {
                "h": 6.0,
                "gap": 0.0,
                "justify": "center",
                "children": [{"kind": "rect", "fill": "#111", "h": 2.0, "text": "mid"}],
            },
            CANVAS_W,
            CANVAS_H,
        )
        mid = _by_text(boxes, "mid")
        self.assertAlmostEqual(mid["y"], 2.0, delta=EPS)


class FlexNestingTest(unittest.TestCase):
    def test_parent_paints_before_children(self) -> None:
        """父在前、子在后：render 按顺序画，卡片底才垫得住卡片里的字。"""
        boxes = solve_layout(
            {
                "children": [
                    {
                        "kind": "round",
                        "fill": "#FFFFFF",
                        "pad": 0.2,
                        "children": [{"kind": "text", "text": "inside", "size": 14}],
                    }
                ]
            },
            CANVAS_W,
            CANVAS_H,
        )
        self.assertEqual(boxes[0]["kind"], "round")
        self.assertEqual(boxes[1]["text"], "inside")
        card, inner = boxes[0], boxes[1]
        self.assertGreaterEqual(inner["x"], card["x"] + 0.2 - EPS)
        self.assertLessEqual(inner["x"] + inner["w"], card["x"] + card["w"] - 0.2 + EPS)

    def test_grouping_container_emits_no_box(self) -> None:
        """光用来分组、自己不画的容器不占盒子额度。"""
        boxes = solve_layout(
            {"children": [{"gap": 0.1, "children": [{"kind": "text", "text": "only", "size": 14}]}]},
            CANVAS_W,
            CANVAS_H,
        )
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["text"], "only")

    def test_fill_without_kind_becomes_rect_or_round(self) -> None:
        plain = solve_layout({"children": [{"fill": "#123456", "h": 1.0}]}, CANVAS_W, CANVAS_H)
        curved = solve_layout(
            {"children": [{"fill": "#123456", "radius": 0.1, "h": 1.0}]}, CANVAS_W, CANVAS_H
        )
        self.assertEqual(plain[0]["kind"], "rect")
        self.assertEqual(curved[0]["kind"], "round")

    def test_depth_and_node_budget_do_not_explode(self) -> None:
        node: dict = {"kind": "text", "text": "deep", "size": 12}
        for _ in range(40):
            node = {"children": [node], "gap": 0.0}
        boxes = solve_layout(node, CANVAS_W, CANVAS_H)
        self.assertLess(len(boxes), 40)


class FlexMeasureTest(unittest.TestCase):
    def test_text_height_grows_with_content(self) -> None:
        """自然高来自真实字宽量出的行数，不是拍脑袋的常数。"""
        short = solve_layout(
            {"children": [{"kind": "text", "text": "短", "size": 16}]}, CANVAS_W, CANVAS_H
        )
        long = solve_layout(
            {"children": [{"kind": "text", "text": "长文" * 200, "size": 16}]}, CANVAS_W, CANVAS_H
        )
        self.assertGreater(long[0]["h"], short[0]["h"] * 2)

    def test_every_box_has_positive_size(self) -> None:
        """解出 0 宽或 0 高会被 parse_box 丢掉，等于凭空少一块。"""
        boxes = solve_layout(
            {
                "pad": 0.4,
                "gap": 0.2,
                "children": [
                    {"kind": "text", "text": "标题", "size": 28},
                    {"kind": "line", "fill": "#ccc"},
                    {"dir": "row", "gap": 0.2, "children": [{"kind": "rect", "fill": "#eee"} for _ in range(6)]},
                ],
            },
            CANVAS_W,
            CANVAS_H,
        )
        self.assertTrue(boxes)
        for box in boxes:
            self.assertGreater(box["w"], 0)
            self.assertGreater(box["h"], 0)


class FlexDeckTest(unittest.TestCase):
    def _deck(self) -> dict:
        cards = [
            {
                "kind": "round",
                "fill": "#FFFFFF",
                "radius": 0.12,
                "pad": 0.24,
                "gap": 0.1,
                "children": [
                    {"kind": "text", "text": f"指标 {i}", "size": 15, "bold": True},
                    {"kind": "text", "text": "一句话说明这个指标为什么重要。", "size": 12},
                ],
            }
            for i in range(4)
        ]
        return {
            "title": "flex 稿",
            "slides": [
                {
                    "kind": "custom",
                    "layout": {
                        "pad": [0.42, 0.62, 0.62, 0.62],
                        "gap": 0.28,
                        "children": [
                            {"kind": "text", "text": "行业采纳", "size": 30, "bold": True, "name": "witty-title"},
                            {"dir": "row", "gap": 0.24, "flex": 1, "children": cards},
                        ],
                    },
                }
            ],
        }

    def test_layout_slide_yields_dense_boxes(self) -> None:
        deck = parse_deck(json.dumps(self._deck(), ensure_ascii=False))
        boxes = deck.slides[0].boxes
        # 标题 + 4 张卡（每张：底 + 标题 + 正文）= 13
        self.assertEqual(len(boxes), 13)
        self.assertTrue(all(box.w > 0 and box.h > 0 for box in boxes))

    def test_layout_boxes_stay_on_canvas(self) -> None:
        deck = parse_deck(json.dumps(self._deck(), ensure_ascii=False))
        for box in deck.slides[0].boxes:
            self.assertGreaterEqual(box.x, -EPS)
            self.assertGreaterEqual(box.y, -EPS)
            self.assertLessEqual(box.x + box.w, CANVAS_W + EPS)
            self.assertLessEqual(box.y + box.h, CANVAS_H + EPS)

    def test_handwritten_boxes_render_under_layout(self) -> None:
        """boxes 和 layout 同时给：手写的当底衬，layout 解出的压在上面。"""
        payload = {
            "title": "混排",
            "slides": [
                {
                    "kind": "custom",
                    "boxes": [{"kind": "rect", "x": 0, "y": 0, "w": 13.333, "h": 7.5, "fill": "#0C447C"}],
                    "layout": {
                        "pad": 0.6,
                        "children": [{"kind": "text", "text": "压在底图上的标题", "size": 34, "bold": True}],
                    },
                }
            ],
        }
        deck = parse_deck(json.dumps(payload, ensure_ascii=False))
        boxes = deck.slides[0].boxes
        self.assertEqual(boxes[0].kind, "rect")
        self.assertEqual(boxes[1].text, "压在底图上的标题")

    def test_slide_without_layout_is_untouched(self) -> None:
        """老稿一个字没改也得照旧解析。"""
        payload = {
            "title": "老稿",
            "slides": [
                {
                    "kind": "custom",
                    "boxes": [
                        {"kind": "text", "x": 0.62, "y": 0.45, "w": 12, "h": 0.7, "text": "标题", "size": 26}
                    ],
                }
            ],
        }
        deck = parse_deck(json.dumps(payload, ensure_ascii=False))
        box = deck.slides[0].boxes[0]
        self.assertAlmostEqual(box.x, 0.62, delta=EPS)
        self.assertAlmostEqual(box.w, 12.0, delta=EPS)


if __name__ == "__main__":
    unittest.main()
