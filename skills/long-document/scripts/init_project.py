"""幂等建长文档工程：提纲、分章、来源账、术语表。已有文件不覆盖。

    <沙箱 Python> init_project.py --name 示范工程 --root <目录> --profile feasibility
    <沙箱 Python> init_project.py --name 示范工程 --root <目录> --profile generic

profile 只是必含章清单，不是业务模板。可研 / 概设 / 详设 都是同一套目录。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROFILES = {
    "generic": "generic.json",
    "feasibility": "feasibility.json",
    "outline-design": "outline-design.json",
    "detailed-design": "detailed-design.json",
}

ALIASES = {
    "可研": "feasibility",
    "可行性研究": "feasibility",
    "概设": "outline-design",
    "初步设计": "outline-design",
    "概要设计": "outline-design",
    "详设": "detailed-design",
    "详细设计": "detailed-design",
    "通用": "generic",
}

def profile_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "references" / "profiles"


def load_profile(name: str) -> dict:
    key = ALIASES.get(name.strip(), name.strip())
    filename = PROFILES.get(key)
    if not filename:
        raise ValueError(f"未知 profile: {name}（可选 {', '.join(PROFILES)} 或 可研/概设/详设）")
    path = profile_dir() / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    data["id"] = key
    return data


def write_if_absent(path: Path, body: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return True


def chapter_entries(profile: dict) -> list[tuple[str, str, int | None, int]]:
    """章条目：[slug, title] / [slug, title, 字数预算] / [slug, title, 预算, 最少表格数]。"""
    entries = []
    for item in profile["required_chapters"]:
        slug, title = item[0], item[1]
        budget = int(item[2]) if len(item) > 2 and item[2] else None
        tables = int(item[3]) if len(item) > 3 and item[3] else 0
        entries.append((slug, title, budget, tables))
    return entries


def outline_body(profile: dict) -> str:
    lines = ["# 提纲", "", f"文种: {profile['title']}", ""]
    for slug, title, budget, tables in chapter_entries(profile):
        suffix = f" ~{budget}字" if budget else ""
        if tables:
            suffix += f" +{tables}表"
        lines.append(f"- [{slug}] {title}{suffix}")
    lines.append("")
    return "\n".join(lines)


def chapter_stub(title: str) -> str:
    return (
        f"# {title}\n\n"
        "本节要点：\n\n"
        "- \n\n"
        "待核实：\n\n"
        "- \n"
    )


def card_body(name: str, profile: dict) -> str:
    return (
        f"# {name}\n\n"
        f"- 文种: {profile['title']}\n"
        f"- profile: {profile['id']}\n"
        "- 规则: 一轮只写一章；数字带来源；缺料标「待核实」并定向回补；出处 [cite:id]，关键数字 [num:id]\n"
        "- 样章: 锁完提纲先写证据最密的一章，check_doc.py --chapter 过了、口径定了才写其余章\n"
        "- 制图: 正文过整合审计前只写题注和 [@fig:] 占位，不生成图片；表格照常随章写\n"
        "- 字数: 提纲里 ~N字 是本章预算，低于 60% 判 FAIL；~N字 后的 +N表 是最少表格数，同样判 FAIL\n"
        "- 数字: 可研阶段拿不到正式报价是常态，给测算值并注明「以询价招标批复为准」+ 写清测算依据；"
        "只有批复文号、牵头部门、正式清单这类查得到却没查的才写「待核实」\n"
        "- 跨章: 写完一章先改 continuity.md，下一章只打开本节交接 + 提纲 + ledger.toml，不要把全书塞进上下文\n"
        "- 图表: 图片写 ![题注](assets/图.png){#fig:标签}；表格前一行写「表: 题注 {#tbl:标签}」；正文引用 [@fig:标签]\n"
        "- 导出: 走 word-docx 的 report.py --toc，不要用 pandoc 过一遍当定稿\n"
    )


def sources_stub() -> str:
    return (
        "# 来源账\n\n"
        "# id 必须能被章节里的 [cite:id] 对上。\n\n"
        "[example]\n"
        "title = \"示例来源（请改或删）\"\n"
        "path = \"\"\n"
    )


def glossary_stub() -> str:
    return "# 术语表\n\n# 一行一个：术语 — 本章里必须用这个写法，不要换简称。\n"


def ledger_stub() -> str:
    return (
        "# 关键数字账。正文写 [num:id]，导出时替换成 text。\n"
        "# 同一笔投资只在这里写一次，不要在各章手敲不同写法。\n\n"
        "[example]\n"
        "label = \"示例数字（请改或删）\"\n"
        "text = \"\"\n"
    )


def continuity_stub() -> str:
    return (
        "# 跨章交接\n\n"
        "写完一章就在这里追加 `## slug` 一节（slug 必须和提纲一致）。\n"
        "下一章只打开本节、提纲、ledger.toml、sources.toml，不要把全书塞进上下文。\n\n"
        "每节四行：\n"
        "- 主张：本章钉死的结论\n"
        "- 数字：用到的 [num:id]\n"
        "- 待核实：留给后面的缺口\n"
        "- 接下章：下一章必须接着的一句\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建长文档工程")
    parser.add_argument("--name", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--profile", default="generic")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"根目录不存在: {root}", file=sys.stderr)
        return 2
    try:
        profile = load_profile(args.profile)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    project = root / args.name
    project.mkdir(parents=True, exist_ok=True)
    created = []
    mapping = [
        (project / "项目卡.md", card_body(args.name, profile)),
        (project / "outline.md", outline_body(profile)),
        (project / "sources.toml", sources_stub()),
        (project / "glossary.md", glossary_stub()),
        (project / "ledger.toml", ledger_stub()),
        (project / "continuity.md", continuity_stub()),
    ]
    for path, body in mapping:
        if write_if_absent(path, body):
            created.append(path.name)
    chapters = project / "chapters"
    chapters.mkdir(exist_ok=True)
    (project / "assets").mkdir(exist_ok=True)
    for slug, title, _budget, _tables in chapter_entries(profile):
        path = chapters / f"{slug}.md"
        if write_if_absent(path, chapter_stub(title)):
            created.append(f"chapters/{path.name}")
    print(project)
    if created:
        print("created: " + ", ".join(created))
    else:
        print("idempotent: nothing overwritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
