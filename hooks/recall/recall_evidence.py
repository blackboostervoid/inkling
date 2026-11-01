# @module: recall_evidence
# @role: Stop-hook - harvest the discarded "why" context of [RECALL-USED] and [SMRITI-USED] markers into an inline evidence log inside each wiki note (both corpora, both doors, one section each)
# @deps: (stdlib only - zero third-party, runs on Stop across machines via py)
# @used-by: Stop hook in settings.json; gated by hooks/recall/test_recall_evidence.py
# @doc: qualitative twin of recall_feedback.py - that fold keeps the COUNT (recall_stats.json), this one keeps the CONTEXT (note-inline "## Applied" for the pushed rail, "## Found by search" for the pulled body search)
"""
recall_evidence.py — evidence-by-backlink for the wiki, reverse-harvested from obsidian-mind.

recall_feedback.py already answers "which notes are load-bearing and how often" by folding
[RECALL-USED] markers into recall_stats.json. But its regex captures only the NAME and throws
away the "— <why>" clause the agent wrote each time. That clause is the qualitative record of
WHY the note helped in this specific case — the shape of usefulness a bare counter erases.

This hook is the second consumer of the same markers. It captures (name, why), keeps only markers
whose note actually FIRED this session (the same intersection guard recall_feedback uses — it
kills the literal "[RECALL-USED] <name>" placeholder in the injected instruction), resolves each
name to its note file via the compiled corpus, and appends one dated line at the end of that note:

    ## Applied
    - 2026-07-23 - a0156c67 - settled the registry-versus-if-elif call in the adapter

So browsing a note in Obsidian shows its lived track record, not just its curated summary.

BOTH DOORS, SINCE 2026-08-07. The pushed rail ([RECALL], which ranks a note's name/tags/summary)
writes to "## Applied"; the pulled body search ([SMRITI-USED] from vault_search, which reads
note BODIES at section granularity) writes to "## Found by search". Two sections, never merged:
a note that is load-bearing through its body and dead through its label is the case worth seeing,
and one list averages that diagnosis away. Until this change the pull door left no trace inside
the note at all, so a note reachable ONLY by body search looked unused in the very place a human
goes to check.

The two doors are attributed differently and must stay that way. The push rail writes a
per-session fires file; the pull door cannot — the MCP server is never told a session id — so its
fires land in one rolling log and are claimed by transcript TIME WINDOW, exactly as
recall_feedback.py claims them. If the two ever disagree, a note earns a written trace it is not
credited for, or the reverse.

The new section is also in semantic.py's SKIP_HEADINGS. It has to be: it records what the body
search itself returned, so indexing it would let that search find notes on the strength of its own
past hits — a self-poisoning loop closed in one hop rather than two.

Corpus-aware (v1): resolves the active namespace from the session's lens exactly like the
scanner (project .claude/lens.yaml wiki_index -> user lens.yaml -> default code corpus), then
looks the fired names up in index_compiled[_<ns>].json and writes into that corpus's vault. So
the same hook serves the code wiki and any second corpus with no branching.

Design guards (same stance as recall_feedback / procedural_feedback):
  - Idempotent. Per-note-per-session dedup: a session id already present in a note's evidence
    section is never re-appended, so running on every Stop of a session never double-logs.
  - Observe-only. This writes a human/agent-readable log into the note; it NEVER feeds scanner
    ranking. Consistent with the observe-not-autotune line drawn for recall_stats.
  - Best-effort. Any single note write failing is swallowed; the hook always exits 0. A vault
    edit must never block session end.

Zero third-party deps (no PyYAML — the one lens field we need is grepped). Invoke via `py`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_WIKI_PATH = ""  # no default: set CLAUDE_WIKI_PATH to the vault root
MAX_EVIDENCE_LINES = 30  # bound growth: a hot note keeps its newest N applications

# ------------------------------------------------------------------
# TWO DOORS, TWO SECTIONS — never one merged list
# ------------------------------------------------------------------
# The push door ([RECALL]) ranks a note's LABEL — name, tags, summary. The pull door
# (vault_search / [SMRITI]) searches note BODIES at section granularity. A note can be
# load-bearing through one and dead through the other, and that SPLIT is the diagnosis:
# applied often from the body but never from the label means the content works and the summary
# lies, which is a repair with a known address. Merging both into one list averages exactly that
# signal away, which is why they get separate headers rather than a shared one with a tag column.
EVIDENCE_HEADER = "## Applied"            # push door: the [RECALL] rail delivered it
SMRITI_HEADER = "## Found by search"      # pull door: vault_search found it in the body

# <MARKER> <name> — <why>. The dash separator is REQUIRED to be whitespace-flanked so a
# kebab-case note name (foo-bar) is never split on its internal hyphen. Accepts em/en/hyphen.
USED_PATTERN = re.compile(r"\[RECALL-USED\]\s+(\S+)\s+[—–-]\s+([^\n]+)")
SMRITI_USED_PATTERN = re.compile(r"\[SMRITI-USED\]\s+(\S+)\s+[—–-]\s+([^\n]+)")
SMRITI_FIRES_FILE = "smriti_fires.jsonl"
DEFAULT_CORPUS = "code"
_LENS_WIKI_INDEX_RE = re.compile(r"^\s*wiki_index:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)


# ============================================================
# CONFIG RESOLUTION (config dir, lens namespace, corpus vault)
# ============================================================
def config_dir() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else Path.home() / ".claude"


def sanitize_namespace(ns: str) -> str:
    """Mirror the scanner's sanitizeNamespace: keep only [A-Za-z0-9_-]."""
    return "".join(c for c in ns if c.isalnum() or c in "_-")


