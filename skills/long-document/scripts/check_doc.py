"""只读校验长文档工程：提纲、缺章、悬空引用、数字账、交接节、术语、待核实、字数预算、图表标签。

    <沙箱 Python> check_doc.py --project <工程目录>
    <沙箱 Python> check_doc.py --project <工程目录> --chapter <slug>

这些错 Word 打得开、对话里也看不出来：提纲有一章、chapters/ 里没有文件；
[cite:foo] 在 sources.toml 里没有；同一术语两种写法；[@fig:x] 引用了不存在的题注；
提纲写了 ~3000字 预算、实际只写了几百字。

`--chapter` 只报指定章的发现，给样章门和逐章循环用：刚写完一章就全工程跑，
结论会被其他未写章的字数 FAIL 淹没，样章过没过根本看不出来。
校验逻辑不变，仍解析全工程（跨章引用、重复标签照查），只是过滤输出。

退出码 0 无 FAIL；1 有 FAIL；2 工程读不进来 / slug 不在提纲里。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

Finding = tuple[str, str, str]

OUTLINE_LINE = re.compile(
    r"^-\s+\[([^\]]+)\]\s+(.+?)(?:\s+~(\d+)\s*字)?(?:\s+\+(\d+)\s*表)?\s*$"
)
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
# 写到预算的这个比例以下判 FAIL，之间判 WARN。
# 原来只在**不足一半**时才 WARN，于是全书系统性地写到 55–87% 就停，一条都不触发，
# 校验还报「0 FAIL」——空心稿子是这么放行的。
BUDGET_FAIL = 0.6
BUDGET_WARN = 0.85
CITE = re.compile(r"\[cite:([A-Za-z0-9_.-]+)\]")
NUM = re.compile(r"\[num:([A-Za-z0-9_.-]+)\]")
SOURCE_HEAD = re.compile(r"^\[([A-Za-z0-9_.-]+)\]\s*$")
TOML_KV = re.compile(r'^([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$')
GLOSS_LINE = re.compile(r"^[-*]\s+([^—\n]+)(?:—|–|-)(.+)$")
PLACEHOLDER = re.compile(r"待核实|TBD|\bTODO\b|占位|xx+", re.I)
MONEY = re.compile(r"(\d+(?:\.\d+)?)\s*(亿|万)?\s*元")
LABEL = re.compile(r"\{#((?:fig|tbl):[\w-]+)\}")
REF = re.compile(r"\[@((?:fig|tbl):[\w-]+)\]")
IMAGE_PATH = re.compile(r"^!\[.*?\]\(([^)]+?)\)", re.M)
CONT_HEAD = re.compile(r"^##\s+(\S+)\s*$")


def parse_outline(text: str) -> list[tuple[str, str, int | None, int]]:
    items: list[tuple[str, str, int | None, int]] = []
    for line in text.splitlines():
        match = OUTLINE_LINE.match(line.strip())
        if match:
            budget = int(match.group(3)) if match.group(3) else None
            tables = int(match.group(4)) if match.group(4) else 0
            items.append((match.group(1).strip(), match.group(2).strip(), budget, tables))
    return items


def count_tables(text: str) -> int:
    """数正文里的 Markdown 表格块。

    连续的 `|...|` 行算一块，至少要有表头 + 分隔行 + 一行数据才算数——只写个表头
    不算一张表。
    """
    total = 0
    run: list[str] = []
    for line in text.splitlines() + [""]:
        if TABLE_ROW.match(line):
            run.append(line.strip())
            continue
        if len(run) >= 3 and set(run[1].replace("|", "").strip()) <= set("-: "):
            total += 1
        run = []
    return total


def parse_sources(text: str) -> set[str]:
    ids = set()
    for line in text.splitlines():
        match = SOURCE_HEAD.match(line.strip())
        if match and match.group(1) != "example":
            ids.add(match.group(1))
    return ids


def parse_ledger(text: str) -> dict[str, str]:
    """[id] + text = \"...\"。example 且 text 为空的占位不算。"""
    tables: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in text.splitlines():
        head = SOURCE_HEAD.match(line.strip())
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


def parse_continuity(text: str) -> set[str]:
    return {match.group(1) for match in CONT_HEAD.finditer(text)}


def parse_glossary(text: str) -> list[str]:
    terms: list[str] = []
    for line in text.splitlines():
        match = GLOSS_LINE.match(line.strip())
        if match:
            terms.append(match.group(1).strip())
    return terms


def heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def check_project(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    outline_path = root / "outline.md"
    if not outline_path.is_file():
        return [("FAIL", "-", "缺 outline.md")]
    chapters_dir = root / "chapters"
    if not chapters_dir.is_dir():
        return [("FAIL", "-", "缺 chapters/")]
    items = parse_outline(outline_path.read_text(encoding="utf-8"))
    if not items:
        findings.append(("FAIL", "outline.md", "提纲里没有 `- [slug] 标题` 行"))
    seen: set[str] = set()
    bodies: dict[str, str] = {}
    for slug, title, budget, want_tables in items:
        if slug in seen:
            findings.append(("FAIL", "outline.md", f"slug 重复: {slug}"))
        seen.add(slug)
        path = chapters_dir / f"{slug}.md"
        if not path.is_file():
            findings.append(("FAIL", slug, f"提纲有「{title}」，chapters/{slug}.md 没有"))
            continue
        body = path.read_text(encoding="utf-8")
        bodies[slug] = body
        actual = heading(body)
        if actual and actual != title:
            findings.append(("FAIL", slug, f"章标题「{actual}」和提纲「{title}」不一致"))
        if PLACEHOLDER.search(body):
            findings.append(("WARN", slug, "还有待核实 / TBD / 占位"))
        if budget:
            count = len(re.sub(r"\s+", "", body))
            if count < budget * BUDGET_FAIL:
                findings.append(
                    ("FAIL", slug, f"实际约 {count} 字，预算 ~{budget}字，不足 {BUDGET_FAIL:.0%}")
                )
            elif count < budget * BUDGET_WARN:
                findings.append(
                    ("WARN", slug, f"实际约 {count} 字，预算 ~{budget}字，欠 {1 - count / budget:.0%}")
                )
            elif count > budget * 2:
                findings.append(("WARN", slug, f"实际约 {count} 字，预算 ~{budget}字，超出一倍"))
        if want_tables:
            # 投资估算、效益测算这类章节，光有叙述不算写完——数字得摆成表才看得出口径。
            got = count_tables(body)
            if got < want_tables:
                findings.append(
                    ("FAIL", slug, f"提纲要求 {want_tables} 张表，正文只有 {got} 张")
                )
    extra = sorted(path.stem for path in chapters_dir.glob("*.md") if path.stem not in seen)
    for slug in extra:
        findings.append(("WARN", slug, "chapters 里有文件，提纲没登记"))

    sources_path = root / "sources.toml"
    source_ids = parse_sources(sources_path.read_text(encoding="utf-8")) if sources_path.is_file() else set()
    cited: set[str] = set()
    for slug, body in bodies.items():
        for cite_id in CITE.findall(body):
            cited.add(cite_id)
            if cite_id not in source_ids:
                findings.append(("FAIL", slug, f"[cite:{cite_id}] 在 sources.toml 里没有"))

    labels: dict[str, str] = {}
    for slug, body in bodies.items():
        for label in LABEL.findall(body):
            if label in labels:
                findings.append(("FAIL", slug, f"题注标签 {{#{label}}} 与 {labels[label]} 重复"))
            else:
                labels[label] = slug
    for slug, body in bodies.items():
        for label in REF.findall(body):
            if label not in labels:
                findings.append(("FAIL", slug, f"[@{label}] 引用悬空：没有对应的题注标签"))
        for rel_path in IMAGE_PATH.findall(body):
            image = Path(rel_path)
            if not image.is_absolute() and not (root / rel_path).is_file():
                findings.append(("WARN", slug, f"图片还不存在: {rel_path}（导出 report.py 会 FAIL）"))

    glossary_path = root / "glossary.md"
    if glossary_path.is_file():
        terms = parse_glossary(glossary_path.read_text(encoding="utf-8"))
        joined = "\n".join(bodies.values())
        for term in terms:
            if term and term not in joined:
                findings.append(("WARN", "glossary.md", f"术语「{term}」登记了，正文一次都没用"))

    ledger_path = root / "ledger.toml"
    ledger = parse_ledger(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {}
    used_nums: set[str] = set()
    for slug, body in bodies.items():
        for num_id in NUM.findall(body):
            used_nums.add(num_id)
            if num_id not in ledger:
                findings.append(("FAIL", slug, f"[num:{num_id}] 在 ledger.toml 里没有"))
            elif not ledger[num_id]:
                findings.append(("FAIL", slug, f"[num:{num_id}] 的 text 是空的"))
    for num_id, shown in ledger.items():
        if shown and num_id not in used_nums:
            findings.append(("WARN", "ledger.toml", f"数字「{num_id}」登记了，正文一次都没用"))

    continuity_path = root / "continuity.md"
    cont_slugs = (
        parse_continuity(continuity_path.read_text(encoding="utf-8"))
        if continuity_path.is_file()
        else set()
    )
    for slug, body in bodies.items():
        if PLACEHOLDER.search(body):
            continue
        if slug not in cont_slugs:
            findings.append(("WARN", slug, "正文已写完，continuity.md 没有对应 ## slug 交接节"))
    for slug in sorted(cont_slugs - seen):
        findings.append(("WARN", "continuity.md", f"## {slug} 提纲里没有这一章"))

    amounts = []
    for slug, body in bodies.items():
        for match in MONEY.finditer(body):
            amounts.append((slug, match.group(0), match.group(1), match.group(2) or ""))
    distinct = {(item[2], item[3]) for item in amounts}
    if len(distinct) > 3:
        findings.append(("WARN", "-", f"金额写法有 {len(distinct)} 种，核对是不是同一笔投资被写成了不同数"))
    return findings


def render(findings: list[Finding]) -> str:
    if not findings:
        return "OK  0 FAIL  0 WARN"
    fails = sum(1 for item in findings if item[0] == "FAIL")
    warns = sum(1 for item in findings if item[0] == "WARN")
    lines = [f"{fails} FAIL  {warns} WARN"]
    for level, where, message in findings:
        lines.append(f"{level}  {where}  {message}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验长文档工程")
    parser.add_argument("--project", required=True)
    parser.add_argument("--chapter", help="只报这一章（slug）的发现，给样章门和逐章循环用")
    args = parser.parse_args(argv)
    root = Path(args.project)
    if not root.is_dir():
        print(f"工程目录不存在: {root}", file=sys.stderr)
        return 2
    findings = check_project(root)
    if args.chapter:
        outline_path = root / "outline.md"
        slugs = (
            {item[0] for item in parse_outline(outline_path.read_text(encoding="utf-8"))}
            if outline_path.is_file()
            else set()
        )
        if args.chapter not in slugs:
            print(f"提纲里没有这一章: {args.chapter}", file=sys.stderr)
            return 2
        findings = [item for item in findings if item[1] == args.chapter]
    print(render(findings))
    return 1 if any(item[0] == "FAIL" for item in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
