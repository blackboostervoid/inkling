# @module: gardener
# @role: Read-only report on what the wiki evidence ledger is doing to ranking — which notes the scanner now lifts, which it damps, how many slots the damped set has been eating, and which notes a human might want to mute or repair by hand. Changes nothing.
# @deps: (stdlib only; reads recall_stats.json + index_compiled*.json written by recall_feedback.py / compile_wiki_index.py)
# @used-by: run by hand; the human-facing half of the evidence loop closed in main.go
# @doc: the inspection surface for evidenceMultiplier() in recall_scanner's main.go — same formula, printed instead of applied
"""
gardener.py — what the ledger is doing to the wiki rail, in plain numbers.

The scanner now multiplies a note's score by its own track record (main.go, evidenceMultiplier).
That is a silent change to ranking, and a silent ranking rule is one nobody can argue with. This
prints it: the same formula, applied to the same ledger, shown rather than used.

    python3 gardener.py                # the full report
    python3 gardener.py --mute-lines   # ready-to-paste lens.yaml suppress lines for the worst
    python3 gardener.py --corpus personal   # a namespace from corpora.json

It writes nothing, ever. Muting stays a human edit to lens.yaml, because a hard mute is a
judgement ("this note should not exist in the rail") while the multiplier is only arithmetic.

Two doors are reported apart on purpose. A note used through the MCP body search but never
through the pushed [RECALL] block is not a weak note — it is a note whose BODY works and whose
SUMMARY lies, and the repair is to rewrite the summary rather than to mute anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

# Kept identical to the Go constants in main.go. They are duplicated rather than shared because
# a hook binary and a report script have no common runtime — so the check below exists to make a
# drift loud instead of letting this report quietly describe a formula that is no longer live.
LIFT_GAIN = 0.5
DAMP_SCALE = 30.0
GO_SOURCE = "main.go"


def config_dir() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else Path.home() / ".claude"


def recall_dir() -> Path:
    return config_dir() / "hooks" / "recall"


def verify_constants_match_go() -> List[str]:
    """Read the live constants out of main.go and complain if this script has drifted from them.

    A report that describes a formula the scanner no longer runs is worse than no report: it is
    a confident wrong answer. Cheap to check, so it is checked on every run.
    """
    problems: List[str] = []
    try:
        src = (recall_dir() / GO_SOURCE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [f"could not read {GO_SOURCE} — constants unverified"]
    for name, mine in (("evidenceLiftGain", LIFT_GAIN), ("evidenceDampScale", DAMP_SCALE)):
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(name):
                try:
                    theirs = float(stripped.split("=", 1)[1].split("//")[0].strip())
                except (IndexError, ValueError):
                    continue
                if theirs != mine:
                    problems.append(f"{name}: this report uses {mine}, {GO_SOURCE} uses {theirs}")
                break
        else:
            problems.append(f"{name}: not found in {GO_SOURCE}")
    return problems


def corpus_names(corpus: str) -> set:
    """Note names belonging to this corpus, so a shared ledger is reported one vault at a time."""
    fname = "index_compiled.json" if corpus in ("", "code", "default") \
        else f"index_compiled_{corpus}.json"
    try:
        data = json.loads((recall_dir() / fname).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {e.get("name") for e in data if isinstance(e, dict) and e.get("name")}


def load_stats() -> Dict[str, dict]:
    try:
        return json.loads((recall_dir() / "recall_stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ============================================================
# THE FORMULA — mirrors evidenceMultiplier() in main.go
# ============================================================
def counts(entry: dict) -> Tuple[int, int, int, int]:
    return (len(entry.get("fired_sessions", [])), len(entry.get("used_sessions", [])),
            len(entry.get("smriti_fired_sessions", [])), len(entry.get("smriti_used_sessions", [])))


def multiplier(entry: dict) -> float:
    pf, pu, sf, su = counts(entry)
    if pu + su > 0:
        conf = max(entry.get("confidence", 0.0) or 0.0,
                   entry.get("smriti_confidence", 0.0) or 0.0)
        return 1.0 + LIFT_GAIN * conf
    fires = pf + sf
    return 1.0 if fires == 0 else 1.0 / (1.0 + fires / DAMP_SCALE)


def days_since(entry: dict) -> str:
    last = entry.get("last_used") or entry.get("last_smriti_used") or \
        entry.get("last_fired") or entry.get("last_smriti_fired")
    if not last:
        return "  -"
    try:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
    except ValueError:
        return "  -"
    return f"{days:3}"


# ============================================================
# REPORT
# ============================================================
def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def report(corpus: str) -> int:
    drift = verify_constants_match_go()
    if drift:
        print("[WARNING] this report may not describe the live formula:")
        for d in drift:
            print(f"  - {d}")

    names = corpus_names(corpus)
    stats = load_stats()
    rows = [(n, e) for n, e in stats.items() if not names or n in names]
    if not rows:
        print(f"[gardener] no ledger entries for the '{corpus}' corpus yet.")
        return 0

    total_fires = sum(sum(counts(e)[::2]) for _, e in rows)
    total_uses = sum(sum(counts(e)[1::2]) for _, e in rows)
    rate = 100 * total_uses / total_fires if total_fires else 0.0

    section(f"WIKI EVIDENCE — corpus '{corpus}'")
    print(f"  {len(rows)} notes · {total_fires} firings · {total_uses} applications "
          f"· {rate:.1f}% applied")
    print(f"  formula: used -> 1.0+{LIFT_GAIN}*confidence   never used -> 1/(1+fires/{DAMP_SCALE:.0f})"
          f"   no history -> 1.0")

    lifted = sorted(((multiplier(e), n, e) for n, e in rows if sum(counts(e)[1::2]) > 0),
                    reverse=True)
    damped = sorted(((multiplier(e), n, e) for n, e in rows
                     if sum(counts(e)[1::2]) == 0 and sum(counts(e)[::2]) > 0))
    neutral = [n for n, e in rows if sum(counts(e)[::2]) == 0]

    section(f"LIFTED — {len(lifted)} note(s) have earned their place")
    print(f"  {'mult':>5} {'push':>9} {'pull':>9} {'age':>4}  note")
    for m, n, e in lifted:
        pf, pu, sf, su = counts(e)
        print(f"  {m:5.2f} {f'{pu}/{pf}':>9} {f'{su}/{sf}':>9} {days_since(e)}d  {n}")

    section(f"DAMPED — {len(damped)} note(s) shown repeatedly, never applied")
    eaten = sum(sum(counts(e)[::2]) for _, _, e in damped)
    print(f"  they have consumed {eaten} of {total_fires} firings "
          f"({100 * eaten / total_fires:.0f}% of every slot ever spent)\n")
    print(f"  {'mult':>5} {'fires':>6} {'age':>4}  note")
    for m, n, e in damped[:25]:
        print(f"  {m:5.2f} {sum(counts(e)[::2]):6} {days_since(e)}d  {n}")
    if len(damped) > 25:
        print(f"  ... and {len(damped) - 25} more")

    # The diagnosis the two counters exist for.
    mislabelled = [(n, e) for n, e in rows
                   if len(e.get("smriti_used_sessions", [])) > 0
                   and len(e.get("used_sessions", [])) == 0
                   and len(e.get("fired_sessions", [])) >= 5]
    section("MISLABELLED — the body works, the summary does not")
    if mislabelled:
        print("  Applied through the MCP body search, never through the pushed block despite\n"
              "  firing there repeatedly. Do NOT mute these — rewrite the summary so the label\n"
              "  says what the note actually contains.\n")
        for n, e in mislabelled:
            pf, pu, sf, su = counts(e)
            print(f"    body {su}/{sf}   label {pu}/{pf}   {n}")
    else:
        print("  None yet. This list needs [SMRITI-USED] evidence to accumulate first —\n"
              "  the pull door only started being counted on 2026-08-06.")

    section("WHAT A HUMAN STILL DECIDES")
    print(f"  {len(neutral)} note(s) have no history at all and rank untouched. That is\n"
          "  deliberate: penalising an unproven note would keep it from ever being shown,\n"
          "  and a corpus that cannot promote a newcomer cannot learn.\n")
    print("  The multiplier only bends ranking. To remove a note from the rail outright, add\n"
          "  it to `suppress` in lens.yaml by hand — run with --mute-lines for the worst\n"
          "  offenders as paste-ready YAML.")
    return 0


def mute_lines(corpus: str, limit: int) -> int:
    names = corpus_names(corpus)
    stats = load_stats()
    worst = sorted(((sum(counts(e)[::2]), n) for n, e in stats.items()
                    if (not names or n in names) and sum(counts(e)[1::2]) == 0),
                   reverse=True)[:limit]
    if not worst:
        print("# nothing has fired often enough without landing to be worth muting")
        return 0
    print("# Paste into lens.yaml under `suppress:`. Muting is a judgement, not arithmetic —")
    print("# read each one and drop the lines you disagree with.")
    print("suppress:")
    for fires, n in worst:
        print(f"  - {n}    # {fires} firings, never applied")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Report what the wiki evidence ledger does to ranking.")
    ap.add_argument("--corpus", default="code")
    ap.add_argument("--mute-lines", action="store_true",
                    help="print paste-ready lens.yaml suppress lines instead of the report")
    ap.add_argument("-n", type=int, default=12, help="how many mute candidates to print")
    args = ap.parse_args()
    return mute_lines(args.corpus, args.n) if args.mute_lines else report(args.corpus)


if __name__ == "__main__":
    sys.exit(main())
