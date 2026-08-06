# inkling MCP server

Five tools over your vault, spoken as MCP: search by meaning, read a note, follow its links, stage
material, link two notes.

## Why this exists

inkling's recall hook is a **push** rail. It fires on every prompt, costs almost nothing, and is the
reason the vault gets read at all. Because it fires always, it has to stay cheap — so the expensive
half, meaning-based search with a judge that can answer *"nothing here"*, lives off the hot path as
a command somebody has to remember to type.

Nobody remembers. In a sibling project the same shape was measured on 2026-08-06: a capability
agents call ran 70 times in a week, and one mandated in prose ran **0 times out of 28**.

A tool carries its own description in the protocol — it sits in front of the model every turn with
no one remembering anything. So this is not mainly a second door for other clients. It is how the
deep half of inkling becomes reachable at all.

The two rails are complements, not alternatives. Keep the hook.

## Install

```sh
cd mcp
npm install
npm run build
```

Then register it with your client. For Claude Code:

```sh
claude mcp add inkling --env CLAUDE_WIKI_PATH=/absolute/path/to/vault -- node /absolute/path/to/inkling/mcp/dist/index.js
```

Or as JSON, for any client that takes a config file:

```json
{
  "mcpServers": {
    "inkling": {
      "command": "node",
      "args": ["/absolute/path/to/inkling/mcp/dist/index.js"],
      "env": {
        "CLAUDE_WIKI_PATH": "/absolute/path/to/your/vault/",
        "CLAUDE_CONFIG_DIR": "/absolute/path/to/your/.claude",
        "CLAUDE_RERANK_MODEL": "gemma3:12b"
      }
    }
  }
}
```

| Variable | Needed | What it does |
|---|---|---|
| `CLAUDE_WIKI_PATH` | **yes** | Vault root. The server refuses to start without it, rather than starting and failing every call. |
| `CLAUDE_CONFIG_DIR` | if not `~/.claude` | Where `hooks/recall/semantic.py` and the compiled index live. |
| `CLAUDE_RERANK_MODEL` | recommended | The local judge. Without it, search cannot answer "nothing here" — and says so in every result rather than pretending. |
| `INKLING_PYTHON` | rarely | Interpreter for `semantic.py`. `python3`, `py` and `python` are tried in that order. |

Search needs the embedding layer from AGENTS.md §4a. Everything else works without it.

## The tools

| Tool | What it answers |
|---|---|
| `vault_search` | "Have I written about this?" — by meaning, not keyword. With a judge configured it can answer **no**. |
| `vault_read` | The whole note behind a hit. |
| `vault_neighbors` | What the author said travels with this note. Walks 1–3 hops; the hook walks exactly one. |
| `vault_capture` | Stage material into `raw/` for later compiling. |
| `vault_link` | Record that two existing notes belong together. |

## Two things it deliberately does not do

**It never writes a note straight into the corpus.** `vault_capture` lands in `raw/`, because
`VAULT_CONTRACT.md` §8 says material enters as raw and leaves as a note, and `/compile` is where a
human still sees it. A server that authored notes directly would break the one rule the rest of the
system rests on, and break it silently.

**It never re-implements retrieval.** Search is `semantic.py` invoked as a process; membership and
the link graph are read from `index_compiled.json`. A second implementation in TypeScript would be a
second answer to a question that already has one, and the two would drift apart without anyone
noticing.

## Checking it

```sh
CLAUDE_WIKI_PATH=/path/to/vault node smoke.mjs
```

Speaks the protocol to the built server and exercises every tool, including the two refusals that
matter: a path that tries to escape the vault, and a link to a note that does not exist.
