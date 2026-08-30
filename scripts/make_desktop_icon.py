"""生成桌面壳图标 apps/desktop/build/icon.ico（「人和」二字，多尺寸）。

用法：uv run python scripts/make_desktop_icon.py
构建 Windows 安装器前由 build_windows_installer.py 自动调用；已有 icon.ico 时跳过，
想重画删掉旧文件再跑。只依赖项目自带的 Pillow 和本机中文字体。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "apps" / "desktop" / "build" / "icon.ico"
TEXT = "人和"
BASE = 512
SIZES = [256, 128, 64, 48, 32, 16]

_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        for index in range(6):
            try:
                font = ImageFont.truetype(path, size, index=index)
            except OSError:
                break
            left, top, right, bottom = font.getbbox(TEXT)
            if right - left > 0 and bottom - top > 0:
                return font
    raise SystemExit("找不到能渲染中文的字体，无法画图标")


def _base_image() -> Image.Image:
    image = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # 底：竖向渐变的圆角方块（深蓝到青），配电系统的常用色相，白字对比够
    top_color = (16, 62, 120)
    bottom_color = (26, 142, 154)
    gradient = Image.new("RGBA", (BASE, BASE))
    grad_draw = ImageDraw.Draw(gradient)
    for y in range(BASE):
        t = y / (BASE - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(top_color, bottom_color)) + (255,)
        grad_draw.line([(0, y), (BASE, y)], fill=color)
    mask = Image.new("L", (BASE, BASE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([8, 8, BASE - 8, BASE - 8], radius=110, fill=255)
    image.paste(gradient, (0, 0), mask)
    # 字：白色「人和」，水平排、居中
    font = _load_font(300)
    left, top, right, bottom = ImageDraw.Draw(image).textbbox((0, 0), TEXT, font=font)
    while right - left > BASE - 96:
        font = _load_font(font.size - 10)
        left, top, right, bottom = draw.textbbox((0, 0), TEXT, font=font)
    x = (BASE - (right - left)) / 2 - left
    y = (BASE - (bottom - top)) / 2 - top
    draw.text((x + 4, y + 6), TEXT, font=font, fill=(0, 0, 0, 70))
    draw.text((x, y), TEXT, font=font, fill=(255, 255, 255, 255))
    return image


def main() -> None:
    base = _base_image()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base.resize((256, 256), Image.LANCZOS).save(
        OUT, format="ICO", sizes=[(s, s) for s in SIZES]
    )
    png = OUT.with_suffix(".png")
    base.resize((256, 256), Image.LANCZOS).save(png, format="PNG")
    sys.stdout.write(f"icon -> {OUT}\n")


if __name__ == "__main__":
    main()
