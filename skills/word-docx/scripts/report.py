"""把 long-document 工程打成可在 Word 里改的 .docx（标题 + 正文 + 表格 + 图片 + 题注 + 交叉引用 + 目录域）。

    <沙箱 Python> report.py --project <工程> --output 稿.docx
    <沙箱 Python> report.py --project <工程> --output 稿.docx --toc

chapters/*.md 里的约定：

- 表格：标准 Markdown 表。表格前一行写「表: 题注 {#tbl:标签}」会生成带 SEQ 域的题注（标签可省）。
- 图片：``![题注](assets/图.png){#fig:标签}`` 单独占一行，路径相对工程根；题注走 SEQ 域自动编号。
- 交叉引用：正文里写 ``[@fig:标签]`` / ``[@tbl:标签]``，导出成指向题注书签的 REF 域。
- ``--toc`` 在正文前插 TOC 域目录；settings.xml 带 updateFields，Word 打开时自动刷新页码和编号。

缺图或引用悬空默认退出 2；``--allow-missing-assets`` 改为落「（缺图：…）」占位并继续。
不按公文版心排。红头走 gongwen.py。不要用 pandoc 当定稿。
纯标准库，不依赖 python-docx。
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

# `~N字` 和 `+N表` 是提纲给 check_doc 的预算标注，不是标题的一部分。
# 少认一个后缀，非贪婪的标题组就会把它一起吞进去，导出的章标题变成「投资估算 ~2000字 +2表」。
OUTLINE_LINE = re.compile(
    r"^-\s+\[([^\]]+)\]\s+(.+?)(?:\s+~(\d+)\s*字)?(?:\s+\+(\d+)\s*表)?\s*$"
)
IMAGE_LINE = re.compile(r"^!\[(.*?)\]\(([^)]+?)\)\s*(?:\{#(fig:[\w-]+)\})?\s*$")
TBL_CAPTION_LINE = re.compile(r"^表[:：]\s*(.+?)\s*(?:\{#(tbl:[\w-]+)\})?\s*$")
REF_INLINE = re.compile(r"\[@((?:fig|tbl):[\w-]+)\]")
NUM_INLINE = re.compile(r"\[num:([A-Za-z0-9_.-]+)\]")
TOML_HEAD = re.compile(r"^\[([A-Za-z0-9_.-]+)\]\s*$")
TOML_KV = re.compile(r'^([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$')
TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"

EMU_PER_PX = 9525          # 96 dpi
MAX_W_EMU = 5394960        # 5.9 英寸，版心 6.27 英寸留边
MAX_H_EMU = 7315200        # 8 英寸

IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif"}

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:settings xmlns:w="{W}"><w:updateFields w:val="true"/></w:settings>'
)

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Normal" w:default="1">
    <w:name w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:pPr><w:jc w:val="center"/><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="黑体"/><w:b/><w:sz w:val="44"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="360" w:after="200"/></w:pPr>
    <w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="280" w:after="160"/></w:pPr>
    <w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="2"/><w:spacing w:before="200" w:after="120"/></w:pPr>
    <w:rPr><w:rFonts w:eastAsia="楷体"/><w:b/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Caption">
    <w:name w:val="caption"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:before="80" w:after="160"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="楷体"/><w:sz w:val="21"/></w:rPr>
  </w:style>
</w:styles>
"""

BODY_RPR = '<w:rPr><w:rFonts w:eastAsia="宋体" w:ascii="Times New Roman"/><w:sz w:val="24"/></w:rPr>'
CAPTION_RPR = '<w:rPr><w:rFonts w:eastAsia="楷体" w:ascii="Times New Roman"/><w:sz w:val="21"/></w:rPr>'


