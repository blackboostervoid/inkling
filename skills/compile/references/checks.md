# Checks — lint-on-touched and the grounding gate

> Loaded on demand by `compile`'s SKILL.md. The unattended path runs both at §A4 steps 7-8, the interactive one at Steps 8 and 8b. The grounding gate runs BEFORE anything is deleted, in both.

## Step 8 — Lint-on-touched (local sanity check)

Run a scoped subset of `/lint` checks against only the pages touched in this run. This is not a full vault audit — just catching the issues this compile could have introduced.

Checks:
- **Recall-registrable** — every touched page lives in a content folder (`apis/ patterns/ tools/ concepts/ discoveries/ research/`) and has non-empty frontmatter `tags`. A note without frontmatter tags is invisible to recall (the new "orphan").
- **Frontmatter completeness** — touched pages have all required fields (`type`, `tags`, `summary`, `confidence`, `sources`, `updated`).
- **Summary-tag contract** — `summary` of each touched page contains ≥2 tag keywords (stem-match).
- **Tag vocabulary drift** — any newly-introduced tag that has a near-duplicate already in the vault (`anthropic` vs `anthropic-api`, or the same word transliterated into another script).
- **Correction-sweep** — when this run CHANGED a volatile fact (a price, token limit, model id, dated "as of" value) on a touched page, `Grep` the vault for the OLD value / the same entity across other pages. Every other page still stating the old value is a missed sweep → surface it: `[SWEEP] apis/other.md still says "<old value>" — update or link to the single source`. This is the write-time prevent side of `/lint` 2.8 `[DRIFT]`; catch the echo now, not three months later in an audit.

If issues found → report them as:
```
[LINT] Issues introduced by this compile:
- [ORPHAN] patterns/new-page.md — no frontmatter tags → invisible to recall
- [TAG-DRIFT] "anthropic" introduced; "anthropic-api" already used in 15 pages
- [SWEEP] apis/openai-api.md still says "$15/1M" — updated on apis/anthropic-api.md this run
```
Ask the user: *"Fix these now (form only), or leave them and run a full `/lint` later?"*

Skip full-vault checks (stale, data gaps, cross-refs, contradictions) — those belong to `/lint`.

## Step 8b — Grounding gate (BEFORE anything is deleted)

For every note written or extended this run from a raw file, check that what it presents as
evidence was actually said:

```bash
py "$CLAUDE_CONFIG_DIR/scripts/grounding_gate.py" <the note> <the raw file(s) it came from>
```

**Exit 1 means a block quote in the note is not verbatim in the source.** Fix it by correcting the
quote to what the source says, or by dropping it — never by rewording the source to fit the note.
Exit 2 means a source is already gone, so nothing could be judged; that is a refusal, not a pass.

**Why this step sits here and not after Step 9.** Step 9's reasoning is sound for the question it
answers — a `processed/` folder would be a second audit trail on top of `log.md`. But that treats
the raw file as a TRAIL. The moment anyone asks whether a note's evidence is real, the raw file
stops being a trail and becomes the EVIDENCE, and evidence cannot be checked after it is deleted.
So the check runs first and Step 9 is unchanged.

A note that quotes nothing exits 0 with `[NOTHING GROUNDED]`. That is a finding, not a pass: a
note compiled from an observation should carry the span it rests on, because once the raw file is
deleted that span is the only record of what was actually said.

