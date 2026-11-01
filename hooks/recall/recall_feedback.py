# @module: recall_feedback
# @role: Stop-hook - fold BOTH wiki doors into global recall_stats.json: the pushed [RECALL] rail (scanner fires + [RECALL-USED] markers) and the pulled MCP body search (smriti_fires.jsonl + [SMRITI-USED] markers). Separate counters per door, hit-rate decayed by recency; observe-only, never fed back into ranking.
# @deps: (stdlib only - zero third-party, runs on Stop across machines via py)
# @used-by: Stop hook in settings.json
# @doc: twin of procedural_feedback.py (wiki side; procedural is the per-project recipe side)
"""
The wiki-recall feedback ledger - the recall-side twin of procedural_feedback.py. On Stop,
reads which wiki notes the scanner FIRED this session (hooks/recall/fires_<session>.jsonl, one
file per session - the session id is the FILENAME, not a field) and which the agent marked
USED ([RECALL-USED] <name> in the transcript), then folds both into the GLOBAL
hooks/recall/recall_stats.json: reuse-strengthens, disuse-decays.

Why global (vs procedural's per-project stats): the wiki is one shared corpus, so a note's
hit-rate is aggregated across every project that recalled it.

Two design guards (identical stance to procedural):
  - Idempotent. Evidence is deduped per session (fired_sessions / used_sessions are sets),
    so running on every Stop of a session never double-counts.
  - Idempotent (above), and until 2026-08-06 also observe-only. That second guard is GONE and
    the reversal is deliberate, so the old wording is not left standing here to mislead: this
    ledger now DOES reach ranking. recall_scanner reads recall_stats.json and multiplies a note's
    score by its own record (evidenceMultiplier in main.go).

    What changed the decision: "observability, not autotune" assumed a human would read the
    numbers and tune a lens from them. Four months on, the ledger held 1357 firings against 31
    applications and the global lens was still empty — the loop was closed by nobody. An
    observation nobody acts on is not a safer design than an automatic one, only a quieter one.

    Two limits keep the reversal honest. Evidence ORDERS the candidate pool and never gates it,
    so a track record cannot veto a topical match (that failure was live for one afternoon and
    has a regression test). And a note with no history is neutral, never penalised, so a new
    note can still be shown, still acquire a record, and still displace an old one.
    `python3 hooks/recall/gardener.py` prints what the formula is doing to which notes.

A USED marker is credited only if that note also FIRED this session (intersection guard), which
makes the literal "[RECALL-USED] <name>" placeholder in the injected instruction a non-issue.
Fast no-op when NEITHER door left evidence. Zero third-party deps. Invoke via `py`.

TWO DOORS since 2026-08-06. The wiki is now reached two ways that read different halves of a
note, so it is counted two ways:

  push  [RECALL]  scanner, ranks name+tags+summary, fires_<session>.jsonl (session id = filename)
  pull  [SMRITI]  MCP vault_search, searches note BODIES, smriti_fires.jsonl (one rolling file,
                  attributed by transcript time window because nothing in that path knows a
                  session id)

Counting only the push door is what made this urgent: a note whose BODY answers questions while
its summary does not would read as 20 firings and 0 uses, and the first weight built on that
number would have demoted it. The counters stay separate on purpose — the gap between a note's
two confidences is a diagnosis, and summing them would erase it: strong on body and weak on
label means the content works and the summary lies.
"""

import itertools
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_tmp_seq = itertools.count()
REPLACE_RETRIES = 5
REPLACE_BACKOFF_S = 0.2


