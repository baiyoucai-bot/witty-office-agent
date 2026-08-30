"""定位本机字体文件，量真实字宽。

metrics 的估算是「中文全宽、拉丁半宽」的启发式，误差 ±10%。这里用 fontTools
读本机字体的 hmtx 表拿真实 advance width，把误差压到排版级；机器上没这个字体
就返回 None，调用方回落启发式——生成机和放映机可能不是同一台，量不到不算错。

只读字体元数据（cmap/hmtx/name），不做整形（shaping）、不管连字和字距，
CJK 场景够用：汉字几乎全是整 em 宽，误差集中在拉丁字母和数字上。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from witty_agent.logging import get_logger

logger = get_logger("pptx")

# 常见中文字体的文件名。先按文件名直找，避免为了一个字体扫全机几百个文件。
_KNOWN_FILES: dict[str, tuple[str, ...]] = {
    "microsoft yahei": ("msyh.ttc", "msyh.ttf", "msyhbd.ttc"),
    "微软雅黑": ("msyh.ttc", "msyh.ttf"),
    "simsun": ("simsun.ttc",),
    "宋体": ("simsun.ttc",),
    "simhei": ("simhei.ttf",),
    "黑体": ("simhei.ttf",),
    "dengxian": ("Deng.ttf", "deng.ttf"),
    "等线": ("Deng.ttf", "deng.ttf"),
    "fangsong": ("simfang.ttf",),
    "仿宋": ("simfang.ttf",),
    "kaiti": ("simkai.ttf",),
    "楷体": ("simkai.ttf",),
    "pingfang sc": ("PingFang.ttc",),
    "苹方": ("PingFang.ttc",),
    "hiragino sans gb": ("Hiragino Sans GB.ttc",),
    "songti sc": ("Songti.ttc",),
    "宋体-简": ("Songti.ttc",),
    "stheiti": ("STHeiti Medium.ttc", "STHeiti Light.ttc"),
    "华文黑体": ("STHeiti Medium.ttc", "STHeiti Light.ttc"),
    "noto sans cjk sc": ("NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf"),
    "source han sans sc": ("SourceHanSansSC-Regular.otf",),
    "思源黑体": ("SourceHanSansSC-Regular.otf", "NotoSansCJK-Regular.ttc"),
    "wenquanyi zen hei": ("wqy-zenhei.ttc",),
    "arial": ("Arial.ttf", "arial.ttf"),
    "calibri": ("calibri.ttf", "Calibri.ttf"),
}

# 找不到指定字体时，按这个顺序找一个能画 CJK 的兜底（光栅器用）。
_CJK_FALLBACKS = (
    "Microsoft YaHei",
    "PingFang SC",
    "Hiragino Sans GB",
    "STHeiti",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "SimHei",
)


def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts")]
        dirs.append(Path.home() / "Library" / "Fonts")
    elif os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(Path(windir) / "Fonts")
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
        dirs.append(Path.home() / ".fonts")
        dirs.append(Path.home() / ".local" / "share" / "fonts")
    extra = os.environ.get("WITTY_FONT_PATH", "")
    if extra:
        dirs = [Path(item) for item in extra.split(os.pathsep) if item.strip()] + dirs
    return [item for item in dirs if item.is_dir()]


def _norm(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


@lru_cache(maxsize=64)
def find_font(family: str) -> str:
    """按字体族名找本机字体文件。找不到返回空串，不报错。"""
    key = _norm(family)
    if not key:
        return ""
    for filename in _KNOWN_FILES.get(key, ()):  # 已知文件名直取
        for base in _font_dirs():
            candidate = base / filename
            if candidate.is_file():
                return str(candidate)
    # 兜底：按文件名词干模糊找（msyh 之外的装法，比如手工装的 otf）
    stem_key = key.replace(" ", "")
    for base in _font_dirs():
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
                continue
            if _norm(entry.stem).replace(" ", "") == stem_key:
                return str(entry)
    return ""


def fallback_font_file() -> str:
    """一个本机确实存在、能画 CJK 的字体文件。光栅器兜底用。"""
    for family in _CJK_FALLBACKS:
        found = find_font(family)
        if found:
            return found
    return ""


class _Face:
    """一个字体面的量宽器。cmap + hmtx，宽度换算成 em。"""

    def __init__(self, font) -> None:
        self.cmap = font.getBestCmap()
        self.hmtx = font["hmtx"]
        self.upm = font["head"].unitsPerEm or 1000

    def width_em(self, ch: str) -> float | None:
        glyph = self.cmap.get(ord(ch))
        if glyph is None:
            return None
        try:
            return self.hmtx[glyph][0] / self.upm
        except (KeyError, IndexError):
            return None


def _face_family(font) -> str:
    try:
        record = font["name"].getDebugName(1)
    except Exception:  # noqa: BLE001 - name 表五花八门，读不出来就算了
        record = None
    return _norm(record or "")


@lru_cache(maxsize=16)
def _load_face(family: str) -> _Face | None:
    path = find_font(family)
    if not path:
        return None
    try:
        from fontTools.ttLib import TTCollection, TTFont

        if path.lower().endswith(".ttc"):
            collection = TTCollection(path, lazy=True)
            want = _norm(family)
            picked = None
            for member in collection.fonts:
                if _face_family(member) == want:
                    picked = member
                    break
            return _Face(picked or collection.fonts[0])
        return _Face(TTFont(path, lazy=True))
    except Exception as exc:  # noqa: BLE001 - 字体文件损坏不该拖垮排版
        logger.warning("字体加载失败 family=%s path=%s err=%s", family, path, exc)
        return None


def char_em(ch: str, family: str) -> float | None:
    """ch 在 family 下的真实宽度（em）。本机没这个字体或没这个字返回 None。"""
    face = _load_face(_norm(family))
    if face is None:
        return None
    return face.width_em(ch)


def has_font(family: str) -> bool:
    return bool(find_font(family))
