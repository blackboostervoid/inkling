# The vault contract

Everything in inkling rests on one agreement about what a note looks like. The compiler reads it,
the scanner ranks against it, the research agent writes to it, and `/compile` produces it. Break the
agreement and nothing errors — the note simply stops being findable, silently.

This document is the agreement. It was derived by reading the parser, not by describing intent, so
where it disagrees with a habit, the parser wins.

---

## 1. The one fact that shapes everything

The retrieval document for a note is exactly three things joined together:

```
filename stem  +  tags  +  summary
```

**The body is never read.** Not by the index compiler, not by the scanner, not by the ranker. A note
whose insight lives only in its prose is invisible to recall — it exists for a human who already
knows to open it, which is the one reader who did not need the vault.

So the discipline is: whatever must be *retrievable* goes in those three fields. The body is where
the reader lands after retrieval succeeds.

## 2. Membership — what is in the corpus

Every `.md` file under the vault root that carries a frontmatter `tags` field is a corpus entry.
There is no index file to maintain; the notes are the index.

- **No `tags` field → not in the corpus.** This is the mechanism, not an oversight: it is how
  `INDEX.md`, scratch files and drafts stay out for free.
- **Excluded directories**, regardless of tags: `.obsidian`, `.claude`, `.git`, `raw`, `media`,
  `bases`, `node_modules`.
- **Name** is the filename stem — `patterns/prompt-engineering.md` is recalled as
  `prompt-engineering`. The stem is a retrieval field, so a file called `notes-2.md` is a note that
  will not be found.
- **Path** is stored relative to the vault root, so the vault may live anywhere on disk. Its
  location comes from the `CLAUDE_WIKI_PATH` environment variable and from nothing else.

## 3. Frontmatter — the exact shapes the parser accepts

The parser is deliberately stdlib-only, so it is a small set of regular expressions rather than a
full YAML engine. It accepts these shapes and silently returns empty for anything else.

**`tags` — required.** Either an inline list or a block list:

```yaml
tags: [yaml, config, gotcha, parsing]
```

```yaml
tags:
  - yaml
  - config
  - parsing
```

**`summary` — required in practice.** A note without one is in the corpus but competes with an
empty document. Three accepted forms:

```yaml
summary: One line, plain or quoted.
```

```yaml
summary: |
  A folded block. Every indented line is joined
  with single spaces into one string.
```

```yaml
summary: >
  Same treatment for the folded-scalar marker.
```

**Optional, read by humans and by other tooling but not by the ranker:** `type` (pattern / api /
tool / concept / discovery), `confidence`, `sources`, `updated`, `related`. Keep them consistent
because the vault health check reads them; the retrieval rail does not.

## 4. Language

**Write notes in the language you actually talk to your agent in. If that is more than one
language, pick the one you use most, and stay with it. The default is English.**

This is not a style preference, it is how matching works. A tag fires when it appears as a
substring of your prompt, so a tag written in a language you never type is a tag that never
matches. A vault of English notes serving prompts asked in Portuguese retrieves nothing, and the
silence looks exactly like an empty vault.

Two consequences worth knowing:

- **Be consistent within a vault.** Half the notes tagged in one language and half in another means
  half your corpus is unreachable on any given prompt. If you genuinely work in two languages, tag
  the important notes in both rather than splitting the vault down the middle.
- **A language with rich inflection needs its stemmer.** Matching is literal, so a tag in the
  dictionary form misses the same word in another case or number. The scanner ships an optional
  stemmer for exactly this, enabled per corpus with `"stem": true` in `corpora.json`. English does
  not need it. Turn it on if your language declines its nouns.

## 5. Tags — the routing surface

A tag matches when it appears **as a substring of the normalized prompt**. That single sentence
determines how tags should be written.

- **Write the words a person would actually type.** `parsing` matches a question about parsing;
  `parsing-subsystem-v2` matches nothing anybody types.
- **A multi-word tag matches as a whole phrase**, not word by word. `prompt engineering` fires only
  on that exact sequence, whereas two tags `prompt` and `engineering` each fire on their own. Prefer
  several short tags over one long one.
