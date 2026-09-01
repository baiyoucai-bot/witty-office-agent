#!/usr/bin/env python
"""资料分类命令行入口。判定口径在 config/prompts.toml 的 file_classify_*，这里只解析参数。

    uv run python skills/file-classify/scripts/classify.py <目录> --taxonomy 类型表.json
"""

from __future__ import annotations

import argparse
import json
import sys

from witty_agent import setup_logging
from witty_agent.plugins.file_classify import classify_directory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按调用方类型表给一个目录做资料分类")
    parser.add_argument("root", help="待分类目录，递归扫描")
    parser.add_argument("--taxonomy", required=True, help="类型表：JSON 文件路径，或直接给 JSON 字符串")
    parser.add_argument("--out", default="", help="结果目录，默认写到 WITTY_HOME/file_classify/<目录名>")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个单元，先小批试跑用")
    parser.add_argument("--concurrency", type=int, default=4, help="同时在飞的模型调用数，网关吃不住就调小")
    parser.add_argument("--batch", type=int, default=8, help="第一轮每批单元数")
    parser.add_argument("--pass2-batch", type=int, default=6, help="第二轮每批单元数，带正文所以更小")
    parser.add_argument("--group-batch", type=int, default=5, help="拆分件确认每批组数")
    parser.add_argument("--excerpt-chars", type=int, default=1200, help="每个文件的正文摘录上限")
    parser.add_argument("--title-chars", type=int, default=160, help="第一轮附的标题行长度，用正文开头核验文件名，0 关闭")
    parser.add_argument("--max-tokens", type=int, default=6000, help="单次模型调用的输出上限")
    parser.add_argument("--timeout", type=int, default=300, help="单次模型调用超时秒数")
    parser.add_argument("--think", default="off", help="模型思考档位：off / short / long")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="低于此置信度的单元进第二轮")
    parser.add_argument("--no-group-check", action="store_true", help="跳过拆分件合并确认，每个文件各判各的")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有结果，全部重判")
    parser.add_argument("--json", action="store_true", help="只输出统计 JSON，便于被别的脚本消费")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    quiet = args.json
    try:
        summary = classify_directory(
            args.root,
            args.taxonomy,
            out_dir=args.out or None,
            limit=args.limit,
            concurrency=args.concurrency,
            pass1_batch=args.batch,
            pass2_batch=args.pass2_batch,
            group_batch=args.group_batch,
            excerpt_chars=args.excerpt_chars,
            title_chars=args.title_chars,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            think=args.think,
            min_confidence=args.min_confidence,
            group_check=not args.no_group_check,
            resume=not args.no_resume,
            progress=None if quiet else (lambda text: print(text, flush=True)),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # 模型侧的失败（鉴权、超时、网关）不该甩一坨调用栈给用户；已判定的批次都已落盘
        print(f"模型调用失败：{exc}", file=sys.stderr)
        print("已判定的批次已落盘，修好后重跑同一条命令即可续上。", file=sys.stderr)
        return 3
    if quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print(
        f"\n完成：单元 {summary['units']} 个（合并 {summary['merged']} 组拆分件），"
        f"二轮读正文 {summary['pass2']} 个，成功 {summary['units_ok']} 个，"
        f"失败 {summary['units_failed']} 个"
    )
    print(f"明细 {summary['result_file']}")
    print(f"报告 {summary['report_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
