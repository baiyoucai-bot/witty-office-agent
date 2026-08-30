"""把已抽出的文本登记进长文档工程的来源账。已有 id 不覆盖。

    <沙箱 Python> import_source.py --project <工程> --input 原稿.md --id draft --title 用户原稿

只登记，不改章节。二进制 .docx/.pdf 先走 word-docx extract_text.py
或 pdf-extract 抽成文本，再交给本脚本。

默认把文件抄到工程 sources/<id><后缀>；--keep 则只记账、不复制。
退出码 0 登记成功；2 工程不存在 / id 已占用 / 读不进来。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

SOURCE_HEAD = re.compile(r"^\[([A-Za-z0-9_.-]+)\]\s*$")
ID_OK = re.compile(r"^[A-Za-z0-9_.-]+$")


def existing_ids(text: str) -> set[str]:
    return {match.group(1) for match in SOURCE_HEAD.finditer(text)}


def entry_block(source_id: str, title: str, rel_path: str) -> str:
    return (
        f"\n[{source_id}]\n"
        f"title = \"{title}\"\n"
        f"path = \"{rel_path}\"\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登记来源到长文档工程")
    parser.add_argument("--project", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--keep", action="store_true", help="不复制，path 记原路径")
    args = parser.parse_args(argv)
    root = Path(args.project)
    src = Path(args.input)
    source_id = args.id.strip()
    if not ID_OK.match(source_id):
        print("id 只允许字母数字 . _ -", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"工程目录不存在: {root}", file=sys.stderr)
        return 2
    if not src.is_file():
        print(f"找不到文件: {src}", file=sys.stderr)
        return 2
    if src.suffix.lower() in {".docx", ".pdf", ".doc", ".xls", ".xlsx", ".ppt", ".pptx"}:
        print(
            f"{src.suffix} 是二进制：先抽成 md/txt 再登记"
            "（word-docx extract_text.py / pdf-extract / office-document convert_legacy.py）",
            file=sys.stderr,
        )
        return 2
    ledger = root / "sources.toml"
    body = ledger.read_text(encoding="utf-8") if ledger.is_file() else "# 来源账\n"
    if source_id in existing_ids(body):
        print(f"id 已占用: {source_id}", file=sys.stderr)
        return 2
    if args.keep:
        try:
            rel = src.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = str(src)
    else:
        dest_dir = root / "sources"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{source_id}{src.suffix or '.txt'}"
        if dest.exists():
            print(f"目标已存在: {dest}", file=sys.stderr)
            return 2
        shutil.copy2(src, dest)
        rel = dest.relative_to(root).as_posix()
    title = args.title.strip() or src.stem
    if not ledger.is_file():
        ledger.write_text("# 来源账\n", encoding="utf-8")
        body = "# 来源账\n"
    if not body.endswith("\n"):
        body += "\n"
    ledger.write_text(body + entry_block(source_id, title, rel), encoding="utf-8")
    print(f"{source_id}  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
