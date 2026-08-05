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

### The note that travels with it

A note can say which other note belongs with it, in its own frontmatter:

```yaml
related:
  - patterns/circuit-breaker.md
```

When the strongest match carries a link, one linked note comes with it, marked so you can tell it apart:

```
  - yaml-booleans: PyYAML defaults to YAML 1.1, so off/on/yes/no parse as booleans unless
    quoted. -> patterns/yaml-booleans.md [linked from circuit-breaker - it did not match your
    words, it is the note that one points to]
```

**Writing those links is easier with an optional helper, and the system does not need it.** If you run a local embedding model — ollama or LM Studio, either works — `semantic.py suggest` proposes `related` lines by meaning, with a score, and writes nothing. Without it you write links by hand, and everything else behaves identically. The model is never involved when a prompt is answered: the link was decided when the note was written, and walking it is a dictionary lookup.

That is the answer to the obvious hole in literal matching: the note you need is about the same thing under different words. Only the top match hops, only one note is added, and the hop never chains, because a hop from a weak match multiplies noise instead of fixing it.

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

The same optional layer has a second stage worth knowing about. With `CLAUDE_RERANK_MODEL` set, a search runs its shortlist past a local model that scores each note against the question and drops the ones that do not clear the bar. What that buys is not a better order, it is the ability to answer *nothing*: similarity scores can rank but never abstain, so without it a search always hands back its closest guess. Measured on a 112-note vault, a question the vault had no answer for came back empty instead of returning a confident-looking wrong note.

The full format is in [VAULT_CONTRACT.md](VAULT_CONTRACT.md). The recipe format for the second rail is in [RECIPE_FORMAT.md](RECIPE_FORMAT.md).

## What it does not do

It does not search note bodies. It does not write notes by itself. It does not make your agent faster. It does one thing: the note you need shows up when you need it.

## Know this before you install

Four honest limits. None of them is a bug, and all four decide whether this fits you.

**Retrieval sees three fields, and a note that skips them fails silently.** Ranking reads the filename, the tags and the summary. A note whose insight lives only in its body is never found, and that looks like an empty vault rather than an error.

How much of that lands on you depends on how the note got there. **Every path that writes for you carries the template**: `/compile` fills the frontmatter and then checks that the summary shares at least two keywords with the tags, `researcher` treats the same frontmatter as a contract it must satisfy before saving, `book-harvest` writes into `raw/` where nothing is indexed until `/compile` gives it that frontmatter, and the capture rail routes through the same door. So notes the system writes are covered by construction.

The discipline you actually owe is for **notes you write by hand in Obsidian**. There the three fields are yours, and nothing stops you from saving a page that cannot be found. Two things push back: the index compiler prints a warning naming every tagged note with no summary, and `/lint` audits the rest of the vault. Neither can write a good summary for you.

**One vault, one language.** A tag matches by appearing in your prompt, so a vault tagged in a language you do not type retrieves nothing. Mixing two languages halves your reachable corpus on any given prompt; tag the important notes in both if you truly work in two. For a language that declines its nouns there is an opt-in stemmer, and its quality is not measured, only exercised by unit tests. Treat it as a mitigation, not a solution.

**It installs globally and runs on every prompt.** The hooks live in `~/.claude`, not in one project, and the scanners execute on each message you send. Measured cost on this repository: **about 500 tokens per turn** for the skill descriptions that stay resident, plus **about 200-250 tokens on a prompt where recall actually fires** (the match cap is two). A prompt that matches nothing adds nothing. The scanners are Go binaries built from the source in `hooks/`, and reading that source before you install is a reasonable thing to want; it is about 1,000 lines and it is the whole rail.

**This is one person's working setup, published.** One author, no release cadence, no support promise. What it does have is a CI run on every push and an acceptance suite you can run yourself: `python3 tests/acceptance.py` builds a throwaway install and proves the rails work, and `python3 tests/check_shipped.py` proves the repository actually contains what the install contract promises. That second check exists because it caught a real failure: a `.gitignore` line inherited from a private repo excluded the researcher's protocol file, every disk-reading test passed, and the published install could not have worked.

## License

MIT.
