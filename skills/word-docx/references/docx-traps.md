# docx 静默出错

写 `.docx` 时**不报错但结果是错的**写法。会抛异常的问题自己会暴露，这里只收不暴露的。

下面每条都在沙箱 python-docx 1.2.0 + 本机 libreoffice 上实测过，测法一并写了。换版本先自己验一遍，不要照抄结论。

## 中文字体丢了

```python
run.font.name = "仿宋"        # 只写了 ascii 和 hAnsi
```

生成的 XML：

```xml
<w:rFonts w:ascii="仿宋" w:hAnsi="仿宋"/>
```

`w:eastAsia` 没写，中文就走 `docDefaults` 的 `w:eastAsiaTheme="minorEastAsia"`，而默认主题里 `<a:ea typeface=""/>` 是空的——Word 拿它自己的默认中文字体渲染。**结果是 XML 里明明写着仿宋，文档打开是宋体。**

三个属性都要写。`scripts/gongwen.py` 的 `set_font()` 就是干这个的：

```xml
<w:rFonts w:ascii="仿宋" w:hAnsi="仿宋" w:eastAsia="仿宋"/>
```

`add_heading()` / 内置 `Heading N` 样式同病：`w:eastAsiaTheme="majorEastAsia"`，也是空的。公文不要用 `add_heading`，标题自己排。

## 每面少一行

固定行距取 29pt 时每面只排 21 行，不是 22 行。测法：

```bash
# 同一份 22 段的文档，只改 linePitch，转 PDF 数第一页行数
pdftotext -layout x.pdf - | head -30
```

结果：`580`（29.0pt）→ 21 行；`579`（28.95pt）→ 22 行。版心高 637.795pt 装不下 638pt。

差 0.2pt 就掉一行，且没有任何提示。见 `gongwen-format.md`「两个错得最多的参数」。

## 每行少一个字

3 号字每行只排 27 个字，不是 28 个。要压 run 上的 `w:spacing`：

| 设法 | 实测每行字数 |
|------|-------------|
| `w:docGrid charSpace` = 0 / −8 / −12 | 27 / 27 / 27 |
| run `w:rPr/w:spacing w:val` = 0 / −3 / −4 | 27 / 27 / 27 |
| run `w:rPr/w:spacing w:val` = **−5** / −6 | **28** / 28 |

`charSpace` 完全不起作用。数字数时用 Python `len()`：

```bash
pdftotext -layout x.pdf x.txt   # 然后用 python len() 数，不要用 awk length
```

`awk '{print length($0)}'` 数的是字节，一个汉字算三个，每行都会报 81 这种数，看着像是有结论其实什么也没量到。

## 单双页页脚被忽略

```python
section.even_page_footer.paragraphs[0].text = "双页"   # 静默无效
```

不开 `settings.xml` 里的 `w:evenAndOddHeaders`，`even_page_footer` 写了也不生效。实测三页文档的页脚：

| settings.xml | 第 1/2/3 页 |
|--------------|-------------|
| 无 `w:evenAndOddHeaders` | 单 / **单** / 单 |
| 有 `w:evenAndOddHeaders` | 单 / 双 / 单 |

公文单页码居右、双页码居左，少这个开关就全部居右。`gongwen.py` 的 `enable_odd_even()` 补的是这个。

## 页码写死

```python
footer.paragraphs[0].text = "— 1 —"
```

一改分页就全错，而且每一页都印同一个数。页码必须是 `PAGE` 域，python-docx 没有域 API，手写 `w:fldChar` / `w:instrText`，见 `page_field()`。

## pPr 子元素顺序

`w:pPr` 的子元素顺序在 schema 里是固定的，`w:pBdr` 必须排在 `w:shd` / `w:spacing` / `w:ind` / `w:jc` 之前。直接 `pPr.append(pBdr)` 会排到 `w:jc` 后面：

