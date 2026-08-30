"""扫描件 OCR：PDF 先用 pdftoppm 转图，再用 RapidOCR（离线，模型随包）认字。

    <沙箱 Python> ocr.py --input 扫描.pdf --output 扫描.txt
    <沙箱 Python> ocr.py --input 扫描.pdf --pages 1-3 --dpi 300
    <沙箱 Python> ocr.py --input 单页.png

先跑 extract.py --check：有文字层就不要 OCR，直接抽更准。
RapidOCR（rapidocr-onnxruntime）在沙箱预装；本机还需要 poppler 的 pdftoppm 转图。
认出来的字带识别误差，金额、编号等关键字段要人工核对，不要直接当来源账。

退出码 0 认到字；1 一个字都没认出来；2 缺 pdftoppm / 缺 RapidOCR / 读不进来。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def load_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        raise RuntimeError(
            "沙箱没装 rapidocr-onnxruntime：uv pip install --python <沙箱解释器> rapidocr-onnxruntime，"
            "或把它加进 config/runtime.toml 的 [sandbox].packages"
        ) from None
    return RapidOCR()


def order_lines(items: list) -> list[str]:
    """RapidOCR 返回 [box, text, score]；按框的左上角先 y 后 x 排成阅读顺序。"""

    def key(item):
        box = item[0]
        ys = [point[1] for point in box]
        xs = [point[0] for point in box]
        return (min(ys), min(xs))

    return [str(item[1]) for item in sorted(items, key=key)]


def ocr_image(engine, path: Path) -> str:
    result = engine(str(path))
    items = result[0] if isinstance(result, tuple) else result
    if not items:
        return ""
    return "\n".join(order_lines(list(items)))


def pdf_to_images(path: Path, start: int, end: int, dpi: int, workdir: Path) -> list[Path]:
    exe = shutil.which("pdftoppm")
    if not exe:
        raise RuntimeError("本机没有 pdftoppm（poppler），PDF 转不了图；图片文件可以直接 OCR")
    cmd = [exe, "-png", "-r", str(dpi)]
    if start > 1:
        cmd.extend(["-f", str(start)])
    if end > 0:
        cmd.extend(["-l", str(end)])
    cmd.extend([str(path), str(workdir / "page")])
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(err or "pdftoppm 失败")
    images = sorted(workdir.glob("page-*.png")) or sorted(workdir.glob("page*.png"))
    if not images:
        raise RuntimeError("pdftoppm 没有产出图片")
    return images


def parse_pages(raw: str) -> tuple[int, int]:
    text = (raw or "").strip()
    if not text:
        return 1, 0
    if "-" in text:
        left, right = text.split("-", 1)
        return int(left), int(right)
    page = int(text)
    return page, page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描件 OCR")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--pages", default="", help="如 1-3 或 2（仅 PDF）")
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args(argv)
    path = Path(args.input)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2
    suffix = path.suffix.lower()
    if suffix != ".pdf" and suffix not in IMAGE_SUFFIXES:
        print(f"只认 .pdf 和图片（{' '.join(sorted(IMAGE_SUFFIXES))}）", file=sys.stderr)
        return 2
    try:
        engine = load_engine()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    pages: list[str] = []
    start, end = parse_pages(args.pages)
    try:
        if suffix == ".pdf":
            with tempfile.TemporaryDirectory() as tmp:
                images = pdf_to_images(path, start, end, args.dpi, Path(tmp))
                for image in images:
                    pages.append(ocr_image(engine, image))
        else:
            pages.append(ocr_image(engine, path))
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    body = "\n\n".join(
        f"--- page {index} ---\n{text}" for index, text in enumerate(pages, start=max(start, 1))
    )
    if args.output:
        dest = Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body + "\n", encoding="utf-8")
        print(f"{dest}  pages={len(pages)}")
    else:
        print(body)
    if all(not text.strip() for text in pages):
        print("WARN 一个字都没认出来：图太糊或不是文字页", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
