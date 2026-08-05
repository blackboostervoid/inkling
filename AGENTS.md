# AGENTS.md — installing inkling

You are an AI coding agent installing inkling on this machine. This file is the contract: follow it
in order, verify each step, and do not improvise paths. When you finish, give the user the short
report described in section 6.

inkling wires an Obsidian vault into Claude Code. Notes surface on their own when a prompt touches
them, research is written back into the vault, books are mined into notes, and the durable moments
of a session are staged for review.

---

## 1. What lands where

The install target is the user's Claude Code config directory. That is `$CLAUDE_CONFIG_DIR` when the
variable is set, and `~/.claude` when it is not. Everything below is relative to that directory,
called `CONFIG` from here on.

| From this repository | To |
|---|---|
| `skills/compile/` | `CONFIG/skills/compile/` |
| `skills/lint/` | `CONFIG/skills/lint/` |
| `skills/research/` | `CONFIG/skills/research/` |
| `skills/book-harvest/` | `CONFIG/skills/book-harvest/` |
| `skills/capture-review/` | `CONFIG/skills/capture-review/` |
| `skills/crystallize/` | `CONFIG/skills/crystallize/` |
| `skills/procedural/` | `CONFIG/skills/procedural/` |
| `agents/researcher.md` | `CONFIG/agents/researcher.md` |
| `agents/research-critic.md` | `CONFIG/agents/research-critic.md` |
| `agents/scout.md` | `CONFIG/agents/scout.md` |
| `agents/procedural-distiller.md` | `CONFIG/agents/procedural-distiller.md` |
| `agent-memory/researcher/MEMORY.md` | `CONFIG/agent-memory/researcher/MEMORY.md` |
| `hooks/recall/` | `CONFIG/hooks/recall/` |
| `hooks/capture/` | `CONFIG/hooks/capture/` |
| `hooks/build_hooks.sh` | `CONFIG/hooks/build_hooks.sh` |
| `scripts/fetch_page.py` | `CONFIG/scripts/fetch_page.py` |

Copy with a command, not by hand:

```sh
CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CONFIG/skills" "$CONFIG/agents" "$CONFIG/hooks" "$CONFIG/scripts" "$CONFIG/agent-memory"
cp -r skills/. "$CONFIG/skills/"
cp -r agents/. "$CONFIG/agents/"
cp -r agent-memory/. "$CONFIG/agent-memory/"
cp -r hooks/. "$CONFIG/hooks/"
cp scripts/fetch_page.py "$CONFIG/scripts/"
```

`agent-memory/researcher/MEMORY.md` is not optional. The `researcher` agent card is an entry point
and its rules live in that file; without it the agent has no protocol to follow.

Two files stay in the repository and are read, not copied: `VAULT_CONTRACT.md` (the note format that
every part of the system depends on) and `RECIPE_FORMAT.md` (the format of a procedural recipe).

## 2. What it depends on

Ask the user for the vault path before doing anything else, then set it:

| Requirement | Why | If missing |
|---|---|---|
| `CLAUDE_WIKI_PATH` | The absolute path of the Obsidian vault root. There is no default, on purpose | Recall has no corpus. The compiler prints one line saying so and exits 0 |
| Python 3.9 or newer, on `PATH` as `python3` | Every hook script and both skill helpers | Nothing runs. On Windows without `python3`, use `py` in the settings entries below |
| A Go toolchain | Builds the two hook binaries from source at session start | The binaries are absent and their hooks fail loudly, which is the correct signal |
| `bash` | Runs `build_hooks.sh` | Same as above; build the binaries by hand with `go build` in each hook directory |

Set the vault path in the user's shell profile as well as in the settings file, so that a hook
started outside a shell still sees it.

No third-party Python packages are required anywhere. That is deliberate: a hook runs on every
prompt, and a missing pip package on one machine would break the hot path silently.

## 3. Wire the hooks

`settings.example.json` in this repository holds the exact entries. Merge them into
`CONFIG/settings.json`. If the user already has hooks on `SessionStart`, `UserPromptSubmit` or
`Stop`, **append** to those arrays rather than replacing them.

