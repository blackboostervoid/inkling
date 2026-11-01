# Interactive path — the eleven steps

Disclosed from the [`compile`](../SKILL.md) skill, §B. Moved out of SKILL.md 2026-08-27 under
skill-craft rule 2.2 (reference behind a pointer), the same move `note-shape.md` got on 08-25.

`[!]` **THE REASON IS A COST THE HEADLESS PATH WAS PAYING AND COULD NEVER USE.** These steps wait
for a human at Step 2 and Step 6. `--auto` has no human by construction, so every unattended run
loaded ~195 lines it was structurally incapable of taking. Nothing here changed; only where it
lives did.

§A points into this file by step number, and those pointers are LOAD-BEARING: the auto path reuses
Step 3's tag-vocabulary rules, Step 4a's frontmatter, Steps 8/8b's lint and grounding gate, and
Step 9/10's archive and log shape. Read the named step when §A names it.

---

# §B. Interactive path (the default)

## Step 1 — Load current state

`[!]` **DO NOT LOAD THE WIKI. RETRIEVE AGAINST IT.** The corpus is 155 notes / ~400,000 tokens and
it grows; the queue is three documents of twenty lines. Reading the library to place a postcard is
what made this skill burn ten minutes and produce nothing. The retrieval layer exists, is built,
and answers in under a second — **use it and load nothing**:

- `mcp__inkling__vault_search` — searches note BODIES at section granularity, returns the matching
  headings. First choice.
- `py "$CLAUDE_CONFIG_DIR/hooks/recall/semantic.py" search "<the finding>" -k 8 --rerank` — the same
  index through Bash. Use when the MCP tool is unavailable; a headless run has no MCP.
- `$CLAUDE_CONFIG_DIR/hooks/recall/index_compiled.json` — 120 entries of `name / summary / tags /
  related`, ~38k tokens, when you genuinely need the whole list at once. Still a tenth of the vault.

A `Glob` over the content folders is allowed for ONE thing: confirming a slug you already have.
Never to survey what exists.

1. Know what pages exist **by asking, not by reading**: one `vault_search` per candidate finding,
   using the finding's own words. The code wiki is **INDEX-less** (retired): a note's frontmatter is
   its own source and the recall corpus is built from it. Do NOT read or write `INDEX.md`.
2. List files in `$CLAUDE_WIKI_PATH/raw/` (top level only — do not recurse).
3. If no unprocessed files → report "Nothing to compile" and stop.

## Step 2 — Analyze raw files

For each file in `raw/`:
1. Read the file.
2. Extract entities and concepts (APIs, patterns, tools, techniques).
3. For each entity, decide UPDATE vs CREATE:
   - Match by note filename/slug across the content folders (`Glob`); if uncertain, grep `tags:` in candidate frontmatter, or run `py "$CLAUDE_CONFIG_DIR/hooks/recall/semantic.py" search "<entity>"` to find an existing page by meaning (name may differ).
4. Classify mixed content: one raw file may produce several entity actions.

Present the compilation plan to the user:

```
Found N files in raw/:
- file1.md → UPDATE apis/anthropic-api.md (new info about X)
- file2.md → CREATE patterns/new-pattern.md
- file3.md → UPDATE + CREATE (multiple entities)

Proceed? (yes/no)
```

Wait for confirmation before continuing.

## Step 2.5 — Web enrichment (optional)

If raw files reference GitHub repos, APIs, or external docs:
1. Extract URLs from raw files.
2. `WebFetch` each URL (or delegate batch to an `Agent`).
3. Use fetched content to ground the wiki page in primary sources instead of marketing-style summaries from raw text.

## Step 3 — Build tag vocabulary

Before writing any frontmatter, build the vault's existing tag vocabulary so new pages reuse canonical tags instead of minting synonyms.

1. Build the canonical tag set from `$CLAUDE_CONFIG_DIR/hooks/recall/index_compiled.json` — 120 entries, each with its tags, already aggregated. **One read, and it is the only sanctioned way.** The `Grep` over `$CLAUDE_WIKI_PATH/**/*.md` that used to stand here as an equal alternative is withdrawn: it walks 155 files to rebuild what that one file already holds, and an OR between a cheap path and an expensive one gets resolved toward the expensive one often enough to matter. If the index is missing or unreadable, say so in the report and fall back to the grep — a stated fallback, never a silent choice.
2. Build an in-memory set: `canonical_tags = {every tag already seen}`.
3. For each proposed new tag:
   - Exact match (case-insensitive, trimmed) → reuse canonical form.
   - Obvious synonym (a plural, an abbreviation, the same word in another script) → reuse existing.
   - Truly new concept → add to proposed set, will become canonical after write.
4. Report at the top of the diff preview:
   ```
   Tag vocabulary lock:
   - Reused: [tag1, tag2, ...]
   - New: [tag3, tag4]
   ```

