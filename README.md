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

## What it looks like in use

You ask about retrying against a slow service. Before the agent starts answering, this arrives in its context:

```
[RECALL] Wiki notes matched by your prompt tags.
  - circuit-breaker: A retry without a circuit breaker turns one slow dependency into a
    full outage: every caller piles on while the callee is already saturated. Break the
    circuit after N consecutive failures and probe with a single request.
    -> patterns/circuit-breaker.md
```

You wrote that note. The agent found it by itself.

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

The full format is in [VAULT_CONTRACT.md](VAULT_CONTRACT.md). The recipe format for the second rail is in [RECIPE_FORMAT.md](RECIPE_FORMAT.md).

## What it does not do

It does not search note bodies. It does not write notes by itself. It does not make your agent faster. It does one thing: the note you need shows up when you need it.

## License

MIT.
