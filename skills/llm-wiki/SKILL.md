---
name: llm-wiki
description: Build and maintain a persistent, interlinked markdown knowledge-base wiki from a curated collection of source documents, instead of doing one-shot RAG-style retrieval. Use this whenever the user wants to set up a personal knowledge base, research wiki, book-companion wiki, competitive-analysis or due-diligence tracker, course-notes wiki, or team knowledge base that should accumulate and cross-reference over time. Also use it whenever the user asks to "ingest"/"add"/"file" a new source (article, paper, transcript, podcast note, chapter) into an existing wiki, asks a question that should be answered from and then filed back into a wiki, or asks to "lint"/health-check/audit a wiki for contradictions, stale claims, or orphan pages. Trigger even if the user doesn't say "wiki" explicitly but describes wanting to accumulate and organize knowledge from many sources over time rather than just search a pile of files once.
network: public
metadata:
  triggers: 知识库 个人知识库 资料库 建知识库 归档进知识库 交叉引用 知识沉淀 wiki llm-wiki
---

# LLM Wiki

A pattern for building a personal, LLM-maintained knowledge base: a persistent, compounding wiki that sits between the user and their raw sources, instead of re-deriving synthesis from scratch on every query (as plain RAG / file-upload chat does).

Read this whole file before doing anything. It is short by design — the heavy lifting for each operation lives in `references/`.

## The core idea

Three layers, always kept separate:

1. **`raw/`** — the user's curated source documents (articles, papers, transcripts, images, data). Immutable. You read from it, you never edit it.
2. **`wiki/`** — markdown pages you write and maintain: an overview, entity pages, concept pages, comparisons, syntheses. You own this layer entirely.
3. **The schema** — a single file at the wiki root (conventionally `AGENTS.md`, or `CLAUDE.md` if the user is on Claude Code) that documents how *this specific* wiki is organized: page types, frontmatter conventions, naming rules, and which of the optional operations below are in use. Co-evolve it with the user; re-read it at the start of every session in this project.

Plus two special files inside `wiki/`: `index.md` (content-oriented catalog of every page) and `log.md` (append-only chronological record). See `references/index-and-log.md` for their exact conventions.

## Decision tree — figure out which mode you're in

1. **No wiki exists yet in this project** (no `raw/`, `wiki/`, or schema file) and the user wants one → go to **Setup**, below.
2. **A wiki exists and the user hands you a new source** (pastes text, uploads a file, gives a URL, says "ingest/add/file this") → follow `references/ingest.md`.
3. **A wiki exists and the user asks a question** → follow `references/query.md`.
4. **A wiki exists and the user asks you to check/clean/audit it, or you notice it's grown large and messy** → follow `references/lint.md`.
5. **Unsure which** → read the schema file and `wiki/index.md` first; usually the mode becomes obvious. If still ambiguous, ask.

Always start a session in an existing wiki project by reading the schema file, then `wiki/index.md`. That's your map — don't re-read every wiki page up front, drill in only as needed (progressive disclosure applies to the wiki itself, not just to this skill).

## Setup (first time in a project)

Don't impose a rigid structure — this pattern is intentionally modular. Have a short conversation to nail down:

- **Domain**: personal tracking, research topic, book companion, team/business wiki, competitive analysis, course notes, hobby deep-dive, etc.
- **Source type(s)**: text articles, PDFs, images, Slack/meeting transcripts, mixed. This determines whether you need the image-handling workaround (see `references/schema-template.md`).
- **Ingest style**: one-source-at-a-time with the user reviewing each summary (default, recommended for most users), or batch ingest with less supervision.
- **Output formats wanted for queries**: plain markdown pages, comparison tables, Marp slide decks, charts, canvases — only set up what they'll actually use.
- **Scale expectation**: rough number of sources. Under ~100 sources / a few hundred pages, `index.md` alone is enough — no search tooling needed. If they expect much larger scale, mention `qmd` (https://github.com/tobi/qmd), a local hybrid BM25/vector search engine for markdown with CLI + MCP server, as an optional addition later. Don't set this up preemptively.

Then:

1. Run `scripts/init.sh` to scaffold the project — it's idempotent (safe to re-run, never overwrites existing files) and does the mechanical part for you:

   ```bash
   bash /path/to/this/skill/scripts/init.sh \
     --name "Wiki Name" \
     --domain "One-line description of what it tracks" \
     --dir path/to/project \
     --schema-file AGENTS.md   # or CLAUDE.md
   ```

   This creates `raw/`, `wiki/` (with `sources/`, `entities/`, `concepts/`, `synthesis/` subfolders — adjust/remove any that don't fit once the schema is filled in), `wiki/index.md`, `wiki/log.md` (seeded per `references/index-and-log.md`), and a schema file seeded from `references/schema-template.md` with `{{Wiki name}}` and `{{domain}}` already filled in from your flags.
2. Open the generated schema file and fill in the remaining `{{placeholders}}` (page types, frontmatter convention, naming, ingest style, output formats, search) based on the setup conversation. This becomes the project's own schema — it is the thing that makes you a disciplined maintainer of *this* wiki rather than a generic chatbot next time.
3. If the user already has sources ready, offer to ingest the first one right away so they see the pattern in action.

Never invent domain-specific page categories (e.g. "Characters", "Companies") without asking — these should come from what the user is actually tracking.

## Operations at a glance

| Operation | When | Reference |
|---|---|---|
| Init a new wiki | First time in a project | `scripts/init.sh` (invoked from **Setup**, above) |
| Ingest a source | User provides new material | `references/ingest.md` |
| Answer a query | User asks a question about the wiki's contents | `references/query.md` |
| Lint / health-check | Periodic maintenance or user request | `references/lint.md` |
| Index & log conventions | Needed by all three operations above | `references/index-and-log.md` |
| Schema file template | Setup, or when evolving conventions | `references/schema-template.md` |

## Principles to hold onto throughout

- **The wiki compounds.** Every ingest and every good query answer should leave the wiki richer than before — update cross-references, don't just append.
- **Raw sources are immutable.** Never edit files under `raw/`.
- **Stay involved with the user for ingest by default**, unless they've told you (via the schema file) they want batch/unsupervised ingest.
- **File good answers back into the wiki**, not just into chat. A comparison, an analysis, a synthesized answer — these are wiki material, not throwaway chat output, unless the user clearly just wants a quick one-off answer.
- **Flag contradictions, don't silently overwrite.** When a new source conflicts with an existing claim, note both and the discrepancy rather than deleting the old claim.
- **It's a git repo of markdown.** If the project is (or should be) under version control, treat commits as natural checkpoints — e.g. one commit per ingest — but don't set up git unprompted if the user hasn't already.