def _run(text: str, rpr: str = BODY_RPR, bold: bool = False) -> str:
    props = rpr
    if bold:
        props = props.replace("</w:rPr>", "<w:b/></w:rPr>")
    return f'<w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _p(style: str, text: str) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        f'<w:r><w:rPr><w:rFonts w:eastAsia="宋体" w:ascii="Times New Roman"/></w:rPr>'
        f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    )


def parse_outline(text: str) -> list[tuple[str, str]]:
    items = []
    for line in text.splitlines():
        match = OUTLINE_LINE.match(line.strip())
        if match:
            items.append((match.group(1).strip(), match.group(2).strip()))
    return items


def _split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [cell.strip() for cell in body.split("|")]


def md_blocks(text: str) -> list[tuple]:
    """解析章节 Markdown 为块序列。

    返回块："h1"/"h2"/"h3"/"p" (text)、"image" (alt, path, label)、
    "table" (header|None, rows, caption|None)，caption 为 (text, label|None)。
    """
    lines = text.splitlines()
    blocks: list[tuple] = []
    pending_caption: tuple[str, str | None] | None = None
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            header = None
            if len(rows) >= 2 and all(TABLE_SEP_CELL.match(cell) for cell in rows[1]):
                header = rows[0]
                rows = rows[2:]
            blocks.append(("table", header, rows, pending_caption))
            pending_caption = None
            continue
        if pending_caption is not None:
            # 题注行后面没跟表格，按普通段落回放
            blocks.append(("p", f"表: {pending_caption[0]}"))
            pending_caption = None
        image = IMAGE_LINE.match(stripped)
        if image:
            blocks.append(("image", image.group(1).strip(), image.group(2).strip(), image.group(3)))
            i += 1
            continue
        caption = TBL_CAPTION_LINE.match(stripped)
        if caption:
            nxt = i + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            if nxt < len(lines) and lines[nxt].strip().startswith("|"):
                pending_caption = (caption.group(1).strip(), caption.group(2))
                i += 1
                continue
        if stripped.startswith("# "):
            blocks.append(("h1", stripped[2:].strip()))
        elif stripped.startswith("## "):
            blocks.append(("h2", stripped[3:].strip()))
        elif stripped.startswith("### "):
            blocks.append(("h3", stripped[4:].strip()))
        else:
            blocks.append(("p", stripped))
        i += 1
    if pending_caption is not None:
        blocks.append(("p", f"表: {pending_caption[0]}"))
    return blocks


def image_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if data[:3] == b"GIF" and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return width, height
    if data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return width, height
            (length,) = struct.unpack(">H", data[index + 2:index + 4])
            index += 2 + length
    return None


def image_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:3] == b"GIF":
        return "gif"
    return None


def load_ledger(root: Path) -> dict[str, str]:
    path = root / "ledger.toml"
    if not path.is_file():
        return {}
    tables: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        head = TOML_HEAD.match(line.strip())
        if head:
            current = head.group(1)
            tables[current] = {}
            continue
        kv = TOML_KV.match(line.strip())
        if current and kv:
            tables[current][kv.group(1)] = kv.group(2)
    out: dict[str, str] = {}
    for key, fields in tables.items():
        shown = (fields.get("text") or "").strip()
        if key == "example" and not shown:
            continue
        out[key] = shown
    return out


