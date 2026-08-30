"""执行沙箱：隔离工作目录 + 自带常用包的 Python。不是 Landlock 式进程监禁。

生成的可运行代码写进沙箱工作区，用沙箱解释器跑，不污染用户本机 site-packages。
工作区按会话 cwd 分目录；venv 共用，避免每个项目重装一遍常用包。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from witty_agent.layout import data_root
from witty_agent.logging import get_logger
from witty_agent.runtime import sandbox_settings

logger = get_logger("sandbox")

_READY: Path | None = None
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Sandbox:
    root: Path
    work: Path
    tmp: Path
    venv: Path
    python: Path
    packages: tuple[str, ...]


def sandbox_root(*, root: Path | None = None) -> Path:
    return (root or data_root()) / "sandbox"


def _workspace_hint(workspace: str | None = None) -> str:
    if workspace:
        return str(Path(workspace).expanduser().resolve())
    env = os.environ.get("WITTY_WORKSPACE")
    if env:
        return str(Path(env).expanduser().resolve())
    return str(Path.cwd().resolve())


def space_key(workspace: str | None = None) -> str:
    return hashlib.sha256(_workspace_hint(workspace).encode("utf-8")).hexdigest()[:16]


def sandbox_space(*, workspace: str | None = None, root: Path | None = None) -> Path:
    return sandbox_root(root=root) / "spaces" / space_key(workspace)


def sandbox_work(*, workspace: str | None = None, root: Path | None = None) -> Path:
    return sandbox_space(workspace=workspace, root=root) / "work"


def sandbox_tmp(*, workspace: str | None = None, root: Path | None = None) -> Path:
    return sandbox_space(workspace=workspace, root=root) / "tmp"


def sandbox_venv(*, root: Path | None = None) -> Path:
    return sandbox_root(root=root) / "venv"


def sandbox_python(*, root: Path | None = None) -> Path:
    venv = sandbox_venv(root=root)
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def workspace_owns_sandbox_name(workspace: str) -> bool:
    """项目里已经有名为 sandbox 的目录时，不抢这个名字。"""
    return (Path(workspace) / "sandbox").exists()


def _marker(venv: Path) -> Path:
    return venv / ".witty-packages"


def _spec_hash(packages: list[str], index_url: str) -> str:
    blob = index_url + "\n" + "\n".join(sorted(packages))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _under(path: Path, root: Path, *, follow: bool = True) -> bool:
    try:
        if follow:
            path.resolve().relative_to(root.resolve())
        else:
            (path.parent.resolve() / path.name).relative_to(root.resolve())
        return True
    except ValueError:
        return False


def sandbox_ready(*, root: Path | None = None) -> bool:
    settings = sandbox_settings()
    python = sandbox_python(root=root)
    ready = _marker(sandbox_venv(root=root))
    if not python.is_file() or not ready.is_file():
        return False
    wanted = _spec_hash(list(settings["packages"]), str(settings["index_url"]))
    return ready.read_text(encoding="utf-8").strip() == wanted


def ensure_sandbox(*, workspace: str | None = None, root: Path | None = None) -> Sandbox:
    """没有就建工作目录和 venv，包集合变了再装。"""
    global _READY
    settings = sandbox_settings()
    base = sandbox_root(root=root)
    work = sandbox_work(workspace=workspace, root=root)
    tmp = sandbox_tmp(workspace=workspace, root=root)
    venv = sandbox_venv(root=root)
    python = sandbox_python(root=root)
    work.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    packages = list(settings["packages"])
    index_url = str(settings["index_url"])
    wanted = _spec_hash(packages, index_url)
    ready = _marker(venv)
    with _LOCK:
        if python.is_file() and ready.is_file() and ready.read_text(encoding="utf-8").strip() == wanted:
            snap = Sandbox(
                root=base,
                work=work,
                tmp=tmp,
                venv=venv,
                python=python,
                packages=tuple(packages),
            )
            _READY = snap.work
            return snap
        logger.info("准备沙箱 venv path=%s packages=%s", venv, len(packages))
        if venv.exists() and not python.is_file():
            shutil.rmtree(venv, ignore_errors=True)
        if not python.is_file():
            _run_uv(["uv", "venv", str(venv), f"--python={sys.executable}"])
        if packages:
            _run_uv(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "-i",
                    index_url,
                    *packages,
                ]
            )
        ready.write_text(wanted + "\n", encoding="utf-8")
        logger.info("沙箱就绪 python=%s work=%s", python, work)
        snap = Sandbox(
            root=base,
            work=work,
            tmp=tmp,
            venv=venv,
            python=python,
            packages=tuple(packages),
        )
        _READY = snap.work
        return snap


def _run_uv(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().replace("\n", " ")
        logger.error("沙箱命令失败 cmd=%s err=%s", cmd[0:4], detail[:400])
        raise


def warm_sandbox(*, workspace: str | None = None, root: Path | None = None) -> None:
    settings = sandbox_settings()
    if not settings["enabled"]:
        return
    try:
        ensure_sandbox(workspace=workspace, root=root)
    except Exception:
        logger.exception("沙箱准备失败，bash 首次执行时会再试")


def _rewrite_prefix(command: str, prefix: str, target: Path) -> str:
    posix = str(target.resolve()).replace("\\", "/").rstrip("/") + "/"
    updated = re.sub(rf"(?<![\w./]){re.escape(prefix)}/", posix, command)
    if os.name == "nt":
        win = str(target.resolve()).replace("/", "\\").rstrip("\\") + "\\"
        updated = re.sub(rf"(?<![\w.\\]){re.escape(prefix)}\\", lambda _match: win, updated)
    return updated


def _rewrite_env_var(command: str, name: str, target: Path) -> str:
    posix = str(target.resolve()).replace("\\", "/")
    updated = command.replace("${" + name + "}", posix)
    return re.sub(rf"\${name}(?![A-Za-z0-9_])", posix, updated)


def rewrite_sandbox_tokens(command: str, *, workspace: str | None = None, root: Path | None = None) -> str:
    """把命令里的 sandbox/、sandbox-tmp/ 和执行期环境变量改成对应沙箱目录。"""
    if not sandbox_settings()["enabled"]:
        return command
    hint = _workspace_hint(workspace)
    rewritten = command
    if not workspace_owns_sandbox_name(hint):
        rewritten = _rewrite_prefix(rewritten, "sandbox-tmp", sandbox_tmp(workspace=hint, root=root))
        rewritten = _rewrite_prefix(rewritten, "sandbox", sandbox_work(workspace=hint, root=root))
    tmp = sandbox_tmp(workspace=hint, root=root)
    work = sandbox_work(workspace=hint, root=root)
    for name in ("TMPDIR", "TEMP", "TMP"):
        rewritten = _rewrite_env_var(rewritten, name, tmp)
    return _rewrite_env_var(rewritten, "WITTY_SANDBOX", work)


_EXEC_DROP = frozenset({"BASH_ENV", "ENV"})


def _without_shell_rc(env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if key not in _EXEC_DROP}


def _windows_bash() -> str:
    """Windows 上 bash 不是系统件：先认 WITTY_BASH（桌面安装版指到自带 busybox），再扫 PATH。"""
    override = os.environ.get("WITTY_BASH", "").strip()
    if override and Path(override).is_file():
        return override
    found = shutil.which("bash")
    if found:
        return found
    from witty_agent.prompts import get_prompt

    raise FileNotFoundError(get_prompt("bash_missing_windows"))


def bash_argv(command: str) -> list[str]:
    """非 login、不读 rc。避免 profile 卡住或逃出路径 jail。"""
    if os.name == "nt":
        shell = _windows_bash()
        if "busybox" in Path(shell).name.lower():
            # busybox 按 argv 选 applet；它的 ash 非交互本来就不读 rc，不认 --noprofile/--norc
            return [shell, "sh", "-c", command]
        return [shell, "--noprofile", "--norc", "-c", command]
    return ["bash", "--noprofile", "--norc", "-c", command]


def apply_exec_env(
    env: dict[str, str],
    *,
    workspace: str | None = None,
    root: Path | None = None,
) -> dict[str, str]:
    cleaned = _without_shell_rc(env)
    settings = sandbox_settings()
    if not settings["enabled"]:
        return cleaned
    hint = workspace or cleaned.get("WITTY_WORKSPACE")
    snap = ensure_sandbox(workspace=hint, root=root)
    merged = dict(cleaned)
    bin_dir = snap.python.parent
    merged["PATH"] = str(bin_dir) + os.pathsep + merged.get("PATH", "")
    merged["VIRTUAL_ENV"] = str(snap.venv)
    merged["PYTHONNOUSERSITE"] = "1"
    merged["PIP_USER"] = "0"
    merged["PIP_INDEX_URL"] = str(settings["index_url"])
    merged["UV_PROJECT_ENVIRONMENT"] = str(snap.venv)
    merged["UV_NO_PROJECT"] = "1"
    merged["UV_PYTHON"] = str(snap.python)
    merged["UV_DEFAULT_INDEX"] = str(settings["index_url"])
    merged["WITTY_SANDBOX"] = str(snap.work)
    merged["TMPDIR"] = str(snap.tmp)
    merged["TEMP"] = str(snap.tmp)
    merged["TMP"] = str(snap.tmp)
    return merged


def allowed_write_roots(workspace: str, *, root: Path | None = None) -> list[Path]:
    roots = [Path(workspace).resolve()]
    settings = sandbox_settings()
    if settings["enabled"] and not workspace_owns_sandbox_name(workspace):
        roots.append(sandbox_work(workspace=workspace, root=root).resolve())
        roots.append(sandbox_tmp(workspace=workspace, root=root).resolve())
    return roots


_SANDBOX_ENV_RE = re.compile(
    r"^\$(\{)?(?P<name>TMPDIR|TEMP|TMP|WITTY_SANDBOX|VIRTUAL_ENV)(?(1)\})(?:/(?P<rest>.*))?$",
    re.IGNORECASE,
)


def parse_sandbox_env_path(raw: str) -> tuple[str, str] | None:
    """认出 bash 执行期会展开的沙箱环境变量。不认系统 os.environ。"""
    match = _SANDBOX_ENV_RE.match(str(raw).replace("\\", "/"))
    if match is None:
        return None
    return match.group("name").upper(), match.group("rest") or ""


_PWD_ENV_RE = re.compile(
    r"^\$(\{)?(?P<name>PWD|OLDPWD)(?(1)\})(?:/(?P<rest>.*))?$",
    re.IGNORECASE,
)
_CD_BUILTINS = frozenset({"cd", "chdir", "pushd"})


def parse_pwd_env_path(raw: str) -> tuple[str, str] | None:
    """认出 $PWD / $OLDPWD。不读进程环境（父进程 PWD 可能在工作区外）。"""
    match = _PWD_ENV_RE.match(str(raw).replace("\\", "/"))
    if match is None:
        return None
    return match.group("name").upper(), match.group("rest") or ""


def sandbox_env_target(
    raw: str,
    *,
    workspace: str,
    root: Path | None = None,
) -> Path | None:
    parsed = parse_sandbox_env_path(raw)
    if parsed is None or not sandbox_settings()["enabled"]:
        return None
    name, rest = parsed
    if name in {"TMPDIR", "TEMP", "TMP"}:
        return sandbox_tmp(workspace=workspace, root=root) / rest
    if name == "WITTY_SANDBOX":
        return sandbox_work(workspace=workspace, root=root) / rest
    return sandbox_venv(root=root) / rest


def fingerprint_target(workspace: str, raw: str, *, root: Path | None = None) -> Path:
    """算指纹/记证据用的规范路径：不抛的 `resolve_allowed`。

    **不代表允许访问**——越界、venv 这类拒绝在这里退回朴素拼接，只为让「同一个文件的两种
    写法算同一笔」这件事有定义。要判能不能访问，仍旧调 `resolve_allowed` 并接住它的异常。

    两个调用方都需要它：证伪账本要盯工具真去看的那个文件，转圈检测要认出
    `a.py` / `./a.py` / 绝对写法是同一次调用。所以放在这里，不各写一份。
    """
    text = str(raw).strip() or "."
    try:
        return resolve_allowed(workspace, text, root=root)
    except (ValueError, OSError):
        target = Path(text)
        if not target.is_absolute():
            target = Path(workspace) / text
        return target


def resolve_allowed(
    workspace: str,
    raw: str,
    *,
    root: Path | None = None,
    follow: bool = True,
) -> Path:
    text = str(raw).replace("\\", "/")
    reserved = sandbox_settings()["enabled"] and not workspace_owns_sandbox_name(workspace)
    mapped = sandbox_env_target(str(raw), workspace=workspace, root=root)
    pwd = parse_pwd_env_path(str(raw))
    if mapped is not None:
        target = mapped
    elif pwd is not None:
        name, rest = pwd
        if name == "OLDPWD":
            from witty_agent.prompts import get_prompt

            raise ValueError(get_prompt("sandbox_denied_outside", path=str(raw)))
        target = Path(workspace) / rest
    elif reserved and (text == "sandbox-tmp" or text.startswith("sandbox-tmp/")):
        rest = "" if text == "sandbox-tmp" else text[len("sandbox-tmp/") :]
        target = sandbox_tmp(workspace=workspace, root=root) / rest
    elif reserved and (text == "sandbox" or text.startswith("sandbox/")):
        rest = "" if text == "sandbox" else text[len("sandbox/") :]
        target = sandbox_work(workspace=workspace, root=root) / rest
    else:
        expanded = expand_jail_path(str(raw))
        target = Path(expanded)
        if not target.is_absolute():
            target = Path(workspace) / expanded
    if follow:
        resolved = target.resolve()
    else:
        parent = target.parent if target.parent.parts else Path(workspace)
        resolved = parent.resolve() / target.name
    venv = sandbox_venv(root=root).resolve()
    from witty_agent.prompts import get_prompt

    if _under(resolved, venv, follow=follow):
        raise ValueError(get_prompt("sandbox_denied_venv", path=str(raw)))
    for allowed in allowed_write_roots(workspace, root=root):
        if _under(resolved, allowed, follow=follow):
            return resolved
    raise ValueError(get_prompt("sandbox_denied_outside", path=str(raw)))


_HOME_VARS = frozenset({"HOME", "USERPROFILE", "HOMEPATH"})
_ENV_PATH = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)$")


def expand_jail_path(raw: str) -> str:
    """展开 ~ 和 $HOME / $USERPROFILE，再交给路径 jail。

    TMPDIR / WITTY_SANDBOX / VIRTUAL_ENV 不走 os.environ（会漏到系统 /tmp），
    由 resolve_allowed 映射到本会话沙箱根。
    """
    text = os.path.expanduser(str(raw))
    match = _ENV_PATH.match(text.replace("\\", "/"))
    if match is None:
        return text
    name, rest = match.group(1), match.group(2)
    if name.upper() not in _HOME_VARS:
        return text
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if name.upper() == "HOMEPATH":
        home = os.environ.get("HOMEPATH") or home
    if not home:
        return text
    return home + rest.replace("/", os.sep)


def _looks_like_path(token: str) -> bool:
    text = token.strip()
    if not text or text == "-":
        return False
    if text.startswith("-") and not text.startswith(("-/", "-\\")):
        return False
    if "://" in text:
        return False
    if text.startswith("~") or text.startswith("$HOME") or text.startswith("${HOME}"):
        return True
    if text.startswith("$USERPROFILE") or text.startswith("${USERPROFILE}"):
        return True
    if text.startswith("$HOMEPATH") or text.startswith("${HOMEPATH}"):
        return True
    if parse_sandbox_env_path(text) is not None:
        return True
    if parse_pwd_env_path(text) is not None:
        return True
    if text in {".", ".."} or text.startswith(("./", "../", ".\\", "..\\")):
        return True
    if text.startswith("/") or (len(text) >= 3 and text[1] == ":" and text[2] in "\\/"):
        return True
    if text == "sandbox" or text.startswith("sandbox/") or text.startswith("sandbox\\"):
        return True
    if text == "sandbox-tmp" or text.startswith("sandbox-tmp/") or text.startswith("sandbox-tmp\\"):
        return True
    return "/" in text or "\\" in text


_REDIR = re.compile(r"(?:\&)?(?:\d)?(?:>>|>|<)")


def _split_redirects(part: str) -> list[str]:
    """`echo x>/tmp/out` 拆出 /tmp/out。仍是路径 jail。"""
    if not any(mark in part for mark in "<>"):
        return [part]
    chunks = [item for item in _REDIR.split(part) if item]
    return chunks or [part]


def command_path_tokens(command: str) -> list[str]:
    import shlex

    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        parts = command.split()
    found: list[str] = []
    for part in parts:
        if part.startswith("-") and "=" in part[1:]:
            part = part.split("=", 1)[1]
        for token in _split_redirects(part):
            if _looks_like_path(token):
                found.append(token)
    return found


def _command_path_raw(
    token: str,
    *,
    cwd: str | None,
    workspace: str | None = None,
    oldpwd: str | None = None,
) -> str:
    pwd = parse_pwd_env_path(token)
    if pwd is not None:
        name, rest = pwd
        if name == "PWD":
            base = cwd or workspace
            if not base:
                return token
            return str(Path(base) / rest) if rest else str(Path(base))
        if not oldpwd:
            from witty_agent.prompts import get_prompt

            raise ValueError(get_prompt("sandbox_denied_outside", path=token))
        return str(Path(oldpwd) / rest) if rest else oldpwd
    text = token.replace("\\", "/")
    if text == "sandbox" or text.startswith("sandbox/"):
        return token
    if text == "sandbox-tmp" or text.startswith("sandbox-tmp/"):
        return token
    if parse_sandbox_env_path(token) is not None:
        return token
    expanded = expand_jail_path(token)
    if expanded != token or Path(expanded).is_absolute():
        return expanded
    if cwd:
        return str(Path(cwd) / token)
    return token


def _cd_dest_index(parts: list[str], index: int) -> int | None:
    token = parts[index]
    nxt = index + 1
    if token in {"builtin", "command"}:
        if nxt >= len(parts) or parts[nxt] not in _CD_BUILTINS:
            return None
        nxt += 1
    elif token not in _CD_BUILTINS:
        return None
    while nxt < len(parts) and parts[nxt].startswith("-") and parts[nxt] != "-":
        if parts[nxt] == "--":
            nxt += 1
            break
        nxt += 1
    return nxt


def _token_stays_in_project(token: str) -> bool:
    """相对工作区的路径才走项目 jail。本机绝对路径、~、$HOME 是操作电脑。"""
    text = token.strip().replace("\\", "/")
    if not text or text == "-":
        return False
    if text.startswith("~"):
        return False
    if text.startswith("$HOME") or text.startswith("${HOME}"):
        return False
    if text.startswith("$USERPROFILE") or text.startswith("${USERPROFILE}"):
        return False
    if text.startswith("$HOMEPATH") or text.startswith("${HOMEPATH}"):
        return False
    if text.startswith("/") or (len(text) >= 3 and text[1] == ":" and text[2] in "\\/"):
        return False
    return True


def _cwd_outside_workspace(cwd: str | None, workspace: str) -> bool:
    if not cwd:
        return False
    try:
        return not _under(Path(cwd).resolve(), Path(workspace).resolve(), follow=False)
    except OSError:
        return True


def deny_sandbox_venv(raw: str, *, root: Path | None = None) -> None:
    parsed = parse_sandbox_env_path(raw)
    if parsed is not None and parsed[0] == "VIRTUAL_ENV":
        from witty_agent.prompts import get_prompt

        raise ValueError(get_prompt("sandbox_denied_venv", path=str(raw)))
    try:
        resolved = Path(expand_jail_path(raw)).resolve()
    except OSError:
        return
    if _under(resolved, sandbox_venv(root=root).resolve(), follow=False):
        from witty_agent.prompts import get_prompt

        raise ValueError(get_prompt("sandbox_denied_venv", path=str(raw)))


def check_command_paths(
    workspace: str,
    command: str,
    *,
    cwd: str | None = None,
    root: Path | None = None,
) -> None:
    """项目相对路径仍 jail。which/npm/brew、/dev/null、本机绝对路径放行。"""
    import shlex

    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        parts = command.split()
    current = cwd
    oldpwd: str | None = None
    index = 0
    while index < len(parts):
        dest_at = _cd_dest_index(parts, index)
        if dest_at is not None:
            if dest_at >= len(parts):
                break
            dest = parts[dest_at].rstrip(";|&")
            if not dest:
                break
            if dest == "-":
                if not oldpwd:
                    from witty_agent.prompts import get_prompt

                    raise ValueError(get_prompt("sandbox_denied_outside", path="$OLDPWD"))
                raw = oldpwd
            else:
                raw = _command_path_raw(
                    dest, cwd=current, workspace=workspace, oldpwd=oldpwd
                )
            host = (
                dest == "-"
                or not _token_stays_in_project(dest)
                or _cwd_outside_workspace(current, workspace)
            )
            if host:
                deny_sandbox_venv(raw, root=root)
                oldpwd = str(Path(current or workspace).resolve())
                try:
                    current = str(Path(expand_jail_path(raw)).resolve())
                except OSError:
                    current = raw
                index = dest_at + 1
                continue
            resolved = resolve_allowed(workspace, raw, root=root)
            oldpwd = str(Path(current or workspace).resolve())
            current = str(resolved)
            index = dest_at + 1
            continue
        part = parts[index]
        if part.startswith("-") and "=" in part[1:]:
            part = part.split("=", 1)[1]
        for token in _split_redirects(part):
            if not _looks_like_path(token):
                continue
            raw = _command_path_raw(
                token, cwd=current, workspace=workspace, oldpwd=oldpwd
            )
            if (
                not _token_stays_in_project(token)
                or _cwd_outside_workspace(current, workspace)
            ):
                deny_sandbox_venv(raw, root=root)
                continue
            resolve_allowed(workspace, raw, root=root)
        index += 1


def display_path(path: Path | str, workspace: str, *, root: Path | None = None) -> str:
    given = Path(path)
    shown = given.parent.resolve() / given.name
    if sandbox_settings()["enabled"] and not workspace_owns_sandbox_name(workspace):
        work = sandbox_work(workspace=workspace, root=root).resolve()
        if _under(shown, work, follow=False):
            rel = shown.relative_to(work).as_posix()
            return "sandbox" if rel in {".", ""} else f"sandbox/{rel}"
        tmp = sandbox_tmp(workspace=workspace, root=root).resolve()
        if _under(shown, tmp, follow=False):
            rel = shown.relative_to(tmp).as_posix()
            return "sandbox-tmp" if rel in {".", ""} else f"sandbox-tmp/{rel}"
    ws = Path(workspace).resolve()
    if _under(shown, ws, follow=False):
        rel = shown.relative_to(ws).as_posix()
        return "." if rel in {".", ""} else rel
    return str(shown)


def rewrite_visible_paths(text: str, workspace: str, *, root: Path | None = None) -> str:
    if not text or not sandbox_settings()["enabled"]:
        return text
    updated = text
    pairs = (
        (sandbox_work(workspace=workspace, root=root), "sandbox"),
        (sandbox_tmp(workspace=workspace, root=root), "sandbox-tmp"),
    )
    for folder, prefix in pairs:
        candidates = {str(folder), str(folder.resolve()), folder.as_posix(), folder.resolve().as_posix()}
        for item in sorted(candidates, key=len, reverse=True):
            updated = updated.replace(item, prefix)
            updated = updated.replace(item.replace("\\", "/"), prefix)
    return updated


def public_sandbox(*, workspace: str | None = None, root: Path | None = None) -> dict[str, str]:
    settings = sandbox_settings()
    if not settings["enabled"]:
        return {
            "enabled": "false",
            "work": "",
            "python": "",
            "packages": "",
            "ready": "false",
            "tmp": "",
        }
    return {
        "enabled": "true",
        "work": str(sandbox_work(workspace=workspace, root=root)),
        "python": str(sandbox_python(root=root)),
        "packages": ", ".join(settings["packages"]) or "-",
        "ready": "true" if sandbox_ready(root=root) else "false",
        "tmp": str(sandbox_tmp(workspace=workspace, root=root)),
    }
