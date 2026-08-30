"""按 Agent Skills 开放规范（agentskills.io）发现并加载技能。

渐进披露：list_skills 只读 name/description；load_skill 才读正文和 scripts/references/assets。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, skills_dir
from witty_agent.logging import get_logger
from witty_agent.runtime import skill_paths

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_SKILL_EXTRAS = ("scripts", "references", "assets")
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", ".git", ".DS_Store")

logger = get_logger("skills")


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path
    skill_file: Path
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    origin: str = "system"
    network: str = "general"


NETWORK_LABELS = {
    "intranet": "内网",
    "public": "外网",
    "general": "通用",
}


def normalize_network(raw: str) -> str:
    folded = (raw or "").strip().casefold()
    if folded in {"intranet", "private", "offline", "local", "内网"}:
        return "intranet"
    if folded in {"public", "internet", "online", "外网"}:
        return "public"
    return "general"


def network_label(network: str) -> str:
    return NETWORK_LABELS.get(normalize_network(network), NETWORK_LABELS["general"])


@dataclass(frozen=True)
class Skill(SkillMeta):
    body: str = ""
    scripts_dir: Path | None = None
    references_dir: Path | None = None
    assets_dir: Path | None = None


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("SKILL.md 必须以 YAML frontmatter（---）开头")
    raw_yaml, body = match.group(1), match.group(2).strip()
    data: dict[str, object] = {}
    current_map: str | None = None
    for line in raw_yaml.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if current_map and (line.startswith("  ") or line.startswith("\t")):
            nested = line.strip()
            if ":" not in nested:
                continue
            nested_key, nested_val = nested.split(":", 1)
            mapping = data.setdefault(current_map, {})
            if not isinstance(mapping, dict):
                raise ValueError(f"frontmatter 字段 {current_map} 不是映射")
            mapping[nested_key.strip()] = nested_val.strip().strip("\"'")
            continue
        current_map = None
        if ":" not in line:
            raise ValueError(f"无法解析 frontmatter 行: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            current_map = key
            data[key] = {}
            continue
        data[key] = value.strip("\"'")
    return data, body


def _identity(data: dict[str, object]) -> tuple[str, str]:
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name or not description:
        raise ValueError("SKILL.md 缺少 name 或 description")
    if len(name) > 64 or not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"技能名 {name!r} 不合法：仅小写字母/数字/单连字符，最长 64"
        )
    if len(description) > 1024:
        raise ValueError("SKILL.md description 超过 1024 字符")
    return name, description


def _validate_name(name: str, directory: Path) -> None:
    if not name or len(name) > 64 or not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"技能名 {name!r} 不合法：仅小写字母/数字/单连字符，最长 64"
        )
    if name != directory.name:
        raise ValueError(f"技能名 {name!r} 必须与目录名 {directory.name!r} 一致")


def _read_meta(skill_file: Path) -> SkillMeta:
    text = skill_file.read_text(encoding="utf-8")
    data, _body = _parse_frontmatter(text)
    name, description = _identity(data)
    directory = skill_file.parent
    _validate_name(name, directory)
    allowed = str(data.get("allowed-tools") or "").split()
    metadata_raw = data.get("metadata") or {}
    metadata = (
        {str(k): str(v) for k, v in metadata_raw.items()}
        if isinstance(metadata_raw, dict)
        else {}
    )
    compatibility = data.get("compatibility")
    if isinstance(compatibility, str) and len(compatibility) > 500:
        raise ValueError(f"{skill_file} compatibility 超过 500 字符")
    network = normalize_network(
        str(data.get("network") or metadata.get("network") or "general")
    )
    return SkillMeta(
        name=name,
        description=description,
        path=directory,
        skill_file=skill_file,
        license=str(data["license"]) if data.get("license") else None,
        compatibility=str(compatibility) if compatibility else None,
        allowed_tools=tuple(allowed),
        metadata=metadata,
        network=network,
    )


def _iter_skill_files(root: Path) -> list[Path]:
    if root.is_file() and root.name == "SKILL.md":
        return [root]
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        skill_file = child / "SKILL.md" if child.is_dir() else None
        if skill_file and skill_file.is_file():
            found.append(skill_file)
    return found


_SCOPE: ContextVar[tuple[str, str, Path | None] | None] = ContextVar("witty_skill_scope", default=None)


def bind_skill_scope(project_id: str, agent_id: str, root: Path | None) -> object:
    return _SCOPE.set((project_id, agent_id, root))


def reset_skill_scope(token: object) -> None:
    _SCOPE.reset(token)  # type: ignore[arg-type]


def user_skills_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return skills_dir(project_id, agent_id, root=root)


def _scan(root: Path, origin: str) -> list[SkillMeta]:
    found: list[SkillMeta] = []
    if not root.exists() and origin == "system":
        logger.info("技能目录不存在，已跳过 path=%s", root)
        return found
    for skill_file in _iter_skill_files(root):
        try:
            meta = replace(_read_meta(skill_file), origin=origin)
        except (OSError, ValueError) as exc:
            logger.warning("跳过无效技能 file=%s err=%s", skill_file, exc)
            continue
        found.append(meta)
        logger.info("发现技能 name=%s origin=%s path=%s", meta.name, origin, meta.path)
    return found


def list_system_skills() -> list[SkillMeta]:
    rows: list[SkillMeta] = []
    seen: set[str] = set()
    for root in skill_paths():
        for item in _scan(root, "system"):
            if item.name in seen:
                rows = [row for row in rows if row.name != item.name]
            seen.add(item.name)
            rows.append(item)
    from witty_agent.skill_guard import skill_compatible

    return [item for item in rows if skill_compatible(item)]


def list_user_skills(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> list[SkillMeta]:
    directory = user_skills_dir(project_id, agent_id, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    from witty_agent.skill_guard import skill_compatible

    return [item for item in _scan(directory, "user") if skill_compatible(item)]


def _read_skill_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md 必须是 UTF-8 文本") from exc


def _copy_skill_extras(source_dir: Path, dest_dir: Path) -> None:
    for extra in _SKILL_EXTRAS:
        src_extra = source_dir / extra
        if src_extra.is_dir():
            shutil.copytree(
                src_extra,
                dest_dir / extra,
                dirs_exist_ok=True,
                ignore=_COPY_IGNORE,
            )


def install_user_skill(
    source: str | Path | None = None,
    *,
    text: str | None = None,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    root: Path | None = None,
    overwrite: bool = False,
) -> SkillMeta:
    """把本地 SKILL.md 或技能目录装进当前 Agent 的用户技能目录。"""
    dest_root = user_skills_dir(project_id, agent_id, root=root)
    dest_root.mkdir(parents=True, exist_ok=True)
    source_dir: Path | None = None
    skill_text = str(text or "").strip()
    if source:
        src = Path(source).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"找不到技能路径 {src}")
        src = src.resolve()
        if src.is_file():
            if src.name != "SKILL.md":
                raise ValueError("请选择 SKILL.md 或包含 SKILL.md 的目录")
            skill_text = _read_skill_text(src)
            source_dir = src.parent
        elif src.is_dir():
            skill_file = src / "SKILL.md"
            if not skill_file.is_file():
                raise ValueError(f"{src} 里没有 SKILL.md")
            skill_text = _read_skill_text(skill_file)
            source_dir = src
        else:
            raise ValueError(f"{src} 不是 SKILL.md 或目录")
    if not skill_text:
        raise ValueError("需要本地路径 source 或 SKILL.md 正文 text")
    data, _body = _parse_frontmatter(skill_text)
    name, _description = _identity(data)
    dest = dest_root / name
    if dest.exists() and source_dir is not None and dest.resolve() == source_dir.resolve():
        return replace(_read_meta(dest / "SKILL.md"), origin="user")
    if dest.exists() and not overwrite:
        raise FileExistsError(f"用户技能 {name} 已存在")
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=dest_root))
    try:
        (staging / "SKILL.md").write_text(skill_text, encoding="utf-8")
        if source_dir is not None:
            _copy_skill_extras(source_dir, staging)
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    meta = replace(_read_meta(dest / "SKILL.md"), origin="user")
    logger.info("安装用户技能 name=%s dest=%s", meta.name, dest)
    return meta


def uninstall_user_skill(
    name: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    """卸下当前 Agent 用户目录里的技能。系统技能只能关，不能删仓库。"""
    known = {item.name: item for item in list_skills(project_id, agent_id, root=root)}
    skill = known.get(name)
    if skill is None:
        raise KeyError(f"未找到技能 {name}")
    if skill.origin != "user":
        raise ValueError(f"系统技能不可卸载，只能停用: {name}")
    dest = user_skills_dir(project_id, agent_id, root=root) / name
    if not dest.exists():
        dest = skill.path
    if dest.exists():
        shutil.rmtree(dest)
    logger.info("卸载用户技能 name=%s path=%s", name, dest)
    return dest


def list_skill_groups(
    project_id: str | None = None,
    agent_id: str | None = None,
    *,
    root: Path | None = None,
) -> dict[str, object]:
    scope = _SCOPE.get()
    if project_id is None and scope is not None:
        project_id, agent_id, root = scope
    system = list_system_skills()
    user: list[SkillMeta] = []
    directory = ""
    if project_id:
        agent_id = agent_id or DEFAULT_AGENT_ID
        user = list_user_skills(project_id, agent_id, root=root)
        directory = str(user_skills_dir(project_id, agent_id, root=root))
    return {
        "system": system,
        "user": user,
        "user_dir": directory,
    }


def list_skills(
    project_id: str | None = None,
    agent_id: str | None = None,
    *,
    root: Path | None = None,
) -> list[SkillMeta]:
    """执行用：系统技能在前，同名用户技能覆盖。"""
    groups = list_skill_groups(project_id, agent_id, root=root)
    by_name = {item.name: item for item in groups["system"]}  # type: ignore[union-attr]
    for item in groups["user"]:  # type: ignore[union-attr]
        by_name[item.name] = item
    skills = list(by_name.values())
    logger.info("技能元数据加载完成 count=%s", len(skills))
    return skills


_MATCH_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "agent",
        "am",
        "are",
        "as",
        "asks",
        "at",
        "about",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "contain",
        "contained",
        "contains",
        "could",
        "data",
        "did",
        "do",
        "does",
        "file",
        "files",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "hello",
        "her",
        "here",
        "hey",
        "hi",
        "him",
        "his",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "let",
        "may",
        "me",
        "might",
        "more",
        "most",
        "must",
        "my",
        "no",
        "not",
        "now",
        "of",
        "ok",
        "on",
        "or",
        "our",
        "out",
        "over",
        "own",
        "path",
        "please",
        "read",
        "requested",
        "said",
        "say",
        "says",
        "shall",
        "she",
        "should",
        "single",
        "skill",
        "so",
        "some",
        "such",
        "task",
        "tasks",
        "test",
        "than",
        "thanks",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "thus",
        "to",
        "too",
        "up",
        "us",
        "use",
        "user",
        "very",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
        "一份",
        "一下",
        "这个",
        "那个",
        "什么",
        "怎么",
        "需要",
        "可以",
        "进行",
    }
)

_GENERIC = frozenset(
    {
        "briefing",
        "complete",
        "configure",
        "create",
        "draft",
        "format",
        "html",
        "implement",
        "improve",
        "markdown",
        "needed",
        "only",
        "requested",
        "review",
        "revise",
        "single",
        "write",
        "writing",
    }
)


_ZH_FUNCTION = frozenset(
    "的了着过是在有和与及我你您他她它们这那哪什么怎样为吗呢吧啊呀把被给对从向到"
    "就都也很太还只再又或者请帮个下上里中内没不要能会想说让使于并且但而如果因"
    "所以之乎其此该些多少几谁何做当把跟同已经将快先然后各种任何一二三四五六七八九十"
)


# 通用实词：不是虚词（_ZH_FUNCTION 按字拦不住），但日常说话里到处都有，
# 撞进描述只说明用了同一个常用词，不说明用户想干这件事。
# 判据是「这个词单独出现时能不能指认一个技能」——「报告」「纪要」「幻灯片」能，
# 「今天」「处理」「打开」不能。收在这里的每条都是实测出的误命中肇事 token：
# 「今天天气不错」→ daily-diary（今天）、「没问题」→ nl2sql（问题）、
# 「时间不够」→ daily-diary（时间线）、「用户说了什么」→ word-docx（用户）。
# 不收「系统」：link-box 把「上次那个系统」写成了显式触发词，砍掉会伤真阳例。
_ZH_GENERIC = frozenset(
    {
        "今天",
        "今日",
        "昨天",
        "明天",
        "打开",
        "完整",
        "时间",
        "用户",
        "处理",
        "维护",
        "问题",
    }
)


_FILE_NAME = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{2,})\."
    r"(txt|md|py|toml|json|csv|tsv|xlsx|pptx|html|css|js|ts|yml|yaml|pdf)"
    r"(?![A-Za-z0-9_-])",
    re.I,
)
_BARE_NAME = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(README|LICENSE|TODO|CONTRIBUTING|NOTICE|CHANGELOG|AGENTS|"
    r"AUTHORS|CODEOWNERS|COPYING|Makefile|Dockerfile)"
    r"(?![A-Za-z0-9_.-])"
)


def _filename_stems(text: str) -> set[str]:
    """写在提示词里的文件名各段：词干和扩展名都算。

    扩展名也要算：`summary.md` 里的 `md` 会撞上任何描述里的 `AGENTS.md`。
    只在文件名之外也出现时才当意图（「转成 md」算，「写 summary.md」不算）。
    """
    stems: set[str] = set()
    for match in _FILE_NAME.finditer(text or ""):
        stems.add(match.group(1).casefold())
        stems.add(match.group(2).casefold())
    stems.update(match.group(1).casefold() for match in _BARE_NAME.finditer(text or ""))
    return stems


def _strip_filenames(text: str) -> str:
    return _BARE_NAME.sub(" ", _FILE_NAME.sub(" ", text or ""))


def _token_outside_filename(token: str, text: str) -> bool:
    """True if the token also appears outside a name.ext or bare root file."""
    return _whole_in_prompt(token, _strip_filenames(text))


def _all_function_zh(token: str) -> bool:
    """整段都是虚词字的中文碎片（了什么 / 是不是 / 怎么样）。"""
    return bool(token) and all(char in _ZH_FUNCTION for char in token)


def _prompt_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        key = item.casefold()
        if not key or key in _MATCH_STOP or key in seen:
            return
        # 中文按 n-gram 切，句式碎片会撞进任意描述（「了什么」撞「今天做了什么」）。
        # 一个实词都没有的碎片不代表意图，丢掉。
        if _all_function_zh(key):
            return
        seen.add(key)
        tokens.append(item)

    for latin in re.findall(r"[A-Za-z0-9_]{2,}", text or ""):
        add(latin)
    for block in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        if 2 <= len(block) <= 6:
            add(block)
        for size in (4, 3, 2):
            if len(block) < size:
                continue
            for index in range(0, len(block) - size + 1):
                add(block[index : index + size])
    return tokens


def _share_stem(left: str, right: str, *, min_len: int = 5) -> bool:
    count = 0
    for first, second in zip(left, right):
        if first != second:
            break
        count += 1
    return count >= min_len


def _token_hit(token: str, haystack: str) -> bool:
    folded = token.casefold()
    if re.search(r"[\u4e00-\u9fff]", token):
        return folded in haystack
    if re.search(rf"\b{re.escape(folded)}", haystack) is not None:
        return True
    if len(folded) < 5:
        return False
    return any(_share_stem(folded, word) for word in re.findall(r"[a-z0-9_]+", haystack))


def _whole_latin(token: str, haystack: str) -> bool:
    folded = token.casefold()
    if not re.fullmatch(r"[a-z0-9_]{2,4}", folded):
        return False
    return re.search(rf"(?<![a-z0-9_-]){re.escape(folded)}(?![a-z0-9_-])", haystack) is not None


def _whole_in_prompt(word: str, haystack: str) -> bool:
    folded = word.casefold()
    if re.search(r"[\u4e00-\u9fff]", word):
        return folded in haystack
    return re.search(rf"(?<![a-z0-9_-]){re.escape(folded)}(?![a-z0-9_-])", haystack) is not None


def _name_pair_bonus(prompt: str, name: str, tokens: list[str]) -> int:
    """Stopped name heads (agent/data) still count when paired with another part."""
    parts = [part for part in name.split("-") if part]
    if len(parts) < 2:
        return 0
    folded_tokens = [token.casefold() for token in tokens]
    for head in parts:
        if head not in _MATCH_STOP or not _whole_in_prompt(head, prompt):
            continue
        for tail in parts:
            if tail == head:
                continue
            for token in folded_tokens:
                if token == tail or _share_stem(tail, token):
                    return 4
    return 0


def _token_weight(token: str, *, whole_alias: bool = False, declared: bool = False) -> int:
    # 技能自己在 triggers: 里写下的词是整词，不是 n-gram 碎片。「开会」的「会」按虚词字
    # 只算 2 分，永远够不到阈值——作者点名的触发词不该被这条折扣打死。
    if declared:
        return 4
    if re.search(r"[\u4e00-\u9fff]", token):
        # 中文按 n-gram 切，碎片会跨词边界。「报告」是两个实词字，是真意图；
        # 「并写」是虚词粘着实词，撞进任何写结论的描述，只能算弱证据。
        if len(token) >= 4:
            return 4
        if any(char in _ZH_FUNCTION for char in token):
            return 2
        return 4
    if whole_alias:
        return 4
    if len(token) >= 6:
        return 4
    if len(token) >= 4:
        return 2
    return 1


def match_relevant_skills(
    prompt: str,
    skills: list[SkillMeta] | None = None,
    *,
    min_score: int = 4,
    limit: int = 1,
) -> list[SkillMeta]:
    """Pick skills whose name/description overlap the user prompt.

    Catalog listing stays first-layer (name + description). This is the
    harness deciding to load the second layer, same as a `/name` slash.
    """
    catalog = skills if skills is not None else list_skills()
    text = (prompt or "").strip()
    if not text or min_score <= 0 or limit <= 0:
        return []
    tokens = _prompt_tokens(text)
    stems = _filename_stems(text)
    haystack_prompt = text.casefold()
    ranked: list[tuple[int, SkillMeta]] = []
    for item in catalog:
        blob = " ".join(
            part
            for part in (
                item.name,
                item.name.replace("-", " "),
                item.description,
                item.metadata.get("triggers", ""),
            )
            if part
        ).casefold()
        # triggers: 是作者点名的整词表，按空白切开就是原词，不用再猜词边界。
        declared_terms = {
            term.casefold()
            for term in (item.metadata.get("triggers", "") or "").split()
            if term
        }
        score = 0
        name = item.name.casefold()
        if name in haystack_prompt or name.replace("-", " ") in haystack_prompt:
            score += 6
        parts = [part for part in name.split("-") if part]
        # 相位技能（nl2sql-deliver / nl2sql-schema）尾段是通用词。头段缺席时
        # 尾段单独命中只是撞词，不给名字分，否则会抢掉 office-document 这类技能。
        head = parts[0] if parts else ""
        tail_gated = (
            len(parts) >= 2
            and head not in _MATCH_STOP
            and not _whole_in_prompt(head, haystack_prompt)
        )
        for part in parts:
            if part in _MATCH_STOP:
                continue
            if tail_gated and part != head:
                continue
            for token in tokens:
                folded = token.casefold()
                if folded == part:
                    score += 4 if len(part) >= 5 else 2
                elif _share_stem(part, folded):
                    score += 3
        score += _name_pair_bonus(haystack_prompt, name, tokens)
        for token in tokens:
            folded = token.casefold()
            if folded in _GENERIC or folded in _ZH_GENERIC:
                continue
            if folded in stems and not _token_outside_filename(token, text):
                continue
            if not _token_hit(token, blob):
                continue
            score += _token_weight(
                token,
                whole_alias=_whole_latin(token, blob),
                declared=folded in declared_terms,
            )
        if score >= min_score:
            ranked.append((score, item))
    ranked.sort(key=lambda row: (-row[0], row[1].name))
    picked: list[SkillMeta] = []
    seen: set[str] = set()
    for _score, item in ranked:
        if item.name in seen:
            continue
        seen.add(item.name)
        picked.append(item)
        if len(picked) >= limit:
            break
    if picked:
        logger.info(
            "技能匹配 prompt_chars=%s names=%s",
            len(text),
            ",".join(item.name for item in picked),
        )
    return picked


def load_skill(
    name: str,
    project_id: str | None = None,
    agent_id: str | None = None,
    *,
    root: Path | None = None,
) -> Skill:
    """激活技能时用：读 SKILL.md 正文和约定子目录。"""
    metas = {item.name: item for item in list_skills(project_id, agent_id, root=root)}
    meta = metas.get(name)
    if meta is None:
        known = ", ".join(sorted(metas)) or "(空)"
        raise KeyError(f"未找到技能 {name!r}，已有: {known}")
    text = meta.skill_file.read_text(encoding="utf-8")
    _data, body = _parse_frontmatter(text)
    def _optional_dir(dirname: str) -> Path | None:
        candidate = meta.path / dirname
        return candidate if candidate.is_dir() else None

    skill = Skill(
        name=meta.name,
        description=meta.description,
        path=meta.path,
        skill_file=meta.skill_file,
        license=meta.license,
        compatibility=meta.compatibility,
        allowed_tools=meta.allowed_tools,
        metadata=meta.metadata,
        origin=meta.origin,
        network=meta.network,
        body=body,
        scripts_dir=_optional_dir("scripts"),
        references_dir=_optional_dir("references"),
        assets_dir=_optional_dir("assets"),
    )
    logger.info(
        "加载技能正文 name=%s body_chars=%s scripts=%s",
        skill.name,
        len(skill.body),
        bool(skill.scripts_dir),
    )
    return skill
