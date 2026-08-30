"""xlsx 的 zip / 工作表 / 单元格只读解析。纯标准库。

openpyxl 会把公式缓存值、共享公式、合并区悄悄改掉；pandas.to_excel 会把公式
写成死数字。本模块只解 zip 读 XML，给 inspect / check / apply 当共同底座。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element

SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS = {"m": SSML, "r": OFFICE_REL, "pr": PKG_REL}

WORKBOOK = "xl/workbook.xml"
WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"
SST = "xl/sharedStrings.xml"
SHEET_TYPE = f"{OFFICE_REL}/worksheet"

_REF = re.compile(r"^([A-Za-z]+)(\d+)$")
_ERROR = re.compile(r"^#(REF|DIV/0|VALUE|NAME|N/A|NULL|NUM|GETTING_DATA)!", re.I)


def qn(local: str) -> str:
    return f"{{{SSML}}}{local}"


def register_ns(raw: bytes) -> None:
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z0-9_.\-]+)="([^"]+)"', raw[:4096]):
        ET.register_namespace(prefix.decode(), uri.decode())
    ET.register_namespace("", SSML)


def read_zip(path: Path) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    with zipfile.ZipFile(path) as zin:
        infos = list(zin.infolist())
        data = {item.filename: zin.read(item.filename) for item in infos}
    return infos, data


def write_zip(path: Path, infos: list[zipfile.ZipInfo], data: dict[str, bytes]) -> None:
    seen: set[str] = set()
    with zipfile.ZipFile(path, "w") as zout:
        for info in infos:
            payload = data.get(info.filename)
            if payload is None:
                continue
            zout.writestr(info, payload)
            seen.add(info.filename)
        for name, payload in data.items():
            if name not in seen:
                zout.writestr(name, payload)


def parse_xml(raw: bytes) -> Element:
    register_ns(raw)
    return ET.fromstring(raw)


def col_index(letters: str) -> int:
    total = 0
    for char in letters.upper():
        total = total * 26 + (ord(char) - 64)
    return total


def col_letters(index: int) -> str:
    chars: list[str] = []
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(65 + rem))
    return "".join(reversed(chars))


def split_ref(ref: str) -> tuple[str, int]:
    match = _REF.match((ref or "").strip())
    if not match:
        raise ValueError(f"不是单元格地址: {ref}")
    return match.group(1).upper(), int(match.group(2))


@dataclass
class Cell:
    ref: str
    formula: str = ""
    value: str = ""
    kind: str = "empty"  # empty / number / text / formula / error / bool
    shared_formula: bool = False

    @property
    def has_formula(self) -> bool:
        return bool(self.formula)


@dataclass
class Sheet:
    name: str
    part: str
    cells: dict[str, Cell] = field(default_factory=dict)
    merged: list[str] = field(default_factory=list)
    dimension: str = ""

    @property
    def formula_count(self) -> int:
        return sum(1 for cell in self.cells.values() if cell.has_formula)

    @property
    def error_refs(self) -> list[str]:
        return [cell.ref for cell in self.cells.values() if cell.kind == "error"]


@dataclass
class Workbook:
    sheets: list[Sheet] = field(default_factory=list)
    defined_names: list[str] = field(default_factory=list)
    strings: list[str] = field(default_factory=list)

    def sheet(self, name: str) -> Sheet | None:
        for item in self.sheets:
            if item.name == name:
                return item
        if name.isdigit():
            index = int(name)
            if 1 <= index <= len(self.sheets):
                return self.sheets[index - 1]
        return None


def _sst(data: dict[str, bytes]) -> list[str]:
    raw = data.get(SST)
    if not raw:
        return []
    root = parse_xml(raw)
    out: list[str] = []
    for node in root.findall("m:si", NS):
        texts = [item.text or "" for item in node.findall(".//m:t", NS)]
        out.append("".join(texts))
    return out


def _cell_value(node: Element, strings: list[str]) -> tuple[str, str, str]:
    """返回 kind, 显示值, 公式。"""
    formula_el = node.find("m:f", NS)
    formula = ""
    shared = formula_el is not None and formula_el.get("t") == "shared"
    if formula_el is not None and (formula_el.text or "").strip():
        formula = formula_el.text.strip()
    cached = node.find("m:v", NS)
    cached_text = (cached.text or "").strip() if cached is not None else ""
    cell_type = node.get("t") or ""
    if formula:
        if cached_text and _ERROR.match(cached_text):
            return "error", cached_text, formula
        return "formula", cached_text, formula
    if shared and not formula:
        # 共享公式的从属格只有 t=shared，公式在主格上
        if cached_text and _ERROR.match(cached_text):
            return "error", cached_text, ""
        return "formula", cached_text, ""
    if cell_type == "s":
        try:
            return "text", strings[int(cached_text)], ""
        except (ValueError, IndexError):
            return "text", cached_text, ""
    if cell_type in {"inlineStr", "str"}:
        texts = [item.text or "" for item in node.findall(".//m:t", NS)]
        body = "".join(texts) if texts else cached_text
        return "text", body, ""
    if cell_type == "b":
        return "bool", cached_text, ""
    if cached_text and _ERROR.match(cached_text):
        return "error", cached_text, ""
    if cached_text == "":
        return "empty", "", ""
    return "number", cached_text, ""


def _load_sheet(part: str, name: str, data: dict[str, bytes], strings: list[str]) -> Sheet:
    raw = data.get(part)
    if not raw:
        return Sheet(name=name, part=part)
    root = parse_xml(raw)
    sheet = Sheet(name=name, part=part)
    dim = root.find("m:dimension", NS)
    if dim is not None:
        sheet.dimension = dim.get("ref") or ""
    merge_root = root.find("m:mergeCells", NS)
    if merge_root is not None:
        sheet.merged = [item.get("ref") or "" for item in merge_root.findall("m:mergeCell", NS) if item.get("ref")]
    for node in root.findall("m:sheetData/m:row/m:c", NS):
        ref = node.get("r") or ""
        if not ref:
            continue
        kind, value, formula = _cell_value(node, strings)
        formula_el = node.find("m:f", NS)
        shared = formula_el is not None and formula_el.get("t") == "shared"
        sheet.cells[ref] = Cell(
            ref=ref,
            formula=formula,
            value=value,
            kind=kind,
            shared_formula=shared,
        )
    return sheet


def load_workbook(path: Path) -> tuple[Workbook, list[zipfile.ZipInfo], dict[str, bytes]]:
    infos, data = read_zip(path)
    if WORKBOOK not in data:
        raise ValueError("不是有效的 .xlsx（缺 xl/workbook.xml）")
    strings = _sst(data)
    rels: dict[str, str] = {}
    rels_raw = data.get(WORKBOOK_RELS)
    if rels_raw:
        rel_root = parse_xml(rels_raw)
        for node in rel_root.findall("pr:Relationship", NS):
            rels[node.get("Id") or ""] = node.get("Target") or ""
    book = Workbook(strings=strings)
    root = parse_xml(data[WORKBOOK])
    for node in root.findall("m:sheets/m:sheet", NS):
        name = node.get("name") or ""
        rid = node.get(f"{REL_NS}id") or node.get("id") or ""
        target = rels.get(rid, "")
        part = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        book.sheets.append(_load_sheet(part, name, data, strings))
    for node in root.findall("m:definedNames/m:definedName", NS):
        label = node.get("name") or ""
        if label:
            book.defined_names.append(label)
    return book, infos, data
