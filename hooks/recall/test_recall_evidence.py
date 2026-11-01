# @module: test_recall_evidence
# @role: gate for the note-inline evidence log - proves a note can carry BOTH doors' sections
#        without either destroying the other, and carries its own red half so a green run means
#        the checks can still fail
# @deps: recall_evidence (sibling module)
# @used-by: run by hand after touching recall_evidence.py; `python3 hooks/recall/test_recall_evidence.py`
# @doc: documentation/ - the two doors are described in CLAUDE.md's [RECALL] / vault_search entries
"""
The defect this exists for, stated once so nobody has to re-derive it:

`split_evidence_section` used to collect every `- ` bullet from its header to END OF FILE. With a
single evidence section that is harmless. The moment a note can carry two, the first section's
read swallows the second's bullets, and the rebuild writes them under the wrong header and drops
the second header entirely. It is silent, and what it destroys is the note's own track record.

So the checks below are not a formality: section 2 and 3 are the defect, and `red_half()` at the
end re-installs the old implementation and asserts the checks GO RED against it. A green run with
no red half proves only that the harness works.

Stdlib only. Invoke via `py`.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall_evidence as ev  # noqa: E402  (path set just above)

PUSH, PULL = ev.EVIDENCE_HEADER, ev.SMRITI_HEADER

NOTE = """---
name: probe-note
---

Body text that must survive every rewrite.

## Раздел с содержанием

- a real content bullet, not evidence
- another one

## Источники

