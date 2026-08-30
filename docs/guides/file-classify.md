# 资料分类（file-classify）使用指南

调用方给一份类型表（JSON，通常几百条），把某个目录下的文件对到这些类型上。**只读原文件**，产出的是映射报告，绝不移动、复制、改名。

## 1. 安装

```bash
cd 你的项目
uv pip install -e "/path/to/witty_agent[classify]"
export WITTY_API_KEY="sk-..."
```

`classify` extra 带 pypdf、python-docx、openpyxl。不带也能跑，只是这三类文件读不到正文，只能靠目录和文件名判——真实资料里这是大头，建议带上。

要固定版本改用 wheel（**先 `sync_package_data.py` 再 `uv build`**，否则对方装完读不到提示词）：

```bash
cd /path/to/witty_agent
uv run python scripts/sync_package_data.py && uv build
uv pip install --reinstall "dist/witty_agent-0.1.0-py3-none-any.whl[classify]"
```

两者的区别只有一处但很关键：**可编辑安装读仓库的 `config/prompts.toml`，改判定口径立刻生效；wheel 读包内快照，改完要重新 build 才带给下游。**

## 2. 库用法

```python
from witty_agent.plugins.file_classify import classify_directory

summary = classify_directory(
    "/data/某工程项目",           # 待分类目录，递归扫描
    taxonomy,                     # 类型表，直接传对象即可
    out_dir="/data/classify_out",
    limit=30,                     # 0=全量。第一次务必先小批试跑
    concurrency=4,                # 同时在飞的模型调用数
    project={"name": "某新建工程",  # 项目背景，可不传
             "code": "XX-2026-007", "category": "建设工程"},
    progress=print,               # 进度文本，可不传
    on_result=lambda rows: ...,   # 每批判完的结果行，可不传
    on_retry=lambda ev: ...,      # 资源池等待事件，可不传
)
```

**这是普通函数调用，不进 agent 循环，全程不会弹审批**，一次跑完。审批只在窗口里模型自己调 `classify_files` 时才有。

批次是**并发**跑的，`concurrency` 卡住同时在飞的模型调用数（默认 4）。几百个文件按 15 个一批要几十次调用，串行会拖成几十分钟；网关扛不住就把这个数调小。

**每批提示词都带项目目录总览**（每个目录一行 + 文件数，超 200 个目录截断）：模型先有全局观——这个项目分了哪些标段、哪些业务环节目录——再判个体归属，而不是只看见单元自己那条路径。

**可选的 `project` 背景**（`{"name": ..., "code": ..., "category": ...}`，也吃 `项目名称`/`项目编号`/`项目分类` 中文键名，三项都可选）也进每批提示词。它的价值不是多给点信息，而是让模型分得清本项目自身的过程文件与引用进来的外部项目材料——投标目录下别的项目的合同是业绩证明，这个判断没有项目名就只能靠猜。提示词里明确它只是背景参考，与目录路径、正文证据冲突时以后者为准；不传则整段不出现。

**读正文的深度由模型自己定**：正文轮首轮给每文件前 `excerpt_chars` 字（默认 1200），单元标注「正文未完」而模型判不足以定论时（`need_content=true`），下一轮给 4 倍正文，直到读完全文、达到 `max_excerpt_chars`（默认 20000）或耗尽 `content_rounds`（默认 3）轮，最后一轮强制定论。

**重试有三层**，各管一件事：HTTP 层可重试状态码快速重试 2 次；模型返回非法 JSON 时整批再重试 2 次（仍失败则本批降级，连续 3 批全废才中止整轮）；连接超时、429、5xx 这类资源池故障走**闸门**，按 `retry_interval`（默认 180 秒）持续重试，`retry_max_attempts=0` 表示不限次数——池子打满是「等一会儿就好」，放弃只会让已烧掉的批次白费。鉴权失败、配额耗尽不重试，立即抛。

闸门是整轮共享的：一批撞上故障后，其余并发批次一起退避；池子回了 `Retry-After` 就按它要求的时长等。等待状态有两条通道——`progress` 收人话文本，`on_retry` 收结构化事件：

