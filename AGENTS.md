# witty_agent 开发规范

本项目是**面向办公场景的通用智能体底座**。所有设计、代码、文档、技能、工具都围绕「通用智能体」展开，不做成某一条业务线的专用脚本。

本项目用 **uv** 管理 Python 环境和依赖，不用 conda / pip 直接装包。

## 产品定位

- 目标场景是各类办公自动化（文档、数据、邮件、教学、法务、审计等），但核心是**可复用的通用 agent 底座**，不是某一个专项助手。
- 具体业务（审计、客服、资料审核、教案、合同审阅等）只能以工具、技能、权限和**可配置提示词**挂到底座上，禁止写进核心循环。
- **Harness 必须做好**：模型调用循环、工具编排、上下文/记忆、技能加载、权限与安全、失败恢复、可观测（日志/轨迹）。缺一块就先补 harness，不要先堆业务。
- **本机环境是内核**：每轮系统提示必须带上探测到的事实：日期时间时区、操作系统、登录用户、工作目录、Git 分支/是否脏、路径分隔与壳、文字编码与换行、网络策略（是否禁公网）、当前模型名、代码沙箱路径与已装包。禁止写死，禁止问用户「你是 Windows 还是 Mac」。bash / 路径 / 打开文件 / 联网以这份说明为准。
- **执行沙箱是内核**：生成的可运行 Python 写到 `WITTY_HOME/sandbox/work`（路径用 `sandbox/…`），解释器用旁边的 uv venv，预装 `[sandbox].packages`。不要往用户系统 Python / 家目录装包或堆试验脚本。这是隔离工作区 + 独立解释器，不是 Landlock/Seatbelt 进程监禁。
- 新增能力先问：这是底座能力，还是某个业务插件？只有前者能进核心包。
- **不把循环做成可插拔插件树。** 扩展只允许「往上加」，不允许卸内核。分层如下：
  - **内核（不可覆盖、不可卸载）**：循环、审批、会话日志、内置工具（read/write/edit/bash 及同包底座工具）、内置命令（`/plan` `/abort` `/loop` `/compact`）。
  - **底座可选**：配置开关，例如 time-context、spill 阈值、MCP；关的是开关，不是热卸载内核。
  - **业务插件**：`SKILL.md`、`[tools].packages` 里的业务 `@tool` 包、业务提示词文件。只加不改核。非内核可热插拔：配置对账只落差集，监视技能目录，活跃技能的 `allowed-tools` 真正收权；卸下回滚副作用，MCP 认 `list_changed`，忙时延后落地。内核仍不可卸。
- 业务包不得占用内核工具名或内核命令名；`list_tools` / `register_tool` / 命令表会拒绝覆盖。
- 数据范围分三层，不要混用：
  - **全局**：`WITTY_HOME`，默认 `~/.witty/data`
  - **项目（租户）**：`<全局>/<project_id>/`，下面可以有多个 Agent；模型钥匙在 `.project_config.toml`。第一期只跑 `default_project` / `default_agent`
  - **工作区**：某次会话的代码目录（cwd），记忆按工作区键分目录，不在这里存密钥
- 自进化挂在 Agent 上，不挂在工作区：`agent_state/`（含技能、AGENTS.md、版本）+ `snapshots/` + `benchmarks/` + `traces/`
- **核心能力必须完整**：循环、事件、四件套工具、session/compaction、技能与提示词分层，不要裁成玩具循环。
- **上层能力同为必备**：多 Agent、租户型项目、审批、轨迹、Memory（user/workspace）、自进化（版本 / snapshot / benchmark / 优化 Skill）。
- **危险工具必须先批准**：`write` / `edit` / `bash` / `run_subagent` 等写或执行类默认 `always-ask`；读类可自动放行。
- **交付面**：Python 核心协议 + HTTP API；客户端/网页端走 Electron（壳不能定义协议）。库用法 `from witty_agent import Witty`，工作区默认调用方 cwd，给后台脚本 pip 安装。桌面/HTTP 仍 always-ask。库权限 `allow` / `ask` / `read-only` / `deny`；`ask` 时写 `WITTY_HOME/approvals/<session>/<id>.reply` 或回调，超时默认放行（`on_timeout=allow`）。
- **打包**：`config/` 与 `skills/` 以仓库根为准。发 wheel 前跑 `uv run python scripts/sync_package_data.py`，装进 `src/witty_agent/data/`。开发时 `project_root()` 仍读仓库根，改配置不用先同步。
- 前端、TUI、桌面安装器可以后做，但核心循环、多 Agent、审批、轨迹、自进化原语不能缺。

## 语言

