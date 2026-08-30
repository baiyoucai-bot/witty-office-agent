"""校验仓库技能目录是否符合规范（skills/README.md 是人读的版本，这里是机器判据）。

    uv run python scripts/check_skills.py
    uv run python scripts/check_skills.py --skills-dir skills --strict

查什么：
- 每个子目录必须有 SKILL.md（加载器会静默跳过没有的目录，CI 里要报出来）；
- frontmatter 过得了加载器（name/description 必填、name 与目录一致、长度帽）；
- network 标签写的是认得的值（general / intranet / public 及别名），写错会被静默归为 general；
- 子目录只允许 scripts/ references/ assets/（其它目录加载器不认，白占体积）；
- 正文里引用的 scripts/xx references/xx 必须真实存在（防死链）。允许跨技能引用
  （「见 nl2sql 技能的 references/x.md」），只要文件在任一技能目录下存在就算数；
  assets/ 前缀不查——正文示例里常用它指用户工程的资源目录；
- allowed-tools 里的名字能对上已注册工具（对不上=收权收了个寂寞）；
- 正文过长提醒拆 references/（渐进披露：SKILL.md 是第二层，不是资料库）。

退出码：0 干净；1 有 FAIL（--strict 时 WARN 也算）；2 跑不起来。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

Finding = tuple[str, str, str]

_KNOWN_NETWORK = {
    "general", "intranet", "public",
    "private", "offline", "local", "内网", "外网", "internet", "online", "通用",
}
_ALLOWED_SUBDIRS = {"scripts", "references", "assets"}
_REF_RE = re.compile(r"(?:scripts|references)/[A-Za-z0-9_.\u4e00-\u9fff/-]+")
_BODY_WARN_CHARS = 12000


def _network_raw(text: str) -> str | None:
    """frontmatter 里 network 的原始写法（含 metadata.network），没写返回 None。"""
    match = re.search(r"^network:\s*(.+)$", text.split("\n---", 1)[0], re.M)
    return match.group(1).strip().strip("\"'") if match else None


def _registered_tool_names() -> set[str] | None:
    """已注册工具名（含内核与业务包）。环境装不全时返回 None = 跳过该项检查。"""
    try:
        from witty_agent.skill_guard import normalize_tool_token
        from witty_agent.tools import list_tools

        names = {normalize_tool_token(spec.name) for spec in list_tools()}
        return names or None
    except Exception:
        return None


def _ref_resolves(ref: str, directory: Path) -> bool:
    """引用在本技能目录、或任一兄弟技能目录下存在就算活链（跨技能引用是合法写法）。"""
    clean = ref.rstrip(".,;:")
    if (directory / clean).exists():
        return True
    skills_root = directory.parent
    return any(
        (sibling / clean).exists()
        for sibling in skills_root.iterdir()
        if sibling.is_dir()
    )


def check_skill_dir(directory: Path, tool_names: set[str] | None) -> list[Finding]:
    from witty_agent.skill_guard import normalize_tool_token
    from witty_agent.skills import _read_meta  # noqa: SLF001 仓库内脚本，允许用加载器私有件

    name = directory.name
    skill_file = directory / "SKILL.md"
    if not skill_file.is_file():
        return [("FAIL", name, "缺 SKILL.md（加载器会静默跳过这个目录）")]
    text = skill_file.read_text(encoding="utf-8")
    findings: list[Finding] = []
    try:
        meta = _read_meta(skill_file)
    except ValueError as exc:
        return [("FAIL", name, f"frontmatter 不合法：{exc}")]

    raw_net = _network_raw(text)
    if raw_net is not None and raw_net.casefold() not in _KNOWN_NETWORK:
        findings.append(("FAIL", name, f"network={raw_net!r} 不认识，会被静默当成 general"))
    if raw_net is None:
        findings.append(("WARN", name, "没写 network 标签（general/intranet/public），默认 general"))

    if not (meta.metadata.get("triggers") or "").strip():
        findings.append(("WARN", name, "metadata.triggers 为空：技能路由只能靠 name/description 撞词"))

    for child in sorted(directory.iterdir()):
        if child.is_dir() and child.name not in _ALLOWED_SUBDIRS and child.name != "__pycache__":
            findings.append(("WARN", name, f"子目录 {child.name}/ 不在规范里（只认 scripts/references/assets）"))

    body = text.split("\n---\n", 2)[-1]
    for ref in sorted(set(_REF_RE.findall(body))):
        if not _ref_resolves(ref, directory):
            findings.append(("WARN", name, f"正文引用 {ref} 在任何技能目录里都不存在"))

    if tool_names:
        for token in meta.allowed_tools:
            normalized = normalize_tool_token(token)
            if normalized not in tool_names:
                findings.append(("FAIL", name, f"allowed-tools 里的 {token!r} 不是已注册工具"))

    if len(body) > _BODY_WARN_CHARS:
        findings.append(
            ("WARN", name, f"正文约 {len(body)} 字符，考虑拆进 references/（SKILL.md 是第二层披露，不是资料库）")
        )
    return findings


def check_all(skills_dir: Path) -> list[Finding]:
    if not skills_dir.is_dir():
        return [("FAIL", "-", f"技能目录不存在: {skills_dir}")]
    tool_names = _registered_tool_names()
    findings: list[Finding] = []
    dirs = [item for item in sorted(skills_dir.iterdir()) if item.is_dir() and item.name != "__pycache__"]
    if not dirs:
        findings.append(("WARN", "-", "技能目录是空的"))
    for directory in dirs:
        findings.extend(check_skill_dir(directory, tool_names))
    return findings


def render(findings: list[Finding], total: int) -> str:
    fails = sum(1 for item in findings if item[0] == "FAIL")
    warns = sum(1 for item in findings if item[0] == "WARN")
    lines = [f"{total} skills  {fails} FAIL  {warns} WARN"]
    for level, where, message in findings:
        lines.append(f"{level}  {where}  {message}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验技能目录规范")
    parser.add_argument("--skills-dir", default=str(REPO / "skills"))
    parser.add_argument("--strict", action="store_true", help="WARN 也算失败")
    args = parser.parse_args(argv)
    skills_dir = Path(args.skills_dir)
    findings = check_all(skills_dir)
    total = sum(1 for item in skills_dir.iterdir() if item.is_dir() and item.name != "__pycache__") if skills_dir.is_dir() else 0
    print(render(findings, total))
    bad = any(item[0] == "FAIL" for item in findings)
    if args.strict:
        bad = bad or any(item[0] == "WARN" for item in findings)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
