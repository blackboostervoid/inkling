# @module: skills.compile.scripts.compile_judge
# @role: the local judge for /compile -- reads every staged item, retrieves existing notes by
#        VECTOR, and proposes new / append-to-<note> / reject for each, so Claude approves a
#        proposal instead of reading the whole vault itself
# @deps: hooks/recall/semantic.py (subprocess, its documented --json interface), ollama
# @used-by: skills/compile/SKILL.md section A, hooks/capture/compile_auto.sh
#
# WHY THIS EXISTS AS CODE AND NOT AS PROSE. Until 2026-08-26 /compile was a SKILL.md and nothing
# else: the decision "does this item deserve a note, and does one already exist" was made by
# Claude reading every staged document and searching the vault by hand. That is the expensive half
# of the work and it is exactly the half a 12B local model does well -- it is retrieval plus a
# yes/no, not judgement about the codebase. The division this file draws:
#
#   the local judge PROPOSES   -- it reads the item, sees the vector-retrieved candidates, and
#                                 says new / append <slug> / reject with a one-line reason;
#   Claude APPROVES            -- it reads a compact proposal list and overrides what is wrong,
#                                 then writes the notes.
#
# Neither half is optional. A local judge alone would write the vault unattended; Claude alone
# pays full price to re-derive what a vector index already knows.
#
# `[!]` THE MODEL IS NAMED IN ONE PLACE FOR THE WHOLE HARNESS and this file does not add a second:
# `CLAUDE_RERANK_MODEL`, the same variable hooks/recall/judge.go reads (`judgeModel` at :43). A
# second spelling here would drift from the rail that already runs it.
#
# `[!]` VRAM IS RELEASED WHEN THE RUN ENDS, in a `finally`, because ollama's default keep_alive
# holds a 7.4 GB judge resident into whatever phase comes next -- and the next phase here is
# Claude writing notes while an embedding model may also want the card. An isolated test never
# sees this: the second model is not loaded, so the contention that causes it does not exist.

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

JUDGE_MODEL = os.environ.get("CLAUDE_RERANK_MODEL", "").strip()
OLLAMA = os.environ.get("CLAUDE_EMBED_URL", "http://127.0.0.1:11434").rstrip("/")
JUDGE_OPTIONS = {"temperature": 0, "num_ctx": 32768}
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["new", "append", "reject"]},
        "target": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "target", "reason"],
}
HOLDS_SCHEMA = {
    "type": "object",
    "properties": {"holds": {"type": "boolean"}, "why": {"type": "string"}},
    "required": ["holds", "why"],
}
WORKERS = 4
CANDIDATES = 6
JUDGE_TIMEOUT_S = 90
ITEM_CHARS = 1200        # what the judge reads of one item
QUERY_CHARS = 400        # what the retriever searches on

_HERE = Path(__file__).resolve()


def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env)
    return _HERE.parents[3]          # skills/compile/scripts/<this> -> config dir


def semantic_py() -> Path:
    return config_dir() / "hooks" / "recall" / "semantic.py"


