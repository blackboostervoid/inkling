# researcher — Universal Memory (user-level)
# Write-once protocol: canonical rules, source matrix, frontmatter contract. Do not mutate from session signal.
# Project artifacts (effort dial, rejected list) live at `<project>/.claude/` and are passed INLINE by the
# /research skill — never described or read here. The ladder's source budgets live in agents/researcher.md,
# in ONE place: a second copy drifts the moment either is tuned.

Loaded by the `researcher` agent on every invocation. This file first, then whatever the skill passes inline.
Keep it under ~160 lines — past 200 the tail is silently dropped on load, and the tail is §11.

## 1. Obsidian locations (canonical)

| Wiki root | `$CLAUDE_WIKI_PATH/` |
| Compiled corpus | `$CLAUDE_CONFIG_DIR\hooks\recall\index_compiled.json` (name+path+tags+summary per note) |
| Semantic search | `python3 "$CLAUDE_CONFIG_DIR/hooks/recall/semantic.py" search "<topic>"` |
| Log | `$CLAUDE_WIKI_PATH/log.md` |
| Page categories | `apis/ patterns/ concepts/ tools/ discoveries/ research/` |

**INDEX-less (retired):** the note's frontmatter `tags` are the recall keys; the compiler globs notes into the corpus. **Protocol:** to find an existing page, `Glob` the categories (filename = slug) or read the compiled corpus / run semantic search; never rely on INDEX.md. Max 2 vault file reads before deciding UPDATE vs CREATE vs KB-hit.

## 2. Source matrix

Anthropic `docs.anthropic.com` · OpenAI `platform.openai.com/docs` · Google Vertex/Gemini `cloud.google.com/vertex-ai/docs` · DeepSeek `platform.deepseek.com/api-docs` · ElevenLabs `elevenlabs.io/docs` · HuggingFace/Qwen `huggingface.co/docs`

Strategy: official docs → `WebSearch site:docs.provider.com [topic]` → GitHub issues/changelogs for edge cases. At `max`, primary layer (spec / paper / source / changelog) comes BEFORE general web search.

**Channel ladder** — rules in agents/researcher.md #9, not restated here: `structured endpoint → WebFetch + hatch → fetch_page.py → zero sources`. Endpoints worth knowing: `pypi.org/pypi/<pkg>/json` · `api.github.com/repos/<o>/<r>/releases/latest` · provider `/v1/models` · `openapi.json` · raw `CHANGELOG.md` · RSS/Atom. Rung 3, on 403 / `NO CONTENT` / a missing value — `python3 "$CLAUDE_CONFIG_DIR/scripts/fetch_page.py" <url> --expect "<value>"` — sends a browser identity, returns JSON-LD → og/meta → state → text (read the typed fields, not the prose), and exits 2 when the value is absent.

## 3. Decision tree (corpus-first)

1. Find an existing page for the topic: `Glob` the categories by slug, or run semantic search (§1) to catch a page named differently. Read the candidate's frontmatter.
2. Page found + `confidence: high` + **fresh for its rate of change** (below) → return its content. Skip web unless asked to re-verify.
3. Page found + stale / low confidence → extract confirmed facts, note gaps, targeted search to fill them.
4. No page → full web research → save.

**Freshness is per-topic, not per-calendar.** One age threshold is at once too generous to fast facts and too strict to stable ones. Classify the topic, then judge `updated`:

| Rate of change | Examples | Trust for |
|---|---|---|
| **Fast** (weekly/monthly) | pricing, rate limits, model IDs, availability, API params, "latest version" | **~1 month** — older re-verifies even at `confidence: high` |
| **Medium** (quarterly) | library APIs, SDK surface, feature sets, deprecations | ~3 months |
| **Slow** (yearly+) | patterns, algorithms, protocol semantics, methodology | **~12 months** — re-verifying burns tokens for nothing |

A two-month-old `confidence: high` note on pricing is not a fact — it is a fact-shaped memory of one. Confidence records how sure we were WHEN WRITTEN; it never ages in place.

**A stored `falsifier` is checked here too.** If the note names a condition that would flip it, test that condition before trusting the note — a falsifier that has fired makes the note stale regardless of its `updated` date.

**Partial familiarity is not knowledge.** Knowing a provider or model FAMILY is not knowing its current release: "I know the Anthropic API" ≠ knowing today's model IDs or prices. An unfamiliar capitalized name almost certainly postdates training — a trigger to look, not to infer. If you cannot name the exact version / ID / number asked about, you do not know it. Look it up or say so; never reconstruct a plausible one.

## 4. Frontmatter contract (mandatory on every wiki page)

```yaml
---
type: api | pattern | tool | concept | discovery
tags: [tag-one, tag-two, ...]    # YAML block list, ~10 items
summary: "2-3 sentences in the vault's working language: what the page is, why it
         exists, and the key insight."
question: "<the user's verbatim question, unchanged>"
confidence: high | medium | low
effort: low | med | max          # research effort this page was produced at
falsifier: "<specific checkable evidence that would flip the recommendation>"
contradictions: "none found (N sources)" | "<N> found — see section" | "unchecked (single-source)"
sources:
  - "tier:primary | Anthropic docs — prompt caching, 2026-04-20 | found: official docs"
updated: YYYY-MM-DD
related: [path/to/related.md]
---
```

