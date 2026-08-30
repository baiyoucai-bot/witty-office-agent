# Query workflow

Triggered when the user asks a question that should be answered from the wiki's accumulated knowledge, rather than "add this source" or "clean up the wiki".

## Steps

1. **Read `wiki/index.md` first.** It's the map — a catalog of every page with a one-line summary, organized by category. Identify which pages look relevant before opening anything.
2. **Drill into the relevant pages** the index points to. Only read what's needed to answer the question; don't re-read the whole wiki.
3. **If a search tool is configured** (e.g. `qmd`, per the schema file) and the wiki is large, use it to find candidate pages instead of scanning the full index by hand — then still open and read the actual pages before answering.
4. **Synthesize an answer with citations** — reference which wiki pages (and, where relevant, which underlying raw sources) support each claim. This is the payoff of the pattern: the cross-references already exist, so synthesis should be fast, not a re-derivation from scratch.
5. **Pick the right output form** for the question, per the schema's configured output formats:
   - Default: a direct answer in chat, or a new/updated markdown page.
   - A comparison → a markdown table.
   - A request for a presentation → a Marp deck.
   - A request for a chart → generate one (e.g. matplotlib) and link/embed it.
6. **Offer to file the answer back into the wiki** if it represents real synthesis (a comparison, an analysis, a connection across sources) rather than a trivial lookup. This is what makes explorations compound instead of disappearing into chat history. If the user agrees:
   - Write it as a new page (e.g. under `wiki/synthesis/`) or fold it into an existing relevant page.
   - Link it from `wiki/index.md`.
   - Append an entry to `wiki/log.md`.
7. If the wiki doesn't actually contain enough to answer the question, say so plainly rather than guessing — and offer to note the gap (see `references/lint.md` on data gaps) or search the web for candidate sources to ingest.

## Notes

- Don't pad the answer with unrelated wiki content just because it was skimmed while searching.
- If pages disagree with each other (a flagged contradiction from a past ingest), surface that in the answer rather than picking one silently.
