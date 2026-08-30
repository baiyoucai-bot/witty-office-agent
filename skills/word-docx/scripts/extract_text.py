"""把别人给的 .docx 全文抽成 Markdown：标题层级、表格、图片、修订取舍。

    <沙箱 Python> extract_text.py --input 稿.docx
    <沙箱 Python> extract_text.py --input 稿.docx --output 稿.md --media 图片目录
    <沙箱 Python> extract_text.py --input 稿.docx --revisions mark

内核 read 读不了二进制：看目录用 outline.py，读全文用本脚本，改字回 revise.py。

--revisions accept（默认）：按「全部接受」读，w:ins 收进正文、w:del 丢弃；
--revisions reject：按「全部拒绝」读；--revisions mark：写成 {+插入+} / {-删除-}。
--media 给目录时把 word/media/ 里的图片落盘，正文写 ![](目录/图名)；不给就落「（图：图名）」。

退出码 0 抽出成功；2 读不进来。纯标准库。
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W}

HEADING_NAME = re.compile(r"^(heading\s*([1-6])|标题\s*([1-6])|heading([1-6]))$", re.I)


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
        elif style_id in {"1", "2", "3", "4", "5", "6"}:
            mapping[style_id] = int(style_id)
    return mapping


def load_image_rels(rels_xml: bytes | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not rels_xml:
        return mapping
    root = ET.fromstring(rels_xml)
    for rel in root.iter(f"{{{PKG_REL}}}Relationship"):
        if rel.get("Type", "").endswith("/image"):
            mapping[rel.get("Id", "")] = rel.get("Target", "")
    return mapping


class Extractor:
    def __init__(self, revisions: str, image_rels: dict[str, str], media_dir: str) -> None:
        self.revisions = revisions
        self.image_rels = image_rels
        self.media_dir = media_dir
        self.used_media: set[str] = set()
        self.stats = {"ins": 0, "del": 0, "image": 0, "table": 0}

    def _image_text(self, node: ET.Element) -> str:
        blip = node.find(f".//{{{A_NS}}}blip")
        if blip is None:
            return ""
        rid = blip.get(f"{{{R_NS}}}embed", "")
        target = self.image_rels.get(rid, "")
        if not target:
            return "（图）"
        name = Path(target).name
        self.used_media.add(name)
        self.stats["image"] += 1
        if self.media_dir:
            return f"![]({self.media_dir}/{name})"
        return f"（图：{name}）"

    def _runs_text(self, node: ET.Element, mode: str) -> str:
        """mode: normal / ins / del，控制修订取舍。"""
        pieces: list[str] = []
        for child in node:
            tag = child.tag
            if tag == qn("r"):
                for item in child:
                    if item.tag in (qn("t"), qn("delText")):
                        pieces.append(item.text or "")
                    elif item.tag == qn("tab"):
                        pieces.append("\t")
                    elif item.tag == qn("br"):
                        pieces.append(" ")
                    elif item.tag == qn("drawing"):
                        pieces.append(self._image_text(item))
            elif tag == qn("ins"):
                self.stats["ins"] += 1
                body = self._runs_text(child, "ins")
                if self.revisions == "accept":
                    pieces.append(body)
                elif self.revisions == "mark" and body:
                    pieces.append("{+" + body + "+}")
            elif tag == qn("del"):
                self.stats["del"] += 1
                body = self._runs_text(child, "del")
                if self.revisions == "reject":
                    pieces.append(body)
                elif self.revisions == "mark" and body:
                    pieces.append("{-" + body + "-}")
            elif tag in (qn("hyperlink"), qn("smartTag"), qn("fldSimple")):
                pieces.append(self._runs_text(child, mode))
        return "".join(pieces)

    def paragraph(self, node: ET.Element, levels: dict[str, int]) -> str:
        text = self._runs_text(node, "normal").strip()
        style = node.find("w:pPr/w:pStyle", NS)
        style_id = style.get(qn("val"), "") if style is not None else ""
        level = levels.get(style_id, 0)
        if level and text:
            return "#" * min(level, 6) + " " + text
        return text

    def table(self, node: ET.Element, levels: dict[str, int]) -> str:
        self.stats["table"] += 1
        rows: list[list[str]] = []
        for tr in node.findall("w:tr", NS):
            cells = []
            for tc in tr.findall("w:tc", NS):
                paras = [self.paragraph(p, {}) for p in tc.findall("w:p", NS)]
                cell = " ".join(p for p in paras if p)
                cells.append(cell.replace("|", "\\|").replace("\n", " "))
            rows.append(cells)
        if not rows:
            return ""
        cols = max(len(row) for row in rows)
        lines = []
        for index, row in enumerate(rows):
            padded = row + [""] * (cols - len(row))
            lines.append("| " + " | ".join(padded) + " |")
            if index == 0:
                lines.append("|" + " --- |" * cols)
        return "\n".join(lines)


def extract(path: Path, revisions: str, media_dir: str) -> tuple[str, Extractor]:
    with zipfile.ZipFile(path) as zin:
        names = set(zin.namelist())
        if "word/document.xml" not in names:
            raise ValueError("不是 docx：缺 word/document.xml")
        document = ET.fromstring(zin.read("word/document.xml"))
        styles = zin.read("word/styles.xml") if "word/styles.xml" in names else None
        rels = (
            zin.read("word/_rels/document.xml.rels")
            if "word/_rels/document.xml.rels" in names
            else None
        )
        levels = load_style_levels(styles)
        worker = Extractor(revisions, load_image_rels(rels), media_dir)
        body = document.find("w:body", NS)
        if body is None:
            raise ValueError("document.xml 里没有 w:body")
        chunks: list[str] = []
        for child in body:
            if child.tag == qn("p"):
                text = worker.paragraph(child, levels)
                if text:
                    chunks.append(text)
            elif child.tag == qn("tbl"):
                text = worker.table(child, levels)
                if text:
                    chunks.append(text)
        if media_dir and worker.used_media:
            dest = Path(media_dir)
            dest.mkdir(parents=True, exist_ok=True)
            for name in sorted(worker.used_media):
                part = f"word/media/{name}"
                if part in names:
                    (dest / name).write_bytes(zin.read(part))
    return "\n\n".join(chunks) + "\n", worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="docx 全文抽成 Markdown")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--media", default="", help="图片落盘目录；不给只写图名")
    parser.add_argument("--revisions", choices=("accept", "reject", "mark"), default="accept")
    args = parser.parse_args(argv)
    path = Path(args.input)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2
    try:
        body, worker = extract(path, args.revisions, args.media)
    except (zipfile.BadZipFile, ET.ParseError, ValueError, OSError) as exc:
        print(f"读不进来: {exc}", file=sys.stderr)
        return 2
    if args.output:
        dest = Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        print(dest)
    else:
        print(body, end="")
    stats = worker.stats
    if stats["ins"] or stats["del"]:
        print(
            f"修订: {stats['ins']} 处插入 {stats['del']} 处删除（--revisions {args.revisions}）",
            file=sys.stderr,
        )
    print(
        f"表格 {stats['table']} 张，图片 {stats['image']} 张",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