def parse_items(path: Path):
    """[(kind, label, body)] from a staged document's `## [kind] label` sections.

    Measured against the real staged shape rather than assumed: obs documents are frontmatter, a
    preamble, then one `## [kind] label` heading per item. A document a HUMAN dropped in has no
    such headings, and that case is not an error -- the whole body becomes ONE item, which is what
    a person dropping a file in means."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    items, cur = [], None
    for ln in lines:
        if ln.startswith("## "):
            if cur:
                items.append(cur)
            head = ln[3:].strip()
            kind, label = "item", head
            if head.startswith("[") and "]" in head:
                kind = head[1:head.index("]")]
                label = head[head.index("]") + 1:].strip()
            cur = {"kind": kind, "label": label, "body": []}
        elif cur is not None:
            cur["body"].append(ln)
    if cur:
        items.append(cur)
    if not items:
        body = text.split("---", 2)[-1] if text.startswith("---") else text
        items = [{"kind": "document", "label": path.stem, "body": body.split("\n")}]
    out = []
    for it in items:
        body = "\n".join(it["body"]).strip()
        if body:
            out.append((it["kind"], it["label"], body))
    return out


def retrieve(query: str, corpus: str):
    """Existing notes that could already hold this, BY VECTOR. Calls semantic.py's documented
    --json interface rather than re-implementing the search: a detector that re-implements its
    consumer's reader drifts from it silently, and then 'no existing note' stops meaning what the
    writer will actually find."""
    cmd = [sys.executable, str(semantic_py()), "search", query[:QUERY_CHARS],
           "-k", str(CANDIDATES), "--corpus", corpus, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as err:
        return None, f"{type(err).__name__}: {err}"
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "").strip()[:200]
    raw = p.stdout.strip()
    if not raw:
        return [], ""
    try:
        data = json.loads(raw[raw.index("{"):] if "{" in raw else raw)
    except ValueError as err:
        return None, f"unparseable search output: {err}"
    rows = data.get("results", data if isinstance(data, list) else [])
    return [{"name": r.get("name", ""), "summary": (r.get("summary") or "")[:300],
             "path": r.get("path", ""), "score": r.get("score")} for r in rows], ""


PROMPT = """You are triaging one staged observation for a personal engineering wiki.

