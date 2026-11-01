---
name: compile
description: Compile raw documents from Obsidian vault into structured wiki pages. Merges into existing pages or creates new ones. Triggers on - "/compile", "process raw", "build the wiki".
context: fork
allowed-tools: Read, Write, Edit, Glob, Grep, Agent, WebFetch, WebSearch, Bash, mcp__inkling__vault_search, mcp__inkling__vault_read, mcp__inkling__vault_neighbors
---

# Compile — Raw to Wiki

Compiles staged documents from the vault's `raw/` into structured wiki notes. **Existing prose is
immutable by default:** form is standardized freely, technical text never without approval.

---

## Mode — interactive (default) or `--auto`

Two entries, one pipeline. **The local judge runs first on BOTH** (A0a): it triages every staged
item to `new` / `append <note>` / `reject`. Only what happens next differs.

- **`/compile --auto`** — you override what the judge got wrong, write, and close. §A is the whole
  protocol.
- **`/compile`** — the same, then you present the plan to a person, who holds
  `references/interactive.md`'s Step 2 and Step 6 gates. The judge changes what you bring to that
  conversation, not who decides it.

`--auto` is narrower on purpose: only machine-staged `obs-*.md`, add-only, answering to a written
bar instead of a person. Everything that makes a note retrievable is identical — none of it ever
needed a human.

### A queue of only `obs-*.md` takes §A even without the flag

**Owner ruling, 2026-08-16: the agent must stop asking about everything. It decides for itself -
turn a raw candidate into a new note in the graph, append it to an existing note, or drop it.**
Every file at the top of `raw/` an `obs-*.md` → run §A, present no plan. The gate is
aimed, not lowered: it guards changes that destroy what a person wrote, and **A2** is add-only.
Bare `/compile` returns the moment a HUMAN-dropped file is in the queue — that person is asking to
be consulted; a Stop hook staging a digest is not.

---

# §A. `--auto` — the unattended path

## A0. What this mode may touch

**Only `raw/obs-*.md`** — staged by `hooks/capture/observe.py`, each item labelled with the kind
that produced it. Any other file there is a HUMAN's and asks for the interactive pass; leave it.
None → report `nothing staged` and stop, a normal outcome.

## A0a. Two commands run this mode

```bash
JUDGE="${COMPILE_JUDGE_JSON:-$TMP/judge.json}"
[ -n "${COMPILE_JUDGE_JSON:-}" ] || py "$CLAUDE_CONFIG_DIR/skills/compile/scripts/compile_judge.py" --json > "$JUDGE"
py "$CLAUDE_CONFIG_DIR/skills/compile/scripts/compile_apply.py" --plan --judge-json "$JUDGE"
```

`[!]` **`COMPILE_JUDGE_JSON` is set when the nightly runner booted you; re-running the judge over it
is a defect, not a refresh** — the document you were shown must be the one that gets archived, and
two runs of a temperature-0 model only USUALLY agree.

**The plan is your whole input.** Survivors come in full; drops come as ONE LINE each, kind first
— a bar error is overturnable on sight and the full text is still in `raw/`. A drop claiming a
DUPLICATE is NOT yours: the judge re-reads the candidate's BODY for that claim, and a one-line
reason is the summary-shaped evidence it was measured to fail on.

**The split is by whether PROSE IS PRODUCED**, not by how important a step looks: `reject`, the
archive and the tally are mechanical. On a real queue, 14 of 17 items were drops.

Then, after the notes are written and the grounding gate has run (**A4** steps 5-6):

```bash
py "$CLAUDE_CONFIG_DIR/skills/compile/scripts/compile_apply.py" --close    --judge-json "$JUDGE" --kept <note> <note> --left <doc-the-gate-refused>
```

`[!]` It **refuses to archive before the log is written**, and **holds back any document carrying
an item the judge could not rule on** — archiving that deletes the evidence for a decision nobody
made. It DECIDES NOTHING; a rule of its own belongs in the judge, where a second look can overturn
it.

### What the judge itself does

Documented in the script that performs it — `scripts/compile_judge.py`, header and `second_look`.
Nothing here restates it: a restatement is a second copy that drifts.

## A1. The bar — the surprisal rule

One question per item, asked before composing anything:

> **Does this record a gap between what was expected and what happened?**

Keep it only on yes. A keeper is one of:

- a **trap** — something behaves differently from the obvious reading of its docs or its name;
- a **root cause** — the bug was here, and the first hypothesis said elsewhere;
- a **measurement that disagreed** with the guess, with both numbers;
- an **approach turned down**, with the reason it lost;
- a **constraint or invariant** discovered by hitting it.

Drop the rest, and expect that to be most of it — specifically:

- anything you would have produced from general knowledge without the note;
- a restatement of published API behaviour — the docs are one search away and stay current;
- progress narration, status, or what was done in a session;
- anything true only until the current task ends;
- **anything about this memory system itself** unless it is a measured finding — staging happens
  inside sessions that work ON the wiki.

**Weigh by the item's `kind`, printed with it:** a `verdict` or a `measurement` happened, while a
`marker` is the agent's assessment of its own insight and must carry a checkable claim to survive.
An empty keep list is a real result — say so and stop.

