"""在 macOS/Linux 上交叉构建 Windows 安装器（NSIS exe）。

用法：
    uv run python scripts/build_windows_installer.py            # 全流程
    uv run python scripts/build_windows_installer.py --stage-only  # 只准备 staging，不跑 electron-builder

产物：apps/desktop/release/WittyAgent-Setup-<version>.exe

安装器内容物（apps/desktop/electron-builder.yml 定义）：
  - Electron 壳（main.js/api.js/preload.js/renderer，打进 app.asar）
  - resources/python：python-build-standalone 的 Windows CPython + witty_agent 及全部依赖
    （含 classify extra 与 Windows 平台的 tzdata），config/skills 由 wheel 的 data/ 内嵌
  - resources/bin：uv.exe（沙箱建 venv / 装包用）+ busybox64.exe（bash 工具的 shell）

下载源都走国内可达的镜像：CPython 与 electron 走 npmmirror，uv.exe 走清华 PyPI，
busybox 走 frippery.org（失败可用环境变量 WITTY_BUSYBOX_FILE 指本地文件）。
下载缓存在 apps/desktop/.build-cache/，重跑不重下。

不签名：产出的 exe 没有代码签名，Windows SmartScreen 会提示「未知发布者」，
属预期，点「仍要运行」即可。要消除提示需要买代码签名证书，这里不代办。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "apps" / "desktop"
STAGE = DESKTOP / "build-win"
CACHE = DESKTOP / ".build-cache"
BUILDTOOLS = DESKTOP / "buildtools"
RELEASE = DESKTOP / "release"

PY_SERIES = os.environ.get("WITTY_PYTHON_SERIES", "3.12")
PY_PLATFORM = "x86_64-pc-windows-msvc"
PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PBS_MIRROR = "https://registry.npmmirror.com/-/binary/python-build-standalone/"
ELECTRON_MIRROR = "https://cdn.npmmirror.com/binaries/electron/"
BUILDER_BINARIES_MIRROR = "https://cdn.npmmirror.com/binaries/electron-builder-binaries/"
NPM_REGISTRY = "https://registry.npmmirror.com"
BUSYBOX_URLS = [
    "https://frippery.org/files/busybox/busybox64.exe",
    "https://github.com/rmyorston/busybox-w32/releases/latest/download/busybox64.exe",
]

_UA = {"User-Agent": "witty-agent-build/0.1"}


def log(message: str) -> None:
    sys.stdout.write(f"[build-win] {message}\n")
    sys.stdout.flush()


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd, env=env)


def fetch_bytes(url: str, *, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_file(url: str, dest: Path, *, timeout: int = 600) -> Path:
    if dest.is_file() and dest.stat().st_size > 0:
        log(f"缓存命中 {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"下载 {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=timeout) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
    tmp.rename(dest)
    log(f"下载完成 {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


# ---------------------------------------------------------------------------
# 1) Windows CPython（python-build-standalone，npmmirror 镜像）


def _pbs_listing(url: str) -> list[dict]:
    return json.loads(fetch_bytes(url, timeout=60).decode("utf-8"))


def _find_cpython_asset() -> tuple[str, str]:
    """返回 (文件名, 下载 url)。从最新的发布日期目录往回找 3.12 的 windows install_only。"""
    dirs = sorted(
        (item["name"].strip("/") for item in _pbs_listing(PBS_MIRROR) if item.get("type") == "dir"),
        reverse=True,
    )
    pin = os.environ.get("WITTY_PBS_TAG", "").strip()
    if pin:
        dirs = [pin]
    pattern = re.compile(
        rf"^cpython-{re.escape(PY_SERIES)}\.\d+\+\d+-{re.escape(PY_PLATFORM)}"
        r"-install_only(_stripped)?\.tar\.gz$"
    )
    for tag in dirs[:10]:
        files = [item for item in _pbs_listing(f"{PBS_MIRROR}{tag}/") if item.get("type") == "file"]
        matches = [item for item in files if pattern.match(item["name"])]
        if not matches:
            continue
        # stripped 更小，优先
        matches.sort(key=lambda item: ("_stripped" not in item["name"], item["name"]))
        chosen = matches[0]
        return chosen["name"], chosen["url"]
    raise SystemExit(f"npmmirror 上找不到 cpython-{PY_SERIES}.* 的 {PY_PLATFORM} install_only 包")


def stage_python() -> Path:
    name, url = _find_cpython_asset()
    tarball = fetch_file(url, CACHE / name)
    target = STAGE / "python"
    if target.exists():
        shutil.rmtree(target)
    log(f"解包 {name}")
    with tarfile.open(tarball, "r:gz") as archive:
        archive.extractall(STAGE, filter="data")
    # 包根是 python/，里面直接是 python.exe / Lib / DLLs
    exe = target / "python.exe"
    if not exe.is_file():
        found = list(STAGE.rglob("python.exe"))
        raise SystemExit(f"解包后找不到 {exe}（rglob 命中 {found}）")
    return target


# ---------------------------------------------------------------------------
# 2) wheel + 依赖装进目标 site-packages（跨平台解析 win_amd64 轮子）


def build_wheel() -> Path:
    run(["uv", "run", "python", "scripts/sync_package_data.py"], cwd=ROOT)
    run(["uv", "build", "--wheel"], cwd=ROOT)
    wheels = sorted((ROOT / "dist").glob("witty_office_agent-*-py3-none-any.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise SystemExit("uv build 没产出 wheel")
    return wheels[-1]


def install_payload(python_root: Path, wheel: Path) -> None:
    site = python_root / "Lib" / "site-packages"
    run(
        [
            "uv",
            "pip",
            "install",
            "--python-platform",
            PY_PLATFORM,
            "--python-version",
            PY_SERIES,
            "--only-binary",
            ":all:",
            "--target",
            str(site),
            "--index-url",
            PYPI_INDEX,
            "--link-mode",
            "copy",
            "--compile-bytecode",
            "--reinstall",
            f"witty-office-agent[classify] @ {wheel.resolve().as_uri()}",
        ],
        cwd=ROOT,
    )
    # 自检：数据文件、Windows 轮子、时区库。缺一样装出去就是废包。
    problems = []
    if not (site / "witty_agent" / "data" / "config" / "prompts.toml").is_file():
        problems.append("witty_agent/data/config/prompts.toml 不在（先跑 sync_package_data 再 build）")
    if not list((site / "PIL").glob("_imaging*win_amd64.pyd")):
        problems.append("PIL 没装到 win_amd64 轮子")
    if not (site / "tzdata").is_dir():
        problems.append("tzdata 没装上（Windows 无系统时区库，ZoneInfo 会崩）")
    if not list((site / "witty_agent" / "data" / "skills").glob("*/SKILL.md")):
        problems.append("内嵌技能目录是空的")
    if problems:
        raise SystemExit("payload 自检失败：\n  - " + "\n  - ".join(problems))
    log("payload 自检通过")


# ---------------------------------------------------------------------------
# 3) resources/bin：uv.exe + busybox64.exe


def stage_uv(bin_dir: Path) -> None:
    page = fetch_bytes(f"{PYPI_INDEX}/uv/", timeout=60).decode("utf-8", "replace")
    versions = re.findall(r"uv-(\d+\.\d+\.\d+)-py3-none-win_amd64\.whl", page)
    if not versions:
        raise SystemExit("清华 PyPI 上找不到 uv 的 win_amd64 wheel")
    latest = max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
    name = f"uv-{latest}-py3-none-win_amd64.whl"
    href = re.search(rf'href="([^"]*{re.escape(name)}[^"]*)"', page)
    if not href:
        raise SystemExit(f"解析不到 {name} 的下载链接")
    url = href.group(1)
    if url.startswith("../.."):
        url = "https://pypi.tuna.tsinghua.edu.cn" + url[len("../..") :]
    wheel = fetch_file(url, CACHE / name)
    with zipfile.ZipFile(wheel) as archive:
        members = [m for m in archive.namelist() if m.endswith("uv.exe") or m.endswith("uvx.exe")]
        if not any(m.endswith("uv.exe") for m in members):
            raise SystemExit(f"{name} 里没有 uv.exe（members={archive.namelist()[:10]}）")
        for member in members:
            data = archive.read(member)
            (bin_dir / Path(member).name).write_bytes(data)
    log(f"uv.exe {latest} 就位")


def stage_busybox(bin_dir: Path) -> None:
    override = os.environ.get("WITTY_BUSYBOX_FILE", "").strip()
    dest = bin_dir / "busybox64.exe"
    if override:
        shutil.copy2(override, dest)
        log(f"busybox 用本地文件 {override}")
        return
    cached = CACHE / "busybox64.exe"
    errors = []
    if not (cached.is_file() and cached.stat().st_size > 100_000):
        for url in BUSYBOX_URLS:
            try:
                fetch_file(url, cached, timeout=180)
                break
            except Exception as exc:  # noqa: BLE001 - 逐源尝试，最后统一报
                errors.append(f"{url} -> {exc}")
                cached.unlink(missing_ok=True)
    if not (cached.is_file() and cached.stat().st_size > 100_000):
        raise SystemExit(
            "busybox64.exe 下载失败（bash 工具在 Windows 上靠它）：\n  "
            + "\n  ".join(errors)
            + "\n手工下载后用 WITTY_BUSYBOX_FILE=/path/busybox64.exe 重跑。"
        )
    shutil.copy2(cached, dest)
    log("busybox64.exe 就位")


# ---------------------------------------------------------------------------
# 4) electron-builder


def ensure_builder() -> Path:
    binary = BUILDTOOLS / "node_modules" / ".bin" / "electron-builder"
    if binary.exists():
        return binary
    BUILDTOOLS.mkdir(parents=True, exist_ok=True)
    pkg = BUILDTOOLS / "package.json"
    if not pkg.is_file():
        pkg.write_text(
            json.dumps({"name": "witty-buildtools", "private": True, "version": "0.0.0"}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    run(
        [
            "npm",
            "install",
            "--save-dev",
            "electron-builder",
            f"--registry={NPM_REGISTRY}",
            "--no-audit",
            "--no-fund",
        ],
        cwd=BUILDTOOLS,
    )
    if not binary.exists():
        raise SystemExit("electron-builder 安装失败")
    return binary


def run_builder(binary: Path) -> None:
    env = dict(os.environ)
    env.setdefault("ELECTRON_MIRROR", ELECTRON_MIRROR)
    env.setdefault("ELECTRON_BUILDER_BINARIES_MIRROR", BUILDER_BINARIES_MIRROR)
    # 不做 mac 侧签名探测；win 目标也不签名
    env.setdefault("CSC_IDENTITY_AUTO_DISCOVERY", "false")
    run([str(binary), "--win", "--x64", "--projectDir", str(DESKTOP)], cwd=DESKTOP, env=env)


def verify_release() -> Path:
    unpacked = RELEASE / "win-unpacked"
    checks = {
        "app.asar": unpacked / "resources" / "app.asar",
        "python.exe": unpacked / "resources" / "python" / "python.exe",
        "witty_agent": unpacked / "resources" / "python" / "Lib" / "site-packages" / "witty_agent" / "__main__.py",
        "uv.exe": unpacked / "resources" / "bin" / "uv.exe",
        "busybox64.exe": unpacked / "resources" / "bin" / "busybox64.exe",
        "主程序": unpacked / "WittyAgent.exe",
    }
    missing = [f"{label}: {path}" for label, path in checks.items() if not path.exists()]
    if missing:
        raise SystemExit("win-unpacked 内容物缺失：\n  - " + "\n  - ".join(missing))
    installers = sorted(RELEASE.glob("*.exe"), key=lambda p: p.stat().st_mtime)
    if not installers:
        raise SystemExit("没找到 NSIS 安装器 exe")
    exe = installers[-1]
    log(f"安装器：{exe} ({exe.stat().st_size / 1e6:.1f} MB)")
    return exe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-only", action="store_true", help="只准备 staging，不跑 electron-builder")
    args = parser.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    bin_dir = STAGE / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    icon = DESKTOP / "build" / "icon.ico"
    if not icon.is_file():
        run(["uv", "run", "python", "scripts/make_desktop_icon.py"], cwd=ROOT)

    python_root = stage_python()
    wheel = build_wheel()
    install_payload(python_root, wheel)
    stage_uv(bin_dir)
    stage_busybox(bin_dir)

    if args.stage_only:
        log("staging 完成（--stage-only）")
        return 0

    binary = ensure_builder()
    run_builder(binary)
    exe = verify_release()
    log(f"完成。把 {exe.name} 发给用户即可；首次运行 SmartScreen 提示属预期（未签名）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
