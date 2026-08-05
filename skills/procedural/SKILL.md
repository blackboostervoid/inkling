---
name: procedural
description: Curate procedural-memory candidates (the memory stack Layer 6) — the conflicts, merges and batch distillation that auto-promotion leaves to a human. A rules mode distils SKILLS into CLAUDE.md rule candidates. Triggers on - "/procedural", "review the recipes", "procedural memory", "distil rules".
allowed-tools: Bash, Read, Edit, Agent
---

# Procedural — Candidate Curation Gate

You are the human-ON-the-loop driver for procedural memory (the memory stack Layer 6). The distiller
auto-writes recipe candidates to `<project>/.claude/procedural_candidates.md`; from there,
**any candidate with no conflict against an active recipe is auto-promoted unattended** by
`lint_procedural.py auto-promote` (wired into SessionStart + run at the end of `batch`).
The human is no longer the input wall — the wall is the conflict guard (same-situation
candidates always wait here) plus output-side decay (STALE recipes flagged for archival).

So this skill is what the automation leaves for a human: reviewing CONFLICTING candidates,
merging, running the weekly `batch`, and `stats`/`archive`. On a plain `/procedural` the
pending list is usually just the conflicting ones — the clean ones already promoted.

The mechanical work lives in a helper script — you NEVER hand-edit the JSON or move blocks by
hand for promote/reject; you call the script. Merge is the only judgment-based action you do
directly.