- https://example.invalid
"""


class Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, label, condition, detail=""):
        print("  %-58s %s" % (label, "OK" if condition else "FAIL " + detail))
        if not condition:
            self.failures.append(label)


class QuietChecker(Checker):
    """Records without printing. Used by the red half, whose failures are the EXPECTED result -
    printing them mixes deliberate red into a passing run's output and reads as a broken gate.
    A subclass rather than a patched instance attribute: Python resolves __call__ on the type,
    so assigning it to an instance silently does nothing (which is how this got noisy once)."""

    def __call__(self, label, condition, detail=""):
        if not condition:
            self.failures.append(label)


def fresh_note() -> Path:
    note = Path(tempfile.mkdtemp()) / "probe.md"
    note.write_text(NOTE, encoding="utf-8")
    return note


def two_section_checks(check: Checker) -> None:
    """The checks that carry the defect. Kept in one function so the red half can rerun them."""
    note = fresh_note()

    print("1. push door writes its section")
    check("append returns True", ev.append_evidence(note, PUSH, "aaaaaaaa", "why push", "2026-08-07"))
    text = note.read_text(encoding="utf-8")
    check("push header present", PUSH in text)
    check("body survived", "Body text that must survive every rewrite." in text)
    check("content bullets survived", "a real content bullet, not evidence" in text)
    check("sources section survived", "## Источники" in text)

    print("2. pull door writes its own section, does not disturb the first")
    check("append returns True", ev.append_evidence(note, PULL, "bbbbbbbb", "why pull", "2026-08-07"))
    text = note.read_text(encoding="utf-8")
    check("BOTH headers present", PUSH in text and PULL in text, "<- the swallow bug")
    check("push line under push header", text.split(PUSH)[1].split("##")[0].count("why push") == 1)
    check("pull line under pull header", PULL in text and text.split(PULL)[1].count("why pull") == 1)
    check("pull line NOT under push header", "why pull" not in text.split(PUSH)[1].split("##")[0])

    print("3. push writes again next session — must not eat the pull section")
    check("append returns True",
          ev.append_evidence(note, PUSH, "cccccccc", "why push 2", "2026-08-08"))
    text = note.read_text(encoding="utf-8")
    check("BOTH headers still present", PUSH in text and PULL in text)
    check("pull bullet survived a push write", "why pull" in text)
    check("push section has both its lines", text.split(PUSH)[1].split("##")[0].count("- ") == 2)
    check("pull section still has exactly one", PULL in text and text.split(PULL)[1].count("- ") == 1)


def remaining_checks(check: Checker) -> None:
    note = fresh_note()
    ev.append_evidence(note, PUSH, "cccccccc", "first", "2026-08-08")

    print("4. dedup is per-section, not per-note")
    check("same session, same section -> no write",
          not ev.append_evidence(note, PUSH, "cccccccc", "dup", "2026-08-08"))
    check("same session, OTHER door -> writes",
          ev.append_evidence(note, PULL, "cccccccc", "same session other door", "2026-08-08"))

    print("5. trimming keeps newest, per section")
    for i in range(ev.MAX_EVIDENCE_LINES + 5):
        ev.append_evidence(note, PUSH, "s%06d" % i, "line %d" % i, "2026-08-09")
    text = note.read_text(encoding="utf-8")
    push_body = text.split(PUSH)[1].split("##")[0]
    check("push section capped at MAX", push_body.count("- ") == ev.MAX_EVIDENCE_LINES,
          "got %d" % push_body.count("- "))
    check("oldest push line dropped", "· first" not in push_body)
    check("newest push line kept", "line %d" % (ev.MAX_EVIDENCE_LINES + 4) in push_body)
    check("pull section untouched by push trimming", text.split(PULL)[1].count("- ") == 1)

    print("6. the two marker patterns do not cross")
    found = ev.SMRITI_USED_PATTERN.findall("[SMRITI-USED] my-kebab-note — because the body answered")
    check("smriti captures name+why", found == [("my-kebab-note", "because the body answered")],
          str(found))
    check("kebab name not split on its own hyphen", bool(found) and found[0][0] == "my-kebab-note")
    check("push pattern ignores a smriti marker", not ev.USED_PATTERN.findall("[SMRITI-USED] n — w"))
    check("smriti pattern ignores a push marker",
          not ev.SMRITI_USED_PATTERN.findall("[RECALL-USED] n — w"))

    print("7. the placeholder inside the injected instruction is not logged")
    check("empty fired-names set logs nothing",
          not ev.read_used_with_why(note, set(), ev.SMRITI_USED_PATTERN))

    print("8. the pull door follows the FIRE's corpus, not the project's lens")
    home = Path(tempfile.mkdtemp(prefix="ev_corpus_"))
    recall = home / "hooks" / "recall"
    recall.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    (recall / ev.SMRITI_FIRES_FILE).write_text("\n".join(json.dumps(r) for r in (
        {"ts": now.isoformat(), "name": "second-note", "corpus": "second"},
        {"ts": now.isoformat(), "name": "some-note", "corpus": ""})) + "\n", encoding="utf-8")
    fires = ev.read_smriti_fires(recall, (now.replace(microsecond=0), now))
    check("a fire carries the corpus that answered it", fires.get("second-note") == "second", repr(fires))
    check("a default-corpus fire keeps an empty corpus", fires.get("some-note") == "")
    with mock.patch.dict(os.environ, {"CLAUDE_WIKI_PATH": str(home)}):
        check("the default corpus also answers to the name vault_search writes",
              ev.corpus_paths("code", home) == ev.corpus_paths("", home) != (None, None))


def red_half() -> List[str]:
    """Re-install the pre-fix reader and return which checks GO RED against it. Empty means the
    gate cannot detect the defect it was written for, which is a failure of the gate itself."""
    def unbounded_split(lines, header):
        for i, line in enumerate(lines):
            if line.strip() == header:
                return lines[:i], [l for l in lines[i + 1:] if l.strip().startswith("- ")], []
        return lines, [], []

    original = ev.split_evidence_section
    ev.split_evidence_section = unbounded_split
    quiet = QuietChecker()
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            stdout, sys.stdout = sys.stdout, sink
            try:
                two_section_checks(quiet)
            finally:
                sys.stdout = stdout
    finally:
        ev.split_evidence_section = original
    return quiet.failures


def main() -> int:
    check = Checker()
    two_section_checks(check)
    remaining_checks(check)

    print()
    print("RED HALF — the same checks against the pre-fix reader (their failure is the pass)")
    caught = red_half()
    print("  %-58s %s" % ("checks detect the old unbounded-read defect",
                          "OK — %d went red" % len(caught) if caught
                          else "FAIL — this gate is rigged, it cannot fail"))
    for name in caught:
        print("       caught: " + name)
    if not caught:
        check.failures.append("red half: checks pass against the known-broken implementation")

    print()
    print("FAILURES: %d" % len(check.failures))
    for name in check.failures:
        print("  - " + name)
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(main())
