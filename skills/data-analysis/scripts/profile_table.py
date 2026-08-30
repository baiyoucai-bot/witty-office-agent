"""一条命令拿到表画像：结构、空值、重复、离群、偏度、相关、时间覆盖。

只读，不改输入文件。判定线见同技能 references/thresholds.md。

用沙箱解释器跑：

    <沙箱 Python> profile_table.py --input data.csv
    <沙箱 Python> profile_table.py --input data.xlsx --sheet 明细 --key 户号
    <沙箱 Python> profile_table.py --input data.csv --warn-pct 15 --fail-pct 60

退出码 0 表示没有 FAIL；1 表示有 FAIL；2 表示读不进来。
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

WARN_PCT = 5.0
FAIL_PCT = 30.0
DUP_FAIL_PCT = 1.0
SKEW_NOTE = 1.0
SKEW_WARN = 2.0
CORR_FLAG = 0.8
CORR_SAME = 0.95
DOMINANT_PCT = 90.0
MIN_CORR_ROWS = 30
# 常见的「未知」编码，当成数值算会污染统计量
SENTINELS = (-1, -999, 9999, 99999, -9999)


def load(path: Path, sheet: str | None) -> pd.DataFrame:
    """按后缀读表。编号类列的类型问题由 --string-cols 显式兜住。"""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path, sheet_name=sheet or 0)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in {".tsv", ".tab"}:
        return pd.read_csv(path, sep="\t", encoding="utf-8-sig")
    return pd.read_csv(path, encoding="utf-8-sig")


def load_as_text(path: Path, sheet: str | None, rows: int = 500) -> pd.DataFrame | None:
    """把前几行按纯文本再读一遍，用来看原始写法（前导 0、千分位）。

    parquet 自带类型，不存在读入丢格式的问题，跳过。
    """
    suffix = path.suffix.lower()
    try:
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            return pd.read_excel(path, sheet_name=sheet or 0, dtype=str, nrows=rows)
        if suffix == ".parquet":
            return None
        sep = "\t" if suffix in {".tsv", ".tab"} else ","
        return pd.read_csv(path, sep=sep, dtype=str, nrows=rows, encoding="utf-8-sig")
    except Exception:
        return None


def raw_format(df: pd.DataFrame, text: pd.DataFrame | None) -> list[str]:
    """读入时就被改掉的写法。这类问题后面每一步都对不上，且不报错。"""
    findings: list[str] = []
    if text is None:
        return findings
    for name in text.columns:
        if name not in df.columns:
            continue
        col = text[name].dropna().astype(str)
        if col.empty:
            continue
        if col.str.match(r"^0\d+$").mean() > 0.5:
            if df[name].dtype.kind in "iuf":
                findings.append(
                    f"FAIL {name} 原文带前导 0 却被读成 {df[name].dtype}：前导 0 已丢，"
                    f"连接会对不上。加 --string-cols {name}"
                )
        if col.str.match(r"^-?[\d,]+\.?\d*$").mean() > 0.5 and col.str.contains(",").any():
            if df[name].dtype.kind in "OU" or str(df[name].dtype) == "str":
                findings.append(f"WARN {name} 带千分位逗号，整列被读成文本：读取时加 thousands=','")
    return findings


def coerce_dates(df: pd.DataFrame) -> list[str]:
    """把看着像日期的文本列转成日期，否则时间检查整节会静默跳过。

    全数字的串（编号、年份）不试 —— to_datetime 会把 '001' 也解析成日期。
    """
    notes: list[str] = []
    for name in df.columns:
        col = df[name]
        if col.dtype.kind in "iufMm" or col.isna().all():
            continue
        sample = col.dropna().astype(str)
        if sample.empty or sample.str.fullmatch(r"\d+").mean() > 0.5:
            continue
        if not sample.str.contains(r"[-/:年月日]").mean() > 0.5:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(col, errors="coerce")
        ok = parsed.notna().mean()
        if ok >= 0.8:
            df[name] = parsed
            if ok < 1.0:
                notes.append(f"WARN {name} 按日期解析，{round((1 - ok) * 100, 2)}% 解析不了")
    return notes


def section(title: str) -> None:
    print(f"\n== {title} ==")


def structure(df: pd.DataFrame, sample_n: int) -> list[str]:
    findings: list[str] = []
    section("结构")
    mem = df.memory_usage(deep=True).sum() / 1_048_576
    print(f"{df.shape[0]:,} 行 × {df.shape[1]} 列，{mem:.2f} MB")

    unnamed = [c for c in df.columns if str(c).startswith("Unnamed:")]
    if unnamed:
        findings.append(f"FAIL 无名列 {unnamed}：表头行可能不在第一行")
    blank = [i for i, c in enumerate(df.columns, 1) if not str(c).strip()]
    if blank:
        findings.append(f"FAIL 第 {blank} 列表头为空")
    dupes = [str(c) for c in df.columns[df.columns.duplicated()].unique()]
    if dupes:
        findings.append(f"FAIL 重复列名 {dupes}：按名取列会拿错")

    info = pd.DataFrame(
        {
            "类型": df.dtypes.astype(str),
            "非空": df.notna().sum(),
            "空值%": (df.isna().mean() * 100).round(2),
            "取值数": df.nunique(dropna=False),
        }
    )
    print(info.to_string())

    if sample_n > 0 and len(df):
        print(f"\n前 {min(sample_n, len(df))} 行：")
        print(df.head(sample_n).to_string(index=False))
    return findings


def nulls(df: pd.DataFrame, warn_pct: float, fail_pct: float) -> list[str]:
    findings: list[str] = []
    section("空值")
    pct = (df.isna().mean() * 100).round(2).sort_values(ascending=False)
    hit = pct[pct > 0]
    if hit.empty:
        print("无空值")
    else:
        for name, value in hit.items():
            level = "FAIL" if value >= fail_pct else "WARN" if value >= warn_pct else "OK"
            print(f"{level:<5} {name}: {value}%")
            if level != "OK":
                findings.append(f"{level} {name} 空值 {value}%")

    # 0 和空值不是一回事：这一列的 0 可能是「没填」的替身
    for name in df.select_dtypes(include="number").columns:
        col = df[name]
        if not len(col):
            continue
        zero_pct = round(float((col == 0).mean() * 100), 2)
        if zero_pct >= 30:
            findings.append(f"WARN {name} 有 {zero_pct}% 的 0：先确认是真值还是缺失替身")
    return findings


def duplicates(df: pd.DataFrame, key: list[str] | None) -> list[str]:
    findings: list[str] = []
    section("重复")
    if not len(df):
        print("空表")
        return findings
    full = int(df.duplicated().sum())
    full_pct = round(full / len(df) * 100, 2)
    print(f"整行重复 {full} 行（{full_pct}%）")
    if full_pct > DUP_FAIL_PCT:
        findings.append(f"FAIL 整行重复 {full_pct}%")
    elif full:
        findings.append(f"WARN 整行重复 {full} 行")

    if key:
        missing = [c for c in key if c not in df.columns]
        if missing:
            findings.append(f"FAIL 指定的键 {missing} 不在表里")
            return findings
        dup = int(df.duplicated(subset=key).sum())
        print(f"键 {key} 重复 {dup} 行")
        if dup:
            findings.append(f"FAIL 键 {key} 不唯一（{dup} 行）：合并会放大金额和行数")
        return findings

    # 没给键就找找有没有天然唯一列，方便下一步指定
    unique_cols = [
        str(c)
        for c in df.columns
        if df[c].notna().all() and df[c].nunique() == len(df)
    ]
    print("候选唯一列：" + (", ".join(unique_cols) if unique_cols else "无"))
    if not unique_cols:
        findings.append("WARN 没有天然唯一列：合并前先确认粒度，用 --key 复核")
    return findings


def numeric(df: pd.DataFrame) -> list[str]:
    findings: list[str] = []
    num = df.select_dtypes(include="number")
    if num.empty:
        section("数值分布")
        print("无数值列")
        return findings

    section("数值分布")
    stats = num.describe(percentiles=[0.05, 0.5, 0.9, 0.95]).T
    stats["skew"] = num.skew()
    print(stats.round(3).to_string())

    section("离群与偏度")
    for name in num.columns:
        col = num[name].dropna()
        if col.empty:
            findings.append(f"FAIL {name} 整列为空")
            continue

        q1, q3 = col.quantile([0.25, 0.75])
        iqr = q3 - q1
        if iqr > 0:
            outside = col[(col < q1 - 1.5 * iqr) | (col > q3 + 1.5 * iqr)]
            if len(outside):
                pct = round(len(outside) / len(col) * 100, 2)
                print(
                    f"{name}: IQR 外 {len(outside)} 个（{pct}%），"
                    f"范围 [{outside.min()}, {outside.max()}]"
                )
                findings.append(f"WARN {name} 离群 {len(outside)} 个：逐个归类，不要直接删")

        std = col.std()
        if std and std > 0:
            far = col[(col - col.mean()).abs() / std > 3]
            if len(far):
                print(f"{name}: |z|>3 有 {len(far)} 个")

        skew = col.skew()
        if pd.notna(skew) and abs(skew) > SKEW_NOTE:
            level = "WARN" if abs(skew) > SKEW_WARN else "OK"
            print(f"{name}: 偏度 {skew:.2f}，均值 {col.mean():.2f} / 中位数 {col.median():.2f}")
            note = f"{name} 偏度 {skew:.2f}：报中位数和分位数，不要只报均值"
            findings.append(f"{level} {note}" if level == "WARN" else f"提示 {note}")

        hit = sorted({int(s) for s in SENTINELS if (col == s).any()})
        if hit:
            findings.append(f"WARN {name} 含哨兵值 {hit}：可能是「未知」编码，别当数值算")

        if (col < 0).any() and col.min() < 0:
            findings.append(f"提示 {name} 有负值（最小 {col.min()}）：确认负值代表什么")
    return findings


def categorical(df: pd.DataFrame) -> list[str]:
    findings: list[str] = []
    cols = [c for c in df.columns if c not in df.select_dtypes(include="number").columns]
    if not cols:
        return findings
    section("分类分布")
    for name in cols:
        col = df[name]
        n = col.nunique(dropna=False)
        if n > 50:
            print(f"{name}: {n} 个取值（高基数，跳过明细）")
            continue
        # dropna=False：空值也是一类，否则占比分母会缩水
        counts = col.value_counts(dropna=False)
        top = counts.head(5)
        print(f"{name}: {n} 个取值 | " + ", ".join(f"{k}={v}" for k, v in top.items()))
        if len(df):
            share = round(counts.iloc[0] / len(df) * 100, 2)
            if share >= DOMINANT_PCT:
                findings.append(
                    f"WARN {name} 单一取值 {counts.index[0]!r} 占 {share}%：这列几乎没有区分度"
                )
    return findings


def correlations(df: pd.DataFrame) -> list[str]:
    findings: list[str] = []
    num = df.select_dtypes(include="number")
    if num.shape[1] < 2:
        return findings
    section("相关（仅数值列，仅线性）")
    if len(df) < MIN_CORR_ROWS:
        print(f"只有 {len(df)} 行，不足 {MIN_CORR_ROWS} 行，相关系数没有意义，跳过")
        return findings
    corr = num.corr(numeric_only=True)
    pairs: list[tuple[str, str, float]] = []
    for i, a in enumerate(corr.columns):
        for b in corr.columns[i + 1 :]:
            r = corr.loc[a, b]
            if pd.notna(r) and abs(r) >= CORR_FLAG:
                pairs.append((str(a), str(b), float(r)))
    if not pairs:
        print(f"没有 |r| ≥ {CORR_FLAG} 的列对")
        return findings
    for a, b, r in sorted(pairs, key=lambda x: -abs(x[2])):
        print(f"{a} ~ {b}: r={r:.3f}")
        if abs(r) >= CORR_SAME:
            findings.append(f"WARN {a} 与 {b} r={r:.3f}：基本是同一个量，留一个")
        else:
            findings.append(f"提示 {a} 与 {b} r={r:.3f}：不要同时进模型")
    return findings


def timeline(df: pd.DataFrame) -> list[str]:
    findings: list[str] = []
    cols = list(df.select_dtypes(include=["datetime", "datetimetz"]).columns)
    if not cols:
        return findings
    section("时间覆盖")
    today = pd.Timestamp.today().normalize()
    for name in cols:
        col = df[name].dropna()
        if col.empty:
            continue
        print(f"{name}: {col.min()} → {col.max()}（{col.nunique()} 个不同取值）")
        if col.max() > today + pd.Timedelta(days=1):
            findings.append(f"FAIL {name} 有未来日期（最大 {col.max()}）")
        if col.min().year < 1970:
            findings.append(f"WARN {name} 最小值 {col.min()}：可能是哨兵日期")
        # 按天计数找断档：上游停了和真实的 0 长得不一样
        daily = col.dt.normalize().value_counts().sort_index()
        if len(daily) > 2:
            span = (daily.index[-1] - daily.index[0]).days + 1
            gaps = span - len(daily)
            if gaps > 0:
                findings.append(f"WARN {name} 跨 {span} 天但只有 {len(daily)} 天有数据（缺 {gaps} 天）")
    return findings


def report(findings: list[str]) -> int:
    section("结论")
    fails = [f for f in findings if f.startswith("FAIL")]
    warns = [f for f in findings if f.startswith("WARN")]
    notes = [f for f in findings if f.startswith("提示")]
    for group in (fails, warns, notes):
        for item in group:
            print(item)
    if not findings:
        print("按默认线没有发现问题。仍要自己确认一行代表什么。")
    print(f"\nFAIL {len(fails)} 项 / WARN {len(warns)} 项 / 提示 {len(notes)} 项")
    if fails:
        print("有 FAIL：不解释就不要往下算。")
    print("阈值见 references/thresholds.md，逐项核对见 references/eda-checklist.md")
    return 1 if fails else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="只读表画像")
    parser.add_argument("--input", required=True, help="csv / tsv / xlsx / parquet 路径")
    parser.add_argument("--sheet", help="Excel 工作表名，默认第一个")
    parser.add_argument("--key", help="主键列，逗号分隔；用来查粒度是否唯一")
    parser.add_argument("--string-cols", help="强制按字符串读的列，逗号分隔；编号类字段用它保住前导 0")
    parser.add_argument("--sample", type=int, default=5, help="打印前几行，0 表示不打印")
    parser.add_argument("--warn-pct", type=float, default=WARN_PCT, help=f"空值 WARN 线，默认 {WARN_PCT}")
    parser.add_argument("--fail-pct", type=float, default=FAIL_PCT, help=f"空值 FAIL 线，默认 {FAIL_PCT}")
    args = parser.parse_args()

    path = Path(args.input).expanduser()
    if not path.is_file():
        print(f"读不到文件：{path}", file=sys.stderr)
        return 2
    try:
        df = load(path, args.sheet)
    except Exception as exc:  # 读不进来就停，不要给个半张表继续算
        print(f"读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.string_cols:
        for name in [c.strip() for c in args.string_cols.split(",") if c.strip()]:
            if name in df.columns:
                df[name] = df[name].astype("string")

    print(f"文件：{path}")
    if args.sheet:
        print(f"工作表：{args.sheet}")

    key = [c.strip() for c in args.key.split(",")] if args.key else None
    findings: list[str] = []

    if df.empty or not len(df.columns):
        # 空表要吵，不能算成「没发现问题」
        section("结构")
        print(f"{len(df)} 行 × {len(df.columns)} 列")
        return report([f"FAIL 表里没有数据行（{len(df)} 行 × {len(df.columns)} 列）：先确认读法和路径"])

    findings += raw_format(df, load_as_text(path, args.sheet))
    findings += coerce_dates(df)
    findings += structure(df, args.sample)
    findings += nulls(df, args.warn_pct, args.fail_pct)
    findings += duplicates(df, key)
    findings += numeric(df)
    findings += categorical(df)
    findings += correlations(df)
    findings += timeline(df)
    return report(findings)


if __name__ == "__main__":
    raise SystemExit(main())
