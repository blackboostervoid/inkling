---
name: research
description: Research a lib / API / model to decide whether and how to build it here. Vault-first: checks the Obsidian wiki before the web, honours the rejected-list, and writes findings back so the question is never paid for twice. Three effort levels, asked up front. Triggers on - "исследуй", "как работает [API/модель]", "найди информацию", "можно ли добавить".
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

1. **The user already said it in their prompt** — "быстро глянь", "по-быстрому", "на максималках", "копай глубоко", "low/med/max" — take it and skip the question. Say which level you took.
2. **Otherwise, ask with `AskUserQuestion`.** One question, three options, with the project default from Step 0 listed first and marked `(по умолчанию проекта)`. If no project default exists, put `med` first and mark it `(рекомендую)`.

   Option labels and descriptions:
   - **low — 1-2 источника.** Быстрый ответ. Часто целиком из вики, без веба. Противоречия не проверяются, контр-поиска нет.
   - **med — 3-5 источников.** Дефолт. Один слот жёстко зарезервирован под контр-поиск, есть раздел расхождений и отметка консенсуса.
   - **max — 6-10 источников + добор.** Первичка вперёд, агент занимает сторону, отдельный де-анкоренный критик ищет чем это опровергнуть, один точечный добор по его наводке.

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

Create the file with header if it does not exist. Report: *«Записано в rejected list.»* Done.

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

Return: structured findings per your report shape — level + source tiers, суть,
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
Contradictions: [what the researcher reported about disagreement, or "не проверялись"]

Return: verdict, ranked overturn leads, best lead, and what class of source
the corpus is missing. You cannot write anywhere — leads only.
```

---

## Step 2.6 — Targeted overturn fetch (`max` ONLY, conditional)

- **Critic verdict `держится`** → skip the fetch. Do not spend a source confirming what already holds. Pass the verdict to the researcher to record in the note and move on.
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
- «Куда легло в вики» in the final report is filled from what was ACTUALLY written to disk,
  never from intention.

```
## Что нашёл

**Уровень:** [low | med | max] — [N] источников ([X primary, Y secondary, Z anecdotal])
**Источник:** [URL / База знаний / project rejected list]
**Суть:** [2-4 предложения]

**Параметры / настройки:**
- `param` — описание, допустимые значения

**Где источники расходятся:**
- [конкретные расхождения; ИЛИ «расхождений не нашёл (N источников)»; ИЛИ «не проверял — один источник»]

**Консенсус:** [что подтверждают ≥3 независимых источника — опустить на low]

**Ограничения:**
- [список]

**Что изменит вывод:** [конкретное проверяемое условие]

**Проверка на прочность:** [только max — вердикт критика + что он не нашёл в корпусе; и потратили ли добор]

**Как вписывается в проект:** [только med/max]
- Затронет: [файлы/модули]
- Blast radius: [low/medium/high]
- Конфликты: [есть / нет]

**Куда легло в вики:** [folder/note.md — создано / обновлено]

**Рекомендация:** [стоит делать / как подходить]
```

Sections that do not apply at the chosen level are omitted, not filled with "н/д".

After findings → ask: *"Хочешь составить план реализации?"*

---

## Step 6 — Post-research rejection capture

If user responds to findings with clear rejection ("не подходит", "не берём", "bad fit", "отклоняем"):

Ask: *«Записать в rejected list для этого проекта? Одна строка причины?»*

On yes + reason → append to `<cwd>/.claude/research-rejected.md`:

```markdown
## <topic-slug>
date: <today>
reason: <user's reason>
```

Create file with header if missing. Confirm: *«Записано. Следующий `/research` на эту тему сделает early-exit.»*

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
