# @module: compile_apply
# @role: Mechanical half of `/compile --auto` — turns the judge's verdicts into the two things that
#        need no language model (the drop bookkeeping and the archive) and hands the model ONLY the
#        items it must actually write.
#        Plain: does the paperwork of a compile run so the agent only writes the notes. Берёт на
#        себя всю бумажную часть разбора, чтобы модель писала только заметки.
# @deps: compile_judge (its --json proposals and its archive(), imported, never re-implemented)
# @used-by: skills/compile/SKILL.md §A; hooks/capture/compile_auto.sh
# @doc: skills/compile/SKILL.md#a-auto
#
# WHY THIS EXISTS, measured 2026-08-27 rather than assumed. `compile_judge.py --json` decides every
# staged item in 24 seconds. The skill around it is 525 lines, of which §A is a per-item protocol
# the AGENT walks by hand -- including steps 9 and 10, which are a file move and a log line. On the
# run that produced this script the judge returned SEVENTEEN verdicts of which FOURTEEN were
# rejects: 82% of the walk was the agent reading a machine's "no" and doing nothing with it.
#
# `[!]` THE SPLIT IS BY WHETHER PROSE IS PRODUCED, not by how important the step looks. `reject` is
# a decision already made, `archive` is a rename, and the log's tally is arithmetic -- none of them
# needs a model. `new` and `append` produce SENTENCES that go into the wiki, and no part of that is
# mechanical. This script does the first set and refuses to touch the second.
#
# `[!]` IT NEVER DECIDES ANYTHING. Every verdict here comes from compile_judge; this only carries
# them out. If it ever grows a rule of its own, that rule belongs in the judge where a second look
# can overturn it.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compile_judge                                                   # noqa: E402


ITEM_PREVIEW_CHARS = 2000


# ============================================================
# READING THE VERDICTS
# ============================================================

def load_proposals(path: str):
    """The judge's --json payload, from a file or from stdin.

    Deliberately NOT re-running the judge: a second run is a second set of verdicts, and the
    document the agent was shown must be the one that gets archived. Two runs of a temperature-0
    local model usually agree, and "usually" is the wrong word for the thing that decides what is
    deleted.
    """
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as err:
        print(f"[ERROR] cannot read the judge payload ({path}): {type(err).__name__}: {err}",
              file=sys.stderr)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        print(f"[ERROR] the judge payload is not JSON: {err}", file=sys.stderr)
        return None
    if not isinstance(payload, dict) or "proposals" not in payload:
        print("[ERROR] no `proposals` key -- this is not `compile_judge.py --json` output. "
              "Refusing rather than reporting an empty queue, which looks identical to a clean one.",
              file=sys.stderr)
        return None
    return payload


def split_by_action(proposals):
    """(survivors, rejects, unjudged). An item with no action is NOT a reject."""
    survivors, rejects, unjudged = [], [], []
    for item in proposals:
        action = (item.get("action") or "").strip()
        if action in ("new", "append"):
            survivors.append(item)
        elif action == "reject":
            rejects.append(item)
        else:
            # The judge failed on this one (model down, timeout, unparseable answer). Silence here
            # would archive its document as fully processed, which deletes an item nobody read.
            unjudged.append(item)
    return survivors, rejects, unjudged


# ============================================================
# THE PLAN — the only thing the agent reads
# ============================================================

def needs_session(payload) -> int:
    """Exit 0 when a language model is needed, 1 when the queue can be closed from bash.

    THE ONE PLACE THAT ANSWERS THIS, and it lives here rather than in the runner because the runner
    would then hold a second copy of the split -- and a rule owned by two readers is a rule that
    drifts. `compile_auto.sh` branches on the exit code and never counts anything itself.

    A session is needed for `new`/`append` (sentences must be written) and for any item the judge
    could NOT rule on (nobody decided it, so nobody may act on it). Nothing else.

    `[!]` THE RESIDUE, NAMED RATHER THAN DISCOVERED LATER. A queue closed from bash never gets §A6's
    misroute look, and that look is JUDGEMENT -- knowing a session was about nutrition rather than
    code needs the document read, which no exit code can do. It is bounded twice: a foreign-domain
    item that CLEARS the bar comes back `new` and boots the session anyway, so only foreign items
    that ALSO fail the bar are affected; and they are archived into `_compiled/`, not deleted, so
    the 30-day prune is the recovery window. Measured 2026-08-14: two of seven documents were
    misrouted, so this is not a theoretical class. Accepted cost, written down here.
    """
    proposals = payload.get("proposals", [])
    survivors, rejects, unjudged = split_by_action(proposals)
    if survivors or unjudged:
        print(f"[SESSION] {len(survivors)} item(s) need sentences written, "
              f"{len(unjudged)} reached no verdict")
        return 0
    # Three states share this exit code and only two of them are "the judge said no". Saying the
    # wrong one is the same defect class this skill spent the day repairing elsewhere: a message
    # that names a state the system is not in.
    if not proposals:
        print("[NO-SESSION] the queue held no items to judge -- nothing to write")
    else:
        print(f"[NO-SESSION] all {len(rejects)} item(s) rejected by the judge -- nothing to write")
    return 1