THE BAR, and it is the only one: an item earns a note ONLY if it records a GAP between what was
EXPECTED and what HAPPENED. A statement that is merely TRUE earns nothing. Work logs ("tests 219
-> 236, four commits, all green"), status reports, and restatements of what any competent engineer
already knows are REJECTED even when every word is accurate. Dropping most items is the normal
outcome.

THE ITEM (kind: {kind}):
{body}

EXISTING NOTES that a vector search says are closest to it:
{candidates}

Answer with ONE JSON object and nothing else:
{{"action": "new" | "append" | "reject", "target": "<note name, ONLY when action is append>", "reason": "<one short sentence>"}}

- "append" when an existing note above ALREADY covers this subject and the item adds a new
  instance, a measurement, or a sharper statement of the same mechanism. Put that note's exact
  name in "target".
- "new" when the item clears the bar and NO note above holds its subject.
- "reject" when it does not clear the bar, or when a note above already says exactly this and the
  item adds nothing.
"""


def judge_one(item, corpus, root=None):
    kind, label, body = item
    cands, err = retrieve(body, corpus)
    if cands is None:
        # An unreachable retriever must not silently become "no existing note" -- that answer
        # would make every duplicate look new. Say it, and forbid the one action that needs it.
        rendered = "(RETRIEVAL FAILED -- you may not answer 'append')"
    elif not cands:
        rendered = "(none)"
    else:
        rendered = "\n".join(f"- {c['name']}: {c['summary']}" for c in cands)

    prompt = PROMPT.format(kind=kind, body=body[:ITEM_CHARS], candidates=rendered)
    payload = {"model": JUDGE_MODEL, "prompt": prompt, "stream": False, "think": False,
               "format": VERDICT_SCHEMA, "options": dict(JUDGE_OPTIONS)}
    req = urllib.request.Request(f"{OLLAMA}/api/generate",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    verdict, jerr = None, ""
    try:
        with urllib.request.urlopen(req, timeout=JUDGE_TIMEOUT_S) as resp:
            raw = json.loads(resp.read().decode("utf-8")).get("response", "")
        verdict = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        jerr = f"{type(e).__name__}: {e}"

    action = (verdict or {}).get("action", "")
    target = (verdict or {}).get("target", "") or ""
    reason = (verdict or {}).get("reason", "") or ""
    if action not in ("new", "append", "reject"):
        action, jerr = "", jerr or f"judge returned no usable action: {verdict!r}"
    if action == "append" and cands is None:
        action, jerr = "", "judge answered append with no retrieval to append to"

    action, target, reason, flags = second_look(action, target, reason, cands, body, root)

    return {"kind": kind, "label": label, "item": body,
            "candidates": cands or [], "retrieval_error": err,
            "action": action, "target": target, "reason": reason,
            "flags": flags, "judge_error": jerr}


# THE SECOND LOOK, and it exists because of a measurement rather than a worry.
#
# MEASURED 2026-08-26, first real run: for two items whose TOP candidate came back at 0.745 and
# 0.742 -- the same note, and it already held them as a compiled section -- the judge answered
# `append <the THIRD candidate, 0.664>` and `new`. The retrieval was right and the verdict walked
# past it. A warning in the skill's prose told the approver to check; prose is executed at the
# reader's expense and skipped at the reader's discretion, so the check is here instead.
#
# `[!]` THE FIRST PASS RULES ON SUMMARIES, AND A SUMMARY IS THE WRONG EVIDENCE for this question.
# A note whose summary is about another subject entirely can still hold the item inside a section
# appended long after that summary was written -- which is exactly the shape that produced the
# near-duplicate. So this pass re-asks ONE question against the candidate's BODY.
STRONG_TOP = 0.70          # measured: the two misses sat at 0.745 and 0.742
BODY_CHARS = 6000


def second_look(action, target, reason, cands, body, root=None):
    """Re-ask the judge about the TOP candidate when the first verdict walked past a strong one."""
    flags = []
    if not cands or not action:
        return action, target, reason, flags
    top = cands[0]
    score = top.get("score") or 0.0
    names = [c.get("name", "") for c in cands]

    # `[!]` THE THIRD TRIGGER IS A REJECT THAT CLAIMS A DUPLICATE, and it was missing while the
    # other two shipped -- an asymmetry with real stakes. `new` and `append` are recoverable: a
    # duplicate note is visible and a misfiled section can be moved. A `reject` is the ONE verdict
    # whose error is irreversible on this path: the document is archived, pruned after 30 days, and
    # `compile_apply.drop_phrases` even deduplicates the forensic reason line. And the claim it
    # rests on -- "note X already says this" -- is judged on SUMMARIES, which is exactly the
    # evidence this function exists to distrust. Printing the reason to a reader cannot fix that: a
    # one-line reason IS summary-shaped. So the check belongs here, where a body read can overturn
    # it, symmetric to the look already run on the other two.
    claims_dup = action == "reject" and _claims_duplicate(reason)
    walked_past = (action == "new" and score >= STRONG_TOP) or (
        action == "append" and target and target != top.get("name") and target in names)
    if not (walked_past or claims_dup):
        return action, target, reason, flags

    if claims_dup:
        flags.append(f"reject claims a duplicate; checking {top.get('name')} by BODY")
    else:
        flags.append(f"walked past top candidate {top.get('name')} at {score:.3f}")
    holds, why = body_holds_item(top, body, root)
    if holds is None:
        flags.append(f"second look could not run: {why}")
        return action, target, reason, flags
    if holds:
        if claims_dup:
            # The duplicate claim is TRUE, and now it stands on a body read instead of a summary.
            flags.append("second look on the BODY confirms the duplicate -- reject stands")
            return action, target, (why or reason), flags
        flags.append("second look on the BODY says it already holds this -- verdict changed")
        return "append", top.get("name", ""), (why or reason), flags
    if claims_dup:
        # The claim was false. Dropping on a duplicate that does not exist loses the item for good,
        # so the verdict moves to the recoverable side rather than to a guess about the bar: what
        # was refuted is "another note holds it", and whether it CLEARS the bar was never asked.
        flags.append("second look on the BODY refutes the duplicate -- reject overturned")
        return "append", top.get("name", ""), (why or reason), flags
    flags.append("second look on the BODY says it does not hold this -- verdict stands")
    return action, target, reason, flags


_DUP_WORDS = ("duplicate", "already covered", "already recorded", "already says",
              "already captured", "already exists", "covered by", "restates")


def _claims_duplicate(reason: str) -> bool:
    """Does this reject rest on 'another note already holds it' rather than on the bar?

    A phrase match, and narrow on purpose. A false positive costs one extra local body read
    (~1-2 s); a false negative leaves the verdict exactly where it stood before this existed. Both
    directions are cheap, which is what makes a cheap discriminator the right one here.
    """
    low = (reason or "").lower()
    return any(word in low for word in _DUP_WORDS)


def body_holds_item(candidate, item, root=None):
    """(True/False/None, why). Reads the candidate note's BODY, not its summary."""
    # `[!]` semantic.py returns the path RELATIVE TO THE VAULT ROOT ("patterns/foo.md"), not to
    # the caller's cwd. The first version of this reader opened it as given and every second look
    # died with FileNotFoundError -- visibly, because the flag says so, which is the only reason
    # this was caught on the run that introduced it rather than months later.
    raw_path = candidate.get("path") or ""
    path = Path(raw_path)
    if not path.is_absolute() and root:
        path = Path(root) / raw_path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:BODY_CHARS]
    except (OSError, ValueError) as e:
        return None, f"unreadable candidate {path}: {type(e).__name__}"
    prompt = (
        "Does the NOTE below already record the OBSERVATION below -- the same mechanism, not "
        "merely a related subject?\n\nNOTE:\n" + text + "\n\nOBSERVATION:\n" + item[:ITEM_CHARS] +
        '\n\nAnswer with one JSON object: {"holds": true|false, "why": "<one short sentence>"}')
    payload = {"model": JUDGE_MODEL, "prompt": prompt, "stream": False, "think": False,
               "format": HOLDS_SCHEMA, "options": dict(JUDGE_OPTIONS)}
    req = urllib.request.Request(f"{OLLAMA}/api/generate",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=JUDGE_TIMEOUT_S) as resp:
            raw = json.loads(resp.read().decode("utf-8")).get("response", "")
        d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return None, f"{type(e).__name__}: {e}"
    return bool(d.get("holds")), str(d.get("why", ""))[:200]


