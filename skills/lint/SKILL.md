---
name: lint
description: Health-check the Obsidian LLM Wiki. Finds contradictions, stale pages, orphans, missing cross-references, and data gaps. Triggers on - "/lint", "check the vault", "vault health", "wiki health check", "lint wiki".
context: fork
allowed-tools: Read, Write, Edit, Glob, Grep
---

# Lint — Wiki Health Check

You audit the LLM Wiki at `$CLAUDE_WIKI_PATH/` for structural and content issues.

## Step 1 — Load wiki state

The code wiki is **INDEX-less** — the notes ARE the catalog. Do not read INDEX.md.

1. Glob all `.md` files in `apis/`, `patterns/`, `concepts/`, `tools/`, `discoveries/`, `research/`
2. Build a map: file path → frontmatter (tags, confidence, updated, related, sources)
3. For cross-referencing usage/recall data, the compiled corpus is `$CLAUDE_CONFIG_DIR/hooks/recall/index_compiled.json`.

## Step 2 — Run checks

### 2.1 Stale pages
- Pages where `updated:` is >6 months old AND `confidence:` is not `high`
- Pages where `confidence: low` regardless of age
- Flag: `[STALE] file.md — last updated YYYY-MM-DD, confidence: X`

### 2.2 Unrecallable / misfiled pages
- Pages with **no frontmatter `tags`** (or an empty list) — invisible to recall, since the compiler only picks up tagged notes. This is the new "orphan".
- Pages sitting OUTSIDE a content folder (`apis/ patterns/ tools/ concepts/ discoveries/ research/`) — the compiler's glob won't reach them.
- Flag: `[UNRECALLABLE] file.md — no frontmatter tags` or `[MISFILED] path — outside content folders, not compiled`

### 2.3 Missing cross-references
- Pages whose `related:` field is empty
- Pages that share 2+ tags with another page but don't reference each other
- Flag: `[MISSING-REF] file-a.md ↔ file-b.md — shared tags: [X, Y]`

### 2.4 Contradictions
- Read pages that cover overlapping topics (same tags, same entity)
- Compare key claims: if page A says X and page B says Y about the same thing, flag it
- Flag: `[CONFLICT] file-a.md vs file-b.md — topic: "claim A" vs "claim B"`

### 2.5 Data gaps
- Topics mentioned in multiple pages but lacking their own dedicated page
- Concepts referenced in `related:` that don't exist
- Flag: `[GAP] "topic" — referenced N times but no page exists`

### 2.6 Frontmatter completeness
- Pages missing required fields: type, tags, summary, confidence, sources, updated
- Flag: `[INCOMPLETE] file.md — missing: field1, field2`

### 2.7 Usage & evidence health (evidence-by-backlink signal)

Consumes two live signals produced by the recall Stop-hooks — usefulness, not just freshness.

1. Read `$CLAUDE_CONFIG_DIR/hooks/recall/recall_stats.json` (small JSON, may be absent → skip this
   check silently). Shape per note: `{fired_sessions:[...], used_sessions:[...], confidence, last_*}`.
   It is GLOBAL (mixes code + personal notes) — evaluate ONLY names present in this wiki (the notes globbed in Step 1, i.e. the compiled corpus `index_compiled.json`).
2. While reading each note in Step 1, capture whether it has a `## Applied` section and how many
   dated entries it holds (written by `recall_evidence.py` each time the note demonstrably helped).

Then classify:

- **Proven load-bearing** — note has `## Applied` entries OR `confidence > 0`. This note has
  paid its rent. **It OVERRIDES the 2.1 stale flag**: usefulness is not freshness — do NOT flag a
  proven-used note as stale just because `updated:` is old. Report only as context, never as an issue.
- **Recall-noise** — in recall_stats with `len(fired_sessions) >= 4` AND `used_sessions` empty
  (confidence 0). It keeps matching conversations but never actually helps → its tags over-match.
  Flag: `[RECALL-NOISE] file.md — fired N sessions, used 0 → retune tags (too broad) or retire`.
- **Cold** (maturity-gated, SOFT) — NOT in recall_stats at all (never once recalled) AND `updated:`
  > 3 months old. Flag: `[COLD] file.md — never recalled since tracking began; verify still relevant`.
  Cold is NOT a delete verdict — tracking may simply be young. Present it as lowest-severity, and if
  the whole wiki shows near-zero `used_sessions`, add one `[MARKER-GAP]` meta-line: "[RECALL-USED]
  emission is low across the corpus → the usage signal is weak this pass; do not over-trust COLD."

