---
name: crystallize
description: After notes land in the vault, ask whether any of that knowledge wants to become a capability - a new skill or agent, or a reference added to one you already have. Proposes, never writes code; the user picks. Not note authoring, that is /compile. Triggers on - "/crystallize", "should this be a skill", "what capability is missing", "review my skills against my notes".
allowed-tools: Read, Glob, Grep, Skill, AskUserQuestion
---

# crystallize — knowledge hardening into capability

A vault fills with things you learned. Some of them are facts, and a fact belongs in a note. A few
are **procedures you keep re-deriving**, and a procedure that gets re-derived is a capability you do
not have yet.

This skill looks at what recently landed in the vault, looks at the skills and agents you already
have, and asks one question per candidate: *should this be a capability, extend one, or stay a
note?* It writes no code. It proposes, you choose.

**The default answer is stay a note.** Most knowledge is knowledge. A skill that exists because a
note existed is overhead you pay on every session for the rest of its life, so the bar below is
deliberately hard to clear.

---

## The ladder, and where a candidate sits on it

Three rungs, and a thing only moves up when it has earned the rung above.

| Rung | What lives there | Home |
|---|---|---|
| **Note** | A fact, a finding, a gotcha. Retrieved when a prompt touches it | the vault |
| **Recipe** | An ordered procedure that worked *in this project*, with the cost of skipping it | `.claude/procedural.md` |
| **Skill** | A procedure that recurs across *different* projects, with steps and a completion bar | `CONFIG/skills/` |

Two promotions, two different bars. Note to recipe: **it has steps, and skipping them costs
something.** Recipe to skill: **it recurred somewhere else too.** A procedure seen once in one
project is a recipe, however elegant it looks.

## Step 0 — Gather the three inputs

1. **What landed.** The notes written or updated recently. After `/compile` those are the pages it
   just touched; run standalone, take the notes changed in the last week.
2. **What you already have.** Every `SKILL.md` under `CONFIG/skills/` and every card in
   `CONFIG/agents/` — read the frontmatter only: name, description, triggers. That is enough to
   know whether a capability already covers a candidate, and it keeps this step cheap.
3. **What already recurs.** `.claude/procedural.md` in this project, if it exists. A recipe already
   promoted here is the strongest evidence a candidate has.

**Done when:** the three lists exist and their sizes are stated. If the vault is unreachable, say so
and stop rather than proposing from memory.

## Step 1 — Filter to candidates

Read each recent note and keep only those that pass **both** tests:

- **It has an ordered procedure**, explicit or implied. "Do X, then Y, and check Z" is a procedure;
  "X costs twice what Y costs" is a fact.
- **It was arrived at more than once**, or it says so outright. Look for the words that betray
  repetition: *again*, *every time*, *as usual*, *the same thing happened*. A single occurrence is
  a note that may become a candidate later.

Everything else is dropped here, silently. Do not carry a weak candidate forward to be judged
later; the later steps are for shaping proposals, not for filtering.

**Done when:** every recent note is either a named candidate or dropped, and the count of each is
stated. Zero candidates is the expected result on most runs and ends the skill cleanly.

## Step 2 — Route each candidate

For each survivor, decide **one** of four routes. The order matters: prefer the cheapest.

1. **Extend an existing skill.** The capability exists and is missing knowledge, not behaviour.
   Cure: a reference file beside it, a line in its body, or one more trigger phrase in its
   description. This is the most common correct answer and the one an eager agent skips.
2. **A recipe, not a skill.** It recurs in this project only. Cure: a block in
   `.claude/procedural.md` per `RECIPE_FORMAT.md`. Cheap, local, reversible.
3. **A new skill.** It recurs across projects, has steps, and no existing skill covers it. Name the
   nearest existing skill and say why it does not fit — if you cannot, this is route 1.
4. **A new agent.** Only when the work needs a *separate context*: a long read whose bulk must not
   enter the main window, or an independent judgement that must not see the main reasoning. A
   procedure that merely has many steps is a skill, not an agent.

**Done when:** every candidate carries a route, a one-line reason, and for routes 3 and 4 the name
of the nearest existing capability it is not.

## Step 3 — Propose, with the shape of the change

Present the candidates to the user as a short list. Per candidate, in three lines: what it is, which
route, and the concrete change — the file that would be created, or the file and section that would
gain a paragraph.

Ask with `AskUserQuestion` when there are one to four candidates. With more, present the list and
let the user name the ones worth doing; a wall of four-option questions is worse than a list.

State the running cost honestly for routes 3 and 4: a model-invoked skill's description sits in the
context window every turn of every session, forever. That is the price of the capability, and it is
paid whether or not it fires.

**Done when:** the user has accepted, deferred or rejected each candidate, and every accepted one
has a route and a target path.

## Step 4 — Hand off, or write the spec

The building is somebody else's job, and which somebody depends on what is installed.

- **`create-skill` or `create-agent` is present** → invoke it with the accepted candidate: its
  purpose, its triggers, the nearest neighbour it is not, and the steps drawn from the notes. Those
  skills own the authoring law; do not restate it here.
- **Neither is installed** → write the proposal to `.claude/capability-proposals.md` as a spec any
  agent can execute: name, purpose, triggers, steps, completion criteria, and the notes it came
  from. Tell the user the file is a hand-off, not a capability.
- **Route 1 or 2** → the change is small enough to make directly, and the target file already
  exists. Show the edit and apply it on the user's confirmation.

**Done when:** every accepted candidate has either been handed to a builder, written to the proposal
file, or applied as an edit — and the user has been told which of the three happened for each.

---

## What this skill refuses to do

- **It does not write skills.** Proposing and authoring are different jobs with different laws, and
  an agent that does both tends to justify what it just built.
- **It does not propose from memory.** Every candidate traces to a note read in Step 0. A capability
  proposed from a vague sense that "this comes up a lot" is exactly the overhead this skill exists
  to prevent.
- **It does not fire on its own.** It is invoked, usually right after `/compile`, when the question
  "did any of this want to be a capability" is live and cheap to answer.
