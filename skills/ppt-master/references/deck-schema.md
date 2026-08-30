# 动态稿

画布 13.333 × 7.5 英寸。`theme` 是本次发明的 token，不是目录 id。

排版有两种写法，**优先用 `layout`**：

| 写法 | 你要做什么 | 什么时候用 |
| --- | --- | --- |
| `layout` | 声明「一行四张卡、间距 0.24」，宽高位置引擎算 | 默认。内容页一律走这个 |
| `boxes` | 自己填每个盒子的 x/y/w/h（英寸） | 全幅底图、骑线压字、精确定位的单个元素 |

两个可以同时给：`boxes` 先画当底衬，`layout` 解出的压在上面。

---

## cover / section 宏（grid 主题）

`theme: "grid"` 时封面和章节页**不要手画色带**，只交字段：

```json
{
  "title": "AI 审计可行性研究",
  "theme": "grid",
  "slides": [
    {
      "kind": "cover",
      "kicker": "可行性研究报告",
      "title": "AI 审计可行性研究",
      "subtitle": "人工智能在内部审计中的落地与应用",
      "meta": "某某公司 · 数字化审计专题 ｜ 2026年8月"
    },
    {
      "kind": "section",
      "kicker": "01",
      "title": "概述",
      "subtitle": "项目背景 · 目标范围 · 建设原则"
    },
    {
      "kind": "custom",
      "title": "建设背景与总体目标",
      "layout": {
        "pad": [1.35, 0.62, 0.62, 0.62],
        "gap": 0.24,
        "children": [
          {"kind": "text", "text": "01 · 概述", "size": 12, "color": "#01706C", "bold": true, "name": "witty-kicker"},
          {"kind": "text", "text": "建设背景与总体目标", "size": 28, "bold": true, "name": "witty-title"},
          {"kind": "text", "text": "正文从这里起……", "size": 16, "name": "witty-body"}
        ]
      }
    }
  ]
}
```

- `cover` / `section` **不要**再塞 `boxes` 去仿色带；有 boxes 引擎会当自由页。
- 内容页根 `pad` 上边距 **1.35**，给白带和右上标识留空。
- 内容页必须有 `witty-title`（或页级 `title` 且能被 chrome 画成标题），否则 `no_title`。

---

## layout：声明式排版（默认写法）

一个节点要么是容器（有 `children`），要么是盒子，**也可以两者都是**——卡片就是「有底色、有内边距、里面还装着字」的节点。

### 容器键

| 键 | 含义 |
| --- | --- |
| `dir` | `"row"` 横排 / `"column"` 竖排。给了 `children` 没给 `dir` 默认竖排 |
| `children` | 子节点数组 |
| `gap` | 子节点之间的间距（英寸） |
| `pad` | 内边距。一个数是四边；`[竖, 横]`；`[上, 右, 下, 左]` |
| `flex` | 主轴上抢剩余空间的权重。竖排里抢高，横排里抢宽 |
| `w` / `h` | 钉死尺寸（英寸）。不给就自动量 |
| `justify` | 主轴富余怎么分：`start` `center` `end` `between` `around` |
| `cross` | 交叉轴对齐：`stretch`（默认）`start` `center` `end` |
| `push` | 吃掉前面的富余，把自己钉到末尾。用来把引文条钉在卡片底部 |

除这些之外的键**原样传给盒子**（`kind` `text` `fill` `color` `size` `bold` `radius` …）。

### 三条要记住的规则

1. **横排里不给 `w` 也不给 `flex` 就是等分**。`{"dir":"row","children":[a,b,c,d]}` 就是四等分，不用自己算 13.333 减边距再除以四。
2. **高度自动量**。文字按真实字宽算折几行，图片按文件头的高宽比，表格按行数。想钉死就给 `h`，想抢满就给 `flex`。
3. **父在前、子在后**。卡片底自动垫在卡片里的字下面，不用操心 z 序。

### 例：一行四张数据卡 + 一条判断

