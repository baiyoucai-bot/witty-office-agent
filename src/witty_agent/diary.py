"""每日日记：用户自述的行为 + agent 干过的活，按日落盘，跨日翻篇时出小结。

一天一份文件，里面分三节：

    # 2026-08-25
    ## 小结          ← 模型写的，跨日或攒够条目时后台生成
    ## 我做了什么      ← agent 工作日志，每轮末尾本地抽，不花 token
    ## 你说了什么      ← 用户自述的行为，靠线索词收

原来只有一条平铺列表，且只收「用户说的话」里撞到 `_CUES` 的碎片——**agent 干了什么
一个字都不记**，所以「今天做了什么」在日记里永远是空的。工作日志那一节补的就是这个。

路径以 `memory_dir` 为准。此前 `diary_dir()` 只认进程级环境变量 `WITTY_MEMORY_USER`，
没设就落 `cwd/.witty/diary`——于是日记跟着**当前工作目录**跑，多 agent 会串，跑测试还会
往用户真实日记里写。调用方本来就一路把 `memory_dir` 传进来了，只是以前只拿它写时间线。
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

from witty_agent.async_bridge import run_sync

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.time_context import clock_now
from witty_agent.timeline import append_timeline

logger = get_logger("diary")
_SPLIT = re.compile(r"[。！？!?\n；;]+")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATE_NAME = ".summary_state"
KIND_WORK = "work"
KIND_CHAT = "chat"
_CUES = (
    "今天",
    "今日",
    "刚才",
    "上午",
    "下午",
    "晚上",
    "打开",
    "看了",
    "发了",
    "写了",
    "去了",
    "做了",
    "开了",
    "开完",
    "开会",
    "处理",
    "发出",
    "提交",
    "验收",
    "汇报",
    "周报",
)


def diary_dir(memory_dir: Path | None = None) -> Path:
    """日记放哪。传了 `memory_dir` 就以它为准，别再看环境变量。"""
    if memory_dir is not None:
        return Path(memory_dir) / "diary"
    raw = os.environ.get("WITTY_DIARY_DIR")
    if raw:
        return Path(raw).expanduser()
    user = os.environ.get("WITTY_MEMORY_USER")
    if user:
        return Path(user) / "diary"
    return Path.cwd() / ".witty" / "diary"


def diary_path(day: str | None = None, memory_dir: Path | None = None) -> Path:
    stamp = day or today_stamp()
    return diary_dir(memory_dir) / f"{stamp}.md"


def today_stamp() -> str:
    return str(clock_now()["date"])


def clock_hhmmss() -> str:
    """`12:41:55`。

    原来是 `iso[-8:]`，可 ISO 串结尾是 `+08:00` 时区偏移，切出来是「秒+时区」——
    落盘的日记里全是 `55+08:00` 这种不是时间的东西。
    """
    raw = str(clock_now().get("iso") or "")
    match = re.search(r"T(\d{2}:\d{2}:\d{2})", raw)
    return match.group(1) if match else ""


def append_diary(
    text: str,
    *,
    day: str | None = None,
    kind: str = KIND_CHAT,
    memory_dir: Path | None = None,
) -> str:
    line = re.sub(r"\s+", " ", (text or "").strip())
    if len(line) < 4:
        return ""
    stamp = day or today_stamp()
    path = diary_path(stamp, memory_dir)
    section = KIND_WORK if kind == KIND_WORK else KIND_CHAT
    parsed = _parse(path)
    entry = f"- {clock_hhmmss()} · {line[:240]}"
    rows = parsed[section]
    # 只挡**紧挨着的**重复，且比正文不比整行。
    # 比整行：原来带时间戳一起比，同一句隔一秒再来就算新条目，于是反复跑同一件事
    # 会把日记堆满同一句话。比全天：下午又改了一遍同一个文件是真事，不该被上午那条吃掉。
    if not rows or _entry_body(rows[-1]) != line[:240]:
        rows.append(entry)
        _write(path, stamp, parsed)
        logger.info("日记追加 day=%s kind=%s", stamp, section)
    _sync_timeline(stamp, line, memory_dir)
    return str(path)


def note_work(text: str, *, day: str | None = None, memory_dir: Path | None = None) -> str:
    """记一条 agent 工作日志。"""
    return append_diary(text, day=day, kind=KIND_WORK, memory_dir=memory_dir)


def harvest_diary(
    text: str,
    *,
    day: str | None = None,
    memory_dir: Path | None = None,
) -> int:
    """从用户这一轮的话里收自述行为。"""
    added = 0
    for part in _SPLIT.split(text or ""):
        line = re.sub(r"\s+", " ", part).strip()
        if _worth(line):
            append_diary(line, day=day, kind=KIND_CHAT, memory_dir=memory_dir)
            added += 1
    return added


def turn_actions(messages) -> str:
    """这一轮 agent 到底做了什么，纯本地抽，不花 token。

    只认工具调用——那是唯一「确实做过」的证据。助手正文里的计划和承诺不算做过。
    只看最后一条用户消息之后的部分，否则整段历史会被反复记成同一轮的战果。
    """
    from witty_agent.guard import changes_state

    rows = list(messages or ())
    start = 0
    for index, message in enumerate(rows):
        if message.role == "user" and not str(message.source or "").startswith("plugin:"):
            start = index
    read: list[str] = []
    wrote: list[str] = []
    ran = 0
    other: list[str] = []
    for message in rows[start:]:
        for call in message.tool_calls():
            name = str(getattr(call, "name", "") or "")
            if not name:
                continue
            args = dict(getattr(call, "arguments", None) or {})
            target = _target(args)
            if name == "bash":
                ran += 1
            elif not changes_state(name):
                if target:
                    read.append(target)
            elif target:
                wrote.append(target)
            else:
                other.append(name)
    parts: list[str] = []
    if wrote:
        parts.append(f"{get_prompt('diary_action_write')} {_join(wrote)}")
    if read:
        parts.append(f"{get_prompt('diary_action_read')} {_join(read)}")
    if ran:
        parts.append(f"{get_prompt('diary_action_run')} bash×{ran}")
    if other:
        parts.append(f"{get_prompt('diary_action_other')} {_join(other)}")
    return "；".join(parts)


def read_diary(day: str | None = None, memory_dir: Path | None = None) -> str:
    path = diary_path(day, memory_dir)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def day_sections(day: str | None = None, memory_dir: Path | None = None) -> dict[str, object]:
    parsed = _parse(diary_path(day, memory_dir))
    return {
        "summary": parsed["summary"],
        "work": list(parsed[KIND_WORK]),
        "chat": list(parsed[KIND_CHAT]),
    }


def entry_count(day: str | None = None, memory_dir: Path | None = None) -> int:
    parsed = _parse(diary_path(day, memory_dir))
    return len(parsed[KIND_WORK]) + len(parsed[KIND_CHAT])


def write_summary(
    body: str,
    *,
    day: str | None = None,
    memory_dir: Path | None = None,
) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    stamp = day or today_stamp()
    path = diary_path(stamp, memory_dir)
    parsed = _parse(path)
    parsed["summary"] = text
    _write(path, stamp, parsed)
    _mark_summarized(stamp, memory_dir, len(parsed[KIND_WORK]) + len(parsed[KIND_CHAT]))
    logger.info("日记小结 day=%s", stamp)
    return str(path)


def list_diary_days(*, limit: int = 14, memory_dir: Path | None = None) -> list[str]:
    folder = diary_dir(memory_dir)
    if not folder.is_dir():
        return []
    names = sorted(
        (item.stem for item in folder.glob("*.md") if _DAY_RE.match(item.stem)),
        reverse=True,
    )
    return names[:limit]


def days_needing_summary(
    memory_dir: Path | None = None,
    *,
    today: str | None = None,
    min_entries: int | None = None,
    limit: int | None = None,
) -> list[str]:
    """该出小结的日子。

    两种情形：**往日翻篇**（有条目、没小结，且已经不是今天了）——这是最可靠的自动触发点，
    一天只总结一次；以及**今天攒够了**（长会话跑一整天，不该等到明天才有小结），条件是
    自上次小结以来又多了 `min_entries` 条。

    今天这条的阈值别调太小：每满一次就是一次模型调用，调到 3 就等于每三个用工具的轮次
    多喊一次模型。
    """
    from witty_agent.runtime import diary_settings

    config = diary_settings()
    if not config["enabled"]:
        return []
    if min_entries is None:
        min_entries = int(config["summary_min_entries"])
    if limit is None:
        limit = int(config["summary_max_days"])
    stamp = today or today_stamp()
    due: list[str] = []
    for day in list_diary_days(limit=14, memory_dir=memory_dir):
        parsed = _parse(diary_path(day, memory_dir))
        total = len(parsed[KIND_WORK]) + len(parsed[KIND_CHAT])
        if not total:
            continue
        done = _summarized_count(day, memory_dir)
        if day < stamp:
            if not parsed["summary"]:
                due.append(day)
        elif total - done >= min_entries:
            due.append(day)
        if len(due) >= limit:
            break
    return due


async def asummarize_day(
    day: str,
    *,
    memory_dir: Path | None = None,
    write_fn=None,
) -> str:
    """把一天的条目交给模型写成小结。`write_fn` 可注入，测试不必联网。

    注入的 `write_fn` 同步异步都收——测试给的多半是普通 lambda。
    """
    parsed = _parse(diary_path(day, memory_dir))
    work = list(parsed[KIND_WORK])
    chat = list(parsed[KIND_CHAT])
    if not work and not chat:
        return ""
    compose = write_fn or _model_summary
    try:
        body = compose(day, work, chat)
        if inspect.isawaitable(body):
            body = await body
    except Exception as exc:
        # 内网没配 key 时模型这条路本来就走不通。宁可给一句本地统计，也不要今天
        # 又是一片空白——「日记里没记」正是要修的毛病。
        logger.warning("日记小结走本地兜底 day=%s err=%s", day, exc)
        body = get_prompt("diary_summary_local", work=str(len(work)), chat=str(len(chat)))
    text = re.sub(r"\s+\n", "\n", str(body or "")).strip()
    if not text:
        return ""
    write_summary(text, day=day, memory_dir=memory_dir)
    return text


def summarize_day(day: str, *, memory_dir: Path | None = None, write_fn=None) -> str:
    """`asummarize_day` 的同步包装，给脚本和测试用。"""
    return run_sync(
        asummarize_day(day, memory_dir=memory_dir, write_fn=write_fn),
        entry="asummarize_day",
    )


def today_excerpt(*, limit: int = 800, memory_dir: Path | None = None) -> str:
    """给系统提示看的当天摘录：有小结就先给小结。"""
    parsed = _parse(diary_path(None, memory_dir))
    blocks: list[str] = []
    if parsed["summary"]:
        blocks.append(f"{get_prompt('diary_heading_summary')}\n{parsed['summary']}")
    if parsed[KIND_WORK]:
        blocks.append(f"{get_prompt('diary_heading_work')}\n" + "\n".join(parsed[KIND_WORK]))
    if parsed[KIND_CHAT]:
        blocks.append(f"{get_prompt('diary_heading_chat')}\n" + "\n".join(parsed[KIND_CHAT]))
    if not blocks:
        return ""
    return "\n\n".join(blocks)[:limit]


def _target(args: dict) -> str:
    for key in ("path", "file", "target", "slug", "name"):
        raw = str(args.get(key) or "").strip()
        if raw:
            return Path(raw).name if "/" in raw or "\\" in raw else raw
    return ""


def _join(items: list[str], cap: int = 4) -> str:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    head = "、".join(seen[:cap])
    return f"{head} 等 {len(seen)} 项" if len(seen) > cap else head


def _entry_body(entry: str) -> str:
    _, _, tail = str(entry).partition("·")
    return tail.strip() if tail else str(entry).lstrip("- ").strip()


def _parse(path: Path) -> dict:
    out = {"summary": "", KIND_WORK: [], KIND_CHAT: []}
    if not path.is_file():
        return out
    heads = {
        get_prompt("diary_heading_summary").strip(): "summary",
        get_prompt("diary_heading_work").strip(): KIND_WORK,
        get_prompt("diary_heading_chat").strip(): KIND_CHAT,
    }
    current = ""
    loose: list[str] = []
    summary: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped in heads:
            current = heads[stripped]
            continue
        if stripped.startswith("#"):
            current = ""
            continue
        if not stripped:
            continue
        if current == "summary":
            summary.append(stripped)
        elif current in {KIND_WORK, KIND_CHAT}:
            out[current].append(stripped)
        elif stripped.startswith("- "):
            # 旧格式是一条平铺列表，没有小节。读回来当自述行为。
            loose.append(stripped)
    out["summary"] = "\n".join(summary).strip()
    if loose:
        out[KIND_CHAT] = loose + out[KIND_CHAT]
    return out


def _write(path: Path, day: str, parsed: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {day}", ""]
    if parsed["summary"]:
        lines += [get_prompt("diary_heading_summary"), "", parsed["summary"], ""]
    if parsed[KIND_WORK]:
        lines += [get_prompt("diary_heading_work"), "", *parsed[KIND_WORK], ""]
    if parsed[KIND_CHAT]:
        lines += [get_prompt("diary_heading_chat"), "", *parsed[KIND_CHAT], ""]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _state_path(memory_dir: Path | None) -> Path:
    return diary_dir(memory_dir) / STATE_NAME


def _summarized_count(day: str, memory_dir: Path | None) -> int:
    path = _state_path(memory_dir)
    if not path.is_file():
        return 0
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, raw = line.partition("=")
        if key.strip() == day:
            try:
                return max(0, int(raw.strip()))
            except ValueError:
                return 0
    return 0


def _mark_summarized(day: str, memory_dir: Path | None, count: int) -> None:
    path = _state_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.is_file():
        rows = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.partition("=")[0].strip() != day and line.strip()
        ]
    rows.append(f"{day} = {count}")
    path.write_text("\n".join(sorted(rows)[-60:]) + "\n", encoding="utf-8")


def _sync_timeline(day: str, line: str, memory_dir: Path | None) -> None:
    target = memory_dir
    if target is None:
        user = os.environ.get("WITTY_MEMORY_USER")
        if user:
            target = Path(user)
    if target is None:
        return
    append_timeline(Path(target), [(day, line[:200])])


def _worth(line: str) -> bool:
    if len(line) < 8:
        return False
    return any(item in line for item in _CUES)


async def _model_summary(day: str, work: list[str], chat: list[str]) -> str:
    system = get_prompt("diary_summary_system")
    user = get_prompt(
        "diary_summary_user",
        day=day,
        work="\n".join(work[:60]) or "（无）",
        chat="\n".join(chat[:60]) or "（无）",
    )
    return await _ask(system, user)


async def _ask(system: str, user: str) -> str:
    from witty_agent.llm import OpenAICompatLLM
    from witty_agent.types import AgentContext, AgentMessage, ModelRef

    llm = OpenAICompatLLM(stream=False, timeout=40, max_tokens=600, retry_attempts=1)
    llm.think_level = "off"
    context = AgentContext(
        system_prompt=system,
        messages=[AgentMessage(role="user", content=user)],
        tools=[],
        workspace_dir="",
        model=ModelRef(provider="openai", model_id=llm.model_id),
        project_id="",
        agent_id="diary-summary",
        session_id="diary-summary",
    )
    message = await llm(context)
    if message.stop_reason == "error":
        raise RuntimeError(message.text() or "diary summary error")
    return message.text()
