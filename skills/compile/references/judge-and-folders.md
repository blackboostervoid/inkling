# Moved out of SKILL.md 2026-08-27 (skill-craft rule 2.2)

Two blocks that DOCUMENT a mechanism rather than instruct a reader. The skill points here;
nothing there restates what is written here.

---

### What the judge itself does

It walks every staged item, retrieves the closest existing
notes BY VECTOR (calling `semantic.py`'s own `--json` interface, not a second search), asks
`CLAUDE_RERANK_MODEL` for one verdict per item -- `new` / `append <note>` / `reject` with a
one-line reason -- and unloads the model from VRAM when it returns. Measured 2026-08-26 on a real
queue: two documents, seventeen items, **25 seconds**, verdicts 3 new / 1 append / 13 reject.

**THE DIVISION OF LABOUR IS THE POINT.** The judge PROPOSES; you APPROVE. What you do with its
output:

- Read the proposals, not the whole vault. The expensive half -- "does a note about this already
  exist" -- is a retrieval question and the vector index has already answered it.
- **Override what is wrong and say so.** The judge is a 12B model with the item and six candidate
  summaries; it has never seen this session. It cannot know that a `reject` restates something the
  session just wrote into a different carrier, and it cannot know that an `append` target is the
  wrong note for a reason that lives in the body. Its verdict is a proposal, and yours is the one
  that lands.
- **`action: ""` is NOT a rejection.** It means the judge gave no usable verdict, or retrieval
  failed and it may not answer `append`. Those items are judged by hand -- treat them as unread,
  never as dropped.
- The script REFUSES rather than guesses: no `CLAUDE_RERANK_MODEL`, no raw folder, or an
  unreachable retriever each exit non-zero with the reason. A refusal is a result; do not route
  around it by reading the queue yourself, fix what it named.


`[!]` **ITS FIRST REAL RUN MEASURED ITS OWN FAILURE MODE, AND THE FIX IS CODE, NOT THIS
PARAGRAPH.** Two items whose top candidate came back at **0.745** and **0.742** -- the same note,
already holding them as a compiled section -- got `append <the THIRD candidate, 0.664>` and `new`.
The vectors did their job; the 12B model picked past them. A warning here telling you to check
would be executed at your expense and skipped at your discretion, so the script now does it:

**THE SECOND LOOK.** When a verdict walks past a strong top candidate -- `new` with a top hit at or
above **0.70**, or `append` aimed at a lower-ranked one -- the script re-asks ONE question against
that candidate's **BODY**, not its summary. A summary is the wrong evidence for this question: the
section that already holds the item may have been appended long after the summary was written,
which is precisely what produced the near-duplicate. If the body holds it, the verdict FLIPS to
`append` and the `flags` field says so.

Proven on the input that produced the defect: the same document that once yielded `new` against a
0.905 top candidate now yields `append -> the-sibling-domain-that-skips-the-guard`, flagged
`second look on the BODY says it already holds this -- verdict changed`.

**READ `flags` ON EVERY PROPOSAL.** It is where the machine tells you what it could not settle:
- `second look could not run: <why>` -- the check FIRED and could not complete. Not a pass. The
  first version of it died with FileNotFoundError on every candidate, because `semantic.py` returns
  paths relative to the VAULT ROOT and the reader opened them relative to cwd; it was caught the
  same run only because the flag said so instead of reporting a clean verdict.
- `walked past top candidate ...` with the verdict unchanged -- the body was read and genuinely
  does not hold the item. That one is settled; the note above it is not.

`[!]` **An empty `candidates` list on a `new` verdict is not a clean answer** -- retrieval returned
nothing, so `new` was the only verdict available and carries no information. Search that item
yourself before writing.

`[!]` **Until 2026-08-26 this step did not exist and this file WAS the mechanism** -- the decision
was made by Claude reading every staged document and searching the vault by hand, at full price,
every run. The prose stayed; what changed is that the retrieval-and-triage half now happens in a
25-second local call and this file's job starts at the approval.

## A6. The two subfolders under `raw/`, and why neither may grow forever

`raw/` holds the live queue at its top level. Two subfolders sit beneath it, and both were quietly
unbounded until 2026-08-16.

**`raw/_compiled/` — sources already consumed.** Step 9 of `references/interactive.md` says delete, Step 8b says the source is the
EVIDENCE a note's quotes rest on and cannot be checked once gone. Both are right about different
spans of time: the grounding gate runs inside the same compile, so the evidence has a useful life
of days. The reconciliation is the retention, not a choice between them — archive here, and
`compile_auto.sh` prunes anything older than 30 days. The durable trail stays `log.md` plus the
note. An archive with no expiry makes the intake shrink at the top and grow underneath, which is
the same pile wearing a tidier name.

**`raw/_misrouted/` — documents belonging to a different vault.** `observe.py` picks its target
from one variable, `CLAUDE_WIKI_PATH`, and knows nothing about what the session was about, so a
session on nutrition or on audio mastering stages into the CODE wiki. Two of seven on 2026-08-14 —
close to a third.

A misrouted document is **ROUTED, then removed from here** — never parked. Read its recorded
disposition, move the file into the owning vault's own intake, and say in the report that it now
waits on that domain (most vaults have no automatic compile, so the honest report is "moved, not
compiled"). Parking is what produced the folder: the two documents found on 08-14 carried a written
verdict naming their domain and were still sitting here eight days later, because "belongs to
someone else" was recorded and nobody was named.

---

