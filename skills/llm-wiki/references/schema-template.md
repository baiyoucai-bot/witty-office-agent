# Schema template

This is the template to copy into a new wiki project's root as `AGENTS.md` (OpenCode / Codex / most agents) or `CLAUDE.md` (Claude Code). Fill in every `{{...}}` placeholder based on the setup conversation with the user, then delete any optional sections that don't apply. This file is the wiki's own configuration — it should be short, concrete, and specific to this wiki, not a copy of the general pattern.

Once created, this file is a living document. When the user says things like "always do X from now on" or you both discover a convention that works, update this file, not just your own behavior for the session.

---

```markdown
# {{Wiki name}} — Schema

Domain: {{one-line description, e.g. "Research wiki on X" / "Personal health & goals tracker" / "Companion wiki for reading Y"}}

## Layers

- `raw/` — immutable source documents. Never edit.
- `wiki/` — LLM-maintained markdown pages. This is what gets read and updated.
- This file — conventions, re-read at the start of every session.

## Page types

{{List the categories of pages this wiki uses, e.g.:}}
- **Entities** (`wiki/entities/`) — people, organizations, characters, products, etc. One page per entity.
- **Concepts** (`wiki/concepts/`) — recurring ideas, themes, mechanisms.
- **Sources** (`wiki/sources/`) — one summary page per ingested source.
- **Comparisons / syntheses** (`wiki/synthesis/`) — cross-cutting analysis, filed query answers.
- `wiki/index.md` — catalog of all pages.
- `wiki/log.md` — chronological record of ingests, queries, lints.

## Frontmatter convention

{{If using YAML frontmatter (needed for Obsidian Dataview), specify fields, e.g.:}}
```yaml
---
type: entity | concept | source | synthesis
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources: [list of source page links]
tags: [...]
---
```
{{If not using frontmatter, say so explicitly: "No frontmatter — plain markdown only."}}

## Naming conventions

{{e.g. "Entity pages: Title Case, e.g. `wiki/entities/Jane Doe.md`. Source pages: `wiki/sources/YYYY-MM-DD - Short Title.md`."}}

## Ingest style

{{Choose one:}}
- **Supervised (default)**: ingest sources one at a time; discuss key takeaways with the user before writing; show a summary of what will change before editing many pages.
- **Batch**: ingest multiple sources with minimal supervision; report a summary of all changes at the end.

## Image handling

{{Only include this section if sources contain images, e.g. clipped web articles.}}
Sources may contain inline markdown images (e.g. from Obsidian Web Clipper) pointing at `raw/assets/`. LLMs can't read markdown with inline images in one pass — read the text first, then separately view the referenced images that matter for context.

## Output formats for queries

{{List only what's actually wanted, e.g.:}}
- Plain markdown wiki pages (default)
- Comparison tables
- Marp slide decks (`.md` with Marp frontmatter, saved to `wiki/decks/`)
- Charts (matplotlib, saved as images and linked)

## Search

{{Default for small wikis:}}
No dedicated search tool yet — `wiki/index.md` is the entry point for every query. Read it, then drill into the relevant pages it links to.

{{If the wiki has grown large, replace with:}}
Search via `qmd` (https://github.com/tobi/qmd) — hybrid BM25/vector search over `wiki/`. Use the CLI/MCP tool before falling back to `index.md`.

## Lint checklist

{{Customize / trim as needed — see references/lint.md in the skill for the full default checklist.}}
- Contradictions between pages
- Stale claims superseded by newer sources
- Orphan pages (no inbound links)
- Important concepts mentioned repeatedly but with no dedicated page
- Missing cross-references between clearly related pages
- Data gaps a web search could fill

## Notes / evolving conventions

{{Free-form space for anything the user and agent discover over time — append here rather than restructuring the whole file.}}
```
