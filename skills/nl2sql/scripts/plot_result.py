"""把 SQL 结果画成一张图。只读输入，只写 --out 指定的那个文件。

图表类型按同技能 references/chart-decision.md 选，不要默认柱状图。

用沙箱解释器跑：

    <沙箱 Python> plot_result.py --input result.csv --chart line --out trend.png
    <沙箱 Python> plot_result.py --input result.csv --chart column --x 区域 --y 负荷MW --out cmp.png
    <沙箱 Python> plot_result.py --input result.csv --chart line --x 月份 --series 区域 --y 负荷MW --out trend.png
    <沙箱 Python> plot_result.py --input result.csv --chart pie --top 5 --out share.png

退出码 0 表示图已出；1 表示被判据拦下（改图表类型，不要加参数硬绕）；2 表示读不进来。
"""

from __future__ import annotations

import argparse
import re
import sys
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 必须在 pyplot 之前：跑在没有显示的环境里

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

PIE_MAX = 6
LINE_MIN_POINTS = 3
LINE_MAX_SERIES = 5
COLUMN_MAX = 8
BAR_MAX = 30
LABEL_MAX = 12
SCALE_GAP = 100  # 序列间量级差这么多倍就该拆图，双 Y 轴是骗人的

CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")

# 按 macOS / Windows / Linux 常见顺序试
CJK_FONTS = (
    "PingFang SC",
    "PingFang HK",
    "Hiragino Sans GB",
    "Heiti SC",
    "STHeiti",
    "Songti SC",
    "STSong",
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
)

NOTES: list[str] = []
HAS_CJK_FONT = True


def note(text: str) -> None:
    NOTES.append(text)


def setup_font() -> None:
    """挑一个装了的中文字体。挑不到就退回英文标签，不画一堆方块。"""
    global HAS_CJK_FONT
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in CJK_FONTS:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, *plt.rcParams["font.sans-serif"]]
            HAS_CJK_FONT = True
            plt.rcParams["axes.unicode_minus"] = False  # 负号在部分中文字体里是方块
            return
    HAS_CJK_FONT = False
    plt.rcParams["axes.unicode_minus"] = False
    note("WARN 系统没有中文字体，中文标签已换成 C1/C2 占位符，对照表见下方；要中文标签请先装字体")


def fallback_labels(labels: list[str]) -> list[str]:
    """没有中文字体时把中文标签换成占位符，并把对照表打出来。"""
    if HAS_CJK_FONT:
        return [str(x) for x in labels]
    out: list[str] = []
    for i, raw in enumerate(labels, 1):
        text = str(raw)
        if CJK_RE.search(text):
            key = f"C{i}"
            note(f"  {key} = {text}")
            out.append(key)
        else:
            out.append(text)
    return out


def safe_text(text: str, kind: str) -> str:
    """标题/轴名没法用占位符代替，中文字体缺失时就不画，改打到 stdout。"""
    if HAS_CJK_FONT or not CJK_RE.search(text):
        return text
    note(f"  {kind}（未画到图上）= {text}")
    return ""


def load(path: Path, sheet: str | None) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path, sheet_name=sheet or 0)
    if suffix in {".tsv", ".tab"}:
        return pd.read_csv(path, sep="\t", encoding="utf-8-sig")
    return pd.read_csv(path, encoding="utf-8-sig")


