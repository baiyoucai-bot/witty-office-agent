"""novel_kit 命令行。退出码是给 goal 模式的客观 gate 用的。

`GateSpec` 跑的是命令、看的是退出码，所以校验器必须先是 CLI，`@tool` 只能是它的
薄包装。0 = 干净，1 = 有达到阈值的 finding，2 = 用法或 IO 错。

    python -m witty_agent.plugins.novel_kit check --book . --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from witty_agent.logging import get_logger, setup_logging
from witty_agent.plugins.novel_kit.check import (
    Coverage,
    coverage,
    SEVERITY_ORDER,
    Finding,
    Thresholds,
    run_checks,
    save_dismissal,
    worst_severity,
)
from witty_agent.plugins.novel_kit.layout import BookPaths
from witty_agent.plugins.novel_kit.records import (
    RecordError,
    append_records,
    load_records,
    max_chapter,
    truncate_records,
)
from witty_agent.plugins.novel_kit.registry import fold, write_registry
from witty_agent.plugins.novel_kit.retrieve import context_pack
from witty_agent.prompts import get_prompt

logger = get_logger("novel")

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _book(args: argparse.Namespace) -> BookPaths:
    return BookPaths.at(args.book)


def _require_book(book: BookPaths) -> None:
    if not book.is_book():
        raise FileNotFoundError(get_prompt("novel_cli_not_book", path=str(book.root)))


def _thresholds() -> Thresholds:
    from witty_agent.runtime import novel_settings

    return Thresholds.from_settings(novel_settings())


def cmd_init(args: argparse.Namespace) -> int:
    book = _book(args)
    book.ensure()
    if not book.records.exists():
        book.records.touch()
    print(get_prompt("novel_cli_init_ok", path=str(book.root)))
    return EXIT_OK


def cmd_ingest(args: argparse.Namespace) -> int:
    book = _book(args)
    book.ensure()
    source = Path(args.file).expanduser()
    if not source.is_file():
        raise FileNotFoundError(get_prompt("novel_cli_no_file", path=str(source)))
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        body = line.strip()
        if not body or body.startswith("//"):
            continue
        try:
            rows.append(json.loads(body))
        except json.JSONDecodeError as exc:
            raise RecordError(f"{source}:{number} 不是合法 JSON：{exc}") from exc
    added = append_records(book.records, rows)
    _write_registry(book)
    print(get_prompt("novel_cli_ingest_ok", count=added, path=str(book.records)))
    return EXIT_OK


def _write_registry(book: BookPaths, through: int | None = None) -> int:
    records = load_records(book.records)
    ceiling = through if through is not None else max_chapter(records)
    write_registry(book.registry, fold(records, through=ceiling))
    return ceiling


def cmd_rebuild(args: argparse.Namespace) -> int:
    book = _book(args)
    _require_book(book)
    ceiling = _write_registry(book, args.through)
    print(get_prompt("novel_cli_rebuild_ok", ch=ceiling, path=str(book.registry)))
    return EXIT_OK


def cmd_truncate(args: argparse.Namespace) -> int:
    book = _book(args)
    _require_book(book)
    removed = truncate_records(book.records, args.through)
    _write_registry(book, args.through)
    print(get_prompt("novel_cli_truncate_ok", ch=args.through, count=removed))
    return EXIT_OK


def _render(findings: list[Finding], ceiling: int, seen: Coverage | None = None) -> str:
    if not findings:
        # 没查到问题 ≠ 没问题。状态库空着时十条规则全都无事可查，这时说「通过」
        # 是在骗人——尤其 goal 模式下客观门只看退出码，一句误导会让整轮白跑。
        if seen is not None and not seen.evaluated:
            return get_prompt("novel_check_hollow", ch=ceiling)
        return get_prompt("novel_check_clean", ch=ceiling)
    counts: dict[str, int] = {}
    for item in findings:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    tally = "，".join(f"{name} {counts[name]}" for name in sorted(counts, key=lambda key: -SEVERITY_ORDER[key]))
    return get_prompt(
        "novel_check_report",
        ch=ceiling,
        count=len(findings),
        tally=tally,
        rows="\n".join(item.line() for item in findings),
    )


def cmd_check(args: argparse.Namespace) -> int:
    book = _book(args)
    _require_book(book)
    records = load_records(book.records)
    ceiling = args.chapter if args.chapter is not None else max_chapter(records)
    findings = run_checks(book, through=ceiling, limits=_thresholds(), records=records)
    seen = coverage(book, records)
    if args.json:
        print(json.dumps([item.to_json() for item in findings], ensure_ascii=False, indent=2))
    else:
        print(_render(findings, ceiling, seen))
    if args.report:
        book.reports.mkdir(parents=True, exist_ok=True)
        body = _render(findings, ceiling, seen)
        book.report_file(ceiling).write_text(body + "\n", encoding="utf-8")
    floor = "warning" if args.strict else "critical"
    blocking = [item for item in findings if SEVERITY_ORDER[item.severity] >= SEVERITY_ORDER[floor]]
    logger.info(
        "一致性校验 ch=%s findings=%s worst=%s blocking=%s prose=%s indexed=%s",
        ceiling,
        len(findings),
        worst_severity(findings),
        len(blocking),
        seen.prose_chapters,
        seen.indexed_chapters,
    )
    if blocking:
        return EXIT_FINDINGS
    # 有正文却一条记录都没有：规则没得跑，不能给绿灯。--strict 下这本来会被
    # unindexed_chapter 挡住，但非 strict 时它只是 warning，得在这里兜住。
    return EXIT_FINDINGS if seen.prose_chapters and not seen.evaluated else EXIT_OK


def cmd_pack(args: argparse.Namespace) -> int:
    from witty_agent.runtime import novel_settings

    book = _book(args)
    _require_book(book)
    settings = novel_settings()
    records = load_records(book.records)
    print(
        context_pack(
            records,
            chapter=args.chapter,
            query=args.query or "",
            budget=args.budget if args.budget is not None else int(settings["context_budget_chars"]),
            hops=args.hops if args.hops is not None else int(settings["expand_hops"]),
        )
    )
    return EXIT_OK


def cmd_dismiss(args: argparse.Namespace) -> int:
    book = _book(args)
    _require_book(book)
    from witty_agent.time_context import clock_now

    save_dismissal(book.dismissals, args.key, args.reason, at=str(clock_now()["date"]))
    print(get_prompt("novel_cli_dismiss_ok", key=args.key, reason=args.reason))
    return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    book = _book(args)
    _require_book(book)
    records = load_records(book.records)
    reg = fold(records)
    print(
        get_prompt(
            "novel_cli_stats",
            path=str(book.root),
            chapters=len(reg.chapters),
            records=len(records),
            characters=len(reg.characters),
            threads=len(reg.threads),
            open_threads=len(reg.open_threads()),
            objects=len(reg.objects),
        )
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel", description="长篇小说状态库与一致性校验")
    parser.add_argument("--book", default=".", help="书目录，默认当前目录")
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("init", help="建立书目录骨架").set_defaults(func=cmd_init)

    ingest = subs.add_parser("ingest", help="把 jsonl 记录追加进事实源")
    ingest.add_argument("--file", required=True, help="待导入的 jsonl")
    ingest.set_defaults(func=cmd_ingest)

    rebuild = subs.add_parser("rebuild", help="重放事实源，重建 registry.json")
    rebuild.add_argument("--through", type=int, default=None, help="只重放到这一章")
    rebuild.set_defaults(func=cmd_rebuild)

    truncate = subs.add_parser("truncate", help="截断事实源到某章，改稿重写下游用")
    truncate.add_argument("--through", type=int, required=True, help="保留到这一章")
    truncate.set_defaults(func=cmd_truncate)

    check = subs.add_parser("check", help="跑确定性一致性校验")
    check.add_argument("--chapter", type=int, default=None, help="截至第几章，默认全书")
    check.add_argument("--strict", action="store_true", help="warning 也算不通过")
    check.add_argument("--json", action="store_true", help="输出 JSON")
    check.add_argument("--report", action="store_true", help="同时写 reports/ 报告")
    check.set_defaults(func=cmd_check)

    pack = subs.add_parser("pack", help="按查询组装章节安全的上下文包")
    pack.add_argument("--chapter", type=int, required=True, help="要写的章号")
    pack.add_argument("--query", default="", help="本章要写什么")
    pack.add_argument("--budget", type=int, default=None, help="字符预算，默认取 [novel]")
    pack.add_argument("--hops", type=int, default=None, help="关系图扩展跳数，默认取 [novel]")
    pack.set_defaults(func=cmd_pack)

    dismiss = subs.add_parser("dismiss", help="把某条 finding 标为「故意的」")
    dismiss.add_argument("--key", required=True, help="finding 的稳定 key")
    dismiss.add_argument("--reason", required=True, help="为什么这不是错")
    dismiss.set_defaults(func=cmd_dismiss)

    subs.add_parser("stats", help="看状态库规模").set_defaults(func=cmd_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (RecordError, FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