def write_atomic(path: Path, text: str) -> None:
    """Write via a per-process temp file + os.replace, which is atomic on Windows.

    Why: this hook is a Stop hook, and a session running parallel subagents fires several of them
    at once against ONE shared ledger. A plain write_text() opens with truncate, so a concurrent
    pair can collide mid-truncation -- observed as `OSError: [Errno 22] Invalid argument` killing
    the hook, and worse, a crash inside that window would leave the ledger truncated instead of
    merely unwritten. The temp name carries the PID because a FIXED temp name only moves the race
    one file over. Same shape as save_index() in semantic.py, which is where the pattern is from.

    Scope, stated honestly: this makes the WRITE atomic. It does NOT serialize read-modify-write --
    two hooks that both read, both update and both replace still let the later one win, so a single
    session's increment can be lost. That trade is deliberate: losing one increment is noise, losing
    a 125-entry ledger to a truncated file is not.

    The temp name is unique per CALL (pid + counter), not per process: pid alone is enough for the
    real deployment, where each hook is its own process, but a concurrency probe showed two callers
    inside one process collide on a pid-only name and fail with WinError 32. Per-call costs nothing
    and closes the class instead of the instance.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{next(_tmp_seq)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        # Windows: os.replace guarantees a READER sees old-or-new, but two writers replacing the
        # SAME destination at once still collide (WinError 5) -- observed in a probe. Bounded retry
        # is the shipped house answer to exactly this OS behaviour; see _atomic_replace_retry in
        # meta/stages/tts_stage.py, from which these constants are copied.
        for attempt in range(REPLACE_RETRIES):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(REPLACE_BACKOFF_S)
    except OSError as error:
        # This ledger is OBSERVE-ONLY (see the module docstring). A lost increment must not put a
        # traceback in the human's face at the end of every turn -- which is exactly the symptom
        # this fix exists to remove. Report one line, never silently, and never fatally.
        print(f"[WARNING] recall_feedback: could not update {path.name} ({error}); "
              "this turn's recall stats were not recorded", file=sys.stderr)
    finally:
        try:
            tmp.unlink(missing_ok=True)      # no-op after a successful replace
        except OSError:
            pass


USED_PATTERN = re.compile(r"\[RECALL-USED\]\s+(\S+)")
HALF_LIFE_DAYS = 30.0
FIRE_RETENTION_DAYS = 7.0

# ------------------------------------------------------------------
# The SECOND door
# ------------------------------------------------------------------
# Two rails now reach the wiki and they read different halves of a note: the scanner ranks on
# name/tags/summary and pushes; the MCP searches note BODIES and is pulled. Counting only the
# first is what made this urgent — a note whose body answers questions but whose label does not
# would show 20 firings and 0 uses, and any weight built on that would demote it first.
#
# The two counters are kept apart rather than summed, and the SPLIT is the useful part: high on
# the body and low on the label means the content works and the summary lies, which is a repair
# with a known address. Summing them would average that diagnosis away.
SMRITI_USED_PATTERN = re.compile(r"\[SMRITI-USED\]\s+(\S+)")
SMRITI_FIRES_FILE = "smriti_fires.jsonl"


# ============================================================
# READ SIDES (fires this session + used markers in transcript)
# ============================================================
def read_session_fires(fires_path: Path) -> tuple:
    """Return (names_fired_this_session, all_lines). The whole file IS this session."""
    if not fires_path.is_file():
        return set(), []
    fired, lines = set(), []
    for raw in fires_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        lines.append(raw)
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if rec.get("name"):
            fired.add(rec["name"])
    return fired, lines


def read_used_markers(transcript_path: Path, fired_names: set, pattern=USED_PATTERN) -> set:
    """Note names the agent marked used, kept only if they actually fired.

    The intersection guard does double duty: it kills the literal `<name>` placeholder that both
    rails print inside their own instruction, and it kills a hallucinated credit for a note the
    door never handed over. Both markers earn credit the same way, so they share this function."""
    if not fired_names or not transcript_path.is_file():
        return set()
    text = transcript_path.read_text(encoding="utf-8", errors="replace")
    used = set()
    for token in pattern.findall(text):
        name = token.strip().strip(".,:;")  # keep internal hyphens (kebab note names)
        if name in fired_names:  # intersection guard kills the placeholder/instruction
            used.add(name)
    return used


def session_window(transcript_path: Path) -> tuple:
    """(first, last) UTC timestamps of this session's transcript, or (None, None).

    The pull door cannot write a session id — the MCP server is never told one and the CLI has
    none — so its fires land in one rolling file and are attributed here by time. Every tool call
    is bracketed by transcript records, so a fire raised during this session necessarily falls
    inside this window, including one raised by a subagent (the parent's Agent call brackets it).

    Two sessions running at once will both claim a fire in the overlap. That inflates the
    denominator, which pushes confidence DOWN — the conservative direction, and the right one for
    a number that will eventually drive demotion."""
    if not transcript_path.is_file():
        return None, None
    first = last = None
    for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw or '"timestamp"' not in raw:
            continue
        try:
            stamp = json.loads(raw).get("timestamp")
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if first is None or when < first:
            first = when
        if last is None or when > last:
            last = when
    return first, last


def read_smriti_fires(recall_dir: Path, window: tuple) -> set:
    """Note names the pull door returned inside this session's window."""
    start, end = window
    path = recall_dir / SMRITI_FIRES_FILE
    if start is None or end is None or not path.is_file():
        return set()  # no window means no attribution: credit nothing rather than credit all
    fired = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
            when = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if rec.get("name") and start <= when <= end:
            fired.add(rec["name"])
    return fired


def vacuum_smriti_fires(recall_dir: Path, now: datetime) -> None:
    """Drop rolling-log lines past retention. Rewritten rather than deleted: unlike the push
    rail's per-session files, this one file holds every session, so aging it out is a filter."""
    path = recall_dir / SMRITI_FIRES_FILE
    if not path.is_file():
        return
    keep = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(str(json.loads(raw).get("ts", "")).replace("Z", "+00:00"))
        except (json.JSONDecodeError, ValueError, TypeError):
            keep.append(raw)  # unparseable -> keep, never silently drop
            continue
        if (now - when).total_seconds() / 86400.0 <= FIRE_RETENTION_DAYS:
            keep.append(raw)
    write_atomic(path, "\n".join(keep) + ("\n" if keep else ""))


# ============================================================
# CONFIDENCE (reuse-strengthens, disuse-decays)
# ============================================================
def add_session(stats: dict, name: str, field: str, session_id: str, now_iso: str, ts_field: str) -> None:
    entry = stats.setdefault(name, {})
    sessions = entry.setdefault(field, [])
    if session_id not in sessions:
        sessions.append(session_id)
    entry[ts_field] = now_iso


def confidence_from(entry: dict, now: datetime, fired_field: str, used_field: str,
                    last_used_field: str, last_fired_field: str) -> float:
    """Hit-rate decayed by recency. One formula, applied per door against that door's own
    counters — the doors are never mixed, because a note can be strong through one and dead
    through the other and that difference is the whole diagnostic value of keeping two."""
    fired = len(entry.get(fired_field, []))
    used = len(entry.get(used_field, []))
    hit = used / fired if fired else 0.0
    last = entry.get(last_used_field) or entry.get(last_fired_field)
    recency = 1.0
    if last:
        try:
            days = (now - datetime.fromisoformat(last)).total_seconds() / 86400.0
            recency = 0.5 ** (max(days, 0.0) / HALF_LIFE_DAYS)
        except ValueError:
            recency = 1.0
    return round(hit * recency, 3)


def recompute_confidence(entry: dict, now: datetime) -> float:
    return confidence_from(entry, now, "fired_sessions", "used_sessions",
                           "last_used", "last_fired")


def recompute_smriti_confidence(entry: dict, now: datetime) -> float:
    return confidence_from(entry, now, "smriti_fired_sessions", "smriti_used_sessions",
                           "last_smriti_used", "last_smriti_fired")


# ============================================================
# FIRES VACUUM (bound growth: delete whole per-session files past retention)
# ============================================================
def newest_ts(lines: list) -> datetime:
    """Newest parseable ts in a fires file, or None if none parse."""
    newest = None
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(json.loads(raw).get("ts", "").replace("Z", "+00:00"))
        except (json.JSONDecodeError, ValueError):
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest


def vacuum_old_fires(recall_dir: Path, now: datetime, current_path: Path) -> None:
    """Delete fires_*.jsonl files (except the current session's) whose newest ts aged out."""
    for fp in recall_dir.glob("fires_*.jsonl"):
        if fp == current_path:
            continue
        try:
            newest = newest_ts(fp.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        if newest is None:  # unparseable -> keep, never silently drop
            continue
        if (now - newest).total_seconds() / 86400.0 > FIRE_RETENTION_DAYS:
            try:
                fp.unlink()
            except OSError:
                continue


# ============================================================
# ENTRY POINT
# ============================================================
def main() -> int:
    try:
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    session_id = hook.get("session_id", "")
    transcript_path = hook.get("transcript_path", "")
    if not session_id:
        return 0

    recall_dir = Path(__file__).resolve().parent
    transcript = Path(transcript_path) if transcript_path else Path()
    fires_path = recall_dir / f"fires_{session_id}.jsonl"

    # Push door: one file per session, the filename IS the session.
    fired, _ = read_session_fires(fires_path)
    used = read_used_markers(transcript, fired, USED_PATTERN)

    # Pull door: one rolling file for every session, attributed by time window.
    smriti_fired = read_smriti_fires(recall_dir, session_window(transcript))
    smriti_used = read_used_markers(transcript, smriti_fired, SMRITI_USED_PATTERN)

    # The old fast no-op keyed on the push door's file alone, which would have skipped the whole
    # hook in any session where only the pull door was used — the precise case this change exists
    # to record. It now takes BOTH doors being silent to skip.
    if not fired and not smriti_fired:
        return 0

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    stats_path = recall_dir / "recall_stats.json"
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.is_file() else {}
    except json.JSONDecodeError:
        stats = {}

    for name in fired:
        add_session(stats, name, "fired_sessions", session_id, now_iso, "last_fired")
    for name in used:
        add_session(stats, name, "used_sessions", session_id, now_iso, "last_used")
    for name in fired:
        stats[name]["confidence"] = recompute_confidence(stats[name], now)

    for name in smriti_fired:
        add_session(stats, name, "smriti_fired_sessions", session_id, now_iso, "last_smriti_fired")
    for name in smriti_used:
        add_session(stats, name, "smriti_used_sessions", session_id, now_iso, "last_smriti_used")
    for name in smriti_fired:
        stats[name]["smriti_confidence"] = recompute_smriti_confidence(stats[name], now)

    write_atomic(stats_path, json.dumps(stats, ensure_ascii=False, indent=2))

    vacuum_old_fires(recall_dir, now, fires_path)
    vacuum_smriti_fires(recall_dir, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
