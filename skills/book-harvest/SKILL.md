---
name: book-harvest
description: Harvest reusable concepts from a book (pdf/epub/fb2/md/txt) into raw concept notes for the wiki, then hand off to /compile. Haiku scouts read the units; the main context sees only their summaries. Triggers on - "/book-harvest", "разбери книгу", "выжми концепты из книги".
allowed-tools: Agent, Bash, Grep, Read, Write, Skill
disable-model-invocation: false
argument-hint: <path-to-book> [--max N] [--min-density 3]
---

# Book Harvest — Concept Extraction Pipeline

Extracts a small set of reusable, named concepts from a long-form book and lands them as raw
notes ready for `/compile`. Built around a hard token-economy rule: **the book itself is large,
so the main orchestration context must never read the full book text.** Heavy reading is fanned
out to cheap haiku scouts (one per chapter); the main context sees only their compact summaries.

---

## Hard Invariants (non-negotiable)

- **This skill is NOT forked.** It runs in the main context because its load-bearing mechanism is
  the haiku scout fan-out via the `Agent` tool — and a forked skill context does not receive the
  `Agent` tool even when `allowed-tools` lists it (verified: the fork sees no Agent/Task). Do NOT
  add `context: fork` back; it silently kills the fan-out and forces the orchestrator to read the
  whole book itself. The token economy is preserved without forking: book text lives only in scout
  subagent contexts, never in the main thread (see next invariant).
- **Main context never reads unit bodies.** It reads `meta.json`, scout summaries, and its own
  working notes — never `temp/<slug>/ch_NN.md`. Reading a full reading unit into the orchestrator
  defeats the entire design.
- **All heavy reading is delegated to haiku scouts** via the Agent tool, one scout per reading unit.
- **Density gate.** Final output is `3-8` concepts by default. `--max N` overrides the ceiling;
  `--min-density` (default `3`) is the floor — fewer real concepts than the floor means the book was
  thin and the skill says so instead of padding.
- **Output is raw notes, not wiki pages.** Final artifacts go to the `/compile` inbox; `/compile`
  owns merge-vs-create and wiki placement. This skill does not touch the wiki directly.

---

## Step 0 — Resolve arguments

1. Source = first positional arg. If missing → ask: *«Путь к книге?»* and stop.
   The arg may be a **book file** OR a **YouTube URL / 11-char video id** — `parse_book.py` fetches
   the transcript and slices it exactly like a book (same `meta.json` / `ch_NN.md` contract). Detect
   a YouTube input by `youtube.com` / `youtu.be` in the string, or a bare 11-char id.
2. Parse optional flags:
   - `--max N` → ceiling on final concept count (default `8`).
   - `--min-density M` → floor on final concept count (default `3`).
3. **Book file:** verify it exists (`Bash` / `Test-Path`). If not → `[ERROR] File not found: <path>`
   and stop. **YouTube input:** skip the existence check (it is not a file).
4. Derive `slug`: **book** → from the filename (lowercase, non-alnum → `_`); **YouTube** → the 11-char
   video id (the human title is unknown until Step 1 fetches it — read it from `meta.json` afterwards
   for reporting). Working dir = `temp/<slug>/` under the skill base dir.

## Step 1 — Slice the book into reading units

Run the bundled parser. It branches on extension and produces reading-unit files + metadata.

```
python3 "<skill-base>/parse_book.py" "<book-path>" --out "temp/<slug>"
```

Parser behavior (see `parse_book.py`):
- `.pdf` → `pymupdf4llm` (markdown) + `fitz` (title/author metadata)
- `.epub` → `ebooklib` + `bs4`
- `.fb2` (or `.fb2.zip`) → `xml.etree` + `zipfile`
- `.md` / `.txt` → native read
- **YouTube URL/id** → `youtube-transcript-api` (transcript) + `yt-dlp` (title/channel/chapters); the
  video's chapters become `## headings` so the SAME split→pack pipeline runs. Title→`title`,
  channel→`author`, `source_format` = `youtube`. Deps (YouTube only):
  `python3 -m pip install youtube-transcript-api yt-dlp`. Without yt-dlp it still works via oEmbed but
  loses chapters (transcript then falls back to flat size-chunking).
- Pipeline: split into raw blocks → **drop boilerplate** (TOC/copyright/front-matter by title
  and head patterns, bilingual RU/EN, any size) → **pack** surviving blocks into ~2800-word
  reading units → drop units still under 200 words. Packing is what prevents the
  "140 micro-fragments" failure: PDF backends emit a heading per bold sub-heading, and packing
  coalesces them back into ~20-30 coherent units. Each unit's title is the " / "-joined titles of
  the blocks it merged.
- Writes `ch_NN.md` per unit + `meta.json`: `{title, author, source_format, chapters:[{n, file,
  title, word_count}]}` (the `chapters` array is the reading units).

Read **only** `temp/<slug>/meta.json`. If `chapters` is empty → `[ERROR] No usable reading units
extracted` and stop. Report unit count + total words to the user.

## Step 2 — Fan out haiku scouts (one per reading unit)

For each reading unit in `meta.json`, spawn a scout via the Agent tool (model `haiku`). Launch them
in parallel batches (up to ~8 concurrent) — independent work, no serialization. Each scout gets
exactly one unit file. Use `meta.json`'s per-unit `title` (the merged sub-headings) to give each
scout a one-line theme hint in its prompt.

