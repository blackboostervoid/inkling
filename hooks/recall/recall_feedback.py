# @module: recall_feedback
# @role: Stop-hook - fold [RECALL-USED] markers + scanner fires into global recall_stats.json (wiki recall confidence/decay)
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
  - Observe-only. Confidence is surfaced to the human (recall_stats.json -> lens tuning); it is
    NEVER fed back into scanner ranking. Same line session 3 drew: observability, not autotune.

A USED marker is credited only if that note also FIRED this session (intersection guard), which
makes the literal "[RECALL-USED] <name>" placeholder in the injected instruction a non-issue.
Fast no-op when this session wrote no fires file. Zero third-party deps. Invoke via `py`.
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


def read_used_markers(transcript_path: Path, fired_names: set) -> set:
    """Note names the agent marked [RECALL-USED], kept only if they actually fired."""
    if not fired_names or not transcript_path.is_file():
        return set()
    text = transcript_path.read_text(encoding="utf-8", errors="replace")
    used = set()
    for token in USED_PATTERN.findall(text):
        name = token.strip().strip(".,:;")  # keep internal hyphens (kebab note names)
        if name in fired_names:  # intersection guard kills the placeholder/instruction
            used.add(name)
    return used


# ============================================================
# CONFIDENCE (reuse-strengthens, disuse-decays)
# ============================================================
def add_session(stats: dict, name: str, field: str, session_id: str, now_iso: str, ts_field: str) -> None:
    entry = stats.setdefault(name, {})
    sessions = entry.setdefault(field, [])
    if session_id not in sessions:
        sessions.append(session_id)
    entry[ts_field] = now_iso


def recompute_confidence(entry: dict, now: datetime) -> float:
    fired = len(entry.get("fired_sessions", []))
    used = len(entry.get("used_sessions", []))
    hit = used / fired if fired else 0.0
    last = entry.get("last_used") or entry.get("last_fired")
    recency = 1.0
    if last:
        try:
            days = (now - datetime.fromisoformat(last)).total_seconds() / 86400.0
            recency = 0.5 ** (max(days, 0.0) / HALF_LIFE_DAYS)
        except ValueError:
            recency = 1.0
    return round(hit * recency, 3)


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
    fires_path = recall_dir / f"fires_{session_id}.jsonl"
    if not fires_path.is_file():
        return 0  # fast no-op: this session wrote no wiki fires

    fired, _ = read_session_fires(fires_path)
    used = read_used_markers(Path(transcript_path), fired)

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

    write_atomic(stats_path, json.dumps(stats, ensure_ascii=False, indent=2))

    vacuum_old_fires(recall_dir, now, fires_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
