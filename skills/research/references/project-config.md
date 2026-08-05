# Project-level research artifacts

Two optional project files tune how `/research` behaves in that project. The skill reads them at Step 0 and passes their content inline to the `researcher` agent. The agent never reads them from disk — single-entrypoint rule.

---

## `<cwd>/.claude/research.yaml`

Single-line dial. Holds the project's **default** effort level. The skill still asks at every invocation (Step 0.5) — this file decides which option is pre-selected, not what runs unasked.

```yaml
effort: low | med | max
```

Auto-bootstrapped: on the first `/research` in a project with no file, the skill asks for a level and then offers to save it. Once present, edit by hand to change.

| Value | Source budget | What it buys |
|---|---|---|
| `low` | **1–2** | One authoritative source. Often answered from the vault alone. No counter-search, no contradiction analysis — the note records `contradictions: unchecked (single-source)`. |
| `med` | **3–5** | Default. One slot reserved for a counter-search; contradiction section and consensus marking are real. |
| `max` | **6–10** + one targeted fetch | Primary sources before commentary; the finding commits to a side; a de-anchored `research-critic` hunts for what would overturn it; one precision fetch on its best lead. |

Discipline is **not** on the dial. The verbatim question, source tiers, the falsifier line, the zero-source stop, always-save, and merge-don't-overwrite run at every level, because none of them costs an extra source.

Each tier is a CEILING, not a mood. Past 10 sources the `researcher` stops and escalates to `/deep-research` rather than grinding — over-budget means the question was mis-scoped, not that it needs more effort.

The authoritative budgets live in `$CLAUDE_CONFIG_DIR/agents\researcher.md` ("The research-effort ladder") — this table mirrors them for the human picking a value. If they ever disagree, the agent file wins and this one is stale.

Pick `low` for prototype/lab cells and quick lookups. Pick `max` when the result will inform an architecture decision you cannot cheaply reverse.

### Legacy `depth:` key

Older files carry `depth: shallow | standard | deep`. The skill reads them with the mapping `shallow → low`, `standard → med`, `deep → max`, then silently rewrites the file to the new key after the run. Nothing breaks; no action needed.

### Naming note

Agent frontmatter also has an `effort:` field — that one is the **model's reasoning budget** and is unrelated to this dial. In prompts and agent files the research dial is always written out as `Research effort` to keep the two apart.

---

## `<cwd>/.claude/research-rejected.md`

List of topics the project has already researched and deliberately chose not to use. Prevents re-research loops.

### Format

```markdown
# Research Rejected — <project-name>
# Topics already evaluated and ruled out. Skill reads this before dispatching to researcher.

## <topic-slug>
date: YYYY-MM-DD
reason: <one-line reason this approach does not fit>
alternatives_considered: [<slug1>, <slug2>]   # optional
```

One `##` block per rejected topic. `topic-slug` is kebab-case and matches how the user would refer to it.

### Example

```markdown
# Research Rejected — a speech pipeline

## cerebras-inference
date: 2026-03-14
reason: no Russian TTS models available, entire stack is TTS not LLM inference
alternatives_considered: [groq, replicate]

## xformers-attention
date: 2026-02-28
reason: doesn't build on Windows without WSL; project policy is native Windows
```

### Population mechanisms

Three ways to add entries (all handled by `/research`, no manual file edits required):

1. **Post-research rejection capture.** After `/research` presents findings, if the user rejects the approach ("no, that does not fit", "we will not use it", "bad fit"), the skill offers: *"Record this in the rejected list? One line for the reason?"*. One confirmation + reason → appended.
2. **`/research --reject <slug> | <reason>` shorthand.** Skips research entirely — and skips the effort question too. Directly appends an entry. Use when the user already knows something is off-limits.
3. **Manual edit.** Plain markdown; edit by hand if needed. Format is stable.

### Matching rules (researcher agent applies)

The agent scans rejected entries and matches against the current topic:
- **Direct slug match** → early-exit with an "already rejected" report.
- **Obvious synonym** (user says "cerebras api" ≈ `cerebras-inference`) → early-exit, same treatment.
- **No match** → proceed with normal research.

On match the agent returns a structured rejection report instead of running web search. The user can always override: *"Research it anyway, the reason may be stale"*.

---

## Skill integration

Step 0 — read both files. Step 0.5 — resolve the effort level (user's own words → `AskUserQuestion` → plain-text fallback), never proceed without one.

Prompt passed to the agent:

```
Research topic: [topic]
Question (verbatim): [the user's original words, unedited]
Goal: [goal]
Research effort: [low | med | max]
Rejected list (project):
[full content of research-rejected.md or "none"]
```

Step 6 — after presenting findings, if the user rejects the researched approach → offer to append a rejected-entry. Collect the reason, append in the block format above.

---

## Why project-local, not agent memory

Rejected lists drift per project (a speech pipeline's rejected ≠ SRT's rejected). Keeping them in the project tree (a) keeps them versioned with the project, (b) avoids per-project agent memory directories, (c) makes the file human-editable without touching agent infra. The agent stays universal; projects tune behaviour through two small files.
