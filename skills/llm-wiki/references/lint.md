# Lint workflow

Triggered when the user asks you to check, clean up, or audit the wiki, or periodically as good practice once a wiki has grown to a meaningful size (roughly a few dozen pages or more) — feel free to proactively suggest a lint pass if you notice signs of drift while doing other work, but don't run one unprompted.

## Default checklist

Use the schema file's lint checklist if it customizes this; otherwise use this default:

1. **Contradictions between pages.** Two pages making incompatible claims about the same entity/fact without cross-referencing each other.
2. **Stale claims.** A page states something that a more recent source (per `wiki/log.md` dates) has superseded, but the page hasn't been updated.
3. **Orphan pages.** Pages with no inbound links from anywhere else in the wiki (check via grep/search across `wiki/`, or ask the user to check Obsidian's graph view for orphans if they use Obsidian).
4. **Missing pages.** Concepts or entities mentioned repeatedly across multiple pages but that don't have their own dedicated page yet.
5. **Missing cross-references.** Two pages that are clearly related (same entity, overlapping topic) but don't link to each other.
6. **Data gaps.** Open questions or thin pages that a targeted web search or a specific new source could fill.

## Steps

1. Read `wiki/index.md` and (if present) `wiki/log.md` for an overview of what exists and when it was last touched.
2. Scan pages methodically against the checklist above — use `grep`/search across `wiki/` for link patterns rather than eyeballing every file if the wiki is large.
3. Produce a findings report, grouped by checklist category, with specific page names/paths — not vague generalities.
4. **Don't auto-fix silently.** Present findings to the user and propose fixes (merge these two claims and flag the discrepancy, add this cross-reference, create this missing page, etc.), then act on what they confirm. Auto-fixing dozens of pages unsupervised risks compounding a wrong guess across the wiki.
5. Append a `lint` entry to `wiki/log.md` summarizing what was found and what was fixed.

## Notes

- A lint pass is a good moment to suggest new questions worth investigating or new sources worth finding, per the general pattern — mention these as suggestions, not as edits you're making unprompted.
