# 批注

批注和修订留痕是两件事：**留痕是「我替你改了」，批注是「这里我有话说，你自己定」。**
审核意见里「请补充责任部门」「这个时间来得及吗」这类不该直接替人写死的，就该走批注。
分不清就问一句：这句话我有资格替他定吗？有就留痕，没有就批注。

这份是 `scripts/comment.py` 的依据和 `check_docx.py --mode comment` 的判定线。
下面的实测都在本机沙箱 python-docx 1.2.0 + libreoffice 上做的，换版本自己再验。

## 六个部件，缺一个都不报错

一条批注不是一段 XML，是**跨四个部件互相挂钩**再加两处引用登记：

| 部件 | 装什么 | 缺了的后果 |
|------|--------|-----------|
| `word/document.xml` | 锚点三件套 | 见下 |
| `word/comments.xml` | 批注正文、作者、时间 | 审阅窗格里空的 |
| `word/commentsExtended.xml` | **回复串 + 已解决** | 回复摊平成独立批注，已解决全丢 |
| `word/commentsIds.xml` | `durableId`（Word 2016+） | 可选，本技能不写 |
| `word/commentsExtensible.xml` | UTC 时间（Word 2018+） | 可选，本技能不写 |
| `word/_rels/document.xml.rels` | 指向前几个部件的关系 | **Word 一句不提示，就是看不见批注** |
| `[Content_Types].xml` | 每个部件的 `Override` | 部分阅读器当整个文件损坏 |

最后两行是这里面最阴的：部件明明躺在 zip 里，内容一个字不差，Word 打开也不报错，
批注就是不显示——因为没人引用它。`comment.py` 的 `ensure_rel` / `ensure_content_type`
就是补这两处，`check_docx.py` 的 `check_comment_wiring` 就是查这两处。

两串关系类型和内容类型，抄错也不报错（同样是「当没有批注」）：

```
rel  comments         http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments
rel  commentsExtended http://schemas.microsoft.com/office/2011/relationships/commentsExtended
ct   comments         application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml
ct   commentsExtended application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml
```

注意 `commentsExtended` 那条关系是 **microsoft.com/office/2011**，不是 openxmlformats
那一串，也不是 2012——2012 是 `w15` 命名空间的年份，两个数字不一样，很容易串。

## 锚点三件套

```xml
<w:commentRangeStart w:id="0"/>
<w:r><w:t>要批注的字</w:t></w:r>
<w:commentRangeEnd w:id="0"/>
<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
  <w:commentReference w:id="0"/></w:r>
```

顺序是 **Start → 被批注的 run → End → 装 Reference 的 run**。三件套少任何一个、或者
顺序错了，后果都不一样，且**都不报错**：

| 情况 | 后果 |
|------|------|
| 少 `Start` / `End` | 批注不知道锚在哪，Word 当文档级批注或者不显示 |
| 少 `Reference` | 审阅窗格里有内容，正文里没有那个可点的小标记 |
| `Reference` 排在 `End` 前面 | 小标记落进锚点范围内侧，点选范围不对 |
| `w:id` 重复 | 重复的那几条只认第一个，后面的等于没有 |

`Reference` 必须裹在一个 `w:r` 里，不能直接当 `w:p` 的子元素。

**批注只能锚在整个 run 上。**要批注半句话，得先把那几个字切成独立 run。
`comment.py` 直接复用 `revise.py` 的 `isolate()` 做这件事，所以 spec 里写
`"find": "九月三十日"` 就能精确锚上去，不用自己数 run。

## 回复串和「已解决」只在 commentsExtended 里

这是整套结构里最容易漏的一层。`comments.xml` 里的批注是**平铺的**，谁回谁、哪条解决了，
全靠 `commentsExtended.xml`：

```xml
<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">
  <w15:commentEx w15:paraId="01234567" w15:done="0"/>
  <w15:commentEx w15:paraId="3D9238C9" w15:done="1"/>
  <w15:commentEx w15:paraId="7A012C2B" w15:done="0" w15:paraIdParent="01234567"/>
</w15:commentsEx>
```

挂钩的键是 `w14:paraId`——一个 8 位十六进制数，写在**批注正文最后一段**的 `w:p` 上
（命名空间是 2010 的 `w14`，不是 2012 的 `w15`，两个都要注册）。取首段的 paraId 会让
所有多段批注全部对不上号，而且不报错。

