# xlsx 静默陷阱

这些错文件都能打开，WPS / Excel 不报错。本技能的 `check_xlsx.py` 就是对着它们写的。

## 1. pandas.to_excel 把公式写成死数字

`df.to_excel("out.xlsx")` 只写值。原稿 `D2=B2*C2`，导一遍之后 D2 是 `20`，公式没了。
改已有工作簿**禁止**整本 to_excel。走 `apply.py`，对照 `--original` 跑 check。

## 2. openpyxl `data_only=True` 再保存

`load_workbook(path, data_only=True)` 读到的是缓存值。这个对象再 `save()`，公式全部消失。
本技能的 inspect / apply **不经过 openpyxl**，解 zip 读 `<f>`。

## 3. 公式 XML 里没有 `=`

OOXML 的 `<f>B2*C2</f>` 不带等号。spec 里写成 `"=B2*C2"` 也可以，脚本会剥掉。
自己手写 XML 时不要带 `=`，否则 Excel 当非法公式，格子显示 `#NAME?`。

## 4. 共享字符串改一处等于改所有引用

`t="s"` 的值是 sharedStrings.xml 的下标。改那一个字符串，所有指向它的格子一起变。
`apply.py` 写文本一律用 `inlineStr`，不碰共享表。

## 5. 日期是序列，不是字符串

Excel 把 2026-08-25 存成 `45928` 之类的序列。当文本写进去，透视和相减会全错。
本技能不自动猜日期。要日期就写数字序列，或在 Excel 里设单元格格式。

## 6. 合并区只能写左上角

往 `A1:C1` 的 B1 写字，文件能打开，WPS 里看不见。check 对合并区报 WARN。

## 7. 缓存值不是现算值

没有重算时 `<v>` 可能是旧的。要核数字：本机有 LibreOffice 就 `recalc.py`，没有就打开 Excel/WPS。
`check_xlsx` 只信「有没有公式」和「缓存是不是 `#REF!`」，不拿缓存当现算结果。

## 8. `.xls` / `.xlsb` 不是同一套包

本技能只认 Office Open XML 的 `.xlsx` / `.xlsm`。老 `.xls` 走 `office-document` 的 `convert_legacy.py`。

## 9. 图表必须最后加

`chart.py` 走 openpyxl，重写整本会丢掉输入里已有的图表和图片。先 `write` / `apply`，最后再加图。输入已有绘图层时脚本拒绝，除非 `--force`。

## 10. 透视表和 VBA 做不了

openpyxl 写不出可用的数据透视，也不能跑宏。需要透视时在 Excel/WPS 里做，或用 SUMIF 做一张汇总 sheet 冒充，不要假装写出了透视缓存。
