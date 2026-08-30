"""Flex 排版层：把声明式的 row / column 树解算成绝对英寸坐标的盒子。

存在的理由：`boxes` 要求作者手算每个盒子的 x/y/w/h。一页排十几个元素就是几十次
算术，模型只能靠少放元素来压低出错概率，稿子于是又空又稀。这里让作者写「一行四张
卡、间距 0.24、各占一份」，宽高位置交给求解器。

输出是普通的盒子字典，交给 `schema.parse_box` 就是原来的 `Box`；下游 render /
preview / raster / lint 一行都不用改。本模块只算几何，不碰 `schema`，所以两边不会
互相 import。

三趟解算：

1. 宽度自上而下：行按 flex 权重分主轴，列直接继承内容宽。
2. 自然高度自下而上：文字走 `metrics` 的真实字宽量行数，容器求和或取大。
3. 最终高度与 y 自上而下：按 flex 权重分剩余空间，`justify` / `push` 处理富余。

宽度不依赖高度，所以三趟一次过，不用迭代到收敛。

命名上有个坑要绕开：盒子本来就有 `align`（文字左中右对齐）。所以交叉轴对齐叫
`cross`，不叫 `align`，免得同一个键在容器和叶子上是两个意思。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from witty_agent.plugins.pptx_kit.metrics import (
    bullets_height,
    table_height,
    text_height,
)
from witty_agent.plugins.pptx_kit.shapes import SHAPE_KINDS, text_inset

# 容器专属的键。除这些之外的键都原样传给盒子，所以 flex 层不用跟着盒子字段升级。
FLEX_KEYS = frozenset(
    {"dir", "children", "gap", "pad", "flex", "justify", "cross", "push", "w", "h"}
)

MAX_DEPTH = 8
MAX_NODES = 400

# 一条 line 盒子的默认粗细；没给 h 时用它，免得解出 0 高被 parse_box 丢掉。
LINE_H = 0.03
# 图片/图表这类量不出内容高度的盒子，没给 h 也没 flex 时的兜底高度。
FALLBACK_H = 1.2
MIN_SIDE = 0.02

_ROW = "row"
_COLUMN = "column"


@dataclass
class _Node:
    direction: str
    children: list["_Node"] = field(default_factory=list)
    gap: float = 0.0
    pad: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    justify: str = "start"
    cross: str = "stretch"
    push: bool = False
    flex: float = 0.0
    fixed_w: float | None = None
    fixed_h: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    nat_h: float = 0.0


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _pad(value: Any) -> tuple[float, float, float, float]:
    """上右下左。给一个数是四边，给两个是「竖 横」，给四个按 CSS 顺序。"""
    if value is None:
        return (0.0, 0.0, 0.0, 0.0)
    if isinstance(value, (int, float, str)):
        one = _num(value, 0.0) or 0.0
        return (one, one, one, one)
    if isinstance(value, (list, tuple)):
        nums = [(_num(item, 0.0) or 0.0) for item in value]
        if len(nums) == 1:
            return (nums[0],) * 4  # type: ignore[return-value]
        if len(nums) == 2:
            return (nums[0], nums[1], nums[0], nums[1])
        if len(nums) == 3:
            return (nums[0], nums[1], nums[2], nums[1])
        if len(nums) >= 4:
            return (nums[0], nums[1], nums[2], nums[3])
    if isinstance(value, dict):
        top = _num(value.get("top") or value.get("t"), 0.0) or 0.0
        right = _num(value.get("right") or value.get("r"), 0.0) or 0.0
        bottom = _num(value.get("bottom") or value.get("b"), 0.0) or 0.0
        left = _num(value.get("left") or value.get("l"), 0.0) or 0.0
        return (top, right, bottom, left)
    return (0.0, 0.0, 0.0, 0.0)


_JUSTIFY = {
    "start": "start",
    "flex-start": "start",
    "top": "start",
    "left": "start",
    "center": "center",
    "middle": "center",
    "end": "end",
    "flex-end": "end",
    "bottom": "end",
    "right": "end",
    "between": "between",
    "space-between": "between",
    "around": "around",
    "space-around": "around",
}

_CROSS = {
    "stretch": "stretch",
    "start": "start",
    "flex-start": "start",
    "top": "start",
    "left": "start",
    "center": "center",
    "middle": "center",
    "end": "end",
    "flex-end": "end",
    "bottom": "end",
    "right": "end",
}


def _direction(raw: dict[str, Any]) -> str:
    value = str(raw.get("dir") or raw.get("direction") or "").strip().lower()
    if value in {"row", "horizontal", "h", "x"}:
        return _ROW
    if value in {"column", "col", "vertical", "v", "y"}:
        return _COLUMN
    # 给了 children 却没写方向：按竖排，这是幻灯片里更常见的默认。
    if raw.get("children"):
        return _COLUMN
    return ""


def _payload_of(raw: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in raw.items() if k not in FLEX_KEYS}
    if not payload.get("kind") and (payload.get("fill") or payload.get("stroke")):
        # 只给了底色或描边的容器：有圆角当卡片，没圆角当色块。
        payload["kind"] = "round" if _num(payload.get("radius"), 0.0) else "rect"
    return payload


def _parse(raw: Any, depth: int, budget: list[int]) -> _Node | None:
    if not isinstance(raw, dict) or depth > MAX_DEPTH or budget[0] <= 0:
        return None
    budget[0] -= 1
    direction = _direction(raw)
    children: list[_Node] = []
    for item in raw.get("children") or []:
        child = _parse(item, depth + 1, budget)
        if child is not None:
            children.append(child)
    justify = _JUSTIFY.get(str(raw.get("justify") or "").strip().lower(), "start")
    cross = _CROSS.get(str(raw.get("cross") or "").strip().lower(), "stretch")
    return _Node(
        direction=direction if children else "",
        children=children,
        gap=max(_num(raw.get("gap"), 0.0) or 0.0, 0.0),
        pad=_pad(raw.get("pad")),
        justify=justify,
        cross=cross,
        push=bool(raw.get("push")),
        flex=max(_num(raw.get("flex"), 0.0) or 0.0, 0.0),
        fixed_w=_num(raw.get("w") if raw.get("w") is not None else raw.get("width")),
        fixed_h=_num(raw.get("h") if raw.get("h") is not None else raw.get("height")),
        payload=_payload_of(raw),
    )


def _image_ratio(path: str) -> float:
    """读 PNG / JPEG 文件头拿高宽比。跟 assets 一样不为了量个尺寸引入 Pillow。"""
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return 0.0
    if blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) >= 24:
        width = int.from_bytes(blob[16:20], "big")
        height = int.from_bytes(blob[20:24], "big")
        return height / width if width else 0.0
    if blob[:2] == b"\xff\xd8":
        pos = 2
        end = len(blob)
        while pos + 9 < end:
            if blob[pos] != 0xFF:
                pos += 1
                continue
            marker = blob[pos + 1]
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            length = int.from_bytes(blob[pos + 2 : pos + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
                height = int.from_bytes(blob[pos + 5 : pos + 7], "big")
                width = int.from_bytes(blob[pos + 7 : pos + 9], "big")
                return height / width if width else 0.0
            pos += 2 + max(length, 2)
    return 0.0


def _leaf_height(node: _Node, width: float) -> float:
    """一个叶子盒子的自然高度。量得出就量，量不出给兜底。"""
    payload = node.payload
    kind = str(payload.get("kind") or "text").strip().lower()
    size = int(_num(payload.get("size"), 18) or 18)
    font = str(payload.get("font") or "")
    if kind == "line":
        return LINE_H
    if kind == "text":
        return text_height(str(payload.get("text") or ""), width, size, font)
    if kind == "bullets":
        items = [str(item) for item in (payload.get("items") or [])]
        return bullets_height(items, width, size, font)
    if kind == "table":
        return table_height(len(payload.get("rows") or []), size)
    if kind == "image":
        ratio = _image_ratio(str(payload.get("image") or ""))
        if ratio > 0:
            return width * ratio
        return FALLBACK_H
    if kind in SHAPE_KINDS:
        body = str(payload.get("text") or "")
        if not body:
            # 纯色块没有内容高度：作者要么给 h，要么给 flex，要么塞 children。
            return 0.0
        # 自动量高比 lint 的底线再松一点，免得算出来正好卡在告警线上。
        return text_height(body, max(width - text_inset(kind) * 2, 0.2), size, font) + 0.14
    return FALLBACK_H


def _spread(free: float, count: int, mode: str) -> tuple[float, float]:
    """主轴富余怎么分。返回（起始偏移，每两个之间的额外间距）。"""
    if free <= 0 or count <= 0:
        return (0.0, 0.0)
    if mode == "center":
        return (free / 2, 0.0)
    if mode == "end":
        return (free, 0.0)
    if mode == "between" and count > 1:
        return (0.0, free / (count - 1))
    if mode == "around":
        unit = free / (count * 2)
        return (unit, unit * 2)
    return (0.0, 0.0)


def _solve_width(node: _Node, avail: float) -> None:
    """第一趟：自上而下定宽。"""
    node.w = max(node.fixed_w if node.fixed_w is not None else avail, MIN_SIDE)
    if not node.children:
        return
    inner = max(node.w - node.pad[3] - node.pad[1], MIN_SIDE)
    if node.direction == _ROW:
        count = len(node.children)
        free = inner - node.gap * (count - 1)
        fixed = [c for c in node.children if c.fixed_w is not None]
        growing = [c for c in node.children if c.fixed_w is None]
        free -= sum(c.fixed_w or 0.0 for c in fixed)
        # 行里既没给宽也没给 flex 的孩子按等分处理：写四个孩子就是四等分，
        # 这是作者写 {"dir":"row","children":[a,b,c,d]} 时想要的结果。
        weights = [c.flex if c.flex > 0 else 1.0 for c in growing]
        total = sum(weights)
        share = max(free, 0.0)
        for child, weight in zip(growing, weights):
            child.w = max(share * weight / total, MIN_SIDE) if total > 0 else MIN_SIDE
        for child in node.children:
            _solve_width(child, child.fixed_w if child.fixed_w is not None else child.w)
        return
    for child in node.children:
        _solve_width(child, child.fixed_w if child.fixed_w is not None else inner)


def _solve_natural(node: _Node) -> None:
    """第二趟：自下而上量自然高。"""
    inner_w = max(node.w - node.pad[3] - node.pad[1], MIN_SIDE)
    if not node.children:
        content = _leaf_height(node, inner_w)
    else:
        for child in node.children:
            _solve_natural(child)
        if node.direction == _ROW:
            content = max((c.nat_h for c in node.children), default=0.0)
        else:
            content = sum(c.nat_h for c in node.children) + node.gap * (len(node.children) - 1)
    if node.fixed_h is not None:
        node.nat_h = max(node.fixed_h, MIN_SIDE)
    else:
        node.nat_h = max(content + node.pad[0] + node.pad[2], MIN_SIDE)


def _place(node: _Node, x: float, y: float, height: float) -> None:
    """第三趟：自上而下定高与 x/y。"""
    node.x = x
    node.y = y
    node.h = max(height, MIN_SIDE)
    if not node.children:
        return
    inner_x = node.x + node.pad[3]
    inner_y = node.y + node.pad[0]
    inner_w = max(node.w - node.pad[3] - node.pad[1], MIN_SIDE)
    inner_h = max(node.h - node.pad[0] - node.pad[2], MIN_SIDE)
    count = len(node.children)

    if node.direction == _ROW:
        used = sum(c.w for c in node.children) + node.gap * (count - 1)
        lead, extra = _spread(inner_w - used, count, node.justify)
        cursor = inner_x + lead
        for child in node.children:
            if child.fixed_h is not None:
                child_h = child.fixed_h
            elif node.cross == "stretch":
                child_h = inner_h
            else:
                child_h = child.nat_h
            child_h = min(max(child_h, MIN_SIDE), inner_h)
            if node.cross == "center":
                child_y = inner_y + (inner_h - child_h) / 2
            elif node.cross == "end":
                child_y = inner_y + inner_h - child_h
            else:
                child_y = inner_y
            _place(child, cursor, child_y, child_h)
            cursor += child.w + node.gap + extra
        return

    basis = [c.fixed_h if c.fixed_h is not None else c.nat_h for c in node.children]
    free = inner_h - sum(basis) - node.gap * (count - 1)
    growing = [i for i, c in enumerate(node.children) if c.flex > 0]
    if growing:
        total = sum(node.children[i].flex for i in growing)
        if free > 0 and total > 0:
            for i in growing:
                basis[i] += free * node.children[i].flex / total
            free = 0.0
        elif free < 0 and total > 0:
            # 装不下先压 flex 的孩子，压到 0 为止；仍装不下就让它溢出，
            # 交给 pptx_check 报出来，不要静默把字裁掉。
            room = sum(basis[i] for i in growing)
            shrink = min(-free, room)
            for i in growing:
                basis[i] -= shrink * (basis[i] / room) if room > 0 else 0.0
            free += shrink

    pushes = [i for i, c in enumerate(node.children) if c.push]
    leads = [0.0] * count
    if free > 0 and pushes:
        unit = free / len(pushes)
        for i in pushes:
            leads[i] = unit
        free = 0.0

    lead, extra = _spread(free, count, node.justify)
    cursor = inner_y + lead
    for index, child in enumerate(node.children):
        cursor += leads[index]
        child_w = min(child.w, inner_w)
        if node.cross == "center":
            child_x = inner_x + (inner_w - child_w) / 2
        elif node.cross == "end":
            child_x = inner_x + inner_w - child_w
        else:
            child_x = inner_x
        _place(child, child_x, cursor, max(basis[index], MIN_SIDE))
        cursor += max(basis[index], MIN_SIDE) + node.gap + extra


def _paints(payload: dict[str, Any]) -> bool:
    """这个节点自己要不要画。纯粹用来分组的容器不画，也就不占盒子额度。"""
    if payload.get("kind"):
        return True
    return any(
        payload.get(key)
        for key in ("text", "items", "fill", "stroke", "image", "chart", "headers", "rows", "series")
    )


def _emit(node: _Node, out: list[dict[str, Any]]) -> None:
    # 先父后子：render 按列表顺序画，父在前正好垫在孩子底下，z 序天然是对的。
    if _paints(node.payload):
        out.append(
            {
                **node.payload,
                "x": round(node.x, 4),
                "y": round(node.y, 4),
                "w": round(node.w, 4),
                "h": round(node.h, 4),
            }
        )
    for child in node.children:
        _emit(child, out)


def solve_layout(raw: Any, canvas_w: float, canvas_h: float) -> list[dict[str, Any]]:
    """把一棵 layout 树解算成盒子字典列表，坐标是页面绝对英寸。

    根节点没给 w / h 时铺满整页。返回的字典直接喂 `schema.parse_box`。
    """
    root = _parse(raw, 0, [MAX_NODES])
    if root is None:
        return []
    _solve_width(root, root.fixed_w if root.fixed_w is not None else canvas_w)
    _solve_natural(root)
    height = root.fixed_h if root.fixed_h is not None else canvas_h
    _place(root, _num(raw.get("x"), 0.0) or 0.0, _num(raw.get("y"), 0.0) or 0.0, height)
    out: list[dict[str, Any]] = []
    _emit(root, out)
    return out
