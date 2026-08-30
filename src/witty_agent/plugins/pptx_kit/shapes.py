"""矢量盒子的形状几何。三个渲染端共用这一份，免得成稿、预览、快照画得不一样。

成稿走 PowerPoint 原生形状（`MSO_SHAPE.CHEVRON` 这些），所以在 WPS 里还能拖点、改色、
改字——这是 ppt-master 的立身之本，不能退化成贴一张图。预览用 CSS `clip-path`、快照用
PIL 画多边形，两边都从这里取同一组顶点，画出来才对得上。

顶点是 0..1 的比例，乘上盒子的宽高就是实际位置。缺口深度这类量按**绝对尺寸**算再折回
比例，所以扁盒子和方盒子的缺口看着一样深，不会被拉变形。
"""

from __future__ import annotations

# 带尖角的形状（chevron / pentagon / arrow）在 kind 里就固定了朝向语义，
# 由 point 决定实际指哪边。
POINTS = ("right", "left", "up", "down")

# 只有 arrow 认朝向。PowerPoint 的内置 chevron / pentagon 固定朝右，转它只能靠 `rot`，
# 而 `rot` 会把形状里的文字一起转过去。所以这两个在三个渲染端都一律朝右——
# 预览和快照要是画了成稿画不出的朝向，自查图就成了骗人的。
POINTED_KINDS = frozenset({"arrow"})

# 箭头杆占盒子高度的比例（上下各留 0.25）
_SHAFT = 0.25

SHAPE_KINDS = frozenset(
    {"rect", "round", "oval", "diamond", "triangle", "chevron", "pentagon", "arrow"}
)
# 这几个要画多边形；rect / round / oval 各有各的画法，不走顶点。
POLY_KINDS = frozenset({"diamond", "triangle", "chevron", "pentagon", "arrow"})


# 形状特有的左右让位（英寸，单边）。这是**额外**的：metrics.text_height 里已经扣过
# 文本框自己的内边距，这里只补形状斜边吃掉的那部分，别重复算。
_SIDE_INSET = {
    "diamond": 0.22,
    "triangle": 0.22,
    "chevron": 0.20,
    "pentagon": 0.20,
    "arrow": 0.20,
    "oval": 0.08,
}


def text_inset(kind: str) -> float:
    """形状内文字额外要让出的单边宽度，单位英寸。

    菱形、流程带这些两边是斜的，字按整宽折行会压到尖上；矩形不用让。
    量高（flex）和查溢出（lint）都从这里取，两边用同一个数才不会互相打架。
    """
    return _SIDE_INSET.get(kind, 0.0)


def normalize_point(value: str) -> str:
    key = (value or "").strip().lower()
    return key if key in POINTS else "right"


def _right_points(kind: str, w: float, h: float) -> list[tuple[float, float]]:
    """按「朝右」算顶点。其它朝向由调用方旋转。"""
    if kind == "diamond":
        return [(0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)]
    if kind == "triangle":
        return [(0.5, 0.0), (1.0, 1.0), (0.0, 1.0)]
    # 缺口/箭头按绝对尺寸取，再折回比例，扁盒子才不会被拉出畸形的尖
    notch = min(h * 0.5, w * 0.5) / w if w > 0 else 0.25
    if kind == "chevron":
        return [(0.0, 0.0), (1.0 - notch, 0.0), (1.0, 0.5), (1.0 - notch, 1.0), (0.0, 1.0), (notch, 0.5)]
    if kind == "pentagon":
        return [(0.0, 0.0), (1.0 - notch, 0.0), (1.0, 0.5), (1.0 - notch, 1.0), (0.0, 1.0)]
    if kind == "arrow":
        head = min(h * 0.62, w * 0.5) / w if w > 0 else 0.4
        top, bottom = _SHAFT, 1.0 - _SHAFT
        return [
            (0.0, top),
            (1.0 - head, top),
            (1.0 - head, 0.0),
            (1.0, 0.5),
            (1.0 - head, 1.0),
            (1.0 - head, bottom),
            (0.0, bottom),
        ]
    return [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def polygon_points(kind: str, w: float, h: float, point: str = "right") -> list[tuple[float, float]]:
    """盒子的顶点，返回 (fx, fy) 比例对。乘宽高即得实际坐标。

    上下朝向按「把盒子转 90 度」算：先用交换过的宽高求朝右的形状，再把坐标换回来，
    这样缺口深度在竖着摆的时候也还是照高度取，不会变成一根细刺。
    """
    if kind not in POLY_KINDS:
        return []
    if kind not in POINTED_KINDS:
        # 菱形、三角没有朝向语义；chevron / pentagon 成稿只能朝右，这里跟着固定，
        # 免得快照画出一个导不出来的朝向。
        return _right_points(kind, w, h)
    facing = normalize_point(point)
    if facing in {"right", "left"}:
        pts = _right_points(kind, w, h)
        if facing == "left":
            return [(1.0 - fx, fy) for fx, fy in pts]
        return pts
    pts = _right_points(kind, h, w)
    if facing == "down":
        return [(1.0 - fy, fx) for fx, fy in pts]
    return [(fy, 1.0 - fx) for fx, fy in pts]


def clip_path(kind: str, w: float, h: float, point: str = "right") -> str:
    """CSS clip-path。预览端用。"""
    pts = polygon_points(kind, w, h, point)
    if not pts:
        return ""
    body = ", ".join(f"{fx * 100:.2f}% {fy * 100:.2f}%" for fx, fy in pts)
    return f"polygon({body})"


def scaled_points(
    kind: str, x: float, y: float, w: float, h: float, point: str = "right"
) -> list[tuple[float, float]]:
    """落到实际坐标系的顶点。快照端用（传像素，返回像素）。"""
    return [(x + fx * w, y + fy * h) for fx, fy in polygon_points(kind, w, h, point)]