def _wiki_index_from(lens_path: Path) -> str:
    try:
        m = _LENS_WIKI_INDEX_RE.search(lens_path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return m.group(1) if m else ""


def resolve_namespace(cwd: str, cfg_dir: Path) -> str:
    """Project lens wins over user lens (same precedence as the scanner). Empty -> code corpus."""
    if cwd:
        proj = _wiki_index_from(Path(cwd) / ".claude" / "lens.yaml")
        if proj:
            return sanitize_namespace(proj)
    return sanitize_namespace(_wiki_index_from(cfg_dir / "lens.yaml"))


def corpus_paths(namespace: str, cfg_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """Return (compiled_index_json, vault_root) for the namespace, or (None, None) if unresolvable.
    Empty namespace -> the default code corpus. A namespace -> corpora.json registry lookup."""
    recall_dir = cfg_dir / "hooks" / "recall"
    if namespace in ("", DEFAULT_CORPUS):
        # No vault configured is a valid state, not a failure: a stranger installs this before
        # pointing it anywhere. Returning a path built from an empty default would write the
        # evidence log into the process's working directory, silently.
        configured = os.environ.get("CLAUDE_WIKI_PATH", DEFAULT_WIKI_PATH).strip()
        if not configured:
            return None, None
        return recall_dir / "index_compiled.json", Path(configured)

    index = recall_dir / f"index_compiled_{namespace}.json"
    try:
        reg = json.loads((recall_dir / "corpora.json").read_text(encoding="utf-8"))
        wiki_path = reg[namespace]["wiki_path"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    return index, Path(wiki_path)


def load_name_to_path(index_json: Path) -> Dict[str, str]:
    """name -> relative note path, from the compiled corpus. Empty on any failure."""
    try:
        entries = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(entries, list):
        return {}
    return {e["name"]: e["path"] for e in entries if e.get("name") and e.get("path")}


# ============================================================
# READ SIDES (fires this session + used markers with their why-clause)
# ============================================================
def read_session_fires(fires_path: Path) -> set:
    """Names that fired this session (the whole per-session file IS this session)."""
    if not fires_path.is_file():
        return set()
    fired = set()
    for raw in fires_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if rec.get("name"):
            fired.add(rec["name"])
    return fired


def _assistant_texts(transcript_path: Path) -> List[str]:
    """Clean assistant text blocks from the JSONL transcript. Parsing per-line (not raw text) is
    what keeps the '— why' capture from swallowing the trailing JSON of a marker's own line."""
    texts: List[str] = []
    try:
        raw = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return texts
    for line in raw.splitlines():
        line = line.strip()
        if not line or '"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
    return texts


def read_used_with_why(transcript_path: Path, fired_names: set,
                       pattern: re.Pattern = USED_PATTERN) -> Dict[str, str]:
    """name -> why, for markers whose note actually fired this session. When a note is marked used
    more than once, the LAST why wins (most refined statement of its usefulness)."""
    if not fired_names or not transcript_path.is_file():
        return {}
    used: Dict[str, str] = {}
    for text in _assistant_texts(transcript_path):
        for name, why in pattern.findall(text):
            name = name.strip().strip(".,:;")
            if name not in fired_names:  # intersection guard: kills placeholder + passing mentions
                continue
            why = why.strip().strip("`*").strip()
            if why:
                used[name] = why
    return used


def session_window(transcript_path: Path) -> Tuple[Optional[datetime], Optional[datetime]]:
    """(first, last) UTC timestamps of this session's transcript, or (None, None).

    The pull door cannot write a session id — the MCP server is never told one — so its fires land
    in one rolling file and are attributed by TIME. Every tool call is bracketed by transcript
    records, so a fire raised during this session necessarily falls inside this window.

    Identical to recall_feedback.py's window, deliberately: the counter and this qualitative log
    must agree on which fires belong to a session, or a note could earn a written trace it is not
    credited for, or the reverse.
    """
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


def read_smriti_fires(recall_dir: Path, window: Tuple[Optional[datetime], Optional[datetime]]) -> Dict[str, str]:
    """name -> corpus, for what the pull door returned inside this session's window."""
    start, end = window
    path = recall_dir / SMRITI_FIRES_FILE
    if start is None or end is None or not path.is_file():
        return {}  # no window means no attribution: credit nothing rather than credit all
    fired: Dict[str, str] = {}
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
            fired[rec["name"]] = rec.get("corpus", "") or ""
    return fired


# ============================================================
# WRITE SIDE (append dated evidence line into the note, deduped)
# ============================================================
def split_evidence_section(lines: List[str], header: str) -> Tuple[List[str], List[str], List[str]]:
    """Return (head, bullets, tail) around `header`. Absent header -> (lines, [], []).

    The section ENDS at the next markdown heading, not at end of file. That bound is the whole
    correctness of this function once a note can carry two evidence sections: an unbounded read
    would collect the second section's bullets as if they were the first's, and the rebuild would
    then write them under the wrong header and delete the second header entirely. Cheap to get
    wrong, silent when wrong, and it destroys a note's own record — so it is a boundary, not a
    filter.
    """
    for i, line in enumerate(lines):
        if line.strip() != header:
            continue
        bullets, end = [], len(lines)
        for j in range(i + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped.startswith("#"):
                end = j
                break
            if stripped.startswith("- "):
                bullets.append(lines[j])
        return lines[:i], bullets, lines[end:]
    return lines, [], []


def append_evidence(note_path: Path, header: str, session_id8: str, why: str, date_str: str) -> bool:
    """Append one dedup'd evidence line under `header`. Returns True if the file was modified."""
    try:
        text = note_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    lines = text.splitlines()
    head, bullets, tail = split_evidence_section(lines, header)

    # Per-note-per-session dedup: this session already logged here -> no-op (idempotent Stop).
    # Scoped to THIS section, so a note reached through both doors in one session records both.
    session_tag = f"· {session_id8} ·"
    if any(session_tag in bullet for bullet in bullets):
        return False

    bullets.append(f"- {date_str} · {session_id8} · {why}")
    if len(bullets) > MAX_EVIDENCE_LINES:
        bullets = bullets[-MAX_EVIDENCE_LINES:]  # keep newest

    parts = ["\n".join(head).rstrip("\n"), "", header, "\n".join(bullets)]
    rebuilt = "\n".join(parts).rstrip("\n") + "\n"
    remainder = "\n".join(tail).strip("\n")
    if remainder:
        rebuilt += "\n" + remainder + "\n"
    try:
        note_path.write_text(rebuilt, encoding="utf-8")
    except OSError:
        return False
    return True


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
    cwd = hook.get("cwd", "")
    if not session_id:
        return 0

    cfg_dir = config_dir()
    recall_dir = cfg_dir / "hooks" / "recall"
    transcript = Path(transcript_path)

    # Each door is read independently. The push door's absence used to end the hook, which was
    # correct while it was the only door: now a session that reached a note ONLY through body
    # search would have left no trace at all, and that is exactly the note this log exists for.
    push_fired = read_session_fires(recall_dir / f"fires_{session_id}.jsonl")
    push_used = read_used_with_why(transcript, push_fired, USED_PATTERN)

    pull_fired = read_smriti_fires(recall_dir, session_window(transcript))
    pull_used = read_used_with_why(transcript, pull_fired, SMRITI_USED_PATTERN)

    if not push_used and not pull_used:
        return 0

    session_id8 = session_id[:8]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts = {EVIDENCE_HEADER: 0, SMRITI_HEADER: 0}
    maps: Dict[str, Tuple[Dict[str, str], Optional[Path]]] = {}

    def corpus_map(corpus: str):
        if corpus not in maps:
            index_json, vault_root = corpus_paths(corpus, cfg_dir)
            maps[corpus] = ((load_name_to_path(index_json) if index_json else {}), vault_root)
        return maps[corpus]

    # The lens names the PUSH rail's corpus only; a pull hit says itself which corpus answered it.
    lens_ns = resolve_namespace(cwd, cfg_dir)
    pushes = ((name, why, lens_ns, EVIDENCE_HEADER) for name, why in push_used.items())
    pulls = ((name, why, pull_fired.get(name, ""), SMRITI_HEADER) for name, why in pull_used.items())
    for name, why, corpus, header in (*pushes, *pulls):
        names, root = corpus_map(corpus)
        rel = names.get(name)
        if rel and root and append_evidence((root / rel).resolve(), header, session_id8, why, date_str):
            counts[header] += 1

    if any(counts.values()):
        print(f"[recall_evidence] {counts[EVIDENCE_HEADER]} pushed + {counts[SMRITI_HEADER]} pulled "
              f"application(s) logged", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
