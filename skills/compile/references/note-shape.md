# Note shape — frontmatter, registration, body, and the write-correctness laws

> Loaded on demand by `compile`'s SKILL.md. Both paths compose under this file: the unattended one at §A4 step 5, the interactive one at Step 4.

### 4a. Standard frontmatter (mandatory shape)

```yaml
---
type: api | pattern | tool | concept | discovery
tags:
  - tag1
  - tag2
  - ...
summary: "2-3 sentences in the vault's working language. What the page is, why it exists, the key insight."
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


### Write-correctness laws (obsidian-mind harvest — apply while composing)

- **Single-source status.** A volatile fact (price, limit, model id, "as of" value) lives on ONE
  page; other pages link to it, never restate it. If composing forces a restatement, that is the
  cue to run the Correction-sweep (Step 8) so the copies never diverge.
- **Mark inference.** A claim in the body not traceable to an entry in `sources:` is tagged
  `(unverified)` inline. Confidence rests on provenance — an unmarked guess reads as a fact and rots as one.
- **Never hardcode a count that rots.** Do not write "18 commands", "3 providers", "5 models" as
  prose. State it as "the commands listed in <X>" and let the list be the count. A frozen number is a
  future lie. (Audited by `/lint` 2.9 `[ROT-COUNT]`.)
- **Atomicity — one concept per note (a RETRIEVAL-quality rule, not filing taste).** Each note covers
  ONE entity/concept — one API, one pattern, one tool. Do NOT bundle UNRELATED entities in a single
  note: semantic search embeds each note as one vector, and a note spanning unrelated topics averages
  into a blurry vector that matches nothing well. One concept = one sharp vector = crisp retrieval.
  **This is NOT "fragment every facet":** a deep, many-section reference for ONE entity (e.g. the whole
  Gemini API) is correctly a single note — its summary vector stays on-topic however long the body is
  (only name+tags+summary are embedded, not the body). Split only when a note bundles concepts that
  stand alone and would each be searched for separately. The grain is the ENTITY, not the section.
