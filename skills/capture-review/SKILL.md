---
name: capture-review
description: Review the staged capture inbox — durable moments the capture harvester caught this/last session — and promote the real ones to their home (wiki / memory / rejected-list) or drop the rest. Triggers on - "/capture-review", "review the inbox", "check capture inbox", "capture review".
allowed-tools: Read, Write, Edit, Glob, Grep
---

# Capture Review — promote or drop staged candidates

The live `[CAPTURE]` hook hints in the moment; `capture_inbox.py` sweeps the session at Stop for
anything the moment missed and stages it into `<cwd>/.claude/capture_inbox.md`. Those are raw
regex catches — NOT notes. This skill applies the judgment regex cannot: keep the durable ones,
kill the noise. Nothing here auto-writes without the user.

## Step 1 — Load

Read `<cwd>/.claude/capture_inbox.md`. Each candidate is a line `- [category] "quote"` under a
`## date · session` header. If the file is missing or has zero `- [` candidate lines → report
"The inbox is empty, nothing to review" and stop.

## Step 2 — Triage each candidate

For every candidate, judge it against ONE bar: **would re-deriving this later cost real time, and
is it durable (true beyond this session)?** If no → it is noise, drop it. Casual phrasing that
merely tripped a keyword is the common case; be willing to drop most.

Group the survivors by their suggested home:

| Category | Home | How to promote |
|---|---|---|
| `decision` | project `auto_memory/<project>/` decision note, OR the code wiki if it is a reusable technical decision | write a memory file (frontmatter `type: decision`... follow the memory format) or hand to `/compile` |
| `gotcha` | `auto_memory/<project>/` (a gotcha/feedback note) | memory file with **Why** + **How to apply** |
| `pattern` | code wiki (`$CLAUDE_WIKI_PATH/`) as a `pattern` page | stage into `raw/` and run `/compile`, or write the page directly |
| `rejected` | `<cwd>/.claude/research-rejected.md` if it exists, else a `rejected` memory note | append the approach + the reason it was ruled out |

## Step 3 — Present + confirm

Show the survivors grouped, with your promote target for each, and the ones you propose to drop:

```
Inbox: N candidates

Suggested PROMOTE:
1. [decision] "..." → auto_memory/<project>/decision_X.md
2. [pattern]  "..." -> wiki: /compile a pattern page

Suggested DROP (noise):
3. [gotcha] "..." - a casual turn of phrase, not durable

Apply? (yes / pick numbers / edits)
```

Wait for the user. Never promote a `pattern` into the wiki without the `/compile` gate or explicit
approval — the wiki write contract (single-source, graph-first, atomicity) still applies.

## Step 4 — Apply

- **Promote:** write each approved candidate to its home following that home's format (memory
  frontmatter, or the `/compile` write path for wiki pages). Preserve the quote as the seed; expand
  into a proper note — do not just paste the raw line.
- **Drop:** no write.
- **Both:** remove the resolved candidate line from `capture_inbox.md`. A resolved candidate leaves
  the inbox entirely — the remaining `- [` lines are the still-pending queue the SessionStart nudge
  counts. Delete now-empty `## date · session` headers too. If the file ends up with zero
  candidates, leave the header comment block (or delete the file — either is fine).

## Step 5 — Report

```
Promoted: K [list with paths]
Dropped: M
Left in the inbox: R
```

## Rules
- Communication: Russian.
- Judgment over volume — dropping most catches is normal and healthy; the inbox is a wide net, not a mandate to keep.
- Never auto-write on trigger — Step 3 confirmation gate is mandatory unless the user said "promote everything".
- A promoted note obeys its home's contract (memory format, or wiki single-source/graph-first via `/compile`). This skill stages the seed; the home owns the shape.
- Idempotent: re-running on an already-clean inbox is a no-op.
