---
name: data-analysis
description: Complete data-analysis tasks with bounded inspection, correct grain/units, native artifacts, and risk-based verification. Use when analyzing tables, CSV, Excel, 数据分析, 统计, 画像, 归因, or spreadsheets.
network: general
metadata:
  triggers: 数据分析 统计 画像 归因 离群 缺失值 csv excel data-analysis
---

# Data Analysis

交付被要求的结果和产物。不要把任务做成证明题，也不要额外堆用户没要的中间文件。

## 开始前

需要具体任务、输入、交付路径和格式。缺信息会改变产物时才问。

## 约定

先读任务和数据说明，标出每个输出路径/格式，以及会改变结果的定义：范围、粒度、键、单位、算子、排序、覆盖、格式。例子默认只是示例，除非任务写明是规范。

## 有界探查

大文件先做清单、schema、抽样或窄查询。只有会改变选择/变换/计算时才扩大探查。

一步拿到画像，不要边猜边试：

```bash
<沙箱 Python> <技能目录>/scripts/profile_table.py --input <数据文件>
```

输出结构、空值、重复、离群、偏度、相关、时间覆盖，并按 `references/thresholds.md` 判 OK / WARN / FAIL。只看表头空列和重复列名走 `table_qa` 工具，更省。

判定要用数，不要用「看起来还行」。阈值和例外见 `references/thresholds.md`；逐项核对见 `references/eda-checklist.md`。

## 数据语义

在正确的行或实体粒度上计算。合取条件必须落在同一条记录上。保留空值、排除规则和明确禁止。枚举输出要覆盖被要求的全集。

先确认一行代表什么，再动手聚合。合并后行数变了就是键不唯一，不是数据变多了。会静默给出错数字的写法见 `references/pandas-traps.md`。

## 结论口径

比较、显著性、趋势、归因的口径见 `references/stats-guide.md`。红线：

- 相关不写成因果。要写因果得有实验、自然实验或明确的混淆讨论。
- 只给点估计不算结论，带上区间或波动范围。
- 显著不等于重要。给出效应量和业务含义。
- 指标变化先和自身波动比，在 ±1.5 个标准差内先当噪声。

## 原生产物

保持被要求的类型和结构。正确性依赖电子表格公式、重算、版式或导出行为时，优先走能保留这些性质的工具路径。

试验脚本和中间产物写沙箱（路径用 `sandbox/…`），用沙箱 Python 跑，预装 numpy / pandas / openpyxl / matplotlib。交付物写用户指定的路径。

要改 `.xlsx` 工作簿、保住公式走 `excel-xlsx`。要写成报告、纪要走 `office-document`；要网页演示走 `slides`；要可编辑 pptx 走 `witty-ppt-skills`。本技能只出分析和数据产物。

## 核查

按会改变答案的风险选最小独立检查。复核实际产物的路径、格式、结构和值，列出输出路径后停止。

至少换一条路径复算关键数字：分组求和对总量、抽样几行手算、口径换一种写法看结论是否翻转。数字来源、排除规则和未核实项写进交付物，不要只留在对话里。
