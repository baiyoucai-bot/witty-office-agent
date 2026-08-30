"""建一个「小说转即梦视频」工程骨架：项目卡、资产台账、关系图、分镜索引、逐帧提示词。

幂等：已存在的文件一律跳过，不覆盖用户改过的内容。只建目录和空模板，不写剧情。

用沙箱解释器跑：

    <沙箱 Python> init_project.py --name 校园重生 --root .
    <沙箱 Python> init_project.py --name 校园重生 --root . --frames 12
    <沙箱 Python> init_project.py --name 校园重生 --root . --duration 5 --ratio 9:16 --style 3D写实

退出码 0 表示建好（含全部跳过）；2 表示参数或路径不可用。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_CARD = "项目卡.md"
LEDGERS = {"人物": "人物.md", "场景": "场景.md", "物品": "物品.md"}
RELATION = "关系.md"
STORYBOARD = "分镜.md"
PROMPT_DIR = "提示词"
ASSET_DIR = "参考图"

DEFAULT_RULES = """动漫风格，高质量。
基础硬性要求：画面全程无任何字幕、无文字叠加、无水印、无 logo、无额外文字标识；\
无背景音乐、无 BGM、无配乐；只保留纯画面、剧情人声与环境/动作音效，拒绝一切文字元素。"""


def project_card(name: str, model: str, duration: float, resolution: str, ratio: str, style: str, rules: str) -> str:
    dur = format_seconds(duration)
    return f"""# {name}

出片参数是全工程的唯一真相。改这里，不要在单帧里各写一套。

## 出片参数
- 模型: {model}
- 单帧时长: {dur}s
- 分辨率: {resolution}
- 画面比例: {ratio}
- 风格: {style}

## 全局执行规则
{rules}

## 预算
- 中文台词按 4.5 字/秒估；一段时间轴里的台词字数不要超过 秒数 × 4.5。
- 单帧镜头切换不超过 3 次；{dur}s 内切三次以上会糊。
- 一帧只讲一个动作单元。讲不完就拆帧，不要压时间轴。

## 素材绑定
即梦网页版按 **人物 / 场景 / 物品** 三个槽位挂参考图，提示词里用 `@名字` 引用。
名字必须和 `人物.md` / `场景.md` / `物品.md` 里的二级标题（`## 名字`）逐字一致，否则 @ 引用不到素材。
"""


def ledger(kind: str) -> str:
    hint = {
        "人物": """二级标题（`## 名字`）是**素材名**，即梦里 @ 的就是它。锚点要可见、可生成、可比对。

## 示例角色
- 锚点: 19 岁男大学生，短寸黑发，左眉尾一道浅疤，洗旧藏青连帽衫
- 声线: 少年音偏清亮，句尾发虚
- 参考图: 参考图/人物/示例角色.png
- 本片状态: 宿醉未醒，眼下青黑
""",
        "场景": """二级标题（`## 名字`）是**素材名**。写死时段、天气、光态——这三样一变，观众就看出穿帮。

## 示例场景
- 锚点: 大学校园空地，红砖教学楼在后景，两排水泥乒乓台
- 光态: 白天，正午顶光，地面有硬阴影
- 参考图: 参考图/场景/示例场景.png
""",
        "物品": """二级标题（`## 名字`）是**素材名**。只登记会被看见、会被拿、会变状态的东西。

## 示例物品
- 锚点: 粉红缎带方形礼盒，掌宽，缎带打十字结
- 状态: 未拆封 → 被塞回 → 掉地
- 参考图: 参考图/物品/示例物品.png
""",
    }[kind]
    return f"""# {kind}台账

{hint}
"""


RELATION_TEMPLATE = """# 角色关系

关系决定每一帧的站位、视线和台词落点。先把关系写清，再写分镜。

## 关系表

| 甲 | 乙 | 关系 | 甲对乙的诉求 | 乙的挡法 | 可见后果 |
|---|---|---|---|---|---|
| 示例角色 | 另一角色 | 同班同学 | 想被承认 | 装听不见 | 递东西被无视，手停在半空 |

## 关系图

```mermaid
graph LR
  A[示例角色] -- 暗恋/被无视 --> B[另一角色]
```

## 阵营与信息差

- 谁知道什么、谁不知道：
- 这一集会被打破的关系：
"""


STORYBOARD_HEADER = """# 分镜索引

一行一帧。这张表是帧清单的真相，`提示词/` 下的文件按它对齐。