def numeric(series: pd.Series) -> pd.Series | None:
    """能当数值用就返回数值列。带千分位的 '1,234' 也认，否则整张图画不出来。"""
    if series.dtype.kind in "iuf":
        return series
    if series.dtype.kind in "Mm" or series.dtype.kind == "b":
        return None
    text = series.astype(str).str.strip().str.replace(",", "", regex=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_numeric(text, errors="coerce")
    if parsed.notna().mean() >= 0.8:
        return parsed
    return None


def as_time(series: pd.Series) -> pd.Series | None:
    """判 X 轴是不是时间。折线图的 X 轴不是时间就该换柱状图，所以这里判严一点。"""
    if series.dtype.kind == "M":
        return series
    text = series.astype(str).str.strip()

    if text.str.fullmatch(r"\d{6}").mean() >= 0.8:  # 202601
        return pd.to_datetime(text, format="%Y%m", errors="coerce")
    if text.str.fullmatch(r"\d{4}").mean() >= 0.8:  # 年份
        return pd.to_datetime(text, format="%Y", errors="coerce")
    ym = text.str.extract(r"^(\d{4})\s*年\s*(\d{1,2})\s*月")
    if ym.notna().all(axis=1).mean() >= 0.8:
        joined = ym[0] + "-" + ym[1].str.zfill(2) + "-01"
        return pd.to_datetime(joined, errors="coerce")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(text, errors="coerce")
    if parsed.notna().mean() >= 0.8:
        return parsed
    return None


def fail(message: str) -> int:
    print(f"FAIL {message}", file=sys.stderr)
    return 1


def pick_columns(
    df: pd.DataFrame, x_arg: str | None, y_arg: str | None, series_arg: str | None
) -> tuple[str, list[str], str | None] | None:
    """定 X、Y、序列列。默认第一列当 X，其余数值列当 Y。"""
    cols = [str(c) for c in df.columns]
    for name, label in ((x_arg, "--x"), (series_arg, "--series")):
        if name and name not in cols:
            print(f"FAIL {label} 指定的列 {name!r} 不在表里，表里有：{cols}", file=sys.stderr)
            return None

    x = x_arg or cols[0]
    if y_arg:
        y = [c.strip() for c in y_arg.split(",") if c.strip()]
        missing = [c for c in y if c not in cols]
        if missing:
            print(f"FAIL --y 指定的列 {missing} 不在表里，表里有：{cols}", file=sys.stderr)
            return None
        bad = [c for c in y if numeric(df[c]) is None]
        if bad:
            print(f"FAIL --y 指定的列 {bad} 不是数值列，画不了", file=sys.stderr)
            return None
    else:
        skip = {x} | ({series_arg} if series_arg else set())
        y = [c for c in cols if c not in skip and numeric(df[c]) is not None]
        if not y:
            print(
                f"FAIL 没有数值列可画（列：{cols}）。"
                "明细类结果本来就不该出图，改成表格交付",
                file=sys.stderr,
            )
            return None
    return x, y, series_arg


def shape_frame(
    df: pd.DataFrame, x: str, y: list[str], series: str | None
) -> pd.DataFrame:
    """整成「索引是 X、每列一个序列」的宽表。长表靠 --series 展开。"""
    work = df.copy()
    for name in y:
        work[name] = numeric(work[name])

    if series:
        frame = work.pivot_table(index=x, columns=series, values=y[0], aggfunc="sum")
        frame.columns = [str(c) for c in frame.columns]
        return frame

    if work[x].duplicated().any():
        # X 有重复又没给 --series：要么漏了分组维度，要么该先聚合
        note(f"WARN {x} 有重复值，已按 {x} 求和合并；若本意是多序列请加 --series <维度列>")
        frame = work.groupby(x, sort=False)[y].sum()
    else:
        frame = work.set_index(x)[y]
    return frame


def limit_top(frame: pd.DataFrame, top: int, lump: bool) -> pd.DataFrame:
    """显式截断。截了什么一定要说，不然读图的人会以为这就是全部。"""
    if top <= 0 or len(frame) <= top:
        return frame
    order = frame.sum(axis=1).sort_values(ascending=False)
    keep = list(order.index[:top])
    dropped = list(order.index[top:])
    head = frame.loc[keep]
    if lump:
        rest = frame.loc[dropped].sum()
        head = pd.concat([head, rest.to_frame("其他").T])
        note(f"WARN 只画了前 {top} 项，其余 {len(dropped)} 项合成「其他」：{dropped[:10]}")
        if float(rest.iloc[0]) > float(head.iloc[0, 0]):
            note("WARN 「其他」比最大的那一项还大：分类粒度选错了，换个维度或改用柱状图")
    else:
        note(f"WARN 只画了前 {top} 项，被丢掉的 {len(dropped)} 项：{dropped[:10]}")
    return head


def limit_series(frame: pd.DataFrame, top: int) -> pd.DataFrame:
    """折线图的 --top 截的是序列（列）。时间点不能丢，丢了趋势就是假的。"""
    if top <= 0 or frame.shape[1] <= top:
        return frame
    order = frame.sum().sort_values(ascending=False)
    keep = list(order.index[:top])
    dropped = list(order.index[top:])
    note(f"WARN 只画了合计最大的 {top} 条序列，被丢掉的 {len(dropped)} 条：{dropped[:10]}")
    return frame[keep]


def check_scale(frame: pd.DataFrame) -> None:
    """量级差太多要拆图。双 Y 轴的刻度可以任意缩放，等于能画出任何想要的相关性。"""
    if frame.shape[1] < 2:
        return
    peaks = frame.abs().max().replace(0, pd.NA).dropna()
    if len(peaks) < 2:
        return
    if float(peaks.max()) / float(peaks.min()) >= SCALE_GAP:
        note(
            f"WARN 序列量级差 {float(peaks.max()) / float(peaks.min()):.0f} 倍，"
            "小的那条会被压平：拆成两张图，不要用双 Y 轴"
        )


def draw_line(
    frame: pd.DataFrame, times: pd.Series, ax: plt.Axes
) -> tuple[int | None, pd.DataFrame]:
    """画折线，并把按时间排好序的表返回去——环比、首末变化要在有序数据上算。"""
    if frame.shape[1] > LINE_MAX_SERIES:
        return (
            fail(
                f"{frame.shape[1]} 条序列超过 {LINE_MAX_SERIES} 条，折线会糊成一团："
                f"先按维度聚合，或者用 --top {LINE_MAX_SERIES} 显式只画前几条"
            ),
            frame,
        )

    stamps = pd.Series(list(times))
    if stamps.isna().any():
        bad = stamps.isna().to_numpy()
        note(f"WARN {int(bad.sum())} 行的时间解析不了，已从折线里去掉")
        frame = frame.loc[~bad]
        stamps = stamps[~bad].reset_index(drop=True)
    if len(frame) < LINE_MIN_POINTS:
        return (
            fail(
                f"只有 {len(frame)} 个时间点，不足 {LINE_MIN_POINTS} 个，画不出趋势："
                "改成 column 做两期对比"
            ),
            frame,
        )

    order = np.argsort(stamps.to_numpy(), kind="stable")
    if list(order) != list(range(len(order))):
        note("WARN 输入没按时间排序，出图时已排序；SQL 里应该补 ORDER BY")
    frame = frame.iloc[order]
    xs = stamps.to_numpy()[order]

    gaps = pd.Series(xs).diff().dropna()
    if len(gaps) > 1 and gaps.max() > gaps.median() * 1.5:
        note(f"WARN 时间轴有断档（最大间隔 {gaps.max()}，常见间隔 {gaps.median()}）：断档不是 0")

    for name in frame.columns:
        ax.plot(xs, frame[name].to_numpy(), marker="o", linewidth=1.8, label=name)
    if frame.shape[1] > 1:
        ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.3)

    labels = fallback_labels([str(v) for v in frame.index])
    if len(labels) <= 12:
        # 点少时用原始标签打刻度，免得 matplotlib 插出 2026-01-15 这种没有数据的刻度
        ax.set_xticks(list(xs))
        ax.set_xticklabels(labels)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    return None, frame


