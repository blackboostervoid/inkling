---
name: compile
description: Compile raw documents from Obsidian vault into structured wiki pages. Merges into existing pages or creates new ones. Triggers on - "/compile", "process raw", "compile my notes", "turn raw into wiki pages".
context: fork
allowed-tools: Read, Write, Edit, Glob, Grep, Agent, WebFetch, WebSearch, Bash
---

# Compile — Raw to Wiki

Compiles unprocessed documents from `$CLAUDE_WIKI_PATH/raw/` into structured LLM wiki pages.
When raw files reference external sources (GitHub repos, docs), fetches additional context to enrich wiki pages.

Hard principle: **core content of existing pages is immutable by default**. The skill freely standardizes form (frontmatter, tags, summary, title, section ordering, markdown hygiene), but any change to actual technical text requires explicit user approval.

---

## Step 1 — Load current state

1. Know what pages exist: `Glob` the content folders (`apis/ patterns/ tools/ concepts/ discoveries/ research/`) — each note's filename is its slug, its frontmatter carries tags + summary. The code wiki is **INDEX-less** (retired): the note's frontmatter IS the source; the compiler globs notes into the recall corpus. Do NOT read or write `INDEX.md`.
2. List files in `$CLAUDE_WIKI_PATH/raw/` (top level only — do not recurse).
3. If no unprocessed files → report "Nothing to compile" and stop.

## Step 2 — Analyze raw files

For each file in `raw/`:
1. Read the file.
2. Extract entities and concepts (APIs, patterns, tools, techniques).
3. For each entity, decide UPDATE vs CREATE:
   - Match by note filename/slug across the content folders (`Glob`); if uncertain, grep `tags:` in candidate frontmatter, or run `python3 "$CLAUDE_CONFIG_DIR/hooks/recall/semantic.py" search "<entity>"` to find an existing page by meaning (name may differ).
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

1. Build the canonical tag set from existing notes' frontmatter `tags` — `Grep` the `tags:` field across `$CLAUDE_WIKI_PATH/**/*.md`, or read the compiled corpus `$CLAUDE_CONFIG_DIR/hooks/recall/index_compiled.json` (each entry lists its tags — fastest, already aggregated).
2. Build an in-memory set: `canonical_tags = {every tag already in use}`.
3. For each proposed new tag:
   - Exact match (case-insensitive, trimmed) → reuse canonical form.
   - Obvious synonym or transliteration of an existing tag → reuse the existing one.
   - Truly new concept → add to proposed set, will become canonical after write.
4. Report at the top of the diff preview:
   ```
   Tag vocabulary lock:
   - Reused: [tag1, tag2, ...]
   - New: [tag3, tag4]
   ```

## Step 4 — Compose

### 4a. Standard frontmatter (mandatory shape)

```yaml
---
type: api | pattern | tool | concept | discovery
tags:
  - tag-one
  - tag-two
  - ...
summary: "2-3 sentences in the vault's working language: what this page is, why it exists,
         and the key insight. See VAULT_CONTRACT.md - state the conclusion, not the topic."
confidence: high | medium | low
sources:
  - "Attribution 1 — e.g. 'Original design: conversation 2026-04-14'"
  - "Attribution 2 — e.g. 'Anthropic docs / prompt-caching'"
updated: YYYY-MM-DD
related:
  - path/to/related.md
---
```

Rules:
- `tags` — always YAML block list, ~10 items, primarily Russian conversational triggers + essential English technical terms.
- `sources` — list of attribution strings (not an integer counter). Each source is a short human-readable string.
- `summary` — quoted string, 2-3 sentences in Russian. **Must contain at least 2 keywords overlapping with `tags`** (stem-match ok). This is a contract with the recall system: if summary and tags drift apart, recall breaks.
- `related` — block list of relative paths; omit if none.

### 4b. Registration — none (INDEX retired)

There is NO INDEX entry to write. The note's **frontmatter `tags`** are its registration and recall keys; the compiler (`compile_wiki_index.py`) globs every note with frontmatter tags into the recall corpus at next session start. So the whole job is: give the note correct, canonical frontmatter `tags` (§4a) and a `summary`. Nothing else registers it.

### 4c. Body structure (for CREATE)

```markdown
# [Entity] — [Topic]

## Summary
[2-4 sentences]

## Key Details
[Parameters, limits, behavior]

## Code Examples
[Snippets — if applicable]

## Limitations / Gotchas
[What to watch out for]
```

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
-summary: "the previous summary"
+summary: "the updated summary (2+ tag keywords)"
@@ body @@
 ## Few-shot examples