def unload():
    """Release the judge from VRAM. keep_alive 0 tells ollama to drop it immediately; without it
    the default holds ~7.4 GB resident into the next phase, which is Claude writing notes while an
    embedding model wants the same card."""
    payload = {"model": JUDGE_MODEL, "prompt": "", "keep_alive": 0, "stream": False}
    req = urllib.request.Request(f"{OLLAMA}/api/generate",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True, ""
    except (urllib.error.URLError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description="Local-judge triage of staged /compile documents: propose new/append/reject.")
    ap.add_argument("--raw", default="", help="the raw/ folder (default: the vault's, from corpora.json)")
    ap.add_argument("--corpus", default="code")
    ap.add_argument("--json", action="store_true", help="machine-readable proposals on stdout")
    ap.add_argument("--archive", nargs="*", default=None, metavar="NAME",
                    help="move these staged documents into _compiled/ and verify EACH BY NAME "
                         "(omit the names to archive whatever is in the queue)")
    args = ap.parse_args()

    if args.archive is not None:
        raw_dir = Path(args.raw) if args.raw else default_raw(args.corpus)
        if raw_dir is None or not raw_dir.is_dir():
            print(f"[ERROR] no raw/ folder found ({raw_dir})", file=sys.stderr)
            return 2
        return archive(raw_dir, args.archive)

    if not JUDGE_MODEL:
        print("[ERROR] CLAUDE_RERANK_MODEL is empty -- there is no local judge to ask, and "
              "proposing without one would be cosine wearing a verdict's clothes. Refusing.",
              file=sys.stderr)
        return 2

    raw_dir = Path(args.raw) if args.raw else default_raw(args.corpus)
    if raw_dir is None or not raw_dir.is_dir():
        print(f"[ERROR] no raw/ folder found ({raw_dir}) -- nothing to triage, and an empty "
              f"answer here is indistinguishable from a clean queue. Refusing.", file=sys.stderr)
        return 2

    root = raw_dir.parent
    docs = sorted(p for p in raw_dir.glob("*.md") if not p.name.startswith("_"))
    if not docs:
        print(json.dumps({"documents": [], "proposals": [], "note": "queue empty"}, ensure_ascii=False)
              if args.json else "[OK] queue empty -- nothing staged")
        return 0

    items = [(d, it) for d in docs for it in parse_items(d)]
    print(f"[INFO] {len(docs)} document(s), {len(items)} item(s), judge={JUDGE_MODEL}",
          file=sys.stderr)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(lambda pair: {**judge_one(pair[1], args.corpus, root),
                                                  "document": pair[0].name}, items))
    finally:
        ok, uerr = unload()
        print(f"[INFO] judge unloaded from VRAM" if ok else f"[WARNING] unload failed: {uerr}",
              file=sys.stderr)

    failed = [r for r in results if not r["action"]]
    if failed:
        print(f"[WARNING] {len(failed)} of {len(results)} item(s) got NO verdict -- they are "
              f"reported with action='' and must be judged by hand, never treated as rejected",
              file=sys.stderr)

    out = {"judge": JUDGE_MODEL, "documents": [d.name for d in docs], "proposals": results}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in results:
            verdict = r["action"] or "NO VERDICT"
            tgt = f" -> {r['target']}" if r["action"] == "append" else ""
            print(f"[{verdict.upper()}{tgt}] ({r['kind']}) {r['label']}")
            print(f"    {r['reason'] or r['judge_error']}")
    return 0