- 全局语言是 **Python**。本仓库开发锁 **3.12**（`.python-version`）。对外包装 `requires-python = ">=3.10"`，调用方 3.10 / 3.11 / 3.12 都能装。核心、harness、工具、脚本、测试只用 Python。
- 不要为底座再引入第二套运行时语言（Go / Java / Node 等）。
- 若以后必须有独立前端，前端不能反向定义核心协议；协议与运行时仍在 Python 侧。

## 并发：能异步就异步

内核循环、模型调用、编排都是 `async`（`loop.py` / `orchestrator.py` / `llm.py`）。新代码默认跟上，不要再写只能串行跑的同步分支。

硬性规则：

1. **凡是 I/O 一律 `async def`**：模型调用、HTTP、MCP、子进程、大批量文件读写。判据是「会等」，不是「代码长」。
2. **纯计算不要为了异步而异步**。解析、拼串、算 hash、内存里过一遍列表，就写普通 `def`。给不会 await 的函数套 `async` 只会污染调用链。
3. **对外同时给同步包装**，命名约定 `async def afoo(...)` + `def foo(...)`。同步版一律走 `witty_agent.async_bridge.run_sync(afoo(...), entry="afoo")`，不要自己再写一遍循环检测。库调用方和脚本用同步版，内核和 HTTP 用异步版。
4. **禁止在事件循环里跑同步阻塞调用**。必须落到线程：`await asyncio.to_thread(blocking_fn, ...)`（对照 `llm.py` 的 `_request` 和 `loop.py` 的工具执行）。同步函数里那种「顺手喊一次模型」的分支，用 `async_bridge.in_event_loop()` 自查并退让，别硬等。
5. **禁止 `llm._request(context)` 这种私有同步旁路**。`OpenAICompatLLM.__call__` 是 `async`，就 `await` 它。已有事件循环时改调私有同步方法会把整个循环卡住，不报错、只是整个 agent 停住，极难查。`tests/test_async_surface.py` 会扫全仓库挡住这种写法。
6. **独立任务并发跑，但必须限流**。用 `asyncio.gather` 并发，同时用 `asyncio.Semaphore` 卡住上限，并发度做成参数而不是写死。几百个单元一次性 gather 出去会把模型网关打爆，也拿不到有意义的错误。
7. **并发不能破坏「每批落盘」**。批与批之间可以乱序完成，但每批完成即追加结果，中途崩了要能续跑。顺序敏感的产出（汇总报告）在全部完成后再统一生成。
8. **回调是调用方的代码，可能是同步的**。`progress` / `on_result` 这类回调直接调，别 `await`；回调抛异常只记 WARN 不中断，已落盘的结果不该因下游失败而作废。

## 提示词必须可配置

任何发给模型的文本都是提示词，包括但不限于：system / developer 指令、角色设定、工具说明、few-shot、评判/改写/摘要提示、技能正文里的模型指令。

硬性规则：

1. **禁止**在 `.py` 或其它逻辑代码里写死提示词长字符串。
2. 提示词一律放在 `config/prompts.toml`（可用环境变量 `WITTY_PROMPTS_FILE` 换文件）。
3. 代码只引用 key，通过 `witty_agent.prompts.get_prompt("key")` 读取；需要占位符时用 `get_prompt("key", name=value)`。
4. 改提示词等于改配置，不发版、不改代码；新增提示词先加配置再写调用。
5. 业务差异优先用不同提示词文件或不同 key，不要 `if 业务 == "审计": prompt = "..."` 这种分支。
6. 技能正文写在该技能的 `SKILL.md` 里（这就是配置），不要再抄进 `.py`。

## 日志

只用统一入口：

```python
from witty_agent import get_logger, set_trace_id, setup_logging

setup_logging()                 # 进程启动时一次
logger = get_logger("skills")   # 得到 witty_agent.skills
set_trace_id(session_id)        # 一条会话一个 trace
logger.info("加载技能 name=%s count=%s", name, count)
```

- 不要 `logging.basicConfig`，不要新建与 `witty_agent` 无关的 logger 树。
- 级别来自 `config/runtime.toml` 的 `[logging].level` 或环境变量 `WITTY_LOG_LEVEL`。
- 禁止把密码、token、密钥、完整敏感正文打进日志。

## 技能与工具

按目前通行的两层模型：