- Ten to fifteen tags per note is normal. Tags are cheap; a missed recall is not.

Alongside the substring ranker runs BM25 over the whole retrieval document, and the two are fused by
reciprocal rank. A note therefore has two ways to be found: a tag the prompt names outright, or
enough shared vocabulary in name and summary. Neither alone has to carry it.

## 6. Summary — written for a stranger deciding in one second

The summary does two jobs at once, and both of them are load-bearing.

It is **half the ranked document**, so its vocabulary decides what the note can be found by. And it
is **what the agent reads in the injected signal** to decide whether to open the file at all — the
signal shows one line per match, and that line is this field.

Therefore:

- Say what the note *concludes*, not what it is *about*. "Retry logic and circuit breakers" names a
  topic; "a retry without a circuit breaker turns one slow dependency into a full outage" carries
  the finding, and only the second one can be judged for relevance without opening the file.
- Include the concrete tokens someone would search by — the library name, the error, the flag, the
  number.
- Keep it current. **A stale summary is worse than no summary**: on 2026-07-28 a note whose body
  correctly said a subsystem was finished carried a summary still calling it under construction, and
  that one line was enough to misroute a recommendation. When the note's status changes, the summary
  is the part that must change with it.

## 7. `raw/` — the staging area

`raw/` holds unprocessed material: a dump from a conversation, an article, the concept notes that
`book-harvest` extracts from a book. It is excluded from the corpus by design, so nothing there
pollutes retrieval.

`/compile` is the one door out. It reads what is in `raw/`, merges it into an existing note or
creates a new one in the right topical folder, and writes the frontmatter this contract specifies.
The rule the pipeline depends on: **material enters as raw and leaves as a note; nothing is authored
straight into the corpus by a machine without passing that step.**

## 8. Layout

```
<vault-root>/
  raw/            staging, outside the corpus
  media/          images, outside the corpus
  patterns/       one folder per kind of knowledge — these names are yours
  tools/
  apis/
  concepts/
  discoveries/
```

Folders are for humans; retrieval does not care which one a note sits in. Choose few and obvious
ones, and keep a note's folder stable — the path is how the reader's link survives.

## 9. Multi-corpus, for a second vault

A second, separate vault (a personal one, a client's) registers in `corpora.json` as a namespace with
its own path, and compiles to its own index. Namespaces use an `INDEX.md` membership list instead of
globbing, which is what lets a corpus include notes living outside its own root. The default corpus
never uses `INDEX.md` — for it, frontmatter is the only source.

## 10. Tuning what surfaces — the lens

Per-project tuning lives in a lens file, and it does three things: `bridge` declares a cluster of
terms that mean the same thing, so any one of them present in the prompt pulls in all the others as
virtual tags; `suppress` drops entries that are permanent noise for this project; `boost` weights a
tag up. Ship an empty lens and one worked example — a lens is how a vault stops repeating a match
nobody wants without deleting a note somebody else needs.

## 11. The five checks before a note is done

1. Does it have `tags`? Without them it is not in the corpus at all.
2. Would the tags appear, literally, in a question someone would ask?
3. Does the summary state the conclusion, and does it still match the body?
4. Is the filename a name — something meaningful as a bare stem?
5. If the note supersedes or contradicts another, does it link it? A contradiction between two live
   notes is a corpus bug, and the only place it can be seen is in the notes themselves.

## Anti-patterns, each with its cure

- **Insight only in the body.** Cure: lift the finding into the summary; the body keeps the
  evidence, the reasoning and the examples.
- **Tags written in a private vocabulary** (`subsystem-x-retry`). Cure: tag with the words of the
  question, not the words of the architecture.
- **A summary that describes the topic.** Cure: state what you now know that you did not before.
- **A summary that aged out.** Cure: update it in the same edit that changes the note's status,
  never later.
- **Notes authored directly into the corpus by tooling.** Cure: everything machine-produced lands in
  `raw/` and passes through `/compile`, which is where a human still sees it.