| 帧 | 秒 | 场景 | 出镜 | 一句话 | 状态 |
|---|---|---|---|---|---|
"""

RE_BOARD_ROW = re.compile(r"^\|\s*(\d{1,3})\s*\|", re.MULTILINE)


def board_row(index: int, duration: float) -> str:
    return f"| {index:03d} | 0-{format_seconds(duration)} |  |  |  | 待写 |"


def board_template(frames: int, duration: float) -> str:
    rows = "".join(f"{board_row(i, duration)}\n" for i in range(1, frames + 1))
    return STORYBOARD_HEADER + rows


def frame_template(index: int, model: str, duration: float, resolution: str, ratio: str, style: str) -> str:
    dur = format_seconds(duration)
    return f"""# FRAME {index:03d}

## 模型
{model} | {dur}s | {resolution} | {ratio} | {style}

## 出镜
人物:
场景:
物品:

## 文案
【场景：】
[首帧画面]
[尾帧画面]

## 视频描述词
[0-{dur}秒]

## 全局执行规则
见 项目卡.md
"""


def format_seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def write_once(path: Path, content: str, created: list[str], skipped: list[str]) -> None:
    if path.exists():
        skipped.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(str(path))


def sync_board(path: Path, frames: int, duration: float, created: list[str], skipped: list[str], grown: list[str]) -> None:
    """分镜索引要跟帧文件对得齐。已存在就只补缺的行，不动已填好的列。"""
    if not path.exists():
        write_once(path, board_template(frames, duration), created, skipped)
        return
    body = path.read_text(encoding="utf-8")
    listed = {int(num) for num in RE_BOARD_ROW.findall(body)}
    missing = [index for index in range(1, frames + 1) if index not in listed]
    if not missing:
        skipped.append(str(path))
        return
    lines = body.splitlines()
    tail = max((i for i, line in enumerate(lines) if line.startswith("|")), default=len(lines) - 1)
    lines[tail + 1 : tail + 1] = [board_row(index, duration) for index in missing]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    grown.append(f"{path}（补 {len(missing)} 行）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建即梦视频工程骨架")
    parser.add_argument("--name", required=True, help="项目名，也是工程目录名")
    parser.add_argument("--root", default=".", help="工程放在哪个目录下，默认当前目录")
    parser.add_argument("--model", default="Seedance-2.0-Mini", help="即梦模型名")
    parser.add_argument("--duration", type=float, default=9, help="单帧时长秒数，默认 9")
    parser.add_argument("--resolution", default="480P", help="分辨率档，默认 480P")
    parser.add_argument("--ratio", default="16:9", help="画面比例，竖屏写 9:16")
    parser.add_argument("--style", default="动漫风格", help="风格，默认动漫风格")
    parser.add_argument("--rules", default=None, help="全局执行规则正文；缺省用无字幕无 BGM 的硬性要求")
    parser.add_argument("--frames", type=int, default=1, help="预建多少个帧文件，默认 1")
    args = parser.parse_args(argv)

    if args.duration <= 0:
        print("FAIL 单帧时长必须大于 0", file=sys.stderr)
        return 2
    if args.frames < 0:
        print("FAIL --frames 不能是负数", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d+:\d+", args.ratio):
        print(f"FAIL 画面比例写法不对: {args.ratio}（要像 16:9 或 9:16）", file=sys.stderr)
        return 2

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"FAIL 目录不存在: {root}", file=sys.stderr)
        return 2

    project = root / args.name
    created: list[str] = []
    skipped: list[str] = []
    grown: list[str] = []
    rules = args.rules if args.rules is not None else DEFAULT_RULES

    write_once(
        project / PROJECT_CARD,
        project_card(args.name, args.model, args.duration, args.resolution, args.ratio, args.style, rules),
        created,
        skipped,
    )
    for kind, filename in LEDGERS.items():
        write_once(project / filename, ledger(kind), created, skipped)
    write_once(project / RELATION, RELATION_TEMPLATE, created, skipped)
    sync_board(project / STORYBOARD, args.frames, args.duration, created, skipped, grown)

    (project / PROMPT_DIR).mkdir(parents=True, exist_ok=True)
    for index in range(1, args.frames + 1):
        write_once(
            project / PROMPT_DIR / f"FRAME-{index:03d}.md",
            frame_template(index, args.model, args.duration, args.resolution, args.ratio, args.style),
            created,
            skipped,
        )

    for kind in LEDGERS:
        (project / ASSET_DIR / kind).mkdir(parents=True, exist_ok=True)

    print(f"工程: {project}")
    for path in created:
        print(f"  建 {path}")
    for path in grown:
        print(f"  补 {path}")
    for path in skipped:
        print(f"  跳过（已存在） {path}")
    print(f"新建 {len(created)} 个，补 {len(grown)} 个，跳过 {len(skipped)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