- `tags` — the words the user actually types when asking, plus the technical terms that have no translation. `updated` — bump on every touch.
- `question` — verbatim, never paraphrased. On UPDATE keep the original and append the new question on a new line.
- `sources` — **attribution strings** `tier:<primary|secondary|anecdotal> | <what> | found: <how>`, never an integer counter. Append, never rewrite.
- `summary` — 2-3 sentences in the vault's working language, **must contain >=2 tag keywords** (stem-match). If not, rewrite before saving.
- `confidence` — raise only on a fresh confirming source. **Never lower without user approval.**
- `effort` — a `low` page is not a weaker claim, it is a *less checked* one; readers need to see which.
- `falsifier` — mandatory at every level. A checkable condition (version, benchmark, study design, deprecation), never "if things change".
- `contradictions` — mandatory. `unchecked (single-source)` is the honest value at `low`; omitting the field is not.

## 5. Tag vocabulary lock (before writing new tags)

Build the canonical set from existing notes' frontmatter `tags` — read the compiled corpus (§1, each entry lists tags) or grep `tags:` across the vault. Then per proposed tag: exact match (case-insensitive) → reuse; obvious synonym or transliteration of an existing tag -> reuse the existing one; genuinely new concept → add.

Goal: prevent synonym drift. `anthropic` and a transliterated spelling of it must not coexist.

## 6. Registration — none (INDEX retired)

There is no INDEX entry to write. A note's **frontmatter `tags`** (§4) are its recall keys and its registration; the compiler globs every tagged note in a content folder into the corpus at next session start. So "registering" a page = giving it correct canonical frontmatter `tags` + `summary`, nothing more.

## 7. UPDATE vs CREATE

**UPDATE** (a page covers the entity): merge into existing sections, never duplicate. Conflict → `> [!conflict]` callout, keep both until the user resolves. Bump `updated`, append to `sources`, raise `confidence` only on a fresh confirming source. Never overwrite `summary` without approval — extend on gaps only. Form-only edits (tags/summary/title/reorder) auto-apply; changing existing prose needs approval (see `/compile`).

**Conclusion history.** When an UPDATE changes the recommendation or the falsifier, the prior one is not deleted — it moves to a `## Conclusion history` section at the bottom as `- YYYY-MM-DD — <prior recommendation> (superseded: <why>)`. Never rewrite history entries; only prepend new ones.

**CREATE** (no page exists): apply the tag lock → frontmatter per §4 (the `tags` register it, §6) → body as Summary → Key Details → Where sources disagree → Limitations/Gotchas → Code Examples. Save into a content folder; no INDEX line.

**One file per entity.** Subtopics are sections, not files. Split only past 500 lines.

## 8. Rejected shortlist (passed inline by the skill)

Entries look like `## <topic-slug>` + `date:` + `reason:` (+ optional `alternatives_considered:`).

Match the current topic against slugs + reasons — direct match OR a known synonym (common sense, not regex). On match, early-exit with:

```
**Source:** project rejected list
**Status:** already rejected on <date>
**Reason:** <reason>
**Recommendation:** revisit only if <reason> no longer applies
```

## 9. Report shape (returned to the skill, not the user)

```
**Level:** low | med | max - <N> sources (<by tier: X primary, Y secondary, Z anecdotal>)
**Source:** [URL / knowledge base: filename / project rejected list]
**Gist:** [2-4 sentences]

**Parameters and settings:**
- `param` - what it does, accepted values
**Disagreements:**
- [exactly how the sources contradict each other; OR "none found (N sources)"; OR "not checked - single source"]
**Consensus:** [what 3 or more independent sources confirm - stated without hedging; omit at low]
**Limitations:**
- [list]
**What would change the conclusion:** [a concrete, checkable condition]

**Recommendation:** [a concrete proposal]
```

At `max` the recommendation takes a side. "Depends on your priorities" is not a recommendation.
No preamble, no commentary. Just the block.

## 10. Logging

After each save, append to `$CLAUDE_WIKI_PATH/log.md`:

```markdown
## [YYYY-MM-DD] query | Topic Name
- Effort: low | med | max — N sources
- Source: [URL or "knowledge base" or "rejected-list hit"]
- Action: created X.md / updated X.md / KB hit (no changes) / early-exit (rejected) / no-sources (nothing saved)
- Pages touched: [list]
```

## 11. Hard rules (summary)

- NEVER create a new file if an existing page covers the entity — UPDATE instead.
- Saved file content: the vault's working language. Report to the caller: the language the
  user is working in.
- Non-technical topics (methodology, opinion) — skip the Obsidian save, still return the report.
- Never guess API behavior — uncertain facts get `confidence: low`.
- Zero usable sources → report it and save NOTHING. Never answer from model memory.
- Every WebFetch prompt ends with the `NO CONTENT` escape hatch; a fetch whose text lacks the asked-for value is **zero sources from that URL**, not a weak one. Nothing crosses into a note without appearing verbatim in fetched text.
- Rejected-list match → early-exit, do not research.
- Respect the ladder's ceiling. Over budget → escalate to `/deep-research`, do not grind.
