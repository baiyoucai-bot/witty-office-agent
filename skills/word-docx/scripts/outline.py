"""从已有 .docx 抽出标题层级。纯标准库。

    <沙箱 Python> outline.py --input 稿.docx
    <沙箱 Python> outline.py --input 稿.docx --json

认 Heading1–6 和「标题 1」以及 Word 中文稿常见的 styleId=1/2/3。
内核 read 拒绝二进制，看目录必须走本脚本。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}

HEADING_NAME = re.compile(
    r"^(heading\s*([1-6])|标题\s*([1-6])|heading([1-6]))$",
    re.I,
)


def qn(local: str) -> str:
    return f"{{{W}}}{local}"


def load_style_levels(styles_xml: bytes | None) -> dict[str, int]:
    mapping: dict[str, int] = {}
    if not styles_xml:
        return mapping
    root = ET.fromstring(styles_xml)
    for style in root.findall("w:style", NS):
        if style.get(qn("type")) not in {None, "paragraph"}:
            continue
        style_id = style.get(qn("styleId")) or ""
        name_el = style.find("w:name", NS)
        name = (name_el.get(qn("val")) if name_el is not None else "") or ""
        outline = style.find("w:pPr/w:outlineLvl", NS)
        if outline is not None and outline.get(qn("val"), "").isdigit():
            mapping[style_id] = int(outline.get(qn("val"))) + 1
            continue
        match = HEADING_NAME.match(name.strip())
        if match:
            level = int(next(group for group in match.groups()[1:] if group))
            mapping[style_id] = level
            mapping[name] = level
    return mapping


def para_text(node) -> str:
    parts: list[str] = []
    for text in node.findall(".//w:t", NS):
        parts.append(text.text or "")
    return "".join(parts).strip()


def outline(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zin:
        doc = zin.read("word/document.xml")
        styles = zin.read("word/styles.xml") if "word/styles.xml" in zin.namelist() else None
    levels = load_style_levels(styles)
    root = ET.fromstring(doc)
    items: list[dict] = []
    for para in root.findall(".//w:p", NS):
        style_el = para.find("w:pPr/w:pStyle", NS)
        style_id = style_el.get(qn("val")) if style_el is not None else ""
        outline_el = para.find("w:pPr/w:outlineLvl", NS)
        level = None
        if outline_el is not None and (outline_el.get(qn("val")) or "").isdigit():
            level = int(outline_el.get(qn("val"))) + 1
        elif style_id in levels:
            level = levels[style_id]
        else:
            match = HEADING_NAME.match(style_id)
            if match:
                level = int(next(group for group in match.groups()[1:] if group))
            elif style_id.isdigit() and 1 <= int(style_id) <= 6:
                level = int(style_id)
        if not level:
            continue
        title = para_text(para)
        if not title:
            continue
        items.append({"level": level, "style": style_id, "title": title})
    return items


def render(items: list[dict]) -> str:
    if not items:
        return "(no headings)"
    lines = []
    for item in items:
        indent = "  " * (item["level"] - 1)
        lines.append(f"{indent}{item['level']}  {item['title']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抽出 docx 标题")
    parser.add_argument("--input", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.input)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2
    try:
        items = outline(path)
    except (KeyError, OSError, ET.ParseError) as exc:
        print(f"读不进来: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(items, ensure_ascii=False, indent=2) if args.json else render(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