## A2. What may be written

| Operation | Allowed | Why |
|---|---|---|
| CREATE a new note | yes | nothing existing is at risk |
| APPEND a new `## ` section to an existing note | yes | the body is indexed per-section, so it is retrievable the moment it lands |
| Bump `updated:`, add to `sources:` / `related:` / `tags:` | yes | form-only, already auto-applied in the interactive path |
| Edit or delete ANY existing prose | **no** | this is the line the mode exists behind |
| Rewrite an existing `summary:` | **no** | its whole retrieval surface on the pushed rail |
| Lower `confidence:` | **no** | a judgement about someone else's note |

**The auto path may add, never revise.** That is what makes an unattended writer safe without an
approver: it cannot produce the class of change the approval gate existed to catch.

CREATE vs APPEND is decided in **A4** step 2; the grain is the ENTITY, per the atomicity law in
`references/note-shape.md`.

## A3. Marking what the machine wrote

Every auto-created note carries in its frontmatter:

```yaml
provenance: auto
confidence: low
sources:
  - "Staged from session <id8> on <date> by observe.py (<kind>)"
```

Every appended section opens with one italic line naming its source. `[!]` `provenance: auto` is
what `gardener.py` holds auto notes to probation by — omitting it removes the note from the only
mechanism that can retire it.

## A4. Steps, in order

`[!]` **The old steps 1-4 are gone, not forgotten:** the judge globs, reads and applies the bar now.
Your override survives — A1 is still what you judge the drop lines against.

1. **Run A0a's two commands.** The plan is your input; the staged documents stay on disk if a drop
   looks wrong.
2. **Per survivor, take or overturn the judge's target** — it gives `new`/`append <note>`, a reason,
   flags and the nearest notes with scores. Overturn on the FLAGS and on what it cannot know: it
   has never seen this session.
   `[!]` **Cosine cannot answer "no hit above the noise floor"** — it always ranks something first,
   and here a real hit and pure noise sit three hundredths apart. Settle a close call by the
   matching SECTION HEADINGS, or by `semantic.py search "<the finding>" -k 8 --rerank`, which CAN
   refuse. Neither settles it → CREATE: a duplicate note is cheap and visible, a finding filed
   under the wrong subject is neither.
3. **Compose and write.** One read of `hooks/recall/index_compiled.json` for the canonical tag set
   (**Step 3** of `references/interactive.md`; never walk the vault to rebuild it), then **Step
   4a**'s frontmatter plus **A3**, under `references/note-shape.md`. No diff, no approval — **A2**
   replaces them.
4. **Grounding gate, ONE call, before anything is archived** (**Step 8b** of `references/interactive.md`):
   ```bash
   for pair in "<note1>:<src1>" "<note2>:<src2>"; do
       py "$CLAUDE_CONFIG_DIR/scripts/grounding_gate.py" "${pair%%:*}" "${pair##*:}"
   done
   ```
   Exit 1 → fix the quote or drop it, never reword the source. Exit 2 → the source is gone, which
   is a refusal and not a pass: name that document to `--left`.
5. **Lint-on-touched** (**Step 8** of `references/interactive.md`) — nobody to ask here: fix what is form-only, log the rest.
6. **Write the prose half of the log** — one entry per document, what was learned — then
   **`compile_apply.py --close`** (A0a) with `--kept` and `--left`. **The drop count is the number
   that matters:** a run that keeps everything means A1 is not being applied, and the log is the
   only place that shows.

## A5. Running unattended

`hooks/capture/compile_auto.sh`, on a schedule; its header carries the detail. It judges BEFORE
deciding whether to boot you, and closes from bash when nothing needs writing.
`[!]` `CLAUDE_AUTO_COMPILE=1` is load-bearing — without it your Stop hook stages a document about
compiling and the next run writes notes about the note-making machine.

## A6. The two subfolders under `raw/`

`_compiled/` (consumed sources, 30-day tail) and `_misrouted/` (another vault's, ROUTED and
removed, never parked): **`references/judge-and-folders.md`**.

# §B. Interactive path — moved to a reference

**`references/interactive.md`** — unchanged, still MANDATORY without `--auto`, and every step §A
names by number resolves there. They moved because they wait for a human at Step 2 and Step 6,
which an unattended run has none of by construction.

## Rules

- Talk to the user in Russian; write notes in the vault's working language (`VAULT_CONTRACT.md`) —
  the one the user actually types in, because a tag in a language nobody types never matches.
- **Never revise existing prose — add, reorder (form-only), or flag a conflict.** In doubt call it
  a content change and ask: a wasted approval beats a silent rewrite.
- A raw file with mixed topics → split into one entity action per topic, never one blended note.
- Personal or non-technical content → flag for a manual move, never compile it into the LLM wiki.
- Summary-tag contract: ≥2 tag keywords must stem-match in the summary, or rewrite it first. Tag
  target ~10, Russian triggers dominant.

**Write-correctness laws, note shape and the body skeleton: `references/note-shape.md`.** Moved out
2026-08-25 under skill-craft rule 2.2; unchanged, and MANDATORY while composing.
