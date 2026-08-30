"""合并 PDF、填写 AcroForm。不从零排版生成 PDF。

    <沙箱 Python> compose.py --merge 甲.pdf 乙.pdf --output 合订.pdf
    <沙箱 Python> compose.py --fill 表单.pdf --spec 字段.json --output 填好.pdf
    <沙箱 Python> compose.py --help-spec

合并按参数顺序拼接页面。填表只改 AcroForm 域值，版式不动。
没有可填域、spec 对不上域名，退出 2，不要假装写进去了。
沙箱预装 pypdf。要 OCR / 抽表走同技能其它脚本。

退出码 0 写出成功；2 缺 pypdf / 读不进来 / spec 对不上。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SPEC_HELP = """填表 spec JSON：对象，键是域名字、值是要写入的字符串。

{
  "姓名": "张三",
  "金额": "100"
}

先 --list 看域名字，再写 spec。没有 AcroForm 的扫描件填不了。
"""


def _pypdf():
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError(
            "沙箱没装 pypdf：uv pip install --python <沙箱解释器> pypdf，"
            "或把它加进 config/runtime.toml 的 [sandbox].packages"
        ) from exc
    return PdfReader, PdfWriter


def list_fields(path: Path) -> dict[str, str]:
    PdfReader, _writer = _pypdf()
    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        raise RuntimeError("PDF 加密了")
    fields = reader.get_fields() or {}
    out: dict[str, str] = {}
    for name, field in fields.items():
        value = ""
        if isinstance(field, dict):
            raw = field.get("/V")
            value = "" if raw is None else str(raw)
        out[str(name)] = value
    return out


def merge_pdfs(sources: list[Path], dest: Path) -> int:
    PdfReader, PdfWriter = _pypdf()
    writer = PdfWriter()
    pages = 0
    for src in sources:
        reader = PdfReader(str(src))
        if getattr(reader, "is_encrypted", False):
            raise RuntimeError(f"{src.name} 加密了，合不进去")
        if len(reader.pages) == 0:
            raise RuntimeError(f"{src.name} 是 0 页")
        writer.append(reader)
        pages += len(reader.pages)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)
    return pages


def fill_form(src: Path, dest: Path, values: dict[str, str]) -> int:
    PdfReader, PdfWriter = _pypdf()
    reader = PdfReader(str(src))
    if getattr(reader, "is_encrypted", False):
        raise RuntimeError("PDF 加密了")
    existing = list_fields(src)
    if not existing:
        raise RuntimeError("没有 AcroForm 可填域（扫描件或纯文字 PDF 填不了）")
    unknown = [key for key in values if key not in existing]
    if unknown:
        raise RuntimeError("spec 对不上这些域名: " + "、".join(unknown))
    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        writer.update_page_form_field_values(page, values)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)
    return len(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合并 PDF 或填写 AcroForm")
    parser.add_argument("--merge", nargs="+", default=[], help="按顺序合并这些 PDF")
    parser.add_argument("--fill", default="", help="要填的表单 PDF")
    parser.add_argument("--spec", default="", help="填表 JSON")
    parser.add_argument("--output", default="")
    parser.add_argument("--list", dest="list_path", default="", help="只列出可填域名")
    parser.add_argument("--help-spec", action="store_true")
    args = parser.parse_args(argv)
    if args.help_spec:
        print(SPEC_HELP)
        return 0
    if args.list_path:
        path = Path(args.list_path)
        if not path.is_file():
            print(f"找不到文件: {path}", file=sys.stderr)
            return 2
        try:
            fields = list_fields(path)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not fields:
            print("0 个可填域", file=sys.stderr)
            return 1
        for name, value in fields.items():
            print(f"{name}\t{value}")
        return 0
    if args.merge:
        if not args.output:
            print("合并需要 --output", file=sys.stderr)
            return 2
        sources = [Path(item) for item in args.merge]
        for src in sources:
            if not src.is_file():
                print(f"找不到文件: {src}", file=sys.stderr)
                return 2
        try:
            pages = merge_pdfs(sources, Path(args.output))
        except (RuntimeError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{args.output}  pages={pages}")
        return 0
    if args.fill:
        if not args.spec or not args.output:
            print("填表需要 --fill --spec --output", file=sys.stderr)
            return 2
        src = Path(args.fill)
        spec_path = Path(args.spec)
        for path, label in ((src, "表单"), (spec_path, "spec")):
            if not path.is_file():
                print(f"找不到{label}: {path}", file=sys.stderr)
                return 2
        try:
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not raw:
                raise ValueError("spec 必须是非空对象")
            values = {str(key): "" if val is None else str(val) for key, val in raw.items()}
            filled = fill_form(src, Path(args.output), values)
        except (json.JSONDecodeError, ValueError, RuntimeError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{args.output}  fields={filled}")
        return 0
    print("需要 --merge、--fill 或 --list", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
