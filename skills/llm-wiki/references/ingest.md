# Ingest workflow

Triggered when the user provides a new source (pastes text, uploads a file, gives a URL, or says something like "add this to the wiki", "ingest this article", "file this podcast transcript").

Always re-read the project's schema file first if you haven't already this session — it may specify supervised vs. batch ingest, naming conventions, and frontmatter rules that override the defaults below.

## Steps (supervised — the default)

1. **Land the source in `raw/`** unmodified, using the schema's naming convention (or a sensible default: `raw/YYYY-MM-DD - Short Title.ext`). If the source is a URL and the user hasn't already clipped it, fetch and save the content; note the original URL in the resulting source page.
2. **Read the source.** For sources with inline images (e.g. web-clipped articles), read the text first, then view the images that matter — don't try to process text+images in one pass.
3. **Discuss key takeaways with the user** before writing anything, unless the schema specifies batch/unsupervised mode. A couple of sentences: what's new or notable here, what it seems to touch. Let the user redirect emphasis before you commit to wiki edits.
4. **Write a source summary page** under the wiki's sources location (e.g. `wiki/sources/`). Include: what the source is, a concise summary in your own words, key claims/data points, and a link back to the raw file.
5. **Update relevant entity/concept pages.** A single source can reasonably touch 10–15 pages. For each entity or concept the source discusses:
   - If the page exists, update it — don't just append; integrate the new information where it belongs, and add a link to the new source page.
   - If the page doesn't exist yet and the entity/concept is substantial enough to warrant one, create it.
   - If new information **contradicts** an existing claim, don't silently overwrite. Note both claims and the discrepancy (e.g. "As of [old source], X. However, [new source] states Y — unresolved as of [date]."). Flag this prominently to the user.
6. **Update `wiki/index.md`** with the new source page and any new entity/concept pages created. See `references/index-and-log.md`.
7. **Append an entry to `wiki/log.md`** in the standard prefix format. See `references/index-and-log.md`.
8. **Report back concisely**: which pages were created, which were updated, and any contradictions flagged. Don't dump full page contents into chat — the user can open the wiki (e.g. in Obsidian) to browse.

## Batch mode

If the schema specifies batch ingest: process each source through steps 1–2 and 4–7 without pausing for discussion at step 3, then give the user a single consolidated summary of everything that changed across all sources, with contradictions called out clearly.

## Notes

- Never edit files under `raw/`.
- If a source is large (e.g. a long paper or transcript), it's fine to skim for structure first, then read sections in more depth as needed — you don't have to load the whole thing into working memory to write a good summary page, but don't fabricate details you didn't actually read.
- If the wiki uses YAML frontmatter, populate it (type, dates, source links, tags) per the schema.
