# @module: capture_inbox
# @role: Stop-hook harvester + SessionStart nudge — the safety net under the live [CAPTURE] hook. At Stop, re-scans the session's USER messages for capture signals and STAGES missed candidates into <cwd>/.claude/capture_inbox.md (never real notes). At SessionStart, nudges if candidates await review.
# @deps: (stdlib only — zero third-party; shares capture_signals.json with capture_scanner)
# @used-by: Stop + SessionStart hooks in settings.json
# @doc: twin of the live capture_scanner. Live hook = catch in the moment (context hot); this = end-of-session sweep of what the moment missed. Wiki/memory-side analog of procedural_candidates.md (cheap wide capture -> human promotion wall via /capture-review).
"""
capture_inbox.py — staged-candidate safety net for capture.

The live capture_scanner hints once per category per session; a hint can be missed (agent
mid-task, or the once-per-session dedup already spent the category). This closes that leak WITHOUT
auto-writing notes: at Stop it re-scans the session transcript's USER messages for the same signals
(capture_signals.json), pulls the sentence around each match, and appends any NEW ones to a
project-local `capture_inbox.md`. A human promotes the real ones later via /capture-review; the rest
are dropped. Regex/keyword can detect a capture-worthy moment but cannot JUDGE what makes a good
note — so this stages, never files. That judgment split is the whole point.

Two modes (mirrors session_manager's multi-mode pattern):
  --mode=harvest   on Stop         — scan transcript, append new staged candidates (deduped)
  --mode=nudge     on SessionStart — if candidates await review, emit [CAPTURE-INBOX] N pending

Guards: idempotent (quote-deduped, so re-running Stop never double-stages); staging-only (writes a
review queue, never a wiki/memory note); best-effort + always exit 0. Zero third-party deps. `py`.

Two failure modes found in the field (2026-07-27) and closed here:

1. **Dedup broke on any quote containing a `"`.** The read-back parser scanned the inbox for
   `"..."` spans instead of parsing candidate LINES, so a staged quote with inner quotes was
   re-read as several fragments, none of which matched the stored quote — and the candidate was
   re-staged on every single Stop. One session appeared four times, identically. The dedup key
   now comes from the candidate line's own grammar (`- [cat] "<quote>"`), which cannot be split
   by punctuation inside the quote.
2. **Self-reference.** Discussing the capture mechanism stages the capture mechanism's own spec:
   the scanner cannot tell a marker word USED from a marker word NAMED, so a signals table listing
   `PATTERN: "always", "every time"` matched on `always`. `is_spec_noise` rejects the shapes that
   only ever occur in a spec — fenced code, table rows, `CATEGORY:` labels, trigger-phrase lists.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

INBOX_NAME = "capture_inbox.md"
INBOX_HEADER = "# Capture Inbox — staged candidates\n\n> Review with `/capture-review`: promote the durable ones to their home (wiki / memory / rejected-list), drop the rest. Regex staged these; judgment keeps or kills them.\n"
MAX_QUOTE_CHARS = 240
MIN_QUOTE_CHARS = 25
# A staged candidate line, RESOLVED OR NOT. Used for dedup, which must see every quote the file
# has ever carried -- a resolved line still has to suppress a re-stage of the same quote.
# The quote is captured greedily to the LAST quote on the line: a staged quote may itself contain
# `"` characters, and a lazy or character-class match would split it and break dedup.
#
# `<!--resolved-->` is how /capture-review marks a line it has judged. The comment that used to
# stand here said "Promoted/dropped lines are removed entirely by /capture-review, so every
# remaining bullet is pending by definition" -- a WHY-line no gate reads, and it was false against
# what reviews actually do. Measured 2026-08-23 on a live project: every one of the seven lines
# inbox had a written disposition, four separate reviews had recorded that they keep resolved lines
# ON PURPOSE (the record of what was dropped is the point, and the quotes still feed dedup), and
# the nudge announced "7 staged candidates await review" at every session start for weeks. Three
# consecutive reviews wrote down that the counter counts records rather than work, and the fourth
# stopped writing it down and fixed it. Deleting the line still works and still reads as zero
# pending -- this only makes the OTHER practice countable.
_CANDIDATE_RE = re.compile(r'^\s*-\s*\[[a-z]+\]\s+"(.*)"(?:\s*<!--resolved-->)?\s*$', re.MULTILINE)
# The same line, UNRESOLVED only. This is what "pending" means, and it is a different question
# from dedup: one asks what is left to do, the other asks what has ever been seen.
_PENDING_RE = re.compile(r'^\s*-\s*\[[a-z]+\]\s+"(.*)"\s*$', re.MULTILINE)
_FENCE_RE = re.compile(r'^\s*(?:```|~~~)')
# A capture category NAMED rather than used — the shape a signals spec has, never natural speech.
_SPEC_LABEL_RE = re.compile(r'(?:^|[\s*|(])(DECISION|GOTCHA|PATTERN|REJECTED|CAPTURE|SIGNAL)S?\s*[:|]')
_QUOTED_ITEM_RE = re.compile(r'"[^"]{2,40}"')


# ============================================================
# CONFIG (shared with the live capture_scanner)
# ============================================================
def config_dir() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else Path.home() / ".claude"


def load_signals(cfg_dir: Path) -> List[Dict]:
    path = cfg_dir / "hooks" / "capture" / "capture_signals.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cats = data.get("categories", [])
    # Precompile one Unicode-boundary pattern per category. Python \w is Unicode by default, so
    # (?<!\w)...(?!\w) gives correct boundaries for Cyrillic (no hand-rolled runes needed here).
    compiled = []
    for cat in cats:
        pats = [re.escape(p.lower()) for p in cat.get("patterns", []) if p]
        if not pats:
            continue
        rx = re.compile(r"(?<!\w)(?:" + "|".join(pats) + r")(?!\w)", re.IGNORECASE)
        compiled.append({"id": cat.get("id", "?"), "rx": rx})
    return compiled


# ============================================================
# TRANSCRIPT — user messages only (parity with the live hook's input)
# ============================================================
def user_messages(transcript_path: Path) -> List[str]:
    out: List[str] = []
    try:
        raw = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or '"user"' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "user" or (rec.get("isMeta") is True):
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    out.append(b["text"])
    return out


def sentence_around(text: str, span: Tuple[int, int]) -> str:
    """The readable snippet holding the match: the line it sits on, trimmed to MAX_QUOTE_CHARS."""
    start, end = span
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    quote = text[line_start:line_end].strip()
    quote = re.sub(r"\s+", " ", quote)
    if len(quote) > MAX_QUOTE_CHARS:
        quote = quote[:MAX_QUOTE_CHARS].rstrip() + "…"
    return quote


def norm(quote: str) -> str:
    return re.sub(r"\s+", " ", quote.lower()).strip()


def fenced_lines(text: str) -> set:
    """0-based indices of lines inside a ``` / ~~~ block — quoted material, not the user speaking."""
    blocked, inside = set(), False
    for i, line in enumerate(text.splitlines()):
        if _FENCE_RE.match(line):
            inside = not inside
            blocked.add(i)
        elif inside:
            blocked.add(i)
    return blocked


def is_spec_noise(quote: str) -> bool:
    """True when the line describes capture signals rather than exhibiting one.

    Discussing this very mechanism used to stage its own signals table: a scanner sees the word
    `always` identically whether the user is stating a rule or listing which words mean a rule.
    Nothing here reads meaning — it rejects the four shapes that only ever occur in a spec.
    """
    stripped = quote.lstrip()
    if len(quote) < MIN_QUOTE_CHARS:
        return True
    if stripped.startswith(("|", "#", ">")) or quote.count("|") >= 2:
        return True
    if _SPEC_LABEL_RE.search(quote):
        return True
    return len(_QUOTED_ITEM_RE.findall(quote)) >= 3


def scan(messages: List[str], signals: List[Dict]) -> List[Tuple[str, str]]:
    """Return (category_id, quote) for the first USABLE match per (message, category).

    Scans every match rather than only the first: when a message both quotes the spec and states a
    real decision, taking the first hit would stage the quotation and lose the decision.
    """
    seen = set()
    found: List[Tuple[str, str]] = []
    for msg in messages:
        blocked = fenced_lines(msg)
        for cat in signals:
            for m in cat["rx"].finditer(msg):
                if msg.count("\n", 0, m.start()) in blocked:
                    continue
                quote = sentence_around(msg, m.span())
                if is_spec_noise(quote):
                    continue
                key = (cat["id"], norm(quote))
                if key not in seen:
                    seen.add(key)
                    found.append((cat["id"], quote))
                break
    return found


# ============================================================
# INBOX (project-local staging file next to HANDOFF / procedural_candidates)
# ============================================================
def inbox_path(cwd: str) -> Path:
    return Path(cwd) / ".claude" / INBOX_NAME


def _inbox_text(inbox: Path) -> str:
    try:
        return inbox.read_text(encoding="utf-8")
    except OSError:
        return ""


def existing_quotes(inbox: Path) -> set:
    """Dedup keys, parsed from candidate LINES.

    Reading the file for bare `"..."` spans instead is what broke dedup: a quote carrying its own
    quotes was read back as fragments that matched nothing, so it re-staged on every Stop.
    """
    return {norm(q) for q in _CANDIDATE_RE.findall(_inbox_text(inbox))}


def pending_count(inbox: Path) -> int:
    """Candidate lines still awaiting a disposition — NOT every candidate line ever staged.

    Deliberately a different regex from `existing_quotes`: dedup must see resolved lines, the
    nudge must not. One regex answering both questions is what made the nudge announce a number
    that could only grow.
    """
    return len(_PENDING_RE.findall(_inbox_text(inbox)))


def harvest(session_id: str, transcript_path: str, cwd: str, cfg_dir: Path) -> int:
    signals = load_signals(cfg_dir)
    if not signals or not transcript_path or not cwd:
        return 0
    msgs = user_messages(Path(transcript_path))
    if not msgs:
        return 0

    inbox = inbox_path(cwd)
    already = existing_quotes(inbox)
    fresh = [(cid, q) for cid, q in scan(msgs, signals) if norm(q) not in already]
    if not fresh:
        return 0

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    block = [f"\n## {date_str} · session {session_id[:8]}"]
    for cid, quote in fresh:
        block.append(f'- [{cid}] "{quote}"')

    inbox.parent.mkdir(parents=True, exist_ok=True)
    header = "" if inbox.exists() else INBOX_HEADER
    try:
        with inbox.open("a", encoding="utf-8") as f:
            if header:
                f.write(header)
            f.write("\n".join(block) + "\n")
    except OSError:
        return 0
    return len(fresh)


# ============================================================
# CONTEXT THRESHOLD — the third arm
# ============================================================
# Capture fires on phrase signals during the turn and sweeps the session at Stop.
# Both can miss the case where a long session simply runs out of room: what was
# learned is still in context, and compaction is about to trade it for space.
# This arm watches the fill level and says so ONCE per session.
#
# Windows are not exposed anywhere the hook can read - not in the transcript, not in
# the hook input - so they are resolved from the model id by substring. The "[1m]"
# marker is first and is the durable rule: a model that announces its own window
# needs no table entry. That rule exists because a model id carrying the suffix once
# fell through to the 200k default and produced a "75% full" warning at 16% of the
# real window. A wrong number is worse than no number: prose gets judged, a figure
# gets used. When the window had to be assumed, the message says so instead of
# printing a confident percentage.

DEFAULT_CONTEXT_WINDOW = 200_000
DEFAULT_FILL_THRESHOLD = 0.80
# Adding a model is adding a line here; no logic changes. Order matters only in that the
# self-declaring marker is checked first. Entries at the standard window are listed
# EXPLICITLY rather than left to the default, so "assumed" means genuinely unrecognised
# instead of merely unlisted - otherwise the honest-mode message would be the common case
# and the number, which is the useful part, would almost never appear.
MODEL_WINDOWS = (
    ("[1m]", 1_000_000),  # self-declaring suffix, covers every future 1M variant
    ("opus-5", 1_000_000),
    ("opus-4-8", 1_000_000),
    ("fable-5", 1_000_000),
    ("mythos-5", 1_000_000),
    ("sonnet-5", 200_000),
    ("haiku-4-5", 200_000),
    ("sonnet-4", 200_000),
    ("opus-4", 200_000),
    ("haiku-4", 200_000),
)


def fill_threshold() -> float:
    raw = os.environ.get("INKLING_CAPTURE_THRESHOLD", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_FILL_THRESHOLD
    return value if 0.0 < value < 1.0 else DEFAULT_FILL_THRESHOLD


def context_window(model_id: str) -> Tuple[int, bool]:
    """Return (window, assumed). `assumed` is True when the model id told us nothing
    and the default had to stand in - the caller must not print a percentage then."""
    override = os.environ.get("CLAUDE_CONTEXT_LIMIT", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override), False
    lowered = model_id.lower()
    for marker, window in MODEL_WINDOWS:
        if marker in lowered:
            return window, False
    return DEFAULT_CONTEXT_WINDOW, True


def latest_context_use(transcript_path: Path) -> Tuple[int, str]:
    """Cumulative input tokens and model id from the newest assistant turn that reports
    usage. Cache reads count: they occupy the window exactly like fresh input does."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, ""
    for line in reversed(lines):
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = rec.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            continue
        total = (int(usage.get("input_tokens", 0) or 0)
                 + int(usage.get("cache_read_input_tokens", 0) or 0)
                 + int(usage.get("cache_creation_input_tokens", 0) or 0))
        if total > 0:
            return total, str(message.get("model", ""))
    return 0, ""


def context_cooldown(session_id: str, cfg_dir: Path) -> Path:
    return cfg_dir / "hooks" / "capture" / f"capture_ctx_{session_id or 'unknown'}.json"


def already_warned(marker: Path) -> bool:
    return marker.is_file()


def mark_warned(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"warned": datetime.now(timezone.utc).isoformat()}),
                          encoding="utf-8")
    except OSError:
        pass  # a cooldown we could not write means one extra reminder, never a crash


def context_message(used: int, window: int, assumed: bool, pending: int) -> str:
    """A reading with no decision rule is decoration. Lead with what to do."""
    if assumed:
        reading = (f"Context holds about {used // 1000}k tokens. The model id did not declare a "
                   f"window, so this is measured against an assumed {window // 1000}k - treat the "
                   f"level as a hint, not a measurement.")
    else:
        reading = (f"Context is about {round(100 * used / window)}% full "
                   f"({used // 1000}k of {window // 1000}k).")

    staged = (f" {pending} candidate(s) are already staged in .claude/{INBOX_NAME}."
              if pending else "")

    return (
        f"[CAPTURE-DUE] {reading} This is the point where something learned in this session "
        f"gets traded for space rather than remembered.{staged}\n"
        "What to do, in this order:\n"
        "1. Finish the user's current request first. Do not break off mid-step for bookkeeping.\n"
        "2. Then sweep THIS session for what would cost real time to re-derive: a decision and "
        "the reason behind it, a gotcha with the symptom that gives it away, an approach tried "
        "and rejected. Skip anything that only matters until this task ends.\n"
        "3. Offer the shortlist to the user, or run /capture-review if candidates are staged. "
        "Nothing becomes a note without a human saying yes.\n"
        "If nothing durable happened, say so in one line and move on. An empty sweep is a valid "
        "result; inventing a note to look thorough is the failure this signal exists to prevent."
    )


# ============================================================
# ENTRY POINT
# ============================================================
def emit(event: str, ctx: str) -> None:
    # Hook stdout is read as UTF-8; Windows Python defaults stdout to the locale codepage, which
    # would mangle any non-ASCII in additionalContext. Force UTF-8 so the emitted bytes match.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}},
                     ensure_ascii=False))


def main() -> int:
    mode = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]

    try:
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        print("{}")
        return 0

    cwd = hook.get("cwd", "")
    cfg_dir = config_dir()

    if mode == "nudge":
        lines = []
        if cwd:
            n = pending_count(inbox_path(cwd))
            if n > 0:
                lines.append(
                    f"[CAPTURE-INBOX] {n} staged capture candidate(s) await review in "
                    f".claude/{INBOX_NAME}. Run /capture-review when convenient to promote the "
                    f"durable ones or drop the rest. Not urgent -- finish the user's task first.")
        # The observation queue rides the same nudge. Two queues, one signal: they are emptied by
        # different skills but at the same moment -- the start of a session, before new work
        # buries them.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import observe
            observed = observe.nudge_line()
            if observed:
                lines.append(observed)
        except (ImportError, OSError, ValueError):
            pass  # a missing nudge is a quiet queue, never a broken session start
        if lines:
            emit("SessionStart", "\n\n".join(lines))
            return 0
        print("{}")
        return 0

    if mode == "context":
        session_id = hook.get("session_id", "")
        transcript = hook.get("transcript_path", "")
        marker = context_cooldown(session_id, cfg_dir)
        if not transcript or already_warned(marker):
            print("{}")
            return 0
        used, model_id = latest_context_use(Path(transcript))
        window, assumed = context_window(model_id)
        if used and used / window >= fill_threshold():
            mark_warned(marker)
            emit("UserPromptSubmit",
                 context_message(used, window, assumed, pending_count(inbox_path(cwd)) if cwd else 0))
            return 0
        print("{}")
        return 0

    # default: harvest (Stop)
    session_id = hook.get("session_id", "")
    transcript = hook.get("transcript_path", "")
    n = harvest(session_id, transcript, cwd, cfg_dir)
    if n:
        print(f"[capture_inbox] staged {n} new candidate(s)", file=sys.stderr)

    # The observation sweep rides in THIS process rather than as a ninth Stop command. The two
    # read the same transcript for opposite halves — capture reads what the USER said, observe
    # reads what the agent and its subagents produced — and a separate hook would pay a fresh
    # interpreter start (~300 ms on every turn end) to walk a file already open here.
    # Its own module, because the two change for different reasons: capture when the signal
    # phrases change, observe when agent types or markers do.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import observe
        staged = observe.harvest(session_id, transcript, cwd)
        if staged:
            print(f"[observe] staged {staged} observation(s) for /compile", file=sys.stderr)
    except (ImportError, OSError, ValueError) as error:
        # Bookkeeping must never break the end of a turn. One line, never silent, never fatal.
        print(f"[WARNING] observe: sweep skipped ({error})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
