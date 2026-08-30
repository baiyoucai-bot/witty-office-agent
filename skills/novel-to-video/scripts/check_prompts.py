"""校验即梦视频工程：@引用能不能落到素材、台词塞不塞得进时间轴、时间轴有没有断口、有没有偷偷加字幕。

只读，不改任何文件。这些都是静默失败——即梦不会报错，它会照着糊出来一版。

用沙箱解释器跑：

    <沙箱 Python> check_prompts.py --project <工程目录>
    <沙箱 Python> check_prompts.py --project <工程目录> --frame 001
    <沙箱 Python> check_prompts.py --project <工程目录> --cps 5.0

退出码 0 表示没有 FAIL；1 表示有 FAIL；2 表示工程读不进来。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

LEDGERS = {"人物": "人物.md", "场景": "场景.md", "物品": "物品.md"}
PROJECT_CARD = "项目卡.md"
PROMPT_DIR = "提示词"

CPS = 4.5  # 中文台词每秒字数上限
CPS_WARN = 0.85  # 到上限这个比例就提醒
MAX_CUTS = 3  # 单帧镜头切换上限
GAP_TOLERANCE = 0.05  # 时间轴衔接容差（秒）

BANNED = ["字幕", "标题文字", "文字叠加", "水印", "logo", "LOGO", "花字", "弹幕", "BGM", "背景音乐", "配乐"]
# 长的写在前面：`镜头切至` 不能再被 `切至` 数第二遍
CUT_WORDS = ["镜头切至", "镜头切到", "镜头转向", "画面切换", "切至", "切到", "转场"]
# 【@赵倩台词】这种归属写法里，@ 后面粘着的角色后缀不算素材名的一部分
SPEAKER_SUFFIX = ("台词", "旁白", "自白", "心声", "配音", "声线")

RE_AT = re.compile(r"@([一-鿿A-Za-z0-9_·]+)")
RE_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
RE_SUBHEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
RE_SECTION = re.compile(r"^##\s+(.+?)$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
RE_TIMELINE = re.compile(r"\[\s*(\d+(?:\.\d+)?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*秒?\s*\]")
RE_DIALOGUE = re.compile(r"[“\"]([^”\"]*)[”\"]")
RE_DURATION = re.compile(r"单帧时长:\s*(\d+(?:\.\d+)?)")
RE_FRAME_ROW = re.compile(r"^\|\s*(\d{1,3})\s*\|", re.MULTILINE)
RE_CUT = re.compile("|".join(re.escape(word) for word in CUT_WORDS))


@dataclass
class Report:
    fails: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def at_refs(text: str, assets: dict[str, str] | None = None) -> set[str]:
    """抽 @引用。剥掉 `【@赵倩台词】` 这类归属写法粘在名字后面的后缀。"""
    refs: set[str] = set()
    for raw in RE_AT.findall(text):
        name = raw
        if assets is None or name not in assets:
            for suffix in SPEAKER_SUFFIX:
                if name.endswith(suffix) and len(name) > len(suffix):
                    name = name[: -len(suffix)]
                    break
        refs.add(name)
    return refs


def load_assets(project: Path, report: Report) -> dict[str, str]:
    """素材名 -> 槽位（人物/场景/物品）。一级和二级标题都算素材名。"""
    assets: dict[str, str] = {}
    for kind, filename in LEDGERS.items():
        path = project / filename
        if not path.exists():
            report.warn(f"缺台账 {filename}，@引用无法核对{kind}")
            continue
        body = read(path)
        names = RE_SUBHEADING.findall(body) or RE_HEADING.findall(body)
        for name in names:
            clean = name.replace("台账", "").strip()
            if not clean or clean == kind:
                continue
            if clean in assets and assets[clean] != kind:
                report.fail(f"素材名 `{clean}` 同时出现在 {assets[clean]} 和 {kind} 台账，@引用会指错槽位")
            assets[clean] = kind
    return assets


def frame_duration(project: Path, default: float) -> float:
    match = RE_DURATION.search(read(project / PROJECT_CARD))
    return float(match.group(1)) if match else default


def sections(body: str) -> dict[str, str]:
    return {name.strip(): text for name, text in RE_SECTION.findall(body)}


def check_at_refs(body: str, assets: dict[str, str], report: Report, label: str) -> None:
    refs = at_refs(body, assets)
    if not refs:
        report.warn(f"{label} 一个 @引用都没有；即梦不会自动认名字，人物场景要显式 @ 才吃参考图")
        return
    for ref in sorted(refs):
        if ref not in assets:
            near = [name for name in assets if ref in name or name in ref]
            hint = f"，最接近的是 {near[0]}" if near else ""
            report.fail(f"{label} 引用了 @{ref}，台账里没有这个素材名{hint}")


def check_timeline(body: str, duration: float, report: Report, label: str) -> list[tuple[float, float, str]]:
    prompt = sections(body).get("视频描述词", "")
    if not prompt.strip():
        report.fail(f"{label} 没有视频描述词正文")
        return []

    spans: list[tuple[float, float, str]] = []
    lines = prompt.splitlines()
    for i, line in enumerate(lines):
        match = RE_TIMELINE.search(line)
        if not match:
            continue
        start, end = float(match.group(1)), float(match.group(2))
        # 台词常写在时间轴的下一行，一起算进这一段
        tail = lines[i + 1] if i + 1 < len(lines) else ""
        spans.append((start, end, line + "\n" + tail))

    if not spans:
        report.warn(f"{label} 视频描述词没有 [0-Xs] 时间轴；多动作帧建议按时间轴分段")
        return []

    for start, end, _ in spans:
        if end <= start:
            report.fail(f"{label} 时间轴 [{start}-{end}] 结束不晚于开始")
        if end > duration + GAP_TOLERANCE:
            report.fail(f"{label} 时间轴到 {end}s，超出单帧时长 {duration}s")

    ordered = sorted(spans, key=lambda s: s[0])
    if ordered[0][0] > GAP_TOLERANCE:
        report.warn(f"{label} 时间轴从 {ordered[0][0]}s 起，0 到 {ordered[0][0]}s 没有内容")
    for (a_start, a_end, _), (b_start, _b_end, _) in zip(ordered, ordered[1:]):
        if b_start - a_end > GAP_TOLERANCE:
            report.fail(f"{label} 时间轴 {a_end}s 到 {b_start}s 是空白，模型会自己编这一段")
        elif a_start < b_start < a_end:
            report.warn(f"{label} 时间轴 [{a_start}-{a_end}] 和 [{b_start}-…] 重叠")
    last_end = ordered[-1][1]
    if duration - last_end > GAP_TOLERANCE:
        report.warn(f"{label} 时间轴只写到 {last_end}s，尾巴 {duration - last_end:g}s 没交代")
    return spans


def check_dialogue(spans: list[tuple[float, float, str]], body: str, duration: float, cps: float, report: Report, label: str) -> None:
    if spans:
        for start, end, text in spans:
            budget = (end - start) * cps
            for line in RE_DIALOGUE.findall(text):
                count = len(re.sub(r"\s", "", line))
                if count > budget:
                    report.fail(
                        f"{label} [{start}-{end}s] 台词 {count} 字，这段只装得下 {budget:.0f} 字：{line[:20]}…"
                    )
                elif count > budget * CPS_WARN:
                    report.warn(f"{label} [{start}-{end}s] 台词 {count} 字，接近上限 {budget:.0f} 字")
        return

    total = sum(len(re.sub(r"\s", "", line)) for line in RE_DIALOGUE.findall(sections(body).get("视频描述词", "")))
    if total > duration * cps:
        report.fail(f"{label} 台词共 {total} 字，{duration:g}s 只装得下 {duration * cps:.0f} 字")


def check_banned(body: str, report: Report, label: str) -> None:
    parts = sections(body)
    checked = "\n".join(parts.get(name, "") for name in ("文案", "视频描述词"))
    for word in BANNED:
        for line in checked.splitlines():
            if word not in line:
                continue
            if re.search(rf"(无|不要|没有|禁止|拒绝|不出现|不加|去掉)[^。；\n]{{0,6}}{re.escape(word)}", line):
                continue
            report.fail(f"{label} 出现 `{word}`：{line.strip()[:40]}")
            break


def check_cuts(body: str, report: Report, label: str) -> None:
    prompt = sections(body).get("视频描述词", "")
    cuts = len(RE_CUT.findall(prompt))
    if cuts > MAX_CUTS:
        report.fail(f"{label} 镜头切换 {cuts} 次，单帧最多 {MAX_CUTS} 次")
    elif cuts == MAX_CUTS:
        report.warn(f"{label} 镜头切换已到 {MAX_CUTS} 次上限")


def check_slots(body: str, assets: dict[str, str], report: Report, label: str) -> None:
    cast = sections(body).get("出镜", "")
    if not cast.strip():
        report.warn(f"{label} 没写出镜清单；即梦要按人物/场景/物品挂参考图")
        return
    for ref in at_refs(sections(body).get("视频描述词", ""), assets):
        if ref in assets and ref not in cast:
            report.fail(f"{label} 描述词 @{ref} 没进出镜清单，参考图不会被挂上")


def check_index(project: Path, frames: list[Path], report: Report) -> None:
    board = project / "分镜.md"
    if not board.exists():
        report.warn("缺 分镜.md，帧清单没有真相来源")
        return
    listed = {int(num) for num in RE_FRAME_ROW.findall(read(board))}
    have = {int(match.group(1)) for path in frames if (match := re.search(r"(\d{1,3})", path.stem))}
    for missing in sorted(listed - have):
        report.fail(f"分镜.md 列了帧 {missing:03d}，`{PROMPT_DIR}/` 下没有对应文件")
    for extra in sorted(have - listed):
        report.warn(f"帧 {extra:03d} 有提示词文件，但没进 分镜.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验即梦视频工程的提示词")
    parser.add_argument("--project", required=True, help="工程目录（含 项目卡.md）")
    parser.add_argument("--frame", default=None, help="只查某一帧，例如 001")
    parser.add_argument("--cps", type=float, default=CPS, help=f"台词字/秒上限，默认 {CPS}")
    parser.add_argument("--duration", type=float, default=9, help="项目卡没写时的单帧时长兜底")
    args = parser.parse_args(argv)

    project = Path(args.project).expanduser()
    if not project.is_dir():
        print(f"FAIL 工程目录不存在: {project}", file=sys.stderr)
        return 2

    prompt_dir = project / PROMPT_DIR
    if not prompt_dir.is_dir():
        print(f"FAIL 没有提示词目录: {prompt_dir}", file=sys.stderr)
        return 2

    frames = sorted(prompt_dir.glob("*.md"))
    if args.frame:
        frames = [p for p in frames if args.frame in p.stem]
    if not frames:
        print(f"FAIL 没有可校验的帧文件（--frame {args.frame}）" if args.frame else "FAIL 提示词目录是空的", file=sys.stderr)
        return 2

    report = Report()
    assets = load_assets(project, report)
    duration = frame_duration(project, args.duration)
    report.note(f"素材 {len(assets)} 个，单帧 {duration:g}s，台词上限 {args.cps:g} 字/秒")

    for path in frames:
        body = read(path)
        label = path.stem
        if not body.strip():
            report.fail(f"{label} 是空文件")
            continue
        check_at_refs(body, assets, report, label)
        spans = check_timeline(body, duration, report, label)
        check_dialogue(spans, body, duration, args.cps, report, label)
        check_banned(body, report, label)
        check_cuts(body, report, label)
        check_slots(body, assets, report, label)

    if not args.frame:
        check_index(project, frames, report)

    for msg in report.notes:
        print(f"NOTE {msg}")
    for msg in report.warns:
        print(f"WARN {msg}")
    for msg in report.fails:
        print(f"FAIL {msg}")
    print(f"\n查 {len(frames)} 帧：{len(report.fails)} FAIL，{len(report.warns)} WARN")
    return 1 if report.fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