def draw_column(frame: pd.DataFrame, ax: plt.Axes) -> None:
    if len(frame) > COLUMN_MAX:
        note(f"WARN {len(frame)} 个类别超过 {COLUMN_MAX} 个：横向 bar 更好读")
    labels = fallback_labels(list(frame.index))
    frame.plot(kind="bar", ax=ax, width=0.7, legend=frame.shape[1] > 1)
    ax.set_xticklabels(labels, rotation=30 if max(len(s) for s in labels) > 4 else 0, ha="right")
    ax.set_ylim(bottom=0)  # 截断轴会把 3% 的差异画成一倍差
    ax.grid(axis="y", alpha=0.3)
    if frame.shape[1] == 1 and len(frame) <= LABEL_MAX:
        for patch, value in zip(ax.patches, frame.iloc[:, 0], strict=False):
            ax.annotate(
                f"{value:,.4g}",
                (patch.get_x() + patch.get_width() / 2, patch.get_height()),
                ha="center",
                va="bottom",
                fontsize=8,
            )


def draw_bar(frame: pd.DataFrame, ax: plt.Axes) -> int | None:
    if len(frame) > BAR_MAX:
        return fail(
            f"{len(frame)} 个类别超过 {BAR_MAX} 个，一张图放不下："
            f"用 --top {BAR_MAX} 显式截断，或改成表格"
        )
    # 排名从大到小、最长的在上
    frame = frame.loc[frame.sum(axis=1).sort_values(ascending=False).index]
    labels = fallback_labels(list(frame.index))
    frame.plot(kind="barh", ax=ax, legend=frame.shape[1] > 1)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlim(left=0)
    ax.grid(axis="x", alpha=0.3)
    return None


