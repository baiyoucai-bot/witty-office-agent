# pandas 静默出错

会算出错数字但**不报错**的写法。报错的问题会自己暴露，这里只收不暴露的。

下面的行为在沙箱 pandas 3.0.5 上实测过。换版本先自己验一遍，不要照抄结论。

## 合并放大

主键不唯一时，`merge` 会让行数和金额一起变多：

```python
left  = pd.DataFrame({"id": [1, 2],       "amt": [10, 20]})
right = pd.DataFrame({"id": [1, 1, 2],    "tag": ["x", "y", "z"]})
left.merge(right, on="id")        # 2 行变 3 行，amt 合计 30 变 40
```

金额、电量、工时被复制成多份，报表总量凭空长大。合并前后都要核对行数。

```python
left.merge(right, on="id", validate="one_to_one")   # 不满足就抛 MergeError
```

`validate` 传 `one_to_one` / `one_to_many` / `many_to_one`，让它替你报错。这是最省事的一道防线。

`how="left"` 也不保证行数不变 —— 它只保证左表的键都在，右表一对多照样放大。

## 空值悄悄消失

| 写法 | 空值去哪了 |
|------|-----------|
| `df.groupby("k").sum()` | 键为空的行**整组丢掉**，不出现在结果里 |
| `df["k"].value_counts()` | 不计空值，占比分母也跟着变小 |
| `df["k"].nunique()` | 不把空值算作一类 |
| `df.pivot_table(...)` | 默认 `dropna=True`，空值组消失 |

都用 `dropna=False` 才能看见。分类汇总时这一条最坑：分组小计加不回总计，通常就是键有空值。

反方向的坑：

```python
pd.Series([np.nan, np.nan]).sum()    # 0.0 —— 全空求和给 0，不是 NaN
pd.Series([np.nan, np.nan]).mean()   # nan
```

「合计 0」和「没有数据」在报表上长得一样。全空列的合计不能直接当 0 汇报。

## 读文件时类型就错了

```python
pd.read_csv(io.StringIO("id\n007\n012"))["id"].iloc[0]   # 7，前导 0 丢了
pd.read_csv(io.StringIO('v\n"1,234"'))["v"].dtype        # str，千分位让整列变文本
```

编号类字段（户号、工单号、身份证、组织机构代码）必须显式指定字符串：

```python
pd.read_csv(path, dtype={"id": "string", "org_code": "string"})
pd.read_csv(path, thousands=",")          # 带千分位的数值列
```

前导 0 丢了之后连接就对不上，而且不报错 —— 表现是「莫名其妙有一批记录连不上」。

日期不要指望自动识别：

```python
pd.to_datetime(pd.Series(["2026-03-01", "01/02/2026"]))   # ValueError
```

混格式会抛错（这算好事）。真正的坑是 `01/02/2026` 单独出现时，到底是 1 月 2 日还是 2 月 1 日 —— 显式传 `format=`，别猜。

`astype(int)` 是截断不是四舍五入：`1.9 → 1`。要四舍五入用 `round()` 再转。

## 比率不能求平均

```python
g = pd.DataFrame({"region": ["A", "B"], "hit": [1, 90], "total": [2, 200]})
g["rate"] = g.hit / g.total
g.rate.mean()                    # 0.475  ← 把 2 个样本的组和 200 个的组等权
g.hit.sum() / g.total.sum()      # 0.4505 ← 正确的总体比率
```

合格率、转化率、故障率都是这样：**先把分子分母分别求和，再相除**。对已经算好的比率求平均，等于给小样本组和大样本组一样的话语权。

按维度拆开各组都涨、合起来却跌（或反过来），通常就是结构变了 —— 各组权重发生了变化，不是数据错了。这种情况必须把权重变化一起报出来。

## 偏态数据只报均值

```python
s = pd.Series([10, 20, 1000])
s.mean()          # 343.3  ← 没有一个样本接近这个数
s.median()        # 20.0
s.quantile(0.9)   # 804.0
```

一条极端值就能把均值拖走。偏度超线的列（见 `thresholds.md`）报中位数和分位数。

## 改了没生效

pandas 3.0 默认 copy-on-write，对切片赋值**既不报错也不生效**：

```python
sub = df[df.a > 1]
sub["b"] = 0        # 无警告
df.b                # 原值不变
```

旧版本会给 `SettingWithCopyWarning`，现在连提示都没有。要改原表就直接在原表上用 `.loc` 定位：

```python
df.loc[df.a > 1, "b"] = 0
```

要单独一份就 `.copy()` 后再改，明确说清自己在改哪一份。

## 时间聚合补出来的 0

```python
df.resample("D").sum()     # 缺的那天补成 0，不是留空
```

补出来的 0 会被后续的均值、环比、同比当成真实的「那天为 0」。上游停机造成的缺口和真实的 0 必须分开处理 —— 先 `asfreq()` 看清哪些日期原本没有数据。

## 顺序和排名

- `sort_values` 默认 quicksort，并列的相对顺序不稳定。做 top-N 或去重取首条时传 `kind="mergesort"`。
- `drop_duplicates` 保留的是**当前顺序**的第一条，不是「最新的一条」。要取最新先按时间排。
- `groupby` 默认按键排序，`sort=False` 保留出现顺序。做对账时两者结果的行序不同，逐行比对会全部错位。

## 浮点相等

`0.1 + 0.2 == 0.3` 是 `False`。对账、核对合计不要用 `==`：

```python
abs(a - b) < 0.01          # 按业务精度定容差
np.isclose(a, b)
```

金额建议按分（整数）算，或明确保留位数后再比。

## 自查

改完数据、出结论前，至少确认这几条：

- 合并前后行数变化能解释。
- 分组小计加得回总计（加不回先查空值键）。
- 编号类列还是字符串，前导 0 还在。
- 比率是分子分母分别汇总后再除的。
- 关键数字换一条路径算出来一样。