```python
{"event": "pool_wait",      # 或 pool_recovered（重试成功后）
 "attempt": 2,              # 这批已重试第几次
 "delay_sec": 180.0,        # 本次等多久
 "error": "504 gateway timeout",
 "message": "模型资源暂不可用，180 秒后重试（第 2 次）",
 "waits": 5, "total_wait_sec": 900.0}   # 整轮累计，也进 summary
```

已经在事件循环里的调用方**不要**用同步版，`await` 异步版（参数完全一样）：

```python
from witty_agent.plugins.file_classify import aclassify_directory

summary = await aclassify_directory(root, taxonomy, out_dir=..., concurrency=4)
```

同步版检测到事件循环会直接报错让你改用异步版，不会静默卡死。

命令行同一套逻辑：

```bash
uv run python skills/file-classify/scripts/classify.py <目录> --taxonomy 类型表.json --limit 30
```

完整示例见 `examples/classify_demo.py`。

## 3. 类型表可以长这样

四种形状都吃，不用先序列化。id 字段认 `id` / `code` / `编号`，名称认 `name` / `名称` / `title`，说明认 `description` / `说明`：

```python
[{"id": "B03", "name": "投标文件-业绩证明", "description": "为响应资格条款附入投标文件的业绩材料"}]
{"C01": "合同", "D01": "发票"}                                    # id → 名称
[{"id": "B01", "name": "投标文件", "children": [{"id": "B03", ...}]}]  # 嵌套子类
"/path/到/类型表.json"                                            # 文件路径
```

## 4. 结果怎么读

| 产物 | 时机 | 是什么 |
|------|------|--------|
| `results.jsonl` | **每批追加** | 真源。一行一个单元 |
| `groups.jsonl` | **每组追加** | 拆分件的合并 / 拆开决策 |
| `calls.jsonl` | **每次模型调用追加** | 执行过程转录：提示词原文、模型原始回复、耗时、是否解析成功。排查口径问题看这里 |
| `report.md` | 全部跑完 | 给人看的，按类型分组 |

不是全跑完才有结果，粒度是「批」（一批 = 一次模型调用，默认第一轮 15 个）。

`results.jsonl` 每行的关键字段：

```python
row["category_id"]   # 类型表里的 id；_待分类 是最后手段（见下）
row["members"]       # 该单元的文件；拆分件会是多个
row["evidence"]      # 判定依据，错分时靠它定位是被路径还是被内容带偏
row["confidence"]
row["status"]        # "ok" / "failed"，只有这两种
row["error"]         # 失败原因；成功为 None
```

**只有定论的单元才出行**——还没判完的不写文件也不进 `on_result`，不存在中间态。失败行的 `category_id` 可能带着上一轮的低置信初判，**落库前按 `status == "ok"` 过滤**，别拿 `category_id` 当判据。

summary 里成功数是 `units_ok`、失败数是 `units_failed`；**`tally`（按类型计数）只含成功行**。

`_待分类` 是最后手段：判定口径要求模型只要有任一证据指向某个类型就选最接近的具体类型（用低 confidence 表达不确定），且第一轮判「待分类」的单元会被强制拽进正文轮，读完正文仍毫无指向才允许收 `_待分类`。

中途断了重跑同一条命令会跳过已完成的单元；要推翻重来加 `--no-resume`。

## 5. 判定规则怎么改

**全部在 `config/prompts.toml` 的 `file_classify_*` 里，不要改 `plugins/file_classify.py`。**

核心是 `file_classify_system`，其中最要紧的一条是**用途优先于形态**：一份资料属于哪类取决于它「为什么存在、为谁服务」，不取决于它「长什么样」。所以投标文件目录下那份用来证明业绩的合同，是投标资料不是合同；招标文件里的合同范本，是招标文件不是合同。

错分方向固定时（总把某类判成另一类），去这一节补一个反例，不要去改代码。

## 6. 先小批试跑

第一次对一个新目录跑**一定带 `limit`**。看 30 个单元的 `report.md`，重点看三件事：待分类比例（超 15% 说明类型表描述太糊）、`evidence` 命中的是 `path:` 还是 `content:`（被内容带偏是最常见的错分）、`groups.jsonl` 里模型主动拆开的组对不对。