def print_rejects(rejects) -> None:
    """One line per drop: KIND first, then the label, then the judge's reason.

    WHY THE LINE EXISTS AT ALL. `--plan` originally hid rejects outright, and that removed a
    capability §A0a promises in writing: the judge "cannot know that a `reject` restates something
    the session just wrote into a different carrier", so the agent is told to OVERRIDE wrong
    rejects. An instruction nobody can follow is worse than no instruction. Fourteen lines buy it
    back.

    `[!]` KIND LEADS THE LINE, and that is the whole design rather than formatting. `observe.py`
    labels every staged item with what produced it, and they are not equally trustworthy: a
    `verdict` and a `measurement` are things that HAPPENED, a `marker` is the agent's assessment of
    its own insight. So a rejected `verdict` from a debugger smells wrong ON SIGHT while a rejected
    `marker` usually does not -- the kind is the discriminating evidence for a bar error, and the
    reason alone is not.

    `[!]` THIS LINE CANNOT SETTLE A DUPLICATION ERROR, and pretending otherwise would repeat the
    mistake this skill already measured. A reject claiming "note X already says this" is judged on
    a SUMMARY-shaped one-liner, and summaries were measured to be the wrong evidence for exactly
    that question -- which is why `compile_judge.second_look` reads a candidate's BODY. That half
    belongs in the judge, symmetric to the look it already runs on false-`new`; it is NOT
    recoverable by printing more here.
    """
    if not rejects:
        return
    print("-" * 72)
    print("  DROPPED -- one line each, so a wrong bar call is still overridable")
    print("-" * 72)
    for item in rejects:
        kind = item.get("kind") or "?"
        label = (item.get("label") or "").strip() or "(no label)"
        print(f"  ({kind}) {label[:60]} -- {(item.get('reason') or '').strip()[:90]}")
    print("  To overturn one: its full text is still in raw/, and --plan wrote nothing.")
    print()


def print_plan(payload) -> int:
    proposals = payload.get("proposals", [])
    survivors, rejects, unjudged = split_by_action(proposals)
    docs = payload.get("documents", [])

    print("=" * 72)
    print(f"  COMPILE PLAN -- {len(docs)} document(s), {len(proposals)} item(s)")
    print(f"  judge: {payload.get('judge', '?')}")
    print("=" * 72)
    print(f"[OK] {len(rejects)} dropped by the judge, {len(survivors)} to write, "
          f"{len(unjudged)} without a verdict")
    print()

    if unjudged:
        print("[WARNING] items with NO verdict -- their documents must NOT be archived:")
        for item in unjudged:
            print(f"  - {item.get('document')}: {item.get('judge_error') or 'no reason given'}")
        print()

    print_rejects(rejects)

    if not survivors:
        print("[OK] nothing to write. Run --close once you have confirmed there is nothing left.")
        return 0

    print("-" * 72)
    print("  WRITE THESE, AND ONLY THESE")
    print("-" * 72)
    for n, item in enumerate(survivors, 1):
        target = item.get("target") or "-"
        print(f"\n[{n}] {item.get('action').upper()} -> {target}   ({item.get('document')})")
        print(f"    why: {item.get('reason') or '-'}")
        for flag in item.get("flags") or []:
            print(f"    flag: {flag}")
        cands = item.get("candidates") or []
        if cands:
            near = ", ".join(f"{c.get('name')} {c.get('score', 0):.3f}" for c in cands[:3])
            print(f"    nearest: {near}")
        text = (item.get("item") or item.get("text") or "").strip()
        print("    ---")
        for line in text[:ITEM_PREVIEW_CHARS].splitlines():
            print(f"    {line}")
        if len(text) > ITEM_PREVIEW_CHARS:
            print(f"    ... [{len(text) - ITEM_PREVIEW_CHARS} more characters in the staged file]")
    print()
    return 0


# ============================================================
# THE CLOSE — bookkeeping and the archive, after the writing
# ============================================================