```json
{
  "kind": "custom",
  "layout": {
    "pad": [0.42, 0.62, 0.62, 0.62],
    "gap": 0.3,
    "children": [
      {"dir": "row", "gap": 0.16, "cross": "center", "children": [
        {"kind": "rect", "w": 0.05, "h": 0.62, "fill": "#185FA5"},
        {"gap": 0.06, "children": [
          {"kind": "text", "text": "02 / 核心技术体系", "size": 12, "bold": true, "color": "#185FA5"},
          {"kind": "text", "text": "行业采纳与效能提升", "size": 30, "bold": true, "name": "witty-title"}
        ]}
      ]},
      {"dir": "row", "gap": 0.24, "flex": 3, "children": [
        {"kind": "round", "fill": "#FFFFFF", "radius": 0.14, "pad": 0.26, "gap": 0.1, "children": [
          {"kind": "text", "text": "83%", "size": 40, "bold": true, "color": "#185FA5"},
          {"kind": "rect", "w": 0.42, "h": 0.045, "fill": "#185FA5"},
          {"kind": "text", "text": "AI 审计采纳率", "size": 14, "bold": true},
          {"kind": "text", "text": "审计职能已试点或使用 AI", "size": 12, "color": "#5F6B7A"}
        ]}
      ]},
      {"kind": "round", "fill": "#185FA5", "radius": 0.14, "pad": 0.26, "gap": 0.08, "flex": 2, "children": [
        {"kind": "text", "text": "关键判断", "size": 16, "bold": true, "color": "#FFFFFF"},
        {"kind": "text", "size": 13, "color": "#D5E7F7",
         "text": "采纳率说明技术不再是可选项，而是审计职能存续的基础设施。"}
      ]}
    ]
  }
}
```

四张卡就是把那个 `round` 节点重复四份改文案，**一次算术都不用做**。上下两块 `flex` 3 : 2 分掉标题之外的高度。

### 别踩的坑

- `align` 是**文字**左中右对齐（盒子键），交叉轴对齐叫 `cross`。同一页两个键别搞混。
- 想让一行里的东西并排，得真的套一层 `{"dir":"row"}`。直接把两个节点丢进竖排容器只会上下堆。
- `flex` 给多了会把卡片抻高、底下留一大片空，`pptx_check` 会报 `slack`。要么补内容，要么把 `flex` 换成 `h`。
- 纯分组、自己不画的容器不产出盒子，不占额度，放心套。

---

## boxes：手填坐标（底图和骑线压字才用）

```json
{
  "title": "年中汇报",
  "footer": "内部",
  "theme": {
    "font": "Microsoft YaHei",
    "bg": "#F4F1EA",
    "ink": "#1B1714",
    "muted": "#6A5E54",
    "accent": "#C45C26",
    "accent2": "#1B1714",
    "cover_bg": "#1B1714",
    "cover_ink": "#F4F1EA"
  },
  "slides": [
    {
      "bg": "#1B1714",
      "boxes": [
        {"kind": "rect", "x": 0, "y": 0, "w": 0.18, "h": 7.5, "fill": "#C45C26"},
        {"kind": "text", "x": 0.7, "y": 2.2, "w": 12, "h": 1.4, "text": "年中汇报", "size": 40, "color": "#F4F1EA", "bold": true, "name": "witty-title"},
        {"kind": "text", "x": 0.7, "y": 3.7, "w": 12, "h": 0.6, "text": "副题按用户原话", "size": 18, "color": "#C45C26"}
      ]
    },
    {
      "bg": "#F4F1EA",
      "boxes": [
        {"kind": "text", "x": 0.62, "y": 0.45, "w": 12, "h": 0.7, "text": "三件事", "size": 26, "color": "#1B1714", "bold": true, "name": "witty-title"},
        {"kind": "bullets", "x": 0.62, "y": 1.4, "w": 12, "h": 5, "items": ["调研", "试点"], "size": 20, "color": "#1B1714", "name": "witty-body"}
      ]
    }
  ]
}
```

用户改风格：只改 `theme` 和相关 box 的 `fill`/`color`/`size`/`x`/`w`，再 `pptx_render`。交稿前 `pptx_check`。

改一块：`pptx_list_boxes` → `pptx_edit_box`。改一页：`pptx_replace_slide`。加一页：`pptx_add_page`。

`round` 默认浅影；`shadow: false` 可关。`radius` 控制圆角（0–0.5）。

盒子 `kind`：`rect` `round` `text` `bullets` `table` `image` `line` `chart`，加矢量形状 `oval` `diamond` `triangle` `chevron` `pentagon` `arrow`。

## 矢量形状：流程图 / 时间轴 / 架构图

画的是 PowerPoint 原生形状，客户在 WPS 里能拖点、改色、改字。不要塞图片截图。

| kind | 长什么样 | 拿来干嘛 |
|------|---------|---------|
| `chevron` | 两头带尖的流程带 | 流程的每一节 |
| `pentagon` | 一头带尖 | 流程的头一节 |
| `arrow` | 粗箭头块 | 两块之间的流转 |
| `oval` | 圆 / 椭圆 | 时间轴节点、序号圈 |
| `diamond` | 菱形 | 判定分支 |
| `triangle` | 三角 | 强调、层级顶 |