- **技能**： [Agent Skills](https://agentskills.io/specification) 开放规范。每个技能一个目录，至少有 `SKILL.md`（YAML frontmatter + 正文），可选 `scripts/`、`references/`、`assets/`。
- **工具**：Python 函数上标 `@tool`，用类型注解和 docstring 生成 JSON Schema；按包扫描注册。这是现在 Python agent 里最常见的写法，不绑死 LangChain。

```
skills/
  pdf-extract/
    SKILL.md
    scripts/
    references/

src/witty_agent/tools/
  builtin.py      # 底座工具
  xxx.py          # 继续往这个包加 @tool
```

加载约定：

1. `list_skills()` 只读 `name` / `description`（启动时渐进披露第一层）。
2. `load_skill(name)` 才读正文和子目录。
3. 技能名必须是小写+数字+单连字符，且与目录名一致。
4. `list_tools()` 扫描 `config/runtime.toml` 里 `[tools].packages`。内核工具名见 `witty_agent.kernel_surface.KERNEL_TOOLS`，业务包同名会被拒绝。
5. 技能目录来自 `[skills].paths`，额外路径用环境变量 `WITTY_SKILLS_PATH`。
6. 内置斜杠命令见 `KERNEL_COMMANDS`，`unregister` 不能卸。

```python
from witty_agent import list_skills, load_skill, list_tools, tool

@tool
def lookup(code: str) -> str:
    """按编码查询条目。

    Args:
        code: 业务编码
    """
    return code
```

业务技能放仓库 `skills/`，不要写进核心循环。

## 环境

- Python：本仓库 `.python-version` 锁定 **3.12**；对外包支持 **3.10+**
- 包管理：`uv`（本机已安装即可，解释器由 uv 拉取）
- 虚拟环境：项目根目录 `.venv`（`uv sync` 自动创建）
- 依赖源：清华 PyPI，阿里云备用，见 `pyproject.toml`

## 常用命令

```bash
# 创建/同步环境（含锁定版本）
uv sync

# 增加运行时依赖
uv add <package>

# 增加开发依赖
uv add --dev <package>

# 在项目环境里跑命令
uv run python -c "import witty_agent"
uv run witty-agent

# 重新锁定依赖
uv lock
```

不要用 `pip install` 往系统或其它环境装这个项目的包。新增依赖必须写进 `pyproject.toml`（通过 `uv add`），并提交 `uv.lock`。

## 改动与问题账本

所有落盘改动、缺陷、现存问题都记在 `docs/change_maintenance/`，不要只留在对话里。

| 文件 | 记什么 |
|------|--------|
| `CHANGELOG.md` | 每次改文件后的全局索引：时间、问题、改了什么、原因、验证、残留风险 |
| `DEFECTS.md` | 缺陷：现象、复现、原因、状态（open / fixed） |
| `UNRESOLVED.md` | 已知但这次不修的问题，以及为什么先挂着 |
| `PROGRESS.md` | 当前目标、已完成、下一步 |

规则：

1. 改了代码、配置、脚本、skill、规则或会影响行为的文档，必须在 `CHANGELOG.md` **追加**一条，不要改写旧条目。
2. 发现缺陷先记 `DEFECTS.md`；修完把状态改为 `fixed`，并在 CHANGELOG 里交叉引用。
3. 这次不修、但后面还会踩到的问题，记 `UNRESOLVED.md`。
4. 只读排查、搜索、没落盘的试验可以不记；试验影响了最终方案的，在 CHANGELOG 的 Rationale 里写一句。
5. 以后某个专题有自己的 CHANGELOG / PROGRESS / UNRESOLVED 时，专题账本和全局索引都要更新。全局日志是索引，不是替代。

CHANGELOG 条目格式：

```markdown
## [YYYY-MM-DD HH:MM CST] <短标题>
- Problem: <用户问题 / 缺陷 / 需求>
- Changes: <关键文件和行为变化>
- Rationale: <为什么这样做；有否决方案就写>
- Verification: <命令或手工检查及结果>
- Risk: <残留风险；没有就写「无」>
```

## 目录

- `src/witty_agent/`：主包（Python）
- `src/witty_agent/async_bridge.py`：同步/异步桥，`afoo` 的 `foo` 包装只走这里
- `src/witty_agent/logging.py`：统一日志入口
- `src/witty_agent/prompts.py`：提示词加载，禁止在此存放正文
- `src/witty_agent/skills.py`：Agent Skills 加载
- `src/witty_agent/tools/`：`@tool` 注册与底座工具
- `config/prompts.toml`：提示词
- `config/runtime.toml`：日志级别、技能目录、工具包
- `skills/`：业务技能（SKILL.md）
- `pyproject.toml`：项目元数据和 uv 配置
- `uv.lock`：锁定依赖，必须入库
- `docs/change_maintenance/`：改动、缺陷、未决问题、进度
- `apps/desktop/`：Electron 壳，只调用 Python HTTP API（`uv run witty-agent serve`）