`SCRIPT = "$CLAUDE_CONFIG_DIR/skills/procedural/scripts/lint_procedural.py"`
`CLAUDE_DIR = "${CLAUDE_PROJECT_DIR}/.claude"` (the current project's .claude dir)

## Mode: `batch` — weekly cross-session distillation (delegates to a Haiku subagent)

If the user invoked `/procedural batch` (or you came here reacting to a `[PROCEDURAL-BATCH-DUE]`
signal), run the distillation FIRST, then fall through to the curation steps below to review
what it produced. A plain `/procedural` skips this section and starts at Step 1.

**Mandate: you MUST delegate this to the `procedural-distiller` subagent. Do NOT read
HANDOFF.md or agent_digests yourself here.** Keeping the many-session reading inside a cheap
Haiku subagent — and out of the orchestrator — is the entire reason the batch exists; doing
it inline defeats the design.

1. Spawn it: Agent tool, `subagent_type: procedural-distiller`, passing the project root in the
   prompt (e.g. "Project root: ${CLAUDE_PROJECT_DIR}. Run the weekly procedural batch.").
2. It scans the cross-session history (full HANDOFF.md + agent_digests), distills sequences that
   RECURRED across sessions, writes them as candidates via `distill.py --from-json` (keyless),
   refreshes `.claude/procedural_batch_stamp`, and returns a summary. Relay that summary.
3. Auto-promote the non-conflicting freshly-written candidates immediately (don't wait for the
   next SessionStart), then fall through to Step 1 — the pending list will then hold only the
   CONFLICTING ones that need your judgment:
   ```
   python3 "$CLAUDE_CONFIG_DIR/skills/procedural/scripts/lint_procedural.py" --dir "${CLAUDE_PROJECT_DIR}/.claude" auto-promote
   ```

## Mode: `stats` — confidence / decay review (Layer 6 feedback)

If the user invoked `/procedural stats`, show how active recipes perform, then stop (no
candidate curation). The `procedural_feedback.py` Stop-hook tracks, per active recipe, how
often it FIRED (`[PROCEDURAL]` surfaced it) vs was USED (the agent emitted `[PROCEDURAL-USED]`),
with time-decay, in `.claude/procedural_stats.json`.

```
python3 "$CLAUDE_CONFIG_DIR/skills/procedural/scripts/lint_procedural.py" --dir "${CLAUDE_PROJECT_DIR}/.claude" stats
```

Relay the table. Confidence is observe-only and NEVER auto-tunes the scanner ranking — but
retirement IS now automated for the dead end of the spectrum: a recipe that fired >= 3 times
and was NEVER used is auto-archived by the `auto-archive` Stop-hook (the symmetric output side
of auto-promote). So a `STALE (fired, never used)` recipe usually self-retires before you see it.

What stays manual is the JUDGMENT end — a `decayed` recipe that WAS useful and then went quiet.
That one the user decides, because it may come back:

```
python3 "$CLAUDE_CONFIG_DIR/skills/procedural/scripts/lint_procedural.py" --dir "${CLAUDE_PROJECT_DIR}/.claude" archive <slug>
```

`archive` moves the recipe procedural.md -> procedural_archive.md (status retired) and recompiles.
For a once-used-now-quiet recipe, get the user's go; provably-dead ones are already gone automatically.

## Mode: rules — distill skills into CLAUDE.md rule candidates (delegates to the distiller)

If the user invoked `/procedural rules`, this is the GLOBAL skills->CLAUDE.md variant (Layer 6 rules
mode), NOT the per-project procedural curation below. It diverges on purpose: the sink is the
always-on CLAUDE.md, so there is **no auto-promote** — every candidate is human-walled. Scope is
`$CLAUDE_CONFIG_DIR` (global), not `${CLAUDE_PROJECT_DIR}`.

1. **Distill (delegate to Haiku — do NOT read skills yourself).** Spawn the `procedural-distiller`
   subagent with a prompt that says **"rules mode"** and the config dir, e.g.
   "rules mode. Config dir: $CLAUDE_CONFIG_DIR. Mine skills for CLAUDE.md rule candidates."
   It runs `rules_inputs.py`, applies the 2+-skills / actionable / violation-risk / not-already-covered
   filter, writes blocks to `$CLAUDE_CONFIG_DIR/rules_candidates.md`, and returns a summary. Relay it.
   (Skip this step if the user is just reviewing an existing `rules_candidates.md`.)

2. **List.** Read `$CLAUDE_CONFIG_DIR/rules_candidates.md`. Present each candidate: slug, verdict,
   target §, evidence skills, violation_risk, and the draft (or before/after for Revise).

3. **Per candidate — promote / reject / skip:**
   - **promote** (judgment Edit — by hand; there is NO script for this sink):
     re-read §target in `$CLAUDE_CONFIG_DIR/CLAUDE.md` first (it is hand-edited between runs, the
     anchor may have moved), then apply —
       Append -> add `draft` to the end of §target;
       New Section -> insert a new `## target` section with `draft`;
       Revise -> replace `before` with `after` in §target.
     Then remove the candidate's block from `rules_candidates.md`.
   - **reject** — remove the block from `rules_candidates.md` (rules have no archive store).
   - **skip** — leave the block for a later session.

4. **Report.** N promoted (into which §), N rejected, N skipped. If anything was promoted, note that
   CLAUDE.md grew and suggest a `/context-budget` pass when several rules land — CLAUDE.md is the #1
   always-on cost, so the rules-in / audit-out loop keeps it from bloating.

## Step 1 — List candidates

```
python3 "$CLAUDE_CONFIG_DIR/skills/procedural/scripts/lint_procedural.py" --dir "${CLAUDE_PROJECT_DIR}/.claude" list
```

Relay the output to the user as-is, then add a one-line read of the machine's recommendation:
- `AUTO-PROMOTES ...` — no conflict, past the gate. It will (or already did) promote unattended;
  nothing for the user to do unless they want to pre-empt with `reject`.
- `REVIEW (conflicts with: X)` — overlaps an active recipe; auto-promote SKIPS it. This is the
  real reason a candidate sits in the pending list. The user decides merge vs keep-both vs drop.
- `hold (N/gate sessions)` — below the evidence gate; waits until it recurs.

If "No pending candidates" → tell the user, stop. (Common now: clean candidates self-promote,
so an empty list usually means everything was auto-accepted, not that nothing was distilled.)

## Step 2 — Per candidate, take the user's decision

Ask the user per candidate (or batch): **promote / reject / merge / skip**.

- **promote**
  ```
  python3 "$CLAUDE_CONFIG_DIR/skills/procedural/scripts/lint_procedural.py" --dir "${CLAUDE_PROJECT_DIR}/.claude" promote <slug>
  ```
  Moves the block to `procedural.md` (status → active) and recompiles the JSON. Report the result.

- **reject**
  ```
  python3 "$CLAUDE_CONFIG_DIR/skills/procedural/scripts/lint_procedural.py" --dir "${CLAUDE_PROJECT_DIR}/.claude" reject <slug>
  ```
  Moves the block to `procedural_archive.md` (status → rejected).

- **merge** (judgment — you do this directly, the script does not auto-merge)
  1. Read the conflicting active recipe in `procedural.md` and the candidate in `procedural_candidates.md`.
  2. Decide the canonical version: fold the better steps / why / keywords into ONE recipe. Edit the
     active recipe in `procedural.md` to the merged form (keep its slug).
  3. Reject the now-absorbed candidate via the `reject` command above.
  4. Recompile so the merged active recipe takes effect:
     `python3 "$CLAUDE_CONFIG_DIR/hooks/recall/compile_procedural.py" --dir "${CLAUDE_PROJECT_DIR}/.claude"`

- **skip** — leave the candidate pending for a future session.

## Step 3 — Report

Summarize: N promoted, N rejected, N merged, N left pending. If anything was promoted or merged,
note that the active `procedural.md` and its compiled JSON are now updated and the scanner will
surface them on matching prompts.

## Rules
- Communication with user: Russian. Recipe content: keep whatever language it was distilled in.
- Promotion is automated for NON-conflicting candidates (SessionStart + batch auto-promote). Your
  manual `promote` is for forcing a flagged/held one the user explicitly judges good. The human is
  the wall only for CONFLICTS and for output-side `archive`, not for clean input.
- promote/reject = the script only. merge = your judgment, then reject the absorbed candidate + recompile.
- Always invoke Python via `py` (3.14), never `python`.
- A `REVIEW` flag is advisory, not a block — the user can still promote a flagged candidate if they judge it distinct.