### 2.8 Correction-sweep (restatement drift)

Single-source (the INDEX/frontmatter contract) reduces duplication but legitimate cross-page mentions
of the same volatile fact still happen. This is the safety net obsidian-mind calls the correction-sweep
law: when a fact changes, every echo must change with it.

- Scan note bodies for volatile facts tied to a named entity: numbers-with-units (prices `$X`, token
  limits `Nk`/`N,000`, percentages), model IDs (`claude-*`, `gpt-*`), dated "as of" claims.
- When the SAME entity carries DIFFERENT values across ≥2 pages, a sweep was missed.
- Flag: `[DRIFT] entity "X": file-a.md says V1, file-b.md says V2 — correction-sweep needed`.
- Distinct from `[CONFLICT]` (2.4): CONFLICT = two pages disagree on a claim/opinion; DRIFT = the same
  hard fact copied to N places, one updated and the rest left to rot.

### 2.9 Rot-counts (never hardcode a count that rots)

- Scan note bodies for hardcoded counts in prose that silently go stale: `N commands`, `N providers`,
  `N models`, `N skills`, `N agents`, `N hooks` and their equivalents in other languages.
- Flag: `[ROT-COUNT] file.md:line - hardcoded "18 commands" rots; state it as "the commands in <list>" and let the list be the count`.
- Frontmatter counts and version numbers are exempt; this targets narrative prose only.

## Step 3 — Suggest improvements

Based on findings, generate:
- **Quick fixes** — things that can be done now (add missing frontmatter tags, fill empty `related:`, add cross-refs)
- **Investigations** — stale pages that need re-research to verify claims
- **New pages** — data gaps that warrant their own wiki entry
- **Sources to find** — specific questions the wiki can't currently answer

## Step 4 — Log the lint pass

Append to `$CLAUDE_WIKI_PATH/log.md`:

```markdown
## [YYYY-MM-DD] lint | Wiki health check
- Pages scanned: N
- Issues found: stale(X), orphan(Y), missing-ref(Z), conflict(W), gap(G), incomplete(I), recall-noise(RN), cold(C), drift(D), rot-count(RC)
- Proven load-bearing (usage/evidence): P pages
- Quick fixes applied: [list or "none — awaiting user approval"]
```

## Step 5 — Report

Present findings grouped by severity:

```
## Wiki Health Report

### Critical (blocks accuracy)
- [CONFLICT] ...
- [UNRECALLABLE] ...  (no frontmatter tags → invisible to recall)
- [DRIFT] ...

### Warning (degrades quality)
- [STALE] ...  (suppressed when the page is proven load-bearing — see 2.7)
- [MISFILED] ...
- [INCOMPLETE] ...
- [RECALL-NOISE] ...

### Improvement (nice to have)
- [MISSING-REF] ...
- [GAP] ...
- [ROT-COUNT] ...
- [COLD] ...  (soft; caveated by [MARKER-GAP] when usage signal is weak)

### Summary
Pages: N total, X healthy, Y need attention, P proven load-bearing
Suggested actions: [numbered list]
```

Ask two separate questions:

1. *"Apply the quick fixes automatically?"* (yes/no/select)
   - Auto-fixable: missing frontmatter `tags` (propose from the note's content), empty `related:` fields, missing frontmatter fields with safe defaults (e.g. `sources: []`).
   - On `select` — show the list, user picks by number.

2. *"Turn the findings into research tasks?"* (yes/no/select)
   - Candidates: every `[STALE]` page + every `[GAP]` topic.
   - For each picked item — invoke `/research` with a pre-built prompt:
     ```
     Research topic: <page name or gap topic>
     Goal: <for STALE: re-verify the facts and bump confidence / for GAP: create a new page>
     Context: existing page at <path>, last updated <date>, known gaps: <from lint>
     ```
   - Batch-mode: if user picks multiple — queue them, run sequentially, report results together.
   - Skip `[CONFLICT]` and `[MISFILED]` — those need human decision, not more research.

## Rules
- Talk to the user in the language they write to you in.
- Do NOT auto-fix contradictions or stale content — flag for user decision
- Auto-fixable: missing frontmatter tags, empty related fields, frontmatter gaps
- Always log the lint pass to log.md
