"""资料分类的库用法。直接函数调用，不进 agent 循环，全程不会弹审批。

    WITTY_API_KEY=... uv run python examples/classify_demo.py <目录> <类型表.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from witty_agent.logging import setup_logging
from witty_agent.plugins.file_classify import classify_directory
from witty_agent.runtime import model_settings


def main() -> int:
    setup_logging()
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    root, taxonomy_path = sys.argv[1], sys.argv[2]

    settings = model_settings()
    if not str(settings["api_key"] or ""):
        print("没有模型密钥。设置环境变量 WITTY_API_KEY 后再跑。", file=sys.stderr)
        return 1

    # 使用方送来的类型表：这里从文件读，实际接口里就是那个 JSON 对象本身。
    # classify_directory 的 taxonomy 参数吃 list / dict / JSON 字符串 / 文件路径四种，
    # 不用先自己序列化。
    taxonomy = json.loads(Path(taxonomy_path).read_text(encoding="utf-8"))

    def on_result(rows: list[dict]) -> None:
        """每批判完立刻回调，不用等整轮跑完，也不用去轮询 results.jsonl。

        只会收到已定论的行：status 是 "ok" 或 "failed"，失败原因在 error。
        """
        for row in rows:
            note = "" if row["status"] == "ok" else f"（{row['error']}）"
            print(f"    [{row['status']}] {row['category_id']:8} {row['members'][0]}{note}", flush=True)

    try:
        summary = classify_directory(
            root,
            taxonomy,
            out_dir="./classify_out",
            # 第一次对一个新目录跑一定带 limit，先看 30 个对口径，别一上来烧全量
            limit=30,
            progress=lambda text: print(text, flush=True),
            on_result=on_result,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"入参有问题：{exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # 模型侧失败。已判定的批次都已落盘，重跑同一条命令会跳过它们
        print(f"模型调用失败：{exc}", file=sys.stderr)
        return 3

    print(f"\n单元 {summary['units']} 个（合并 {summary['merged']} 组拆分件）")
    print(f"一轮定论 {summary['pass1']}，二轮读正文 {summary['pass2']}")
    print(f"成功 {summary['units_ok']}，失败 {summary['units_failed']}")

    # results.jsonl 是真源，一行一个单元，可以直接喂给下游
    for line in Path(summary["result_file"]).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        # 只有 status == "ok" 能当结论用，落库前先按它过滤
        flag = "" if row["status"] == "ok" else f"  ← 失败：{row['error']}"
        print(f"{row['category_id']:8} {row['confidence']:<5} {row['members'][0]}{flag}")
        print(f"         依据 {'; '.join(row['evidence'])}")

    if summary["units_failed"]:
        print(f"\n有 {summary['units_failed']} 个失败，原因见各行 error；细节去 report.md 或 reasoning 里看")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
