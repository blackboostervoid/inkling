# @module: procedural_feedback
# @role: Stop-hook - fold [PROCEDURAL-USED] markers + scanner fires into per-project procedural_stats.json (Layer 6 confidence/decay)
# @deps: (stdlib only - zero third-party, runs on Stop across machines via py)
# @used-by: Stop hook in settings.json
# @doc: RECIPE_FORMAT.md#lifecycle
"""
The procedural feedback ledger (Layer 6 confidence/decay). On Stop, reads which recipes
the scanner FIRED this session (<cwd>/.claude/procedural_fires.jsonl) and which the agent
marked USED ([PROCEDURAL-USED] <slug> in the transcript), then folds both into
<cwd>/.claude/procedural_stats.json: reuse-strengthens, disuse-decays.

Two design guards:
  - Idempotent. Evidence is deduped per session (fired_sessions / used_sessions are sets),
    so running on every Stop of a session never double-counts.
  - Observe-only. Confidence is surfaced to the human via `/procedural stats`; it is NEVER
    fed back into scanner ranking (same stance as recall: observe first, never auto-tune).

A USED marker is credited only if that recipe also FIRED this session (intersection guard),
which makes the literal "[PROCEDURAL-USED] <name>" placeholder in the injected instruction a
non-issue. Fast no-op when the project has no procedural_fires.jsonl (the common case), so
the Stop path stays cheap. Zero third-party deps. Invoke via `py`.
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

    Twin of the helper in recall_feedback.py, and the same reason: this is a Stop hook, a session
    running parallel subagents fires several at once against ONE shared ledger, and a plain
    write_text() opens with truncate -- so a concurrent pair can collide mid-truncation (observed
    as `OSError: [Errno 22] Invalid argument`) and a crash inside that window truncates the ledger
    rather than merely failing to update it. The PID in the temp name matters: a fixed temp name
    only relocates the race. Pattern copied from save_index() in semantic.py.

    Deliberately NOT solved here: read-modify-write is still unserialized, so a concurrent pair can
    lose one side's increment. Losing an increment is noise; losing the ledger is not.

    Temp name is unique per CALL (pid + counter), not per process — a probe showed a pid-only name
    collides between two callers inside one process and fails with WinError 32.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{next(_tmp_seq)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        # Windows: two writers replacing the SAME destination concurrently collide (WinError 5)
        # even though each replace is individually atomic. Bounded retry, constants copied from
        # _atomic_replace_retry in meta/stages/tts_stage.py -- the shipped answer to this exact
        # OS behaviour.
        for attempt in range(REPLACE_RETRIES):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(REPLACE_BACKOFF_S)
    except OSError as error:
        # Observe-only ledger: a lost increment must not end the human's turn with a traceback.
        print(f"[WARNING] procedural_feedback: could not update {path.name} ({error}); "
              "this turn's procedural stats were not recorded", file=sys.stderr)
    finally:
        try:
            tmp.unlink(missing_ok=True)      # no-op after a successful replace
        except OSError:
            pass

USED_PATTERN = re.compile(r"\[PROCEDURAL-USED\]\s+(\S+)")
HALF_LIFE_DAYS = 30.0
FIRE_RETENTION_DAYS = 7.0


# ============================================================
# READ SIDES (fires this session + used markers in transcript)
# ============================================================
def read_session_fires(fires_path: Path, session_id: str) -> tuple:
    """Return (names_fired_this_session, all_lines) from the per-project fires log."""
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
        if rec.get("session") == session_id and rec.get("name"):
            fired.add(rec["name"])
    return fired, lines


def read_used_markers(transcript_path: Path, fired_names: set) -> set:
    """Slugs the agent marked [PROCEDURAL-USED], kept only if they actually fired."""
    if not fired_names or not transcript_path.is_file():
        return set()
    text = transcript_path.read_text(encoding="utf-8", errors="replace")
    used = set()
    for token in USED_PATTERN.findall(text):
        slug = token.strip().strip(".,:;-")
        if slug in fired_names:  # intersection guard kills the placeholder/instruction
            used.add(slug)
    return used


# ============================================================
# CONFIDENCE (reuse-strengthens, disuse-decays)
# ============================================================
def add_session(stats: dict, slug: str, field: str, session_id: str, now_iso: str, ts_field: str) -> None:
    entry = stats.setdefault(slug, {})
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
# FIRES LOG VACUUM (bound growth across sessions)
# ============================================================
def prune_fires(lines: list, now: datetime) -> list:
    kept = []
    for raw in lines:
        try:
            ts = datetime.fromisoformat(json.loads(raw).get("ts", ""))
        except (json.JSONDecodeError, ValueError):
            kept.append(raw)  # unparseable -> keep, never silently drop
            continue
        if (now - ts).total_seconds() / 86400.0 <= FIRE_RETENTION_DAYS:
            kept.append(raw)
    return kept


# ============================================================
# ENTRY POINT
# ============================================================
def main() -> int:
    try:
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    cwd = hook.get("cwd", "")
    session_id = hook.get("session_id", "")
    transcript_path = hook.get("transcript_path", "")
    if not cwd or not session_id:
        return 0

    claude_dir = Path(cwd) / ".claude"
    fires_path = claude_dir / "procedural_fires.jsonl"
    if not fires_path.is_file():
        return 0  # fast no-op: this project saw no procedural fires

    fired, fire_lines = read_session_fires(fires_path, session_id)
    used = read_used_markers(Path(transcript_path), fired)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    stats_path = claude_dir / "procedural_stats.json"
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.is_file() else {}
    except json.JSONDecodeError:
        stats = {}

    for slug in fired:
        add_session(stats, slug, "fired_sessions", session_id, now_iso, "last_fired")
    for slug in used:
        add_session(stats, slug, "used_sessions", session_id, now_iso, "last_used")
    for slug in fired:
        stats[slug]["confidence"] = recompute_confidence(stats[slug], now)

    write_atomic(stats_path, json.dumps(stats, ensure_ascii=False, indent=2))

    kept = prune_fires(fire_lines, now)
    if len(kept) != len(fire_lines):
        write_atomic(fires_path, "\n".join(kept) + ("\n" if kept else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
