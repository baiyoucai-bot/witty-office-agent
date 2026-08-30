"""内置图形资源。主题标识随包走，运行时不下载。

命名约定：`<名字>.png` 是深色墨版，给浅底用；`<名字>-white.png` 是反白版，给深底用。
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "brand"


def asset_names() -> list[str]:
    return sorted(item.stem for item in _DIR.glob("*.png"))


@lru_cache(maxsize=32)
def asset_path(stem: str) -> str:
    """按名字取内置 PNG。取不到返回空串——缺一张标识不该让整稿失败。"""
    name = (stem or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return ""
    found = _DIR / f"{name}.png"
    return str(found) if found.is_file() else ""


@lru_cache(maxsize=32)
def asset_size(stem: str) -> tuple[int, int]:
    """直接读 PNG 的 IHDR 拿像素尺寸，不为了量个宽高就引入 Pillow。"""
    path = asset_path(stem)
    if not path:
        return (0, 0)
    blob = Path(path).read_bytes()[:24]
    if len(blob) < 24 or blob[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")


@lru_cache(maxsize=32)
def asset_data_uri(stem: str) -> str:
    """给 HTML 预览用的内联图。预览是单文件，不能依赖外部路径。"""
    path = asset_path(stem)
    if not path:
        return ""
    blob = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{blob}"
