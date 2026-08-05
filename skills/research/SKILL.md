---
name: research
description: Research a lib / API / model to decide whether and how to build it here. Vault-first: checks the Obsidian wiki before the web, honours the rejected-list, and writes findings back so the question is never paid for twice. Three effort levels, asked up front. Triggers on - "research X", "how does [API/model] work", "find information on", "can we add".
context: fork
allowed-tools: Read, Write, Edit, Agent, AskUserQuestion
---

# Research Mode

You are the **orchestrator** for this research workflow. Delegate knowledge gathering to the `researcher` agent — do not do web searches or vault reads yourself.

See `references/project-config.md` for the project-local `research.yaml` + `research-rejected.md` format, and for the effort ladder as a human-facing table.

---

## Step 0 — Load project config

1. **`<cwd>/.claude/research.yaml`**
   - Present → read `effort`. **Legacy compatibility:** if the file carries the old `depth:` key instead, map `shallow → low`, `standard → med`, `deep → max` and treat that as the project default. Do not rewrite the file mid-run.
   - Missing → there is no project default. That is fine; Step 0.5 handles it.
2. **`<cwd>/.claude/research-rejected.md`**
   - Present → read, keep the full content in memory for the agent prompt.
   - Missing → pass `"none"`. Do NOT pre-create an empty file — it appears naturally on first rejection.

---

## Step 0.5 — Ask for the effort level (ALWAYS, unless already known)

**Never sit silently waiting for the user to volunteer a level.** Resolve it, in this order:

1. **The user already said it in their prompt** — "quick look", "just glance at it", "go all in", "dig deep", "low/med/max" — take it and skip the question. Say which level you took.
2. **Otherwise, ask with `AskUserQuestion`.** One question, three options, with the project default from Step 0 listed first and marked `(project default)`. If no project default exists, put `med` first and mark it `(recommended)`.

   Option labels and descriptions:
   - **low - 1-2 sources.** A quick answer, often entirely from the vault with no web at all. Contradictions are not checked and there is no counter-search.
   - **med - 3-5 sources.** The default. One slot is reserved for a counter-search, and the report carries a disagreements section and a consensus note.
   - **max - 6-10 sources plus a follow-up fetch.** Primary sources first, the agent takes a position, a separate de-anchored critic hunts for what would refute it, and one targeted fetch chases the critic's best lead.

3. **If `AskUserQuestion` is unavailable in this context**, ask in one plain message listing the same three options and their costs, then wait. Never proceed on a guess, and never proceed silently.

**First run in this project:** if `research.yaml` was missing, after the user picks a level ask once whether to save it as the project default. On yes → write `effort: <level>` to `<cwd>/.claude/research.yaml`. On no or no answer → use it for this run only, do not write the file, ask again next time.

**If the project default came from a legacy `depth:` key**, silently rewrite that file to `effort: <mapped value>` after the run completes. One-time migration, no need to bother the user with it.

---

## Shorthand: `/research --reject <slug> | <reason>`

If invoked with `--reject`, skip everything above and below — no effort question, no research. Append an entry to `<cwd>/.claude/research-rejected.md`:

```markdown
## <slug>
date: <today>
reason: <reason>
```

Create the file with header if it does not exist. Report: *"Recorded in the rejected list."* Done.

---

## Step 1 — Clarify (if needed)

Ask the user (if not already clear):
- Which provider / model / feature is involved?
- What's the goal — new capability, optimization, or troubleshooting?
- Any constraints (cost, latency, quality)?

**Capture the user's verbatim question before you clarify anything.** Their original wording is what goes to the agent as `Question (verbatim)` and into the note's `question:` field. Clarifications enrich `Goal` and `Context`; they never replace the original words.

---

## Step 2 — Delegate to the `researcher` agent

**Agent priority:** project-local `.claude/agents/researcher.md` first, user-level fallback. User has decided against project-specific researcher agents, so user-level is the normal path.

Pass the following prompt:

```
Research topic: [exact topic from user]
Question (verbatim): [the user's original words, character-for-character, unedited]
Goal: [what the user wants to achieve]
Context: [relevant project context if applicable]
Research effort: [low | med | max from Step 0.5]
Rejected list (project):
[full content of research-rejected.md OR "none"]

Return: structured findings per your report shape — level + source tiers, the gist,
parameters, contradictions, consensus, limitations, falsifier, recommendation.
If topic matches rejected list — early-exit per protocol, do not research.
If zero usable sources — report that and save nothing. Do not answer from memory.
Fetch discipline (Always-on #9) is not optional here. Climb the ladder: structured
endpoint -> WebFetch with the NO CONTENT escape hatch -> fetch_page.py --expect
(on a 403 or a missing value) -> zero sources. Presence gate at every rung: a value
that never appeared in fetched text does not enter the note.
```

Wait for the structured report.

---

## Step 2.5 — Adversarial pass (`max` ONLY)

Skip entirely at `low` and `med`.

Spawn the `research-critic` agent. It is deliberately de-anchored: give it the question, the findings and the sources, and **not** the researcher's reasoning.

```
Question (verbatim): [the user's original words]
Domain: code
Finding: [the conclusion + recommendation from the researcher's report]
Sources: [the source list with tiers and breadcrumbs, verbatim from the report]
Contradictions: [what the researcher reported about disagreement, or "not checked"]

Return: verdict, ranked overturn leads, best lead, and what class of source
the corpus is missing. You cannot write anywhere — leads only.
```

---

## Step 2.6 — Targeted overturn fetch (`max` ONLY, conditional)

