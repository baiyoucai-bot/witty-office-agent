"""把散落的旧日记迁进 agent 记忆目录，顺手剔掉测试语料和链接噪声。

旧的 `diary_dir()` 只认进程级环境变量，没设就落 `cwd/.witty/diary`——于是日记跟着**当前
工作目录**跑：仓库根下攒了一份，agent 记忆里另有一份，跑一次测试还会往里灌一批测试夹具。

判据只用客观的，不猜：
  - 正文在 `tests/` 里逐字出现过 → 测试夹具，剔
  - 整条就是一个 URL → 旧 `_worth` 的 http 分支放进来的噪声，新规则已经不收，剔
  - 其余留下，按新格式归到「你说了什么」

剔掉的不删，进 `diary/retired/`，跟记忆的退休层一个意思：可以不再出现在日记里，
但不能凭空消失。

用法：
    uv run python scripts/migrate_diary.py            # 只看会怎么动
    uv run python scripts/migrate_diary.py --apply    # 真的动
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from witty_agent.diary import KIND_CHAT, KIND_WORK, _parse, _write  # noqa: E402
from witty_agent.layout import memory_user_dir  # noqa: E402

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_ONLY = re.compile(r"^https?://\S+$")


def entry_body(line: str) -> str:
    """把 `- 51+08:00 · chat · 正文` 剥成正文。

    旧行的时间戳是 `iso[-8:]` 切出来的「秒+时区」，本来就不是时间，恢复不了，直接丢。
    """
    text = line.lstrip("- ").strip()
    while "·" in text:
        head, _, tail = text.partition("·")
        head = head.strip()
        if re.fullmatch(r"[\d:+.-]{2,12}", head) or head in {"chat", "note", "work"}:
            text = tail.strip()
            continue
        break
    return text.strip()


def test_corpus() -> str:
    blob = []
    for path in (ROOT / "tests").rglob("*.py"):
        blob.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(blob)


def classify(body: str, corpus: str) -> str:
    if not body:
        return "empty"
    if body in corpus:
        return "test-fixture"
    if URL_ONLY.match(body):
        return "link-noise"
    return "keep"


def collect(folder: Path) -> dict[str, list[str]]:
    days: dict[str, list[str]] = {}
    if not folder.is_dir():
        return days
    for path in sorted(folder.glob("*.md")):
        if not DAY_RE.match(path.stem):
            continue
        parsed = _parse(path)
        rows = list(parsed[KIND_WORK]) + list(parsed[KIND_CHAT])
        if rows:
            days.setdefault(path.stem, []).extend(rows)
    return days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写盘，默认只演练")
    ap.add_argument("--project", default="default_project")
    ap.add_argument("--agent", default="default_agent")
    args = ap.parse_args()

    home = memory_user_dir(args.project, args.agent)
    target = home / "diary"
    sources = [ROOT / ".witty" / "diary", target]
    corpus = test_corpus()

    merged: dict[str, list[str]] = {}
    for folder in sources:
        for day, rows in collect(folder).items():
            merged.setdefault(day, []).extend(rows)

    kept: dict[str, list[str]] = {}
    dropped: list[tuple[str, str, str]] = []
    for day in sorted(merged):
        seen: set[str] = set()
        for row in merged[day]:
            body = entry_body(row)
            verdict = classify(body, corpus)
            if verdict != "keep":
                dropped.append((day, verdict, body))
                continue
            if body in seen:
                dropped.append((day, "duplicate", body))
                continue
            seen.add(body)
            kept.setdefault(day, []).append(f"- {body}")

    print(f"日记目标目录：{target}")
    print(f"扫到 {len(merged)} 天，{sum(len(v) for v in merged.values())} 条")
    print(f"保留 {sum(len(v) for v in kept.values())} 条，剔除 {len(dropped)} 条")
    for day, verdict, body in dropped:
        print(f"  剔 [{verdict}] {day} {body[:60]}")
    for day, rows in sorted(kept.items()):
        print(f"  留 {day} {len(rows)} 条")

    if not args.apply:
        print("\n（演练，没有写盘；加 --apply 才动）")
        return 0

    retired = target / "retired"
    retired.mkdir(parents=True, exist_ok=True)
    if dropped:
        lines = [f"- {day} [{verdict}] {body}" for day, verdict, body in dropped]
        (retired / "dropped.md").write_text(
            "# 迁移时剔掉的日记条目\n\n" + "\n".join(lines) + "\n", encoding="utf-8"
        )
    # 仓库和家目录常在不同卷上（这台机器就是），`Path.replace` 会 EXDEV。
    for path in sorted((ROOT / ".witty" / "diary").glob("*.md")):
        shutil.move(str(path), str(retired / f"repo-root-{path.name}"))
    for day in sorted(merged):
        path = target / f"{day}.md"
        rows = kept.get(day) or []
        if not rows:
            if path.is_file():
                shutil.move(str(path), str(retired / f"empty-{path.name}"))
            continue
        _write(path, day, {"summary": "", KIND_WORK: [], KIND_CHAT: rows})
    print(f"\n已写盘。剔掉的存档在 {retired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
