"""从 PDF 抽表格（pdfplumber），输出 Markdown。

    <沙箱 Python> tables.py --input 文件.pdf
    <沙箱 Python> tables.py --input 文件.pdf --pages 2-5 --output 表.md

只认有线框或对齐结构的文字层表格；扫描件先走 ocr.py。
pdfplumber 在沙箱预装（[sandbox].packages）；没有装时退出 2 并给装法。

退出码 0 抽到表；1 一张表都没有；2 读不进来或没有 pdfplumber。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PageTables = tuple[int, list[list[list[str]]]]


def normalize(table: list[list[object]]) -> list[list[str]]:
    rows = []
    for row in table:
        cells = []
        for cell in row:
            text = "" if cell is None else str(cell)
            cells.append(text.replace("|", "\\|").replace("\n", " ").strip())
        rows.append(cells)
    return rows


def to_markdown(pages: list[PageTables]) -> str:
    chunks: list[str] = []
    count = 0
    for page_no, tables in pages:
        for index, table in enumerate(tables, start=1):
            if not table:
                continue
            count += 1
            cols = max(len(row) for row in table)
            lines = [f"### 第{page_no}页 表{index}", ""]
            for r_index, row in enumerate(table):
                padded = row + [""] * (cols - len(row))
                lines.append("| " + " | ".join(padded) + " |")
                if r_index == 0:
                    lines.append("|" + " --- |" * cols)
            chunks.append("\n".join(lines))
    if not count:
        return ""
    return "\n\n".join(chunks) + "\n"


def extract_tables(path: Path, start: int, end: int) -> list[PageTables]:
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "沙箱没装 pdfplumber：uv pip install --python <沙箱解释器> pdfplumber，"
            "或把它加进 config/runtime.toml 的 [sandbox].packages"
        ) from None
    pages: list[PageTables] = []
    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        first = max(start, 1)
        last = total if end <= 0 else min(end, total)
        for number in range(first, last + 1):
            page = pdf.pages[number - 1]
            tables = [normalize(table) for table in page.extract_tables()]
            tables = [table for table in tables if table]
            if tables:
                pages.append((number, tables))
    return pages


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
    parser = argparse.ArgumentParser(description="从 PDF 抽表格")
    parser.add_argument("--input", required=True)
    parser.add_argument("--pages", default="", help="如 1-3 或 2")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    path = Path(args.input)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2
    start, end = parse_pages(args.pages)
    try:
        pages = extract_tables(path, start, end)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # pdfplumber 对坏文件抛的类型不稳定
        print(f"读不进来: {exc}", file=sys.stderr)
        return 2
    body = to_markdown(pages)
    if not body:
        print("0 张表：没有线框/对齐结构，或本来就是扫描件（先 ocr.py）", file=sys.stderr)
        return 1
    total = sum(len(tables) for _, tables in pages)
    if args.output:
        dest = Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        print(f"{dest}  tables={total}")
    else:
        print(body, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