The entries do three jobs:

- **SessionStart** builds the binaries if their source changed, compiles the vault index and the
  project recipes, seeds the lens files, and nudges if capture candidates are waiting.
- **UserPromptSubmit** runs the two scanners, which is where `[RECALL]`, `[PROCEDURAL]`,
  `[RAIL-BROKEN]` and `[CAPTURE]` come from, plus the context-threshold arm.
- **Stop** folds the feedback, writes evidence back into notes, and harvests staged candidates.

After merging, validate the file parses as JSON before ending the session. A broken `settings.json`
disables every hook at once.

## 4. What degrades, and how

Each of these is optional. Its absence disables one feature silently and leaves the rest working.

| Absent | Lost | Behaviour |
|---|---|---|
| ollama with the `bge-m3` model | Meaning-based search (`semantic.py`) | The tool reports ollama is unreachable. Lexical recall is unaffected |
| `<project>/.claude/procedural.md` | The procedural rail for that project | No `[PROCEDURAL]` block. Nothing is written, nothing errors |
| A lens file | Per-project tuning of what surfaces | Defaults apply: no bridges, no suppressions, no boosts |
| `corpora.json` entries | Extra vaults | Only the default corpus compiles. `corpora.example.json` shows the shape |
| A `project-explorer` agent | The blast-radius step of `/research` at `med` and `max` | That step is skipped. It is not part of inkling |

One thing never degrades quietly: a corpus file that **exists but fails to load** produces a
`[RAIL-BROKEN]` block instead of silence, because a blind rail that looks like an honest miss is how
this kind of system dies unnoticed.

## 5. How it is invoked, and what done looks like

The hooks run themselves. The skills are invoked by name in a session:

| Skill | What it does |
|---|---|
| `/compile` | Turns files in the vault's `raw/` into notes with correct frontmatter |
| `/lint` | Audits the vault for contradictions, stale pages, orphans |
| `/research <topic>` | Vault first, then the web, then writes the finding back as a note |
| `/book-harvest <path>` | Cheap scouts read a book in units; concepts land in `raw/` |
| `/capture-review` | Promotes or drops the candidates staged from a session |
| `/crystallize` | Asks whether recent notes want to become a capability, and proposes how |
| `/procedural batch` | Distils recurring action sequences into recipe candidates |

Verify the install by running the acceptance suite, which builds a throwaway sandbox and touches
nothing of the user's:

```sh
python3 tests/acceptance.py
```

**The install is done when that run reports every check passed.** It proves, among other things,
that the binaries build from source, that the index compiles, that a tagged prompt produces a
`[RECALL]` block, that a broken corpus speaks, and that an unconfigured vault says so instead of
indexing the current directory. If any check fails, fix that check before reporting success: a
partially wired install fails silently later, which is the failure mode this whole system exists to
prevent.

Then create one real note in the user's vault to close the loop: it needs frontmatter `tags` and a
`summary`, per `VAULT_CONTRACT.md`. Recall reads the filename, the tags and the summary, and never
the body, so a note whose insight lives only in its prose cannot be found.

## 6. The report you give the user

End with a short spoken-language summary. No file lists, no command dumps, no jargon. Cover four
things, in about a paragraph each:

1. **What is now installed** — an Obsidian vault wired into their agent, in one sentence.
2. **What changes for them day to day** — notes they wrote months ago will appear on their own when
   a question touches them; research they ask for gets saved instead of scrolling past; a long
   session will offer to keep what was worth keeping.
3. **What they have to do** — write notes with tags and a summary, because that is what retrieval
   reads; put unprocessed material in `raw/` and run `/compile`; say yes or no when the system
   proposes something for the vault, since nothing is written without them.
4. **The one thing that would break it** — moving the vault without updating `CLAUDE_WIKI_PATH`.

Be accurate about what it does not do. It does not read their notes' bodies to find them, it does
not write notes on its own, and it does not make anything faster. It makes the right note show up
at the right moment, which is a different promise.