Scout prompt template (per chapter):
```
You are a concept scout. Read ONLY this file: temp/<slug>/ch_NN.md
Extract every reusable, transferable CONCEPT (a named idea, principle, model, technique, framework
— not a plot point, not an anecdote, not a quote unless it crystallizes a concept).
For each concept output:
  - name: short noun phrase (coin one if the book doesn't name it)
  - one_line: the concept in a single sentence
  - why_reusable: where this idea transfers beyond the book's own domain
  - evidence: <=2 short paraphrased supporting points (NO long verbatim quotes)
Return STRICT JSON: {"chapter": NN, "concepts": [ ... ]}. If the chapter has no real concept,
return {"chapter": NN, "concepts": []}. Do not summarize the chapter narratively.
```

Collect each scout's JSON. **Do not** read the chapter files yourself to "verify" — trust the scout
output; that is the whole point of the fan-out.

## Step 3 — Aggregate and dedup

Working only from scout JSON (compact), in the main context:
1. Flatten all `concepts[]` across chapters into one list.
2. Cluster near-duplicates by name + one_line semantic overlap (same idea phrased differently across
   chapters → one concept, merge evidence, keep the clearest name). `[NAMING SMELL]` if the same
   concept appears under 3+ names — pick the sharpest, note the aliases.
3. Rank by cross-chapter recurrence + breadth of `why_reusable` (a concept that recurs and transfers
   widely ranks above a one-chapter curiosity).

## Step 4 — Apply the density gate

- Keep the top concepts, capped at `--max` (default 8).
- If the deduped count `< --min-density` (default 3): report
  `[WARNING] Only K real concepts found (floor is M). The book is concept-thin.` — list what was
  found, ask the user whether to proceed with fewer or abort. Do **not** invent filler concepts.
- If between floor and ceiling: keep all.

Print the shortlist to the user (names + one-liners only) as an informational trace — do not block on
it. The actual review checkpoint is `/compile`'s approval gate downstream, not here.

## Step 5 — Write raw concept notes

For each surviving concept, write one markdown file to the `/compile` inbox:
`$CLAUDE_WIKI_PATH/raw/book_<slug>_<concept_slug>.md`

File body:
```markdown
# <Concept Name>

**Source:** <book title> — <author> (book-harvest)

## One line
<one_line>

## Why it's reusable
<why_reusable — cross-domain transfer>

## Evidence
- <paraphrased point>
- <paraphrased point>

## Appears in
Chapters: <list of chapter numbers where it surfaced>
```

These are raw notes — `/compile` decides UPDATE vs CREATE and final wiki placement. Keep them concept-
scoped (one concept per file) so `/compile`'s entity matching works cleanly.

**Sizing — dense seed, not transcript (~150-300 words).** Do NOT pad for volume; padding re-introduces
the water the scouts filtered out and forces `/compile` to re-distill. The note must carry the
*concrete* specifics scouts surfaced (formulas, named anti-patterns, metric definitions) — those are
high recall value and cheap. Lever for richer wiki pages is `/compile`'s CREATE step, not a fatter
seed. One calibration cue: a concept likely to MERGE into an existing page can stay lean; a concept
likely to CREATE a new page should carry enough substance to stand as the page's backbone.

## Step 6 — Cleanup

Delete `temp/<slug>/` (chapter files + meta) once notes are written — the chapter slices are
disposable intermediate state, not an artifact. Keep nothing heavy around.

## Step 7 — Report

```
[SUCCESS] Book harvested: <title> — <author>
- Chapters scanned: N (scouts: N)
- Concepts found: K (after dedup), within density gate [min M / max P]
- Raw notes written: K → $CLAUDE_WIKI_PATH/raw/
  - book_<slug>_<concept>.md  (xK)
```

## Step 8 — Chain to /compile YOURSELF (mandatory)

**Invoke the `/compile` skill via the `Skill` tool as the last action of this run.** This skill runs
in the main context (see the Hard Invariants), so it holds `Skill` and the chain is its own job — not
a request left for someone else to notice. Do not stop at the report and do not ask whether to
continue: notes in the inbox are work-in-progress, wiki pages are the deliverable, and the human
gate lives inside `/compile` — chaining delivers the run TO that gate rather than stopping short of it.

**Fallback only.** If `Skill` is unavailable here, emit the marker as the final line so the main
context can chain manually:

```
[COMPILE-READY] K raw concept notes written to the /compile inbox. Invoke the /compile skill now
to merge them into the wiki.
```

The marker is the fallback, not the contract. The contract is that this skill finishes the job.

---

## Rules
- Communication with user: Russian. Note content: English.
- **Main context reads only meta.json + scout JSON + its own notes — never chapter bodies.** This is the load-bearing rule.
- One scout = one chapter. Parallelize; never serialize independent scouts.
- Density gate is real: never pad to reach the floor, never exceed `--max`.
- No long verbatim quotes in notes — concepts are paraphrased, transferable ideas (copyright + signal hygiene).
- Always invoke Python via `py` (3.14), never `python`.
- Clean up `temp/<slug>/` on success. On error, leave it for inspection.
- **The run is not done until `/compile` has been invoked** — stopping at the report leaves the user
  to finish the pipeline by hand.
