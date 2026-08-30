# 长文档工作区

工程是版本化产物，不是一次模型输出。一轮只写一章。

```
<项目>/
  项目卡.md       文种、profile、纪律
  outline.md      `- [slug] 标题`，唯一结构真相
  sources.toml    [id] title/path，章节用 [cite:id]
  ledger.toml     [id] text，章节用 [num:id]
  continuity.md   每章写完追加 ## slug
  glossary.md     术语必须用登记写法
  chapters/       一章一个 md，文件名 = slug
  assets/         图片，路径写相对工程根
  sources/        import_source.py 抄过来的原文
```

## 流程

1. `init_project.py` 起工程，选 profile（generic / feasibility / outline-design / detailed-design，或中文别名 可研/概设/详设）。
2. 旧稿先抽成文本，再 `import_source.py` 登记。不要对着 .docx/.pdf 改。
3. 盘料再锁提纲：每章的料在哪，缺料列回补清单，撑不起的章砍或并。改提纲先改 `outline.md`，再补/删 `chapters/`。不要只改一处。
4. 样章先行：证据最密的一章先写，`check_doc.py --chapter <slug>` 过了、口径定了才量产其余章。
5. 每章：要点、依据 `[cite:id]`、关键数字 `[num:id]`、缺料写「待核实」并定向回补。写完改 `continuity.md`。
6. 结构齐了全工程 `check_doc.py`，再做整合审计（术语、论断、重复、引用、衔接）；实质冲突回责任章改。
7. 正文过审后才画 `assets/` 图片；导出 Word 走 `word-docx` 的 `report.py --toc`，不要 pandoc 当定稿。

## 文种

profile 只提供必含章。不要把某个行业的条款写进脚本。业务差异用另一份 profile JSON 往上加。

写法来源见 `method.md`。
