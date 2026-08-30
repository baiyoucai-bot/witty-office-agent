"""构建 macOS 桌面安装器（dmg，Apple Silicon）。

用法：
    uv run python scripts/build_macos_installer.py             # 全流程
    uv run python scripts/build_macos_installer.py --stage-only  # 只准备 staging

产物：apps/desktop/release/WittyAgent-<version>-arm64.dmg

内容物（apps/desktop/electron-builder.yml 的 mac 段定义）：
  - Electron 壳（app.asar）
  - resources/python：python-build-standalone 的 macOS arm64 CPython + witty_agent 及依赖
  - resources/bin：uv（沙箱建 venv / 装包用）。macOS 自带 bash，不需要 busybox

与 Windows 版的差异：不捆 busybox（有系统 bash）、不查 tzdata（有系统时区库）、
布局是 python/bin/python3 + python/lib/python3.x/site-packages（main.js 的非 win 分支认这个）。

不签名：没有开发者证书时 electron-builder 会做 ad-hoc 签名（Apple Silicon 必需）。
用户首次打开会遇 Gatekeeper 提示，右键 App「打开」或
`xattr -dr com.apple.quarantine /Applications/人和.app` 可放行；要消除提示需要
Apple Developer 证书 + 公证，这里不代办。

Intel 机器：WITTY_MAC_ARCH=x86_64 重跑（同机只能交叉 staging，dmg 目标要 --x64，
本脚本会跟着切）。
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
STAGE = DESKTOP / "build-mac"
CACHE = DESKTOP / ".build-cache"
BUILDTOOLS = DESKTOP / "buildtools"
RELEASE = DESKTOP / "release"

PY_SERIES = os.environ.get("WITTY_PYTHON_SERIES", "3.12")
MAC_ARCH = os.environ.get("WITTY_MAC_ARCH", "aarch64")  # aarch64 | x86_64
PY_PLATFORM = f"{MAC_ARCH}-apple-darwin"
PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PBS_MIRROR = "https://registry.npmmirror.com/-/binary/python-build-standalone/"
ELECTRON_MIRROR = "https://cdn.npmmirror.com/binaries/electron/"
BUILDER_BINARIES_MIRROR = "https://cdn.npmmirror.com/binaries/electron-builder-binaries/"
NPM_REGISTRY = "https://registry.npmmirror.com"

_UA = {"User-Agent": "witty-agent-build/0.1"}


def log(message: str) -> None:
    sys.stdout.write(f"[build-mac] {message}\n")
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
# 1) macOS CPython（python-build-standalone，npmmirror 镜像）


def _pbs_listing(url: str) -> list[dict]:
    return json.loads(fetch_bytes(url, timeout=60).decode("utf-8"))


def _find_cpython_asset() -> tuple[str, str]:
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
    exe = target / "bin" / "python3"
    if not exe.is_file():
        found = list(STAGE.rglob("bin/python3"))
        raise SystemExit(f"解包后找不到 {exe}（rglob 命中 {found}）")
    return target


def _site_packages(python_root: Path) -> Path:
    hits = sorted(python_root.glob("lib/python3.*/site-packages"))
    if not hits:
        raise SystemExit(f"{python_root} 下找不到 lib/python3.*/site-packages")
    return hits[-1]


# ---------------------------------------------------------------------------
# 2) wheel + 依赖装进目标 site-packages（跨架构解析 macOS 轮子）


def build_wheel() -> Path:
    run(["uv", "run", "python", "scripts/sync_package_data.py"], cwd=ROOT)
    run(["uv", "build", "--wheel"], cwd=ROOT)
    wheels = sorted(
        (ROOT / "dist").glob("witty_office_agent-*-py3-none-any.whl"), key=lambda p: p.stat().st_mtime
    )
    if not wheels:
        raise SystemExit("uv build 没产出 wheel")
    return wheels[-1]


def install_payload(python_root: Path, wheel: Path) -> None:
    site = _site_packages(python_root)
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
    # 自检：数据文件、darwin 轮子。缺一样装出去就是废包。
    problems = []
    if not (site / "witty_agent" / "data" / "config" / "prompts.toml").is_file():
        problems.append("witty_agent/data/config/prompts.toml 不在（先跑 sync_package_data 再 build）")
    if not list((site / "PIL").glob("_imaging*.so")):
        problems.append("PIL 没装到 darwin 轮子")
    if not list((site / "witty_agent" / "data" / "skills").glob("*/SKILL.md")):
        problems.append("内嵌技能目录是空的")
    stale = [item.name for item in site.glob("*.dist-info") if item.name.startswith("witty_agent-")]
    if stale:
        problems.append(f"site-packages 里有旧发行名残留：{stale}")
    if problems:
        raise SystemExit("payload 自检失败：\n  - " + "\n  - ".join(problems))
    log("payload 自检通过")


# ---------------------------------------------------------------------------
# 3) resources/bin：uv（macOS 轮子里抽二进制）


def stage_uv(bin_dir: Path) -> None:
    wheel_arch = "arm64" if MAC_ARCH == "aarch64" else "x86_64"
    page = fetch_bytes(f"{PYPI_INDEX}/uv/", timeout=60).decode("utf-8", "replace")
    pattern = rf"uv-(\d+\.\d+\.\d+)-py3-none-macosx_\d+_\d+_{wheel_arch}\.whl"
    versions = re.findall(pattern, page)
    if not versions:
        raise SystemExit(f"清华 PyPI 上找不到 uv 的 macosx {wheel_arch} wheel")
    latest = max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
    name_re = rf"uv-{re.escape(latest)}-py3-none-macosx_\d+_\d+_{wheel_arch}\.whl"
    href = re.search(rf'href="([^"]*?({name_re})[^"]*)"', page)
    if not href:
        raise SystemExit(f"解析不到 uv {latest} 的下载链接")
    url, name = href.group(1), href.group(2)
    if url.startswith("../.."):
        url = "https://pypi.tuna.tsinghua.edu.cn" + url[len("../..") :]
    wheel = fetch_file(url, CACHE / name)
    with zipfile.ZipFile(wheel) as archive:
        members = [m for m in archive.namelist() if m.endswith("/uv") or m.endswith("/uvx")]
        if not any(m.endswith("/uv") for m in members):
            raise SystemExit(f"{name} 里没有 uv 二进制（members={archive.namelist()[:10]}）")
        for member in members:
            dest = bin_dir / Path(member).name
            dest.write_bytes(archive.read(member))
            dest.chmod(0o755)
    log(f"uv {latest}（{wheel_arch}）就位")


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
    # 无证书：electron-builder 对 arm64 自动做 ad-hoc 签名，不去钥匙串里找身份
    env.setdefault("CSC_IDENTITY_AUTO_DISCOVERY", "false")
    arch_flag = "--arm64" if MAC_ARCH == "aarch64" else "--x64"
    run([str(binary), "--mac", arch_flag, "--projectDir", str(DESKTOP)], cwd=DESKTOP, env=env)


def verify_release() -> Path:
    arch_dir = "mac-arm64" if MAC_ARCH == "aarch64" else "mac"
    apps = sorted((RELEASE / arch_dir).glob("*.app"))
    if not apps:
        raise SystemExit(f"{arch_dir} 下没有 .app")
    app = apps[-1]
    resources = app / "Contents" / "Resources"
    site = _site_packages(resources / "python")
    checks = {
        "app.asar": resources / "app.asar",
        "python3": resources / "python" / "bin" / "python3",
        "witty_agent": site / "witty_agent" / "__main__.py",
        "uv": resources / "bin" / "uv",
    }
    missing = [f"{label}: {path}" for label, path in checks.items() if not path.exists()]
    if missing:
        raise SystemExit(f"{arch_dir} 内容物缺失：\n  - " + "\n  - ".join(missing))
    dmgs = sorted(RELEASE.glob("*.dmg"), key=lambda p: p.stat().st_mtime)
    if not dmgs:
        raise SystemExit("没找到 dmg")
    dmg = dmgs[-1]
    log(f"安装器：{dmg} ({dmg.stat().st_size / 1e6:.1f} MB)")
    return dmg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-only", action="store_true", help="只准备 staging，不跑 electron-builder")
    args = parser.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    bin_dir = STAGE / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    icon = DESKTOP / "build" / "icon.png"
    if not icon.is_file():
        run(["uv", "run", "python", "scripts/make_desktop_icon.py"], cwd=ROOT)

    python_root = stage_python()
    wheel = build_wheel()
    install_payload(python_root, wheel)
    stage_uv(bin_dir)

    if args.stage_only:
        log("staging 完成（--stage-only）")
        return 0

    binary = ensure_builder()
    run_builder(binary)
    dmg = verify_release()
    log(f"完成。首次打开遇 Gatekeeper 提示属预期（未公证）：右键 App 选「打开」即可。产物 {dmg.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
