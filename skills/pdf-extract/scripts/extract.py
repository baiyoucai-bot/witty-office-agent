"""从 PDF 抽文本。优先 pdftotext -layout，没有再试 pypdf。

    <沙箱 Python> extract.py --input 文件.pdf --output 文件.txt
    <沙箱 Python> extract.py --input 文件.pdf --pages 1-3
    <沙箱 Python> extract.py --input 文件.pdf --check

--check 只读：0 页、加密、抽出来是空的（扫描件）报 FAIL。
扫描件走同技能 ocr.py；表格走 tables.py。不创建 PDF、不填表。

退出码 0 成功/无 FAIL；1 有 FAIL；2 读不进来或没有抽取器。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

Finding = tuple[str, str]


def _pdftotext() -> str | None:
    return shutil.which("pdftotext")


def _pypdf_text(path: Path, start: int, end: int) -> tuple[int, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("没有 pdftotext，沙箱也没装 pypdf") from exc
    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        raise RuntimeError("PDF 加密了，抽不出来")
    total = len(reader.pages)
    first = max(start, 1)
    last = total if end <= 0 else min(end, total)
    pages = []
    for index in range(first, last + 1):
        text = reader.pages[index - 1].extract_text() or ""
        pages.append(text)
    return total, pages


def _pdftotext_text(path: Path, start: int, end: int) -> tuple[int, list[str]]:
    exe = _pdftotext()
    if not exe:
        raise RuntimeError("no pdftotext")
    cmd = [exe, "-layout"]
    if start > 1:
        cmd.extend(["-f", str(start)])
    if end > 0:
        cmd.extend(["-l", str(end)])
    cmd.extend([str(path), "-"])
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(err or "pdftotext 失败")
    body = proc.stdout.decode("utf-8", "replace")
    chunks = re.split(r"\f", body)
    pages = [item.rstrip("\n") for item in chunks if item.strip() or item == chunks[0]]
    if pages and pages[-1] == "":
        pages = pages[:-1]
    # pdftotext 不给总页数；用 pdfinfo 或自己数 form feed
    total = max(len(pages), end, start)
    return total, pages


def extract_pages(path: Path, start: int = 1, end: int = 0) -> tuple[int, list[str], str]:
    if _pdftotext():
        total, pages = _pdftotext_text(path, start, end)
        return total, pages, "pdftotext"
    total, pages = _pypdf_text(path, start, end)
    return total, pages, "pypdf"


def is_encrypted(path: Path) -> bool:
    sample = path.read_bytes()[:8192]
    return b"/Encrypt" in sample


def check(path: Path) -> list[Finding]:
    found: list[Finding] = []
    if is_encrypted(path):
        found.append(("FAIL", "文件带 /Encrypt，未解密抽不出字"))
        return found
    try:
        total, pages, engine = extract_pages(path)
    except RuntimeError as exc:
        found.append(("FAIL", str(exc)))
        return found
    if total <= 0 or not pages:
        found.append(("FAIL", "0 页或抽出来是空的（多半是扫描件，走同技能 ocr.py）"))
        return found
    empty = [index + 1 for index, text in enumerate(pages) if not text.strip()]
    if empty:
        found.append(("FAIL", f"第 {','.join(map(str, empty))} 页没有文字层"))
    if all(not text.strip() for text in pages):
        found.append(("FAIL", f"{engine} 抽到 0 字：扫描 PDF 走同技能 ocr.py"))
    return found


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
    parser = argparse.ArgumentParser(description="从 PDF 抽文本")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--pages", default="", help="如 1-3 或 2")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.input)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2
    if args.check:
        findings = check(path)
        if not findings:
            print("OK  0 FAIL")
            return 0
        print(f"{sum(1 for item in findings if item[0] == 'FAIL')} FAIL")
        for level, message in findings:
            print(f"{level}  {message}")
        return 1
    start, end = parse_pages(args.pages)
    try:
        total, pages, engine = extract_pages(path, start, end)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    body = "\n\n".join(
        f"--- page {index} ---\n{text}" for index, text in enumerate(pages, start=max(start, 1))
    )
    if args.output:
        dest = Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body + "\n", encoding="utf-8")
        print(f"{dest}  pages={len(pages)}/{total}  engine={engine}")
    else:
        print(body)
    if all(not text.strip() for text in pages):
        print("WARN 抽到 0 字，多半是扫描件，走同技能 ocr.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
