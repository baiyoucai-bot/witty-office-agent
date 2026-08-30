# 长文档写法从哪来

本技能是通用工程循环，不是某家产品的移植。下面只记收了什么、为什么没整段搬。

## 收进来的

| 来源 | 收了什么 | 落在哪 |
|------|----------|--------|
| [Stanford STORM](https://github.com/stanford-oval/storm) | 先综合出提纲，再按节扩写；下一节只带提纲和已核实材料，不把全书塞进上下文 | `outline.md` 先锁；一轮一章；`continuity.md` |
| [GPT-researcher](https://github.com/assafelovic/gpt-researcher) | 规划 / 执行 / 汇总拆开；出处进账，不靠模型默写数字 | `sources.toml` + `[cite:id]`；`ledger.toml` + `[num:id]` |
| Karpathy LLM Wiki | 工作区文件是真相，对话只是一次编辑 | 工程目录；`check_doc.py` 对文件而不是对聊天 |
| Anthropic 办公技能 | 静默失败用脚本抓（公式被覆盖、题注悬空），不靠模型自查 | `check_doc.py` / `report.py` 退出码 |
| 学术综述的证据驱动流程（选题→证据闭环→样章→整合） | 开写前盘料并设「料够不够」门；证据不足**定向回补**而不是重搜；**核心样章试写通过再量产**；整合审计查术语/论断/重复/引用/衔接，实质冲突回责任章节；正文与引用锁定后**延后制图** | 步骤 2 盘料门；步骤 3 样章 + `check_doc.py --chapter`；步骤 4 回补账；步骤 6 整合审计；步骤 7 制图后置 |

## 没搬的

- STORM 的多视角对话生成提纲、默认上网检索：底座默认内网，提纲由用户或 profile 提供。
- GPT-researcher 的多角色调研团：那是另一个产品。本技能一个循环写一章。
- 某行业可研范本、固定条款：属于业务 profile，往 `references/profiles/` 加 JSON，不要写进脚本。
- 综述流程里的发刊环节：期刊定位、Cover Letter、投稿终检、版权核查、审稿返修。工作文档没有「投稿」，交付就是导出 + 校验结论；对应物已在步骤 5/7。

文种差异只允许出现在 profile 的章节清单里。
