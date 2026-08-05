<p align="center">
  <img src="assets/logo.png" alt="inkling" width="180">
</p>

<h1 align="center">inkling</h1>

<p align="center">Your Obsidian vault, wired into Claude Code: notes surface on their own, and research writes itself back.</p>

---

## What it is

Notes tend to sit there. You wrote something down six months ago, forgot you wrote it, and solved the same problem again. The problem is not search. To find a note, you first have to remember it exists.

inkling removes that step. A hook reads every prompt you send, matches it against the tags and summaries of every note you have, and hands the matching ones to the agent. The note arrives when it is relevant, not when you happen to remember it.

The reverse direction works the same way. The research skill checks your vault before it checks the web, and saves what it found as a note. The same question gets paid for once.

Books are mined by cheap scout agents. The book is cut into units, the scouts read them in parallel, and only their summaries reach the expensive context. What comes out is draft concept notes.

And at the end of a long session, the system points out that something worth keeping happened. It never writes a note on its own. That call stays yours.

Once in a while a note turns out to be a procedure you keep re-deriving rather than a fact you looked up. `/crystallize` catches those: it reads what recently landed, compares it against the skills and agents you already have, and proposes either extending one or building something new. Its default answer is that a note stays a note, so most runs end in one line.

## What it looks like in use

Every block below is real output, copied from the acceptance run you can execute yourself. The
instructional headers are trimmed with `[...]` where they get long.

### A note you forgot you wrote

You ask about retrying against a slow service. Before the agent starts answering, this arrives in
its context:

```
[RECALL] Wiki notes matched by your prompt tags. For each: read the summary and decide if it
enriches the current task. [...]
  - circuit-breaker: A retry without a circuit breaker turns one slow dependency into a full
    outage: every caller piles on while the callee is already saturated. Break the circuit
    after N consecutive failures and probe with a single request.
    → /vault/patterns/circuit-breaker.md
```

You wrote that note. Nobody searched for it.

What made it findable is the entry the compiler built from its frontmatter. This is the whole
document that retrieval ranks against, and the note's body is not in it:

```json
{
  "name": "circuit-breaker",
  "path": "patterns/circuit-breaker.md",
  "tags": ["circuit-breaker", "retry", "resilience", "timeout", "outage"],
  "summary": "A retry without a circuit breaker turns one slow dependency into a full outage:
              every caller piles on while the callee is already saturated. Break the circuit
              after N consecutive failures and probe with a single request."
}
```

### Something you already did once, in this project

The second rail stores procedure rather than knowledge. Ask how to rebuild the index and it hands
back the sequence that worked here before, tagged with how much evidence stands behind it:

```
[PROCEDURAL] Action sequence(s) that worked in this project before. [...]
  - rebuild-index-after-editing-notes: [worked once] an edited note does not show up in recall -
    an edited note stays invisible until the index is recompiled
    → .claude/procedural.md#rebuild-index-after-editing-notes
```

`[worked once]` is the evidence base and it tells the agent how hard to lean. One success is a
hypothesis that happened to work; several sessions make it a pattern. Neither makes it a law.

### A moment worth keeping

You say you settled on something. The capture rail notices and suggests, without writing anything:

```
[CAPTURE] The user's message looks capture-worthy. These are SUGGESTIONS - act ONLY if the
content is genuinely durable, and never auto-write a note without the user. [...]
- This looks like a DECISION. If it is durable, meaning it shapes future work, record it
  together with the reason behind it so the same choice is not reopened in a month.
```

At the end of the session a harvester re-scans what you said and stages anything the live hint
missed. Candidates wait in `.claude/capture_inbox.md` until you run `/capture-review`. Nothing is
promoted to a note without you.

### Running out of room

A long session eventually trades what it learned for space. Before that happens:

```
[CAPTURE-DUE] Context is about 85% full (170k of 200k). This is the point where something
learned in this session gets traded for space rather than remembered.
What to do, in this order:
1. Finish the user's current request first. Do not break off mid-step for bookkeeping.
2. Then sweep THIS session for what would cost real time to re-derive: a decision and the
   reason behind it, a gotcha with the symptom that gives it away, an approach tried and
   rejected. Skip anything that only matters until this task ends.
3. Offer the shortlist to the user, or run /capture-review if candidates are staged.
If nothing durable happened, say so in one line and move on. An empty sweep is a valid result;
inventing a note to look thorough is the failure this signal exists to prevent.
```

The window comes from the model id, so a session with a million tokens of room is not warned at a
sixth of its capacity. A reading with no rule attached is decoration, which is why the block says
what to do rather than only how full things are.

### When the rail itself breaks

This is the part worth trusting. If the index is unreadable, the system says so instead of matching
nothing and looking like a quiet miss:

```
[RAIL-BROKEN] A recall corpus failed to load. This is a BUG, not an empty corpus - the affected
rail matched nothing because it could not read its index, not because nothing matched. Recall is
BLIND for that rail this session. [...]
  - wiki (Layer 5): parse index_compiled.json: invalid character 'b' looking for beginning of
    object key string
```

Silence means "searched, found nothing". It never means "did not search".

## Install

The short path: tell your agent to read `AGENTS.md` in this repository. It is the full install contract, written for an agent rather than for you. It will place the files, wire the hooks, run the acceptance suite, and then explain in plain language what changed.

By hand, three steps:

```sh
git clone https://github.com/blackboostervoid/inkling.git && cd inkling
export CLAUDE_WIKI_PATH="/path/to/your/ObsidianVault/"
CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
cp -r skills agents agent-memory hooks scripts "$CONFIG/"
```

Then merge `settings.example.json` into your own `settings.json`, and check that it works:

```sh
python3 tests/acceptance.py
```

That builds a throwaway sandbox in a temporary directory, exercises both rails inside it, and prints one line per check. Your real vault is never touched.

You need Python 3.9 and a Go toolchain. Go builds the two hooks that run on every prompt, which is why they are not written in Python.

## Writing notes that can be found

One rule matters more than the rest: **retrieval reads the filename, the tags and the `summary`, and never the body.**

```yaml
---
tags: [circuit-breaker, retry, resilience, timeout]
summary: >
  A retry without a circuit breaker turns one slow dependency into a full
  outage: every caller piles on while the callee is already saturated.
---
```

Write tags in the words you ask questions with, not the words your architecture is named with. Put the conclusion in the summary, not the topic. "A retry without a circuit breaker takes the system down" can be found and judged. "About retries and timeouts" cannot.

**Write in the language you actually talk to your agent in, and stay with one.** The default is English. This is mechanics, not style: a tag matches by appearing in your prompt, so a tag in a language you never type never fires, and the silence looks exactly like an empty vault. If your language declines its nouns, turn on the optional stemmer with `"stem": true` in `corpora.json`, or the dictionary form will keep missing every other case.

The full format is in [VAULT_CONTRACT.md](VAULT_CONTRACT.md). The recipe format for the second rail is in [RECIPE_FORMAT.md](RECIPE_FORMAT.md).

## What it does not do

It does not search note bodies. It does not write notes by itself. It does not make your agent faster. It does one thing: the note you need shows up when you need it.

## License

MIT.
