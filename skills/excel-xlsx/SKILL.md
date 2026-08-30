---
name: excel-xlsx
description: 生成和改已有 .xlsx 工作簿，默认保住公式、合并区和多 sheet；可在最后一步加原生图表和条件格式。用户点名 xlsx、工作簿、填报、改格子、公式保全、xlsx图表 时使用。不要用于 CSV 统计分析或表头空列检查。
network: intranet
metadata:
  triggers: xlsx 工作簿 填报 改格子 公式保全 excel-xlsx xlsx图表 条件格式
---

# Excel XLSX

产出 **Excel / WPS 里能改、公式还在的 `.xlsx`**。分析 CSV 走 `data-analysis`；CSV 质检走 `table-qa`。

## 开始前

确认是工作簿（多 sheet / 公式 / 填报）而不是「看一眼这张表的数」。缺会改变格子的事实才问。

**公式错看不出来。** pandas 导一遍、openpyxl `data_only=True` 再保存，格子里的数字还在，公式没了，文件照样打开。先看 `check_xlsx.py` 的退出码。

## 生成

```bash
<沙箱 Python> <技能目录>/scripts/write.py --spec <表.json> --output <路径.xlsx>
```

`--help-spec` 打印字段。以 `=` 开头的格子写成公式，不要先在 Python 里算完再写入。

## 改已有工作簿

先只读看结构：

```bash
<沙箱 Python> <技能目录>/scripts/inspect.py --input 原稿.xlsx
```

再按 spec 改指定格子（默认**拒绝**把公式格写成值）：

```bash
<沙箱 Python> <技能目录>/scripts/apply.py --input 原稿.xlsx --output 改过.xlsx --spec 改动.json
```

`--help-spec` 打印字段。真要把公式格改成死数字，必须 `overwrite_formula=true`。

**不要** `pandas.DataFrame.to_excel` 覆盖别人的工作簿。陷阱见 `references/xlsx-traps.md`。

## 校验

```bash
<沙箱 Python> <技能目录>/scripts/check_xlsx.py --input 改过.xlsx --original 原稿.xlsx
```

不带 `--original` 只查空表名、重名、`#REF!` 一类错误缓存、合并区。带上原稿才查「公式被值覆盖」。

退出码 0 才算过。FAIL 必修；WARN 逐条判断。

## 图表和条件格式

必须放在**最后一步**：openpyxl 重写会丢掉输入里已有的图表/图片。

```bash
<沙箱 Python> <技能目录>/scripts/chart.py --input 表.xlsx --output 表-图.xlsx --spec 图.json
```

`--help-spec` 打印字段。认柱状 / 条形 / 折线 / 饼，以及色阶、数据条、单元格条件、公式条件格式。输入里已有图表时拒绝，除非 `--force`。透视表和 VBA 做不了（openpyxl 无此能力），不要假装能做。

## 重算（可选）

openpyxl 和本技能的 write 都只写公式字符串。要在无 GUI 环境核缓存值：

```bash
<沙箱 Python> <技能目录>/scripts/recalc.py --input 表.xlsx --output 表-算过.xlsx
```

本机没有 `soffice`，只有 `libreoffice`。没有 LibreOffice 时脚本退出 2，打开 Excel/WPS 也会自动算，不要假装算过。

## 交付

报输出路径、`check_xlsx` 结论、未决项。不要复述过程。
