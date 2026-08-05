# @module: recall_evidence
# @role: Stop-hook - harvest the discarded "why" context of [RECALL-USED] markers into an inline evidence log inside each wiki note (both corpora)
# @deps: (stdlib only - zero third-party, runs on Stop across machines via py)
# @used-by: Stop hook in settings.json
# @doc: qualitative twin of recall_feedback.py - that fold keeps the COUNT (recall_stats.json), this one keeps the CONTEXT (note-inline "## Applied" log)
"""
recall_evidence.py — evidence-by-backlink for the wiki, reverse-harvested from obsidian-mind.

recall_feedback.py already answers "which notes are load-bearing and how often" by folding
[RECALL-USED] markers into recall_stats.json. But its regex captures only the NAME and throws
away the "— <why>" clause the agent wrote each time. That clause is the qualitative record of
WHY the note helped in this specific case — the shape of usefulness a bare counter erases.

This hook is the second consumer of the same [RECALL-USED] markers. It captures (name, why),
keeps only markers whose note actually FIRED this session (the same intersection guard
recall_feedback uses — it kills the literal "[RECALL-USED] <name>" placeholder in the injected
instruction), resolves each name to its note file via the compiled corpus, and appends one dated
line to an "## Applied" section at the end of that note:

    - 2026-07-23 - a0156c67 - settled the registry-versus-if-elif call in the adapter

So browsing a note in Obsidian shows its lived track record, not just its curated summary.

Corpus-aware (v1): resolves the active namespace from the session's lens exactly like the
scanner (project .claude/lens.yaml wiki_index -> user lens.yaml -> default code corpus), then
looks the fired names up in index_compiled[_<ns>].json and writes into that corpus's vault. So
the same hook serves the default vault and every registered namespace with no branching.

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
EVIDENCE_HEADER = "## Applied"
MAX_EVIDENCE_LINES = 30  # bound growth: a hot note keeps its newest N applications

# [RECALL-USED] <name> — <why>. The dash separator is REQUIRED to be whitespace-flanked so a
# kebab-case note name (foo-bar) is never split on its internal hyphen. Accepts em/en/hyphen.
USED_PATTERN = re.compile(r"\[RECALL-USED\]\s+(\S+)\s+[—–-]\s+([^\n]+)")
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
    if not namespace:
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


def read_used_with_why(transcript_path: Path, fired_names: set) -> Dict[str, str]:
    """name -> why, for [RECALL-USED] markers whose note actually fired this session. When a note
    is marked used more than once, the LAST why wins (most refined statement of its usefulness)."""
    if not fired_names or not transcript_path.is_file():
        return {}
    used: Dict[str, str] = {}
    for text in _assistant_texts(transcript_path):
        for name, why in USED_PATTERN.findall(text):
            name = name.strip().strip(".,:;")
            if name not in fired_names:  # intersection guard: kills placeholder + passing mentions
                continue
            why = why.strip().strip("`*").strip()
            if why:
                used[name] = why
    return used


# ============================================================
# WRITE SIDE (append dated evidence line into the note, deduped)
# ============================================================
def split_evidence_section(lines: List[str]) -> Tuple[List[str], Optional[int], List[str]]:
    """Return (head_lines, header_index, existing_evidence_lines). header_index is None when the
    note has no evidence section yet. Evidence lines are the '- ...' bullets under the header."""
    for i, line in enumerate(lines):
        if line.strip() == EVIDENCE_HEADER:
            evidence = [l for l in lines[i + 1:] if l.strip().startswith("- ")]
            return lines[:i], i, evidence
    return lines, None, []


def append_evidence(note_path: Path, session_id8: str, why: str, date_str: str) -> bool:
    """Append one dedup'd evidence line to note. Returns True if the file was modified."""
    try:
        text = note_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    lines = text.splitlines()
    head, _, evidence = split_evidence_section(lines)

    # Per-note-per-session dedup: this session already logged here -> no-op (idempotent Stop).
    session_tag = f"· {session_id8} ·"
    if any(session_tag in ev for ev in evidence):
        return False

    new_line = f"- {date_str} · {session_id8} · {why}"
    evidence.append(new_line)
    if len(evidence) > MAX_EVIDENCE_LINES:
        evidence = evidence[-MAX_EVIDENCE_LINES:]  # keep newest

    body = "\n".join(head).rstrip("\n")
    rebuilt = f"{body}\n\n{EVIDENCE_HEADER}\n" + "\n".join(evidence) + "\n"
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
    fires_path = cfg_dir / "hooks" / "recall" / f"fires_{session_id}.jsonl"
    if not fires_path.is_file():
        return 0  # fast no-op: this session fired no wiki recalls, so nothing was used

    fired = read_session_fires(fires_path)
    used = read_used_with_why(Path(transcript_path), fired)
    if not used:
        return 0

    namespace = resolve_namespace(cwd, cfg_dir)
    index_json, vault_root = corpus_paths(namespace, cfg_dir)
    if index_json is None or vault_root is None:
        return 0
    name_to_path = load_name_to_path(index_json)
    if not name_to_path:
        return 0

    session_id8 = session_id[:8]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    written = 0
    for name, why in used.items():
        rel = name_to_path.get(name)
        if not rel:
            continue  # marked used but not in this corpus (cross-corpus mention) -> skip
        note_path = (vault_root / rel).resolve()
        if append_evidence(note_path, session_id8, why, date_str):
            written += 1

    if written:
        print(f"[recall_evidence] logged {written} application(s) into {namespace or 'code'} corpus",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