- **Critic verdict `holds`** → skip the fetch. Do not spend a source confirming what already holds. Pass the verdict to the researcher to record in the note and move on.
- **Any other verdict** → re-invoke `researcher` with exactly one instruction: chase the critic's best lead, spend **one** fetch, then revise or confirm the saved note and its `falsifier`.

```
Follow-up on your saved finding for: [topic]
Critic verdict: [verdict + why]
Best overturn lead: [the lead, verbatim from the critic]
Search shape suggested: [the critic's query]

Spend exactly ONE fetch on this lead. Then update the saved note: revise the
recommendation if the lead holds, or record it under Limitations if it does not.
Update `falsifier` and `contradictions` accordingly. Do not widen the sweep.
```

Record the critic's verdict in the final report regardless of which branch ran.

---

## Step 3 — Check project impact (`med` and `max` only)

Skip at `low` — a one-source lookup does not justify a blast-radius pass.

Use `project-explorer` with:

```
Given research findings about [topic], which existing modules would be affected?
What's the blast radius of integrating this?
```

Include the blast radius output in the project-fit section. **This step is optional:** `project-explorer` is not part of inkling, so skip the whole step silently when the agent is absent.

---

## Step 4 — Update hub note

The researcher agent handles wiki registration itself (per its memory): a saved note's frontmatter `tags` ARE its registration — the code wiki is INDEX-less, the compiler globs tagged notes into the corpus. Skill does NOT duplicate that work. Only handle personal-vault hubs if relevant:

- AI/API/prompting → researcher agent already saved the note with frontmatter tags (no INDEX to touch).
- Personal prompts → user adds manually, skill does not touch.
- Learning / methodology → user adds manually.

---

## Step 4.5 — Save guarantee (ALWAYS, no approval)

A completed research run MUST leave a saved wiki note. Saving is automatic — never ask the
user for permission to save, never defer it to "a separate step".

- Normal path: the `researcher` agent saved the note itself (its report names the path).
- **Fallback — the skill saves the note itself** when the agent could not: the Agent tool is
  absent in this fork context, the run was executed inline, or the report lacks a saved-note
  path. In that case: read `~/.claude/agent-memory/researcher/MEMORY.md`, apply its frontmatter
  contract (`question` verbatim / `effort` / `falsifier` / `contradictions`) and tag vocabulary
  lock, and Write the note per its UPDATE-vs-CREATE rules before presenting findings.
- The fallback path inherits the fetch discipline in full — the whole ladder (structured endpoint →
  WebFetch + `NO CONTENT` hatch → `python3 "$CLAUDE_CONFIG_DIR/scripts/fetch_page.py" <url> --expect "<value>"`
  → zero sources), and the presence gate at every rung: a value that never appeared in fetched text
  does not enter the note, however plausible it reads.
- The only valid no-save outcome is a zero-source result.
- "Where it landed in the vault" in the final report is filled from what was ACTUALLY written to disk,
  never from intention.

```
## What I found

**Level:** [low | med | max] - [N] sources ([X primary, Y secondary, Z anecdotal])
**Source:** [URL / knowledge base / project rejected list]
**Gist:** [2-4 sentences]

**Parameters and settings:**
- `param` - what it does, accepted values

**Where the sources disagree:**
- [the concrete disagreements; OR "no disagreement found (N sources)"; OR "not checked - single source"]

**Consensus:** [what 3 or more independent sources confirm - omit at low]

**Limitations:**
- [list]

**What would change the conclusion:** [a concrete, checkable condition]

**Stress test:** [max only - the critic's verdict, what it found missing from the corpus, and whether the follow-up fetch was spent]

**How it fits this project:** [med and max only]
- Affects: [files and modules]
- Blast radius: [low/medium/high]
- Conflicts: [yes / no]

**Where it landed in the vault:** [folder/note.md - created / updated]

**Recommendation:** [worth doing or not, and how to approach it]
```

Sections that do not apply at the chosen level are omitted, not filled with "n/a".

After findings, ask: *"Want an implementation plan?"*

---

## Step 6 — Post-research rejection capture

If user responds to findings with clear rejection ("does not fit", "we will not use it", "bad fit", "rejected"):

Ask: *"Record this in the project rejected list? One line for the reason?"*

On yes + reason → append to `<cwd>/.claude/research-rejected.md`:

```markdown
## <topic-slug>
date: <today>
reason: <user's reason>
```

Create file with header if missing. Confirm: *"Recorded. The next `/research` on this topic will exit early."*

On no → proceed normally, no file touched.

---

## Rules

- Delegate research to the `researcher` agent — never use WebSearch/WebFetch directly.
- **Never start research without a resolved effort level.** Ask; do not default silently.
- All communication with user: Russian (casual).
- Frontmatter contract, tag vocab lock, registration (INDEX-less — tags register the note) — responsibility of the researcher agent (see its MEMORY.md). Skill does not duplicate those rules.
- The effort ladder's source budgets live in `agents/researcher.md`. This file routes by level; it does not restate the numbers.
- Effort level and rejected list are passed inline in the agent prompt — the agent does not read them from disk.
- Rejected-list matching is early-exit: no research tokens spent on already-rejected topics.
- `--reject` shorthand is for cases where the user knows something is out of scope and wants to block future research without running a session.
- `research-critic` runs at `max` only, and only ever produces leads — it has no write access by design.
- A zero-source result is a valid outcome. Report it as a failure to find, never paper over it with a plausible answer.
- A fetch that returned without the asked-for value is a zero-source fetch. The vault is read back by agents and never by eye, so a silently empty page becomes a permanent wrong fact — the presence gate is what stops that.
- Wiki save is unconditional and approval-free (Step 4.5). A run that found sources but saved nothing is a broken run, whoever executed it.
