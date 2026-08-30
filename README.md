# Witty Office Agent

**面向办公场景的通用智能体底座（Python）。**
技能可插拔、提示词全配置、危险操作先审批、代码执行进沙箱；一套内核，三种用法：Python 库、HTTP 服务、桌面应用。

写给真实的办公人群：**产品经理、教师、律师、行政、财务、数据分析师、工程人员**。覆盖的场景包括但不限于：需求文档与竞品分析、教案课件、合同与案卷、行政文书、工程报告（可研/概设/详设）、演示汇报、数据问答与分析、邮件与日程、资料归档分类。业务能力全部以技能和插件挂载，内核不含任何行业逻辑——换一个行业，换的是技能和提示词，不是代码。

> 默认模型是任意 OpenAI 兼容端点：只需一个 `base_url` + 一个 `api_key`。密钥只走环境变量或本机保险柜，不进仓库。

---

## 为什么是这个底座

市面上不缺 agent 框架，缺的是把「办公」当成一等公民、且**内核纪律严格**的底座。Witty Agent 的取舍：

1. **内核不可卸，扩展只能往上加。**
   循环、审批、会话日志、内置工具（read / write / edit / bash 等）、内置命令（`/plan` `/abort` `/loop` `/compact`）是内核，业务包无法覆盖或劫持。技能、业务工具、提示词是插件层，热插拔不伤内核。
2. **提示词 100% 外置。**
   所有发给模型的文本都在 `config/prompts.toml`，改判定口径、换业务话术不需要发版。`WITTY_PROMPTS_FILE` 可整文件替换。
3. **危险操作默认先审批。**
   写文件、执行命令、发邮件默认 always-ask；桌面弹窗审批，无人值守的库模式走 `allow / ask / read-only / deny` 四档权限 + 文件回执审批 + 超时策略。
4. **执行有沙箱。**
   模型生成的代码写进独立工作区、跑在独立 venv（预装 numpy / pandas / matplotlib 等），不污染系统环境；文件访问有路径 jail。
5. **上下文是工程问题。**
   会话 jsonl 落盘可恢复；自动压缩对 prompt cache 友好（能省才裁，裁必够本）；超长工具结果落盘只留预览（spill）；会话可分叉、可回滚。

## 独有能力