-3-5 examples give the best ROI
+3-7 examples give the best ROI (2026 data)
```

## Step 7 — Write

Only after approval:
1. `Write` / `Edit` each target page. That is the whole write — the note's frontmatter registers it.
2. No INDEX to update (retired). A CREATE just needs to land in a content folder with frontmatter `tags`; the compiler picks it up at next session start.

## Step 8 — Lint-on-touched (local sanity check)

Run a scoped subset of `/lint` checks against only the pages touched in this run. This is not a full vault audit — just catching the issues this compile could have introduced.

Checks:
- **Recall-registrable** — every touched page lives in a content folder (`apis/ patterns/ tools/ concepts/ discoveries/ research/`) and has non-empty frontmatter `tags`. A note without frontmatter tags is invisible to recall (the new "orphan").
- **Frontmatter completeness** — touched pages have all required fields (`type`, `tags`, `summary`, `confidence`, `sources`, `updated`).
- **Summary-tag contract** — `summary` of each touched page contains ≥2 tag keywords (stem-match).
- **Tag vocabulary drift** — any newly-introduced tag that has a near-duplicate already in the vault (`anthropic` vs a transliterated spelling of it).
- **Correction-sweep** — when this run CHANGED a volatile fact (a price, token limit, model id, dated "as of" value) on a touched page, `Grep` the vault for the OLD value / the same entity across other pages. Every other page still stating the old value is a missed sweep → surface it: `[SWEEP] apis/other.md still says "<old value>" — update or link to the single source`. This is the write-time prevent side of `/lint` 2.8 `[DRIFT]`; catch the echo now, not three months later in an audit.

If issues found → report them as:
```
[LINT] Issues introduced by this compile:
- [ORPHAN] patterns/new-page.md — no frontmatter tags → invisible to recall
- [TAG-DRIFT] "anthropic" introduced; a near-duplicate spelling already used in 15 pages
- [SWEEP] apis/openai-api.md still says "$15/1M" — updated on apis/anthropic-api.md this run
```
Ask the user: *"Fix now (form only), or leave it and run a full `/lint` later?"*

Skip full-vault checks (stale, data gaps, cross-refs, contradictions) — those belong to `/lint`.

## Step 9 — Delete consumed sources

Delete each successfully compiled raw file from `$CLAUDE_WIKI_PATH/raw/`. The audit trail lives in `log.md` (Step 10, one entry per source) plus the resulting wiki page — moving the file to a `processed/` folder would be a redundant second trail that only accumulates dead weight. Delete only files that compiled cleanly; leave any that errored in `raw\` for the next run.

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

---

## Rules

- Communication with user: Russian.
- Wiki page content: English.
- Batch: process 5–10 files per run when queue is large.
- **Core immutability is non-negotiable.** When in doubt, classify as content change and ask. Better a wasted approval than a silent rewrite.
- Never delete information from existing pages without approval — only add, reorder (form-only), or flag conflicts.
- Raw files with mixed topics → split into separate entity actions in the plan.
- Personal prompts / non-technical content → flag for manual move, do not compile into LLM wiki.
- Summary-tag contract: ≥2 tag keywords must appear (stem-match) in the summary text. If validation fails, rewrite summary before diff preview.
- Tag count target: ~10. Language mix: Russian conversational triggers dominant, English for technical terms that have no natural Russian equivalent.

### Write-correctness laws (obsidian-mind harvest — apply while composing)

- **Single-source status.** A volatile fact (price, limit, model id, "as of" value) lives on ONE
  page; other pages link to it, never restate it. If composing forces a restatement, that is the
  cue to run the Correction-sweep (Step 8) so the copies never diverge.
- **Mark inference.** A claim in the body not traceable to an entry in `sources:` is tagged
  `(unverified)` inline. Confidence rests on provenance — an unmarked guess reads as a fact and rots as one.
- **Let the list be the count.** Write "the commands listed in <X>" rather than "18 commands",
  so the number is derived at reading time and cannot drift from the thing it counts. A frozen
  number in prose is a future lie, and it rots silently because nothing recounts it.
  (Audited by `/lint` 2.9 `[ROT-COUNT]`.)
- **Atomicity — one concept per note (a RETRIEVAL-quality rule, not filing taste).** Each note covers
  ONE entity/concept — one API, one pattern, one tool. Do NOT bundle UNRELATED entities in a single
  note: semantic search embeds each note as one vector, and a note spanning unrelated topics averages
  into a blurry vector that matches nothing well. One concept = one sharp vector = crisp retrieval.
  **This is NOT "fragment every facet":** a deep, many-section reference for ONE entity (e.g. the whole
  Gemini API) is correctly a single note — its summary vector stays on-topic however long the body is
  (only name+tags+summary are embedded, not the body). Split only when a note bundles concepts that
  stand alone and would each be searched for separately. The grain is the ENTITY, not the section.

---

## After compiling — the question worth asking once

Notes just landed. Some of what you compiled may be a procedure you keep re-deriving rather than a
fact you looked up, and a procedure that recurs is a capability you do not have yet.

Offer `/crystallize` in one line, and only offer it. It reads what landed, compares it against the
skills and agents already installed, and proposes: extend an existing one, write a project recipe,
or build something new. Its default answer is that a note stays a note, which is why the offer costs
nothing when there is nothing there.