def draw_pie(frame: pd.DataFrame, ax: plt.Axes) -> int | None:
    if frame.shape[1] > 1:
        note(f"WARN 饼图只画一个指标，用了 {frame.columns[0]}，其余忽略")
    values = frame.iloc[:, 0]
    if (values < 0).any():
        return fail("有负值，饼图画不了构成：改成 column")
    if len(values) > PIE_MAX:
        return fail(
            f"{len(values)} 个类别超过 {PIE_MAX} 个：用 --top {PIE_MAX} 把尾部合成「其他」，"
            "或者改成横向 bar"
        )
    if float(values.sum()) <= 0:
        return fail("合计为 0 或负，占比没有意义")
    labels = fallback_labels(list(values.index))
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90, counterclock=False)
    ax.axis("equal")
    note("提示 饼图只在各项构成同一个整体时成立；平均值、比率类指标加起来没有意义")
    return None


def highlights(frame: pd.DataFrame, chart: str) -> None:
    """把差距、变化率算出来打到 stdout。分析要点名数字，不能只说「存在差异」。"""
    print("\n== 要点 ==")
    print(f"{len(frame)} 行 × {frame.shape[1]} 个序列")
    for name in frame.columns:
        col = frame[name].dropna()
        if col.empty:
            print(f"{name}: 全空")
            continue
        top_i, low_i = col.idxmax(), col.idxmin()
        line = f"{name}: 最高 {top_i}={col.max():,.4g}，最低 {low_i}={col.min():,.4g}"
        if col.min() > 0:
            line += f"，{col.max() / col.min():.2f} 倍"
        line += f"，极差 {col.max() - col.min():,.4g}，合计 {col.sum():,.4g}"
        print(line)

        if chart == "line" and len(col) >= 2:
            first, last = col.iloc[0], col.iloc[-1]
            prev = col.iloc[-2]
            if first:
                print(f"  首末变化 {(last - first) / abs(first) * 100:+.2f}%（{first:,.4g} → {last:,.4g}）")
            if prev:
                print(f"  环比 {(last - prev) / abs(prev) * 100:+.2f}%")
            std = col.std()
            if std and std > 0:
                sigma = (last - col.mean()) / std
                verdict = "在 ±1.5σ 内，先当噪声" if abs(sigma) <= 1.5 else "超出 ±1.5σ，值得追"
                print(f"  末值偏离均值 {sigma:+.2f}σ，{verdict}")
        elif chart == "pie" and col.sum() > 0:
            share = (col.sort_values(ascending=False) / col.sum() * 100).head(3)
            head = "，".join(f"{k} {v:.1f}%" for k, v in share.items())
            print(f"  前 {len(share)} 项占比：{head}（合计 {share.sum():.1f}%）")


