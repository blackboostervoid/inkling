# The recipe contract

The second rail stores what worked *here*, in this project, as recipes in
`<project>/.claude/procedural.md`. This file is what the compiler parses. Get the shape wrong and
nothing errors — you get zero recipes and a cheerful success line, which is exactly how a
hand-written file stays invisible.

That trap is why this page exists, and why the compiler now warns when a file with content yields
no recipes.

---

## One recipe

```markdown
---
slug: rebuild-index-after-editing-notes
trigger: an edited note does not show up in recall
keywords: [index, rebuild, search, notes, recall]
status: active
---
1. Edit the note.
2. Run the index compiler.
3. Confirm the entry count changed.
Why: an edited note stays invisible until the index is recompiled, so the symptom looks
like a retrieval bug when it is a stale build.
Anti-pattern: debugging the scanner, which is working correctly on a stale corpus.
```

Blocks repeat. Everything between one closing `---` and the next opening `---` is that recipe's
body.

## The fields

| Field | Required | What it does |
|---|---|---|
| `slug` | yes | Stable identifier. The signal points the agent at `procedural.md#<slug>`, and promote / reject / archive act on it |
| `trigger` | yes | The situation in one line. Half of what gets matched against the prompt |
| `keywords` | yes | Inline list. The routing surface, same substring logic as note tags |
| `status` | yes | **Only `active` compiles.** Anything else — `candidate`, `archived` — is stored but never surfaces |

In the body, the **first `Why:` line is the other half of the matchable text** and the line the
agent reads in the signal. Write the causation there: not what the steps are, but why skipping them
costs something.

**Steps are deliberately not compiled.** The signal shows trigger plus why; the agent that decides
the recipe fits then opens the file and reads the steps. This keeps the injected block short enough
that it stays cheap on every single prompt.

## What the agent sees

```
[PROCEDURAL] Action sequence(s) that worked in this project before.
  - rebuild-index-after-editing-notes: [worked once] an edited note does not show up in recall -
    an edited note stays invisible until the index is recompiled ... -> .claude/procedural.md#rebuild-index-after-editing-notes
```

The bracketed tag is the **evidence base**, and it says how hard to lean. `worked once` is a
hypothesis that happened to succeed — read the steps, then decide whether they fit and expect to
deviate. Several sessions make it a pattern. Neither makes it a law: a recipe is an observation
about the past, and a rule in your project's instructions outranks it every time.

## Lifecycle

A recipe is born as a candidate (distilled from past sessions, or written by hand), gets promoted
to `active` by a human, and is archived when the world it described changes. The promote / reject /
archive commands live in the `/procedural` skill. Compilation happens on a freshness check at
session start, so an edited `procedural.md` is live in the next session without any manual step.

## Two checks before a recipe is done

1. Does the trigger describe a **situation you would recognise**, rather than a task you would name?
   The rail fires on the prompt, so it must match how the problem shows up, not how the fix is filed.
2. Does the `Why:` line state a **cost** — what goes wrong without these steps? A why that restates
   the steps teaches nothing and cannot be judged for relevance from the signal alone.