`paraId` 不能是 `00000000`。`comment.py` 里按序号推算而不用随机数——同样的输入要出
同样的文件，方便 diff 和复现。

删掉一条有回复的父批注时，回复里那个 `w15:paraIdParent` 会指向一个不存在的 paraId。
`comment.py --delete` 的处理是**把这个属性摘掉**，回复留下来变成独立批注；不摘的话
Word 里那条回复的显示是不确定的。

## 实测：python-docx 的 add_comment() 不够用

沙箱 python-docx 1.2.0 有 `Document.add_comment(runs, text, author, initials)`，
实测写出来的东西：

| | 写了吗 |
|--|-------|
| `comments.xml` | 写 |
| 锚点三件套（顺序 S→E→R） | 写，且顺序对 |
| rels / Content_Types | 写 |
| `commentsExtended.xml` | **不写** |
| `w14:paraId` | **不写** |

所以它**没有回复串、没有「已解决」**，而且只能锚在整个 run 上——批注半句话要自己先切
run。`check_docx.py` 碰到这种文件报的是一条 WARN，不是 FAIL：结构本身合法，只是能力缺一半。

但它有一个别的用处：**round-trip 无损**。29 段公文过一遍 `Document(p).save()`，段数、
起间隔的空段、`sectPr`、压字距的 run 数、`w:eastAsia` 数、`docGrid` 全部一致，
`--mode gongwen` 仍然 0 FAIL；手工塞进去的 `commentsExtended.xml` 也原样保留。
这和 pandoc 正好相反。所以它可以当**独立读取端**交叉验证（`Document(p).comments`），
写还是走 `comment.py`。

## 怎么验：三个独立通道

改完不能只看「文件打开了」。三条互不依赖的验证路线：

```bash
# 1. 独立读取端：python-docx 自己解析一遍
<沙箱 Python> -c "from docx import Document; [print(c.comment_id, c.author, c.text) for c in Document('批注稿.docx').comments]"

# 2. 渲染端：LibreOffice 转 fodt，批注变成 office:annotation
libreoffice --headless --convert-to fodt --outdir /tmp 批注稿.docx
grep -o '<office:annotation[ >]' /tmp/批注稿.fodt | wc -l      # 条数
grep -o 'loext:resolved="true"' /tmp/批注稿.fodt | wc -l        # 已解决数

# 3. 结构端：本技能自己的判定
<沙箱 Python> <技能目录>/scripts/check_docx.py --input 批注稿.docx --mode comment
```

第 2 条有个量法坑：正则写 `<office:annotation` 会**连 `<office:annotation-end` 一起匹配**，
匹配区间从结束标记跨到下一个闭合标签，量出来的东西完全不对。必须写
`<office:annotation[ >]`。同一类错见 `docx-traps.md` 里 `awk length` 那条。

**PDF 不渲染批注**——实测转出来的 PDF 里批注 0 处。所以核查版式那套「转 PDF 量一遍」
的纪律在批注上不适用，只能靠上面三条。

## 判定线（`--mode comment`）

`check_docx.py` 只要在文档里嗅到批注痕迹就查，不看 `--mode`——孤立锚点这类错和
「你打算查什么」无关，碰见了就该报，不然默认模式跑一遍全绿反而误导。

FAIL：孤立批注（有正文无锚点）、孤立锚点（有锚点无正文）、三件套不是各一个、
顺序不是 S→E→R、`w:id` 重复、缺 `w:author`/`w:date`、末段缺 `w14:paraId`、
`commentsExtended` 里 paraId 是野的、回复指向不存在的父批注、自己回复自己、
`commentsIds`/`commentsExtensible` 对不上、rels 或 Content_Types 缺项。

WARN：没有 `commentsExtended`（能力缺一半但结构合法）、有批注没进 `commentsExtended`、
`w:date` 不是 ISO 8601。

这 15 条**每一条都拿故意做坏的文档验过真的会报**，不是写完就算。做坏样本的脚本思路：
从一份好文档按字节改一处，跑一遍看该条判定有没有出现。写这类脚本时正则要吃
`/>` 和 ` />` 两种写法——ElementTree 写自闭合标签带一个空格，按记忆写死 `/>`
会一条都匹配不上，然后误以为「判定没报错」。