def main() -> int:
    parser = argparse.ArgumentParser(description="把 SQL 结果画成一张图")
    parser.add_argument("--input", required=True, help="结果文件：csv / tsv / xlsx")
    parser.add_argument("--out", required=True, help="输出图片路径（.png / .svg / .pdf）")
    parser.add_argument(
        "--chart", required=True, choices=["line", "bar", "column", "pie", "table"], help="图表类型"
    )
    parser.add_argument("--x", help="X 轴/类别列，默认第一列")
    parser.add_argument("--y", help="数值列，逗号分隔；默认所有数值列")
    parser.add_argument("--series", help="长表的序列维度列，会展开成多条线/多组柱")
    parser.add_argument("--sheet", help="Excel 工作表名")
    parser.add_argument("--title", default="", help="图标题")
    parser.add_argument("--xlabel", default="", help="X 轴名，默认用列名")
    parser.add_argument("--ylabel", default="", help="Y 轴名，默认用列名")
    parser.add_argument("--top", type=int, default=0, help="只画前 N 项，被丢掉的会打出来")
    parser.add_argument("--width", type=float, default=9.0, help="画布宽（英寸）")
    parser.add_argument("--height", type=float, default=5.0, help="画布高（英寸）")
    args = parser.parse_args()

    if args.chart == "table":
        return fail("表格不出图：把结果按 markdown 表格写进正文，超过 50 行说明总行数")

    path = Path(args.input).expanduser()
    if not path.is_file():
        print(f"读不到文件：{path}", file=sys.stderr)
        return 2
    try:
        df = load(path, args.sheet)
    except Exception as exc:  # 读不进来就停，不要出一张空图
        print(f"读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if df.empty or not len(df.columns):
        print(f"结果里没有数据行（{len(df)} 行 × {len(df.columns)} 列）", file=sys.stderr)
        return 2

    setup_font()
    picked = pick_columns(df, args.x, args.y, args.series)
    if picked is None:
        return 1
    x, y, series = picked

    frame = shape_frame(df, x, y, series)
    # 折线的 X 轴是时间，--top 该截序列；其余类型的 X 轴是类别，截的是类别
    if args.chart == "line":
        frame = limit_series(frame, args.top)
    else:
        frame = limit_top(frame, args.top, lump=args.chart == "pie")
    if frame.empty:
        print("整理后没有可画的数据", file=sys.stderr)
        return 2
    check_scale(frame)

    times = None
    if args.chart == "line":
        parsed = as_time(pd.Series(frame.index))
        if parsed is None:
            return fail(
                f"X 轴 {x!r} 不像时间，折线图会把无序的类别连成假趋势："
                "改成 column（分类对比）或 bar（排名）"
            )
        times = parsed

    fig, ax = plt.subplots(figsize=(args.width, args.height))
    rc: int | None = None
    if args.chart == "line":
        rc, frame = draw_line(frame, times, ax)
    elif args.chart == "column":
        draw_column(frame, ax)
    elif args.chart == "bar":
        rc = draw_bar(frame, ax)
    else:
        rc = draw_pie(frame, ax)
    if rc:
        plt.close(fig)
        return rc

    if args.title:
        ax.set_title(safe_text(args.title, "标题"), fontsize=13)
    if args.chart != "pie":
        ax.set_xlabel(safe_text(args.xlabel or (x if args.chart != "bar" else ""), "X 轴名"))
        default_y = args.ylabel or (y[0] if len(y) == 1 and not series else "")
        ax.set_ylabel(safe_text(default_y, "Y 轴名"))
        if args.chart == "bar":  # 横向图的量在 X 轴上
            ax.set_xlabel(safe_text(args.ylabel or (y[0] if len(y) == 1 else ""), "X 轴名"))
            ax.set_ylabel("")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)

    print(f"图已出：{out}")
    print(f"类型 {args.chart}，X={x}，序列={list(frame.columns)}")
    highlights(frame, args.chart)
    if NOTES:
        print("\n== 提醒 ==")
        for item in NOTES:
            print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
