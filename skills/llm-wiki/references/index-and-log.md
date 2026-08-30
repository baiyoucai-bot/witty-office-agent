# index.md and log.md conventions

Two special files live at `wiki/index.md` and `wiki/log.md`. They serve different purposes and both should be updated on every ingest and every filed-back query answer.

## index.md — content-oriented

A catalog of everything in the wiki, organized by category, so a query can find relevant pages without scanning every file. Update it whenever a page is created; touch it lightly when a page is meaningfully updated (e.g. if the one-line summary changes).

Template for a new wiki:

```markdown
# Index

Catalog of all pages in this wiki. Updated on every ingest.

## Sources

- [Title](sources/YYYY-MM-DD%20-%20Title.md) — one-line summary. (YYYY-MM-DD)

## Entities

- [Name](entities/Name.md) — one-line summary.

## Concepts

- [Concept](concepts/Concept.md) — one-line summary.

## Synthesis / filed answers

- [Title](synthesis/Title.md) — one-line summary. (YYYY-MM-DD)
```

Adjust the categories to match whatever the project's schema file defines — this is illustrative, not fixed.

## log.md — chronological

An append-only record of what happened and when: ingests, queries filed back, lint passes. Each entry starts with a **consistent prefix** so it stays parseable with plain unix tools, e.g.:

```
grep "^## \[" wiki/log.md | tail -5
```

Template for a new wiki:

```markdown
# Log

Append-only. Do not edit past entries except to fix typos.

## [YYYY-MM-DD] ingest | Source Title
- Created: sources/....md, entities/....md
- Updated: entities/....md, concepts/....md
- Notes: any contradictions flagged, anything notable.

## [YYYY-MM-DD] query | Short description of the question
- Filed as: synthesis/....md (or "not filed — one-off answer")

## [YYYY-MM-DD] lint
- Findings: N contradictions, N orphan pages, N missing cross-refs
- Fixed: what was actually changed
```

Always append new entries at the bottom (or top, if the schema specifies reverse-chronological — pick one convention per project and stay consistent within it).

## Entry format rules

- Keep the `## [YYYY-MM-DD] <type> | <short title>` header exact and consistent — it's what makes the log grep-able.
- `<type>` is one of `ingest`, `query`, `lint` by default; the schema file may extend this.
- Keep entries short — a handful of bullet points, not a re-summary of the content itself (that belongs on the actual wiki page).
