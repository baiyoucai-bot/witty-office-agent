"""图形资源。运行时不下载，只认随包资源和用户自己放的图。

命名约定：`<名字>.png` 是深色墨版，给浅底用；`<名字>-white.png` 是反白版，给深底用。

取图顺序：随包 `brand/` → 用户 `$WITTY_HOME/brand/` → 当成文件路径直接读。
开源版包里不带任何企业标识，想让自家标识上稿只能靠后两条——所以这两条是
功能的一部分，不是兜底：`theme_overrides={"logo": "state-grid"}` 配
`$WITTY_HOME/brand/state-grid.png`，或者直接写 `{"logo": "/abs/mark.png"}`。
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from witty_agent.layout import data_root

_DIR = Path(__file__).resolve().parent / "brand"


def user_brand_dir() -> Path:
    """用户自备标识目录。跟着 WITTY_HOME 走，不缓存——环境变量随时可能改。"""
    return data_root() / "brand"


def asset_names() -> list[str]:
    """随包 + 用户目录里的可用名字。同名时用户目录不额外列一次。"""
    found = {item.stem for item in _DIR.glob("*.png")}
    user = user_brand_dir()
    if user.is_dir():
        found.update(item.stem for item in user.glob("*.png"))
    return sorted(found)


def _white_variant(path: Path) -> Path:
    """`a/b.png` → `a/b-white.png`。反白版和主图放一起，按后缀前插。"""
    return path.with_name(f"{path.stem}-white{path.suffix}")


def _as_file(name: str) -> str:
    """把「看着像路径」的写法解析成文件。

    调用方探反白版时传的是 `<名字>-white`，对路径写法就成了 `/x/mark.png-white`，
    得先还原成 `/x/mark-white.png` 再判存在，不然深底页永远拿不到反白版。
    """
    text = name
    white = False
    if text.endswith("-white"):
        text = text[: -len("-white")]
        white = True
    path = Path(text).expanduser()
    if path.suffix.lower() != ".png":
        return ""
    if white:
        path = _white_variant(path)
    return str(path) if path.is_file() else ""


@lru_cache(maxsize=64)
def asset_path(stem: str) -> str:
    """按名字取 PNG。取不到返回空串——缺一张标识不该让整稿失败。"""
    name = (stem or "").strip()
    if not name:
        return ""
    if "/" in name or "\\" in name or name.startswith("."):
        # 带分隔符的一律按文件路径处理：裸名字禁止分隔符（防目录穿越），
        # 想读包外的图就必须写成明确的路径。
        return _as_file(name)
    for base in (_DIR, user_brand_dir()):
        found = base / f"{name}.png"
        if found.is_file():
            return str(found)
    return ""


@lru_cache(maxsize=64)
def asset_size(stem: str) -> tuple[int, int]:
    """直接读 PNG 的 IHDR 拿像素尺寸，不为了量个宽高就引入 Pillow。"""
    path = asset_path(stem)
    if not path:
        return (0, 0)
    blob = Path(path).read_bytes()[:24]
    if len(blob) < 24 or blob[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")


@lru_cache(maxsize=64)
def asset_data_uri(stem: str) -> str:
    """给 HTML 预览用的内联图。预览是单文件，不能依赖外部路径。"""
    path = asset_path(stem)
    if not path:
        return ""
    blob = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{blob}"


def clear_asset_cache() -> None:
    """换了 WITTY_HOME 或刚放进新图之后清一次。缓存按名字存的是解析结果。"""
    asset_path.cache_clear()
    asset_size.cache_clear()
    asset_data_uri.cache_clear()