| 能力 | 说明 |
|------|------|
| **九宫格记忆（原创设计）** | 见下节专门说明——固定九格 + 可配置分类格的确定性记忆系统，不靠向量库、可读可审计 |
| **技能路由与渐进披露** | 30+ 内置技能按 [Agent Skills](https://agentskills.io) 规范组织。启动只读名字和描述；用户的话命中才注入正文；技能可声明 `allowed-tools` 真收权 |
| **目标模式（客观完成判据）** | 长任务用 shell 退出码当客观 gate + 只追加的回归义务台账 + 无工具判官模型三层判「做完没有」，不信模型自述 |
| **自进化 `/refine`** | 复盘本会话轨迹，沉淀角色守则 / 记忆 / 技能草稿。每条沉淀必须带轨迹原文当证据（对不上机械丢弃），先快照后落笔，`/refine undo` 整体回滚 |
| **持久解释器** | `python_repl` 工具的变量跨工具调用存活：读一次大表，后续轮次直接用，不重复读盘 |
| **证伪账本** | 失败过的动作（文件不存在等）在证据没变时直接拦下重试，不烧第二遍 token |
| **转圈检测** | 同一调用重复到阈值先提醒后停轮，「重复」不要求连续，只读调用洗不掉计数 |
| **可编辑 PPT 生产线** | `witty-ppt-skills` 产出真 .pptx：声明式 flex 排版（不让模型手算坐标）、原生矢量形状（流程图/架构图在 WPS/PowerPoint 里可改）、光栅自查防瞎交稿 |
| **证据驱动长文工程** | `long-document` 把几万字报告当工程写：锁提纲、样章先行、来源账 `[cite:]`、数字账 `[num:]`、跨章交接、确定性校验脚本、Word 导出带目录和交叉引用 |
| **红头公文** | `word-docx` 按 GB/T 9704 版式生成红头文件：份号、密级、发文字号、版记，字体字号间距按规程 |
| **自然语言问数** | `nl2sql` 系列技能：读 schema、生成带行数上限的 SQL、结果聚类定置信度、按问题类型分级出图 |
| **资料分类** | `file-classify` 把整个目录的文件对到调用方的类型表上：用途优先于形态、路径与正文双证据、断点续跑、并发限流，只出映射报告不动原文件 |

## 九宫格记忆（本项目的原创设计）

主流 agent 记忆要么是向量库检索（黑盒、要基础设施、错了没法改），要么是让模型自由记笔记（记什么全凭当轮发挥，越攒越乱）。九宫格走第三条路：**把「一个办公助手该记住用户什么」收敛成九个固定格子**——

> 身份与角色 · 当前目标 · 红线与约束 · 个人偏好 · 领域要点 · 项目与资产 · 关系与组织 · 已做决定 · 待跟进

再加一排**可配置的分类格**（默认带产品需求、教学课程、法务合同、市场、工程、数字化等办公分类，`config/memory.toml` 里改配置即可换行业）。它跟常见做法的实质区别：

1. **写入是确定性判据，不是模型即兴。** 每轮对话自动收割：分句仲裁（一句话里哪个分句归哪格）、问句和派活不当事实收、按「事」去重——同一件事重申一百遍只占一个槽，只刷新时间和位置。这些判据全部有校准过的测试集压着。
2. **召回按词打分，不需要向量库。** 数真词不数碎片、生僻词按语料频率加权、旧条目衰减、偏好和红线常驻不衰减。零基础设施：没有嵌入服务也能跑。
3. **落盘是纯 Markdown，可读可审计可手改。** 每格一个文件，条条带来源；工作集 → 归档 → 退休三层，满了不静默丢。
4. **分层双域。** 用户层（你是谁、你的偏好红线）跟着人走，工作区层（这个项目的事实与决定）跟着目录走，互不污染。
5. **看得见。** 桌面端有九宫格可视化页：格子可点开、可检索、召回命中会点亮对应格子，答案里的记忆依据可以点回出处。

## 内置技能矩阵

| 类别 | 技能 |
|------|------|
| 文档 | `long-document`（可研/详设/概设长文工程）、`word-docx`（公文/题注/交叉引用/长文导出）、`office-document`（纪要/公函/老格式转换）、`pdf-extract`（表格/OCR/填表）、`excel-xlsx`（公式/图表/条件格式）、`doc-qa`、`table-qa` |
| 演示 | `witty-ppt-skills`（可编辑 PPTX 生产稿）、`slides`（HTML/Markdown 演示） |
| 数据 | `nl2sql` / `nl2sql-schema` / `nl2sql-sql` / `nl2sql-deliver`（问数四段式）、`data-analysis` |
| 信息流 | `mail-desk`（IMAP/SMTP 邮件）、`agenda-digest`（日程摘要）、`week-digest`（周报）、`daily-diary`（行为日记）、`link-box`（链接库）、`llm-wiki`（工作区知识库） |
| 归档 | `file-classify`（目录资料分类） |
| 创作 | `novel-to-video`（小说转分镜文案） |
| 元能力 | `agent-creation` / `agent-evaluation` / `agent-optimization` / `benchmark-design`（造 agent、评 agent、优化 agent）、`find-skills`（从 [skills.sh](https://skills.sh) 生态搜索并安装社区技能）、`skill-porting`、`session-health`、`generation-ui`、`software-engineering` |

**场景举例**：产品经理用 `long-document` 写需求文档、`slides` 出评审材料、`nl2sql` 直接对埋点库问数，九宫格替他记住每次评审拍板的结论；教师用 `witty-ppt-skills` + `long-document` 出课件和讲义，用 `data-analysis` 批改成绩分布；律师用 `doc-qa` 问案卷、`file-classify` 归档卷宗、`word-docx` 出规范文书；行政用 `office-document` + `mail-desk` + `week-digest` 处理纪要邮件周报。

技能怎么写、怎么校验，见 [`skills/README.md`](skills/README.md)；提交前跑 `uv run python scripts/check_skills.py`。

---

## 快速开始

需要 Python 3.10+（本仓库开发锁 3.12）和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone <this-repo> && cd witty_agent
uv sync

# 只需一个 OpenAI 兼容端点。也可以 cp .env.example .env 填好后 source
# （全部可用变量见 .env.example，注意程序不自动加载 .env）
export WITTY_API_KEY="sk-..."
export WITTY_BASE_URL="https://api.example.com/v1"
export WITTY_MODEL_ID="your-model-id"

uv run witty-agent          # 冒烟：加载提示词/技能/工具
uv run witty-agent serve    # HTTP API，默认 127.0.0.1:8765
```

桌面窗口（需本机 Node / npm）：

```bash
cd apps/desktop
npm install
npm start
```

没有 Electron 时，先 `serve`，再用浏览器打开 `apps/desktop/renderer/index.html`。
Windows 一键安装器（自带 Python 与依赖）用 `uv run python scripts/build_windows_installer.py` 构建。

## 当作 Python 库

```bash
uv pip install -e /path/to/witty-office-agent   # 或打 wheel 固定版本
```

发行名是 `witty-office-agent`，导入名仍是简短的 `witty_agent`，命令行是 `witty-agent`。

```python
from witty_agent import Witty

agent = Witty()                       # 工作区默认取调用方 cwd
result = agent.run("总结这个目录")
print(result.text)                    # 最终正文
print(result.tools)                   # 用过的工具与结果
```

- 指定工作区 / 权限 / 超时：`Witty(workspace=..., permission="ask", timeout_sec=30, on_timeout="allow")`
- 异步：`await agent.arun(...)`（`run()` 内部是 `asyncio.run`，已有事件循环时用 `arun`）
- 流式回调：`Witty(on_event=fn)`，`fn` 收 `text_delta / tool_start / tool_end / approval / done`
- 模型可代码传入：`Witty(api_key=..., base_url=..., model_id=...)`，未传项回落环境变量，再回落 `config/runtime.toml`

### 权限四档（库 / 后台）

| `permission` | 行为 |
|--------------|------|
| `allow` | 危险工具直接执行 |
| `ask`（默认） | 先问：日志 + 待批文件 `~/.witty/data/approvals/<会话>/<id>.json`，写 `.reply` 一行 `allow`/`deny` 回复；或传 `ask=` 回调；超时按 `on_timeout` |
| `read-only` | 只读，写和执行一律拒绝 |
| `deny` | 危险工具全部拒绝 |

桌面与 HTTP 不走这套——始终 always-ask 弹窗审批。

## 配置

| 东西 | 位置 | 说明 |
|------|------|------|
| 所有提示词 | `config/prompts.toml` | 改配置即生效；`WITTY_PROMPTS_FILE` 换整份 |
| 运行开关 | `config/runtime.toml` | 模型默认地址、循环阈值、沙箱包、压缩水位、MCP 服务器…… `WITTY_RUNTIME_FILE` 换整份 |
| 技能 | `skills/<name>/SKILL.md` | 目录名 = frontmatter `name`，小写-连字符；`WITTY_SKILLS_PATH` 加目录 |
| 工具 | `@tool` 装饰的函数 | `[tools].packages` 按包扫描；内核工具名不可占用 |
| 密钥 | 环境变量 / 桌面保险柜 | `WITTY_API_KEY`（后备 `OPENAI_API_KEY`）；桌面存 vault，serve 启动时补进环境 |
| 网络搜索 | `[web]` + 环境变量 | `web_search` 工具：默认 Tavily（`WITTY_SEARCH_API_KEY`，后备 `TAVILY_API_KEY`）；内网可改 `search_provider = "searxng"` 指自建实例，免 key |

数据落点：全局 `WITTY_HOME`（默认 `~/.witty/data`，放项目、保险柜、沙箱、审批收件箱）→ 项目（租户）→ 工作区（调用方 cwd）。密钥永远不落工作区。

## MCP

`config/runtime.toml` 的 `[mcp].servers` 登记 stdio 服务器：

```toml
[[mcp.servers]]
name = "files"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

工具以 `mcp__<server>__<tool>` 注册进循环；`tools/list_changed` 通知会触发热刷新；连不上的服务器跳过不拖启动。当前支持 stdio 传输与 tools 原语（resources / prompts / HTTP 传输在路线图）。

## 热插拔

技能目录、业务 `@tool` 包、MCP 服务器、提示词文件都可以在不重启内核的前提下增删：配置对账只落差集，活跃技能的 `allowed-tools` 真正收权，卸载回滚副作用；内核工具与命令不可卸。

## 测试

```bash
uv run python -m unittest discover -s tests -q   # 全量（1200+ 例）
uv run python scripts/check_skills.py            # 技能规范校验
uv run ruff check                                # lint
```

## 边界（诚实说明）

- 沙箱是「独立工作区 + 独立解释器 + 路径 jail」，**不是** OS 级进程隔离（Seatbelt/Landlock），挡不住蓄意逃逸。
- HTTP 服务默认只绑 `127.0.0.1`，没有对外鉴权网关，不要直接暴露公网。
- MCP 只有 stdio 传输；resources / prompts 原语未接。
- 公网抓取 `web_fetch` 默认按配置策略放行/拒绝，内网部署可锁公网。
- 邮件需自行配置 IMAP/SMTP 主机。

## 目录

```
src/witty_agent/     内核与底座（循环、工具、审批、记忆、会话、沙箱、MCP、HTTP）
src/witty_agent/plugins/   业务插件（pptx、邮件、链接、问数、资料分类……）
skills/              内置技能（SKILL.md，Agent Skills 规范）
config/              prompts.toml / runtime.toml / memory.toml
apps/desktop/        Electron 壳（只调本机 HTTP API）
scripts/             构建与校验脚本
tests/               单元与集成测试
docs/                指南与维护记录
```

## License

Apache-2.0，见 [LICENSE](LICENSE)。