```xml
<w:pPr><w:jc w:val="center"/><w:pBdr>…</w:pBdr></w:pPr>   <!-- 错序 -->
```

实测 LibreOffice 渲染时**这条边框整个不画**（正序的那一段画出来了，错序的那一段没有）。既不报错也不提示，就是少一条线。Word 有时容错、有时不容错，不要赌。用 `_ppr_insert()` 插到正确位置。

## `paragraph.text = ` 清掉格式

```python
p.text = "改后"
```

| | runs 数 | bold | size |
|--|--------|------|------|
| 赋值前 | 1 | `True` | 16pt |
| 赋值后 | 1 | `None` | `None` |

它会删掉所有 run 再建一个裸 run，字体、字号、加粗、字距全丢。改文字要遍历 `p.runs` 逐个改 `run.text`，或者删旧 run 再用 `set_font()` 重建。

## `line_spacing` 传数字是倍数

| 写法 | 生成的 XML |
|------|-----------|
| `line_spacing = 1.5` | `<w:spacing w:line="360" w:lineRule="auto"/>` |
| `line_spacing = Pt(28.95)` | `<w:spacing w:line="579" w:lineRule="exact"/>` |

传裸数字得到的是 `auto`（倍数行距），会随字号变，撑不住每面固定行数。要固定行距必须传 `Pt()`，`line_spacing_rule` 也要显式设成 `EXACTLY`。

## 缩进：磅和「字」都会错，方向不一样

```python
p.paragraph_format.first_line_indent = Pt(32)     # 写成 w:firstLine
```

3 号字下 32pt 恰好是两字，看着没问题；换成 4 号字就变成 2.29 字。所以看起来该用字为单位的
`w:leftChars` / `w:firstLineChars` / `w:hangingChars`（单位 1/100 字）。

**但字单位那套实测渲染不到标准要求的位置。** 同一段文字两种写法各生成一份，转 PDF 用
`pdftotext -bbox` 量首行和回行的左边界（本意：首行左空 2 字，回行对齐 5 字）：

| `w:ind` 属性 | 实测首行 / 回行 |
|-------------|----------------|
| `leftChars="500" hangingChars="300"` | 5.00 / 8.00 字 |
| `left="1600" hanging="960"`（磅，1600tw=5字@16pt） | **2.00 / 5.00 字** |
| 两者都写 | 5.00 / 8.00 字 |
| `leftChars="100"`，run 是 14pt | 1.15 字 |
| `left="280"`，run 是 14pt | 1.01 字 |

两处独立的错：

- **悬挂缩进方向反了**：`hangingChars` 没把首行拉回来，而是加到了回行上。
- **字宽取的不是本 run 的字号**：`leftChars=100` 配 14pt 的 run 渲染成 1.15 字，正好是
  1 × 16/14 —— 用的是 `Normal` 样式的 16pt。四号要素（版记、页码）全部偏 14%。

而且**两套都写时字单位优先**，想靠补一份磅值兜底是无效的。

所以 `indent_chars()` 的入参是字数、写进 XML 的是磅：`twips = 字数 × size_pt × 20`，
`size_pt` 必填不给默认值——四号要素按三号折算就是上面那 14%。代价是事后在 Word 里改字号
缩进不跟着变，公文字号由标准定死，且真改字号也得重算每行 28 字，这个代价可以接受。

`check_docx.py` 的 `check_indent_units()` 查的是「有没有出现 `w:*Chars`」。磅值算得对不对
它查不出来——那个只有渲染出来量才知道。

## 中西混排时半角字宽不是 0.5

按「一个汉字算 1、半角算 0.5」估文本宽度，用来算对齐位置，会偏。实测「2026年8月20日」
里的阿拉伯数字渲染出来约 0.64 字宽，7 个数字就多出 1 个字。

成文日期要求「首字比署名首字右移二字」。常见写法是右对齐加右缩进 `署名宽 − 日期宽`，
它要知道日期自己多宽，于是被数字字宽带跑——实测只右移了 1 字。改成左对齐按左缩进定位：