def archive(raw_dir: Path, names):
    """Move staged documents into _compiled/ and verify EACH ONE BY NAME.

    `[!]` THIS REPLACES A TOTAL COUNT, and the replacement is the whole point. An earlier run's
    self-check was `61 + 2 = 63` and it reconciled perfectly -- while `obs-2026-08-26-970847b8.md`
    sat un-moved in `raw/`, because a SUM cannot say WHICH files moved. Found 2026-08-26, one
    redundant pass later. A count is a checksum over the population, and the question here is about
    a MEMBER of it; those are different questions and only one of them was being asked."""
    dest = raw_dir / "_compiled"
    dest.mkdir(exist_ok=True)
    wanted = list(names) if names else [p.name for p in sorted(raw_dir.glob("*.md"))
                                        if not p.name.startswith("_")]
    if not wanted:
        print("[OK] nothing staged to archive")
        return 0

    moved, failed = [], []
    for name in wanted:
        src, tgt = raw_dir / name, dest / name
        try:
            if src.is_file():
                src.replace(tgt)
        except OSError as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            continue
        # Verify the MEMBER, not the count: it is there, under its own name, non-empty.
        if tgt.is_file() and tgt.stat().st_size > 0:
            moved.append((name, tgt.stat().st_size))
        else:
            failed.append((name, "not in _compiled/ after the move, or empty"))

    for name, size in moved:
        print(f"[OK] archived {name} ({size} bytes, verified by name)")
    for name, why in failed:
        print(f"[ERROR] {name}: {why}", file=sys.stderr)
    if failed:
        print(f"[ERROR] {len(failed)} of {len(wanted)} document(s) did NOT reach _compiled/ -- "
              f"the queue is NOT drained, whatever a total count would say", file=sys.stderr)
        return 1
    left = [p.name for p in raw_dir.glob("*.md") if not p.name.startswith("_")]
    if left:
        print(f"[WARNING] still staged and not named to this call: {left}", file=sys.stderr)
    return 0


def default_raw(corpus: str):
    """The vault's raw/ folder, resolved by CALLING semantic.py's own `wiki_root` rather than by
    re-reading corpora.json here.

    `[!]` The first version of this function did re-read it, and it was wrong within the hour:
    corpora.json holds ONLY the corpora that override the default (often none at all), under the
    key `wiki_path`, while `code` -- the corpus this script is run on -- is resolved by
    semantic.py's own fallback and appears in that file nowhere. A resolver that re-implements its
    consumer's answers this one 'not found' on the vault the retriever searches every day."""
    recall = str(config_dir() / "hooks" / "recall")
    added = recall not in sys.path
    if added:
        sys.path.insert(0, recall)
    try:
        import semantic
        root = semantic.wiki_root(corpus)
    except (ImportError, OSError, ValueError):
        return None
    finally:
        # Leave sys.path as it was found: a config-dir engine imported for one call and left on
        # the path has changed what every later import in this process resolves to.
        if added and recall in sys.path:
            sys.path.remove(recall)
    return (Path(root) / "raw") if root else None


if __name__ == "__main__":
    sys.exit(main())