def drop_phrases(rejects) -> str:
    """One phrase per drop reason, deduplicated, in the order the judge produced them.

    The skill's Step 10 asks for exactly this and calls the drop count "the number that matters":
    a run that keeps everything means the bar is not being applied, and the log is the only place
    that becomes visible.
    """
    seen, out = set(), []
    for item in rejects:
        phrase = (item.get("reason") or "").strip().rstrip(".")
        key = phrase.lower()
        if phrase and key not in seen:
            seen.add(key)
            out.append(phrase)
    return "; ".join(out) if out else "no reasons recorded"


def append_log(log_path: Path, payload, kept, left) -> bool:
    proposals = payload.get("proposals", [])
    survivors, rejects, unjudged = split_by_action(proposals)
    line = (f"- Auto: {len(kept)} kept of {len(proposals)} staged "
            f"(dropped: {drop_phrases(rejects)})")
    block = ["", line]
    if left:
        block.append(f"- Auto: LEFT IN PLACE, not archived: {', '.join(sorted(left))}")
    if unjudged:
        block.append(f"- Auto: {len(unjudged)} item(s) reached no verdict and were not disposed of")
    try:
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(block) + "\n")
    except OSError as err:
        print(f"[ERROR] the log line was not written ({type(err).__name__}: {err}). "
              "NOT archiving: the archive is what makes this run unrepeatable, and it must never "
              "outrun its own record.", file=sys.stderr)
        return False
    print(f"[OK] log line appended -> {log_path}")
    print(f"     {line}")
    return True


def close_run(payload, raw_dir: Path, log_path: Path, kept, left) -> int:
    proposals = payload.get("proposals", [])
    _survivors, _rejects, unjudged = split_by_action(proposals)

    # A document holding an item the judge could not rule on is NOT fully processed. Archiving it
    # deletes evidence for a decision nobody made.
    blocked = {item.get("document") for item in unjudged if item.get("document")}
    left_set = set(left) | blocked
    if blocked:
        print(f"[WARNING] holding back {len(blocked)} document(s) with an unjudged item: "
              f"{', '.join(sorted(blocked))}")

    to_archive = [name for name in payload.get("documents", []) if name not in left_set]
    if not to_archive:
        print("[OK] nothing to archive -- every document is held back.")
        return 0

    if not append_log(log_path, payload, kept, sorted(left_set)):
        return 1
    return compile_judge.archive(raw_dir, to_archive)


# ============================================================
# ENTRY
# ============================================================

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Carry out compile_judge's verdicts: show what must be WRITTEN, then do the "
                    "bookkeeping and the archive. Decides nothing.")
    parser.add_argument("--judge-json", default="-",
                        help="the judge's --json payload; '-' reads stdin (default)")
    parser.add_argument("--plan", action="store_true",
                        help="print the items that need writing, and nothing else. Writes nothing.")
    parser.add_argument("--needs-session", action="store_true",
                        help="exit 0 if a language model is needed, 1 if the queue can be closed "
                             "from bash, 2 on any error. Writes nothing.")
    parser.add_argument("--close", action="store_true",
                        help="append the log line and archive the processed documents")
    parser.add_argument("--kept", nargs="*", default=[], metavar="NOTE",
                        help="--close: the notes this run created or appended to")
    parser.add_argument("--left", nargs="*", default=[], metavar="DOC",
                        help="--close: documents to leave in raw/ (grounding gate refused, "
                             "an error, anything not fully processed)")
    parser.add_argument("--raw", default="", help="the raw/ folder (default: the vault's)")
    parser.add_argument("--corpus", default="code")
    args = parser.parse_args()

    chosen = [name for name, on in (("--plan", args.plan), ("--close", args.close),
                                    ("--needs-session", args.needs_session)) if on]
    if len(chosen) != 1:
        print("[ERROR] choose exactly one of --plan, --close or --needs-session.", file=sys.stderr)
        return 2

    payload = load_proposals(args.judge_json)
    if payload is None:
        return 2

    if args.needs_session:
        return needs_session(payload)
    if args.plan:
        return print_plan(payload)

    raw_dir = Path(args.raw) if args.raw else compile_judge.default_raw(args.corpus)
    if raw_dir is None or not raw_dir.is_dir():
        print(f"[ERROR] no raw/ folder found ({raw_dir})", file=sys.stderr)
        return 2
    return close_run(payload, raw_dir, raw_dir.parent / "log.md", args.kept, args.left)


if __name__ == "__main__":
    sys.exit(main())