三个只有形状才有的键：

- `text`：**写在形状里面**，默认居中。别再单摞一个文本框，那样在 WPS 里拖形状字不跟着走。`rect` / `round` 也能带 `text`。
- `stroke` + `stroke_w`：描边色和线宽（pt，≤6）。给了 `stroke` 没给 `fill` 就是空心框——架构图那种「白底 + 主色边」。
- `point`：**只有 `arrow` 认**，取 `right`（默认）`left` `up` `down`。`chevron` / `pentagon` 一律朝右（PowerPoint 内置形状就是朝右的，转它会把里面的字也转过去）。要表达倒序就换文案顺序，别指望翻转形状。

`line` 给了 `point` 就变成**带箭头的细连接线**（比 `arrow` 干净，适合架构图连线），线宽走 `stroke_w`；不给 `point` 还是一条色条。

流程图就是一行 `chevron` 中间夹 `arrow`，交给 `layout` 排，不用自己算坐标：

```json
{"dir": "row", "gap": 0.14, "children": [
  {"kind": "chevron", "flex": 1, "h": 0.84, "fill": "#0C447C", "color": "#FFFFFF", "text": "数据归集", "size": 15, "bold": true},
  {"kind": "arrow", "w": 0.42, "h": 0.42, "fill": "#B8C4CE", "point": "right"},
  {"kind": "chevron", "flex": 1, "h": 0.84, "fill": "#185FA5", "color": "#FFFFFF", "text": "清洗建模", "size": 15, "bold": true}
]}
```

架构图是几行「层名 + 若干空心框」：

```json
{"dir": "row", "gap": 0.18, "h": 1.0, "children": [
  {"kind": "rect", "w": 1.55, "fill": "#0C447C", "color": "#FFFFFF", "text": "应用层", "size": 13, "bold": true},
  {"kind": "rect", "flex": 1, "fill": "#FFFFFF", "stroke": "#0C447C", "stroke_w": 1.25, "text": "财务审计", "size": 12}
]}
```

坑：

- 带文字的形状能自己量高，纯色形状不能——没文字就得给 `h` 或 `flex`。
- 尖角形状的字要收窄，`chevron` 少于 1.6 英寸宽就别放四个字以上。
- 形状里的字照样查对比度和溢出，深色填充配 `color: "#FFFFFF"`。

原生图表：

```json
{
  "kind": "chart",
  "chart": "column",
  "x": 0.62, "y": 1.55, "w": 12.05, "h": 4.7,
  "text": "季度完成率",
  "categories": ["一季度", "二季度", "三季度", "四季度"],
  "series": [{"name": "完成率", "values": [72, 81, 86, 91]}],
  "colors": ["#C45C26"],
  "name": "witty-chart"
}
```

`chart` 取值：`column`（柱）`bar`（条）`line`（折线）`pie` / `doughnut`。也可用 `headers`+`rows`（第一列当类目）。数字必须来自用户或本地文件。

折线图要写全 `{"kind": "chart", "chart": "line"}`。只写 `kind: "line"` 是分隔线，不是折线图。

饼 / 环图：

- 扇区 `colors` 用偏深色（`#01706C`、`#C0392B`、`#9A4A00`）。浅黄 `#F4C542` 可以有一块，但不要指望白字标签。
- 引擎默认标签：**深墨 `#123F3C`、14pt、同时显示数值和百分比**。不要解压 pptx 去改 `chartN.xml`。
- 必须带齐 `categories` + `series[].values`；缺了 lint 报 `empty_chart`，图是空的。

放进 `layout` 时把 `x`/`y`/`w`/`h` 去掉，改用 `flex`：图表和图片量不出自然高度，不给 `flex` 也不给 `h` 会按兜底高度排。`image` 例外——引擎会读文件头按高宽比算高。

## 安全色对（grid）

彩色底上写字前先过这一关，避免交稿后被 `contrast` 追着改：

| fill | color |
| --- | --- |
| `#01706C` / `#123F3C` / `#0C6E63` | `#FFFFFF` |
| `#9A4A00` / `#BF6A0A` | `#FFFFFF` |
| `#EAF7F3` / `#FFFFFF` / `#F4F9F8` | `#1F3A36` / `#123F3C` |

**不要**：`#2FA98B` + 白字、`#E67E22` + 白字 / `#fff3e0`。