```python
left = 版心字数 - cell_units(署名)      # 只依赖署名，机关名称是纯汉字
```

日期宽度不进公式，目标机器换西文字体也不影响。**凡是要对齐的地方，都优先找一个纯汉字的
锚点去量，不要把西文宽度算进去。**

## 用 pandoc 过一遍，版式全没了

```bash
pandoc --track-changes=accept 送审稿.docx -o 定稿.docx    # 文字对了，版式全丢
```

pandoc 是「解析成 AST 再重新生成」，不是在 XML 上改。一份 29 段的公文过一遍：
段数 29→20、起间隔作用的空段 9→**0**、`sectPr` **整个丢失**（纸张/版心/网格全无）、
压字距的 run 11→**0**、`w:eastAsia` 25→**0**。

打开一看文字都在，所以很容易当成功了。**pandoc 只能用来读**（`pandoc -t markdown`）。
要接受修订用 `scripts/revise.py --accept`，它只动 `word/document.xml` 里的留痕节点，
其余部件按原顺序原压缩方式复制回去。详见 `tracked-changes.md`。

## 表格默认没有框线

```python
t = d.add_table(rows=2, cols=2)     # style = "Normal Table"，无框线
t.style = "Table Grid"              # 要显式设
```

`add_table` 默认 `Normal Table`，`tblBorders` 不写，打印出来是一张没有格子的表。`Table Grid` 样式在默认模板里已定义，直接按名字设即可。

## 目录域刷不出来

`TOC` 域可以手写，但**只有 Word / WPS 打开后按 F9 才会填内容**。实测 LibreOffice 转 PDF：域里只剩 `separate` 段落的占位文字，两个标题一条都没进目录。

所以：要么在正文里手排目录（页码得自己算，不可靠），要么放 `TOC` 域并在交付说明里写清「打开后按 F9 更新目录」。不要生成完转个 PDF 就以为目录有了。

## 表格和页眉页脚里的文字读不到

```python
[p.text for p in Document(path).paragraphs]
```

只返回 body 里的段落，**表格单元格、页眉、页脚里的段落都不在里面**。实测：写进表格的「表格里的字」在 `doc.paragraphs` 里搜不到。

想遍历全文要另外走 `doc.tables[*].rows[*].cells[*].paragraphs` 和 `section.header/footer.paragraphs`。校验脚本干脆不用 python-docx 读，直接解 zip 里的 XML 全量 `iter()`，就没有这个漏。

## 校验脚本自己的坑：ElementTree 元素为假

```python
any(run.iter(tag("t")))            # 空的 <w:t> 会被判成不存在
next(run.iter(tag("t")), None) is not None    # 这样才对
```

ElementTree 里**没有子元素的元素本身为假**。用 `any()` 判 run 里有没有 `w:t`，凡是文字挂在自己 `.text` 上、没有子元素的 `<w:t>` 全被跳过，结果是「单段最多 0 个 run」「7 处层次字体全部继承」这种一眼假的数字。

写校验的时候更要留意：**一个只会打印 OK 的校验脚本比没有校验更糟**。写完必须拿一份故意做坏的文档跑一遍，确认它真的报错。

## 转 PDF 用哪个命令

本机**没有 `soffice`**，只有 `/usr/local/bin/libreoffice`：

```bash
libreoffice --headless --convert-to pdf --outdir <目录> <文件.docx>
pdftoppm -jpeg -r 80 <文件.pdf> <前缀>      # 出图看版式
pdftotext -layout <文件.pdf> <文件.txt>     # 数行数字数
```

网上的教程一律写 `soffice`，照抄会得到 `command not found`。LibreOffice 的中文字体替换和 Word 不完全一致，PDF 只用来量几何（红线位置、页码、行数字数），字体最终要在目标机器的 Word / WPS 上确认。