class Builder:
    """两遍成稿：第一遍编号和登记书签标签，第二遍出 XML。"""

    def __init__(self, root: Path, allow_missing: bool) -> None:
        self.root = root
        self.allow_missing = allow_missing
        self.ledger = load_ledger(root)
        self.labels: dict[str, str] = {}
        self.errors: list[str] = []
        self.media: list[tuple[str, bytes]] = []
        self.parts: list[str] = []
        self.rel_extra: list[str] = []
        self.exts: set[str] = set()
        self.fig_num = 0
        self.tbl_num = 0
        self.bookmark_id = 0
        self.drawing_id = 0

    # ---------- 第一遍：编号 ----------

    def number_pass(self, chapter_blocks: list[list[tuple]]) -> None:
        fig = tbl = 0
        for blocks in chapter_blocks:
            for block in blocks:
                if block[0] == "image" and (block[1] or block[3]):
                    fig += 1
                    if block[3]:
                        self._register(block[3], f"图{fig}")
                elif block[0] == "table" and block[3]:
                    tbl += 1
                    if block[3][1]:
                        self._register(block[3][1], f"表{tbl}")
        for blocks in chapter_blocks:
            for block in blocks:
                texts: list[str] = []
                if block[0] == "p":
                    texts.append(block[1])
                elif block[0] == "image":
                    texts.append(block[1] or "")
                elif block[0] == "table":
                    if block[3]:
                        texts.append(block[3][0])
                    for row in ([block[1]] if block[1] else []) + list(block[2]):
                        texts.extend(row)
                for text in texts:
                    for label in REF_INLINE.findall(text):
                        if label not in self.labels:
                            self.errors.append(f"[@{label}] 引用悬空：没有对应的题注标签")
                    for num_id in NUM_INLINE.findall(text):
                        if num_id not in self.ledger or not self.ledger[num_id]:
                            self.errors.append(f"[num:{num_id}] 在 ledger.toml 里没有或 text 为空")

    def _register(self, label: str, shown: str) -> None:
        if label in self.labels:
            self.errors.append(f"题注标签重复: {{#{label}}}")
            return
        self.labels[label] = shown

    # ---------- 第二遍：出 XML ----------

    @staticmethod
    def bookmark_name(label: str) -> str:
        return "_ref_" + re.sub(r"[^0-9A-Za-z_]", "_", label)

    def expand_nums(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            num_id = match.group(1)
            return self.ledger.get(num_id) or match.group(0)

        return NUM_INLINE.sub(repl, text)

    def inline(self, text: str, rpr: str = BODY_RPR, bold: bool = False) -> str:
        """展开 [num:] 和 [@fig:/@tbl:]，出一串 run。

        表格单元格和正文段落共用这一条：number_pass 本来就扫单元格里的 [@…]，
        渲染却只扫段落，于是表里引用别的表会原样印出 `[@tbl:xxx]`——校验说没问题，
        Word 里却是个死标记。
        """
        text = self.expand_nums(text)
        runs: list[str] = []
        cursor = 0
        for match in REF_INLINE.finditer(text):
            if match.start() > cursor:
                runs.append(_run(text[cursor:match.start()], rpr, bold=bold))
            label = match.group(1)
            shown = self.labels.get(label, f"[@{label}]")
            if label in self.labels:
                runs.append(
                    f'<w:fldSimple w:instr=" REF {self.bookmark_name(label)} \\h ">'
                    f"{_run(shown, rpr, bold=bold)}</w:fldSimple>"
                )
            else:
                runs.append(_run(shown, rpr, bold=bold))
            cursor = match.end()
        if cursor < len(text) or not runs:
            runs.append(_run(text[cursor:], rpr, bold=bold))
        return "".join(runs)

    def para(self, text: str) -> str:
        return f"<w:p><w:pPr></w:pPr>{self.inline(text)}</w:p>"

    def caption(self, kind: str, number: int, text: str, label: str | None) -> str:
        seq_name = "图" if kind == "fig" else "表"
        inner = (
            _run(seq_name, CAPTION_RPR)
            + f'<w:fldSimple w:instr=" SEQ {seq_name} \\* ARABIC ">'
            + _run(str(number), CAPTION_RPR)
            + "</w:fldSimple>"
        )
        if label:
            self.bookmark_id += 1
            name = self.bookmark_name(label)
            inner = (
                f'<w:bookmarkStart w:id="{self.bookmark_id}" w:name="{name}"/>'
                + inner
                + f'<w:bookmarkEnd w:id="{self.bookmark_id}"/>'
            )
        if text:
            inner += _run("  " + self.expand_nums(text), CAPTION_RPR)
        return f'<w:p><w:pPr><w:pStyle w:val="Caption"/></w:pPr>{inner}</w:p>'

    def image(self, alt: str, rel_path: str, label: str | None) -> list[str]:
        source = Path(rel_path)
        if not source.is_absolute():
            source = self.root / rel_path
        if not source.is_file():
            message = f"缺图: {rel_path}"
            if self.allow_missing:
                return [self.para(f"（{message}）")]
            self.errors.append(message)
            return []
        data = source.read_bytes()
        ext = image_ext(data)
        if ext is None:
            self.errors.append(f"不认识的图片格式（只认 png/jpeg/gif）: {rel_path}")
            return []
        size = image_size(data) or (400, 300)
        cx = size[0] * EMU_PER_PX
        cy = size[1] * EMU_PER_PX
        if cx > MAX_W_EMU:
            cy = cy * MAX_W_EMU // cx
            cx = MAX_W_EMU
        if cy > MAX_H_EMU:
            cx = cx * MAX_H_EMU // cy
            cy = MAX_H_EMU
        index = len(self.media) + 1
        filename = f"image{index}.{ext}"
        rid = f"rIdImg{index}"
        self.media.append((filename, data))
        self.exts.add(ext)
        self.rel_extra.append(
            f'<Relationship Id="{rid}" Type="{R_NS}/image" Target="media/{filename}"/>'
        )
        self.drawing_id += 1
        drawing = (
            '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
            f'<wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:docPr id="{self.drawing_id}" name="图片{self.drawing_id}"/>'
            f'<a:graphic xmlns:a="{A_NS}">'
            f'<a:graphicData uri="{PIC_NS}">'
            f'<pic:pic xmlns:pic="{PIC_NS}">'
            f'<pic:nvPicPr><pic:cNvPr id="{self.drawing_id}" name="{escape(filename)}"/>'
            "<pic:cNvPicPr/></pic:nvPicPr>"
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
        )
        out = [drawing]
        if alt or label:
            self.fig_num += 1
            out.append(self.caption("fig", self.fig_num, alt, label))
        return out

    def table(self, header: list[str] | None, rows: list[list[str]],
              caption: tuple[str, str | None] | None) -> list[str]:
        out: list[str] = []
        if caption:
            self.tbl_num += 1
            out.append(self.caption("tbl", self.tbl_num, caption[0], caption[1]))
        all_rows = ([header] if header else []) + rows
        if not all_rows:
            return out
        cols = max(len(row) for row in all_rows)
        width = 9026 // max(cols, 1)
        grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(cols))
        borders = "".join(
            f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            for side in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        body_rows: list[str] = []
        for r_index, row in enumerate(all_rows):
            bold = header is not None and r_index == 0
            cells = []
            for c_index in range(cols):
                raw = row[c_index] if c_index < len(row) else ""
                cells.append(
                    f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
                    f"<w:p><w:pPr></w:pPr>{self.inline(raw, BODY_RPR, bold=bold)}</w:p></w:tc>"
                )
            body_rows.append(f"<w:tr>{''.join(cells)}</w:tr>")
        out.append(
            "<w:tbl><w:tblPr>"
            '<w:tblW w:w="5000" w:type="pct"/>'
            f"<w:tblBorders>{borders}</w:tblBorders>"
            "</w:tblPr>"
            f"<w:tblGrid>{grid}</w:tblGrid>"
            f"{''.join(body_rows)}</w:tbl>"
        )
        out.append("<w:p/>")  # 相邻表格会被 Word 合并，垫一个空段
        return out

    def toc_block(self) -> list[str]:
        heading = (
            '<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="240" w:after="240"/></w:pPr>'
            '<w:r><w:rPr><w:rFonts w:eastAsia="黑体" w:ascii="Times New Roman"/><w:b/>'
            '<w:sz w:val="32"/></w:rPr><w:t>目  录</w:t></w:r></w:p>'
        )
        field = (
            '<w:p><w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r>'
            '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            + _run("（目录域：Word 打开时自动更新，或全选后按 F9）")
            + '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        )
        page_break = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
        return [heading, field, page_break]

    def render(self, block: tuple) -> list[str]:
        kind = block[0]
        if kind == "h1":
            return [_p("Heading1", block[1])]
        if kind == "h2":
            return [_p("Heading2", block[1])]
        if kind == "h3":
            return [_p("Heading3", block[1])]
        if kind == "image":
            return self.image(block[1], block[2], block[3])
        if kind == "table":
            return self.table(block[1], block[2], block[3])
        return [self.para(block[1])]


def project_title(root: Path) -> str:
    card = root / "项目卡.md"
    if card.is_file():
        for line in card.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return root.name


def content_types(exts: set[str]) -> str:
    defaults = "".join(
        f'<Default Extension="{ext}" ContentType="{IMAGE_TYPES[ext]}"/>' for ext in sorted(exts)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{defaults}"
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        "</Types>"
    )


def doc_rels(extra: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
        f"{''.join(extra)}"
        "</Relationships>"
    )


def build(root: Path, toc: bool, allow_missing: bool) -> tuple[str, Builder]:
    outline_path = root / "outline.md"
    if not outline_path.is_file():
        raise ValueError("缺 outline.md")
    items = parse_outline(outline_path.read_text(encoding="utf-8"))
    if not items:
        raise ValueError("提纲是空的")
    builder = Builder(root, allow_missing)
    chapter_blocks: list[list[tuple]] = []
    for slug, title in items:
        path = root / "chapters" / f"{slug}.md"
        if not path.is_file():
            chapter_blocks.append([("h1", title), ("p", "（缺稿）")])
            continue
        blocks = md_blocks(path.read_text(encoding="utf-8"))
        # 第一个 h1 归一成提纲标题；没有 h1 就补
        if blocks and blocks[0][0] == "h1":
            blocks[0] = ("h1", blocks[0][1] or title)
        else:
            blocks.insert(0, ("h1", title))
        chapter_blocks.append(blocks)
    builder.number_pass(chapter_blocks)
    parts = [_p("Title", project_title(root))]
    if toc:
        parts.extend(builder.toc_block())
    for blocks in chapter_blocks:
        for block in blocks:
            parts.extend(builder.render(block))
    if builder.errors and not allow_missing:
        raise ValueError("；".join(dict.fromkeys(builder.errors)))
    sect = (
        "<w:sectPr>"
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr>"
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" xmlns:r="{R_NS}" xmlns:wp="{WP_NS}">'
        f"<w:body>{''.join(parts)}{sect}</w:body></w:document>"
    )
    return xml, builder


def write_docx(root: Path, dest: Path, toc: bool = False, allow_missing: bool = False) -> None:
    xml, builder = build(root, toc, allow_missing)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("[Content_Types].xml", content_types(builder.exts))
        zout.writestr("_rels/.rels", ROOT_RELS)
        zout.writestr("word/_rels/document.xml.rels", doc_rels(builder.rel_extra))
        zout.writestr("word/styles.xml", STYLES)
        zout.writestr("word/settings.xml", SETTINGS)
        zout.writestr("word/document.xml", xml)
        for filename, data in builder.media:
            zout.writestr(f"word/media/{filename}", data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长文档工程导出 docx")
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--toc", action="store_true", help="正文前插目录域")
    parser.add_argument("--allow-missing-assets", action="store_true",
                        help="缺图/悬空引用不退出，落占位")
    args = parser.parse_args(argv)
    root = Path(args.project)
    if not root.is_dir():
        print(f"工程目录不存在: {root}", file=sys.stderr)
        return 2
    try:
        write_docx(root, Path(args.output), toc=args.toc,
                   allow_missing=args.allow_missing_assets)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