## Step 4 — Compose

### 4a-4c. Frontmatter, registration, body structure

**`references/note-shape.md`** -- the mandatory frontmatter shape, why there is no INDEX entry
to write, and the CREATE body skeleton. Read it before composing; it also carries the
write-correctness laws the composition must satisfy.

## Step 5 — Change classification (core immutability)

For each target page, classify every proposed edit into one of two buckets:

### Form-only (NO approval needed — just do it)

- Add / normalize / reorder tags (after vocabulary lock).
- Write or rewrite `summary` field.
- Rename the page title (H1) or filename.
- Reorder sections without touching their text.
- Normalize markdown (headings depth, list style, code fences).
- Bump `updated` date.
- Append to `sources` / `related`.
- **Append new H2 sections** with new material to the end of the file.
- Add `> [!conflict]` callouts below existing sections when new info contradicts.

### Content change (APPROVAL REQUIRED — Step 6 gate)

Any of these triggers the approval gate:
- Modifying existing sentences inside an existing section.
- Deleting any existing prose (even "obsolete" parts).
- Rewriting existing code examples.
- Lowering `confidence`.
- Removing tags or sources.
- Overwriting an existing `summary` (first write is form-only; rewriting an existing one is content change).

**Rule of thumb:** if the diff of an existing page contains any `-` lines beyond whitespace/reordering, it's a content change.

## Step 6 — Approval gate

Before `Write`/`Edit` executes:

1. Build the full change set in memory.
2. For each target page, emit a **unified diff preview**:
   - CREATE → show full new file.
   - UPDATE form-only → show diff with note: `[form-only, auto-applied]`.
   - UPDATE content change → show diff with note: `[CONTENT CHANGE — approval required]`.
3. If no content-change items present → apply all form-only changes and proceed.
4. If content-change items present → present them together and wait for:
   - `yes` — apply all.
   - `yes form-only` — apply form changes, skip content changes.
   - `edit` — user describes adjustments, skill rebuilds the diff.
   - `no` — abort, leave raw file in place.

Diff preview example:
```
=== patterns/prompt-engineering.md [CONTENT CHANGE — approval required] ===
@@ frontmatter @@
+  - new-tag
-summary: "the old summary"
+summary: "the updated summary (2+ tag keywords)"
@@ body @@
 ## Few-shot examples
-3-5 examples give the best ROI
+3-7 examples give the best ROI (new data, 2026)
```

## Step 7 — Write

Only after approval:
1. `Write` / `Edit` each target page. That is the whole write — the note's frontmatter registers it.
2. No INDEX to update (retired). A CREATE just needs to land in a content folder with frontmatter `tags`; the compiler picks it up at next session start.

## Step 8 / 8b — Lint-on-touched, then the grounding gate

Both check lists live in **`references/checks.md`** (moved 2026-08-25, unchanged). The order
is the mechanism: the grounding gate reads the RAW file, so it runs before Step 9 deletes it.

## Step 9 -- Archive consumed sources, and verify EACH ONE BY NAME

```bash
py "$CLAUDE_CONFIG_DIR/skills/compile/scripts/compile_judge.py" --archive <name.md> [<name.md> ...]
```

Name only the sources that compiled cleanly; leave anything that errored in `raw/` for the next
run. **Never before Step 8b** -- the grounding gate reads the RAW file, so archiving first destroys
the only thing it can check. Omitting the names archives whatever is still in the queue, which is
right only when every staged document was consumed.

The command moves each file into `raw/_compiled/` and then asks, per file, whether it is there
under its own name and non-empty. It exits non-zero and NAMES the ones that are not.

`[!]` **THIS REPLACED A TOTAL COUNT, and the replacement is the point.** A run on 2026-08-26 logged
its own check as `61 + 2 = 63`, reconciled perfectly, and left `obs-2026-08-26-970847b8.md` sitting
un-moved in `raw/` -- because a SUM cannot say WHICH files moved. Cost: one redundant pass over a
document already dispositioned, found only when a later run tripped over the file. A count is a
checksum over the population; "did THIS member land" is a different question, and only one of them
was being asked. **Do not re-introduce a count here, not even beside the per-name check** -- an
instrument wider than what it verifies reads as coverage.

## Step 10 — Log

Append to `$CLAUDE_WIKI_PATH/log.md`, one entry per compiled source:

```markdown
## [YYYY-MM-DD] ingest | Source Title
- Action: created page-name.md / updated page-name.md
- Pages touched: [list]
- Tag reuse: N reused / M new
- Content changes: yes/no (approved)
```

## Step 11 — Report

```
Compilation complete:
- Created: N new pages [list]
- Updated: M existing pages [list] (form-only: K, content-approved: L)
- Tag vocabulary: reused X / new Y
- Deleted: P consumed source files from raw/

Log appended. (No INDEX — notes register via frontmatter; recall corpus rebuilds next session.)
```

