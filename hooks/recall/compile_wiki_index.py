#!/usr/bin/env python3
"""
compile_wiki_index.py — rebuild index_compiled.json from the Obsidian wiki.

Runs as a SessionStart hook. Idempotent and cheap:
  - parses INDEX.md once
  - mtime-checks INDEX.md + every referenced note vs current output
  - if nothing is newer than the existing index_compiled.json → no rebuild

Closes the v2 "wiki edits invisible until /compile is run manually" gap.

Stdlib-only (no PyYAML dependency) — keeps the script portable and the
hook startup fast. The frontmatter parser handles the two summary shapes
actually used in the vault: single-line scalar and `|` block scalar.

Invocation context (Claude Code SessionStart hook):
  stdin: ignored
  env:   CLAUDE_WIKI_PATH    — vault root (REQUIRED; unset means nothing to compile)
         CLAUDE_CONFIG_DIR   — config root (fallback: ~/.claude)

Multi-corpus (v2.7): besides the default corpus, every namespace registered in
$CLAUDE_CONFIG_DIR/hooks/recall/corpora.json ({namespace: {wiki_path}}) is
compiled to index_compiled_<namespace>.json. Absent registry → default only
(back-compatible). The recall_scanner selects the corpus per workspace via the
lens `wiki_index` field, reading vault paths from the same corpora.json.

Always exits 0. Hook failures must never block session start.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_WIKI_PATH = ""  # no default: the vault lives wherever the user keeps it

# ============================================================================
# Frontmatter parser — stdlib only
# ============================================================================

_FM_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_BLOCK_SCALAR_INDICATORS = {"|", ">", "|-", "|+", "|2", ">-", ">+"}


def extract_summary(content: str) -> str:
    """Return the YAML frontmatter `summary` field, or empty string."""
    m = _FM_BLOCK_RE.match(content)
    if not m:
        return ""
    lines = m.group(1).split("\n")

    for i, line in enumerate(lines):
        ms = re.match(r"^summary:\s*(.*)$", line)
        if not ms:
            continue
        rest = ms.group(1).rstrip()

        if rest in _BLOCK_SCALAR_INDICATORS:
            collected: List[str] = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.startswith((" ", "\t")) or nxt.strip() == "":
                    collected.append(nxt.strip())
                    j += 1
                else:
                    break
            return " ".join(s for s in collected if s)

        if rest:
            if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ("'", '"'):
                rest = rest[1:-1]
            return rest

        return ""

    return ""


# ============================================================================
# INDEX.md parser
# ============================================================================

# Per-entry shape:  - [name](path) [tag1, tag2, ...]
_INDEX_ENTRY_RE = re.compile(
    r"^\s*-\s*\[([^\]]+)\]\(([^\)]+)\)\s*\[([^\]]+)\]",
    re.MULTILINE,
)


def parse_index(index_md_text: str) -> List[Dict]:
    entries: List[Dict] = []
    for m in _INDEX_ENTRY_RE.finditer(index_md_text):
        name = m.group(1).strip()
        path = m.group(2).strip()
        tags_blob = m.group(3)
        tags = [t.strip() for t in tags_blob.split(",") if t.strip()]
        entries.append({"name": name, "path": path, "tags": tags})
    return entries


# ============================================================================
# Mtime gate
# ============================================================================

def _mtime_or_none(p: Path) -> Optional[float]:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def needs_rebuild(
    wiki_root: Path,
    index_md: Path,
    output: Path,
    entries: Iterable[Dict],
) -> bool:
    out_mtime = _mtime_or_none(output)
    if out_mtime is None:
        return True

    idx_mtime = _mtime_or_none(index_md)
    if idx_mtime is None or idx_mtime > out_mtime:
        return True

    for entry in entries:
        note_mtime = _mtime_or_none(wiki_root / entry["path"])
        if note_mtime is not None and note_mtime > out_mtime:
            return True

    return False


# ============================================================================
# Build
# ============================================================================

def build(wiki_root: Path, entries: List[Dict]) -> List[Dict]:
    result: List[Dict] = []
    for entry in entries:
        note_path = wiki_root / entry["path"]
        try:
            text = note_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        summary = extract_summary(text)
        result.append({
            "name": entry["name"],
            "path": entry["path"],
            "tags": entry["tags"],
            "summary": summary,
        })
    return result


# ============================================================================
# Frontmatter enumeration (INDEX-less source — the default code corpus)
# ============================================================================

_FM_TAGS_INLINE = re.compile(r"^tags:\s*\[([^\]]*)\]", re.MULTILINE)
_FM_TAGS_BLOCK = re.compile(r"^tags:\s*\n((?:[ \t]*-[ \t]*.+\n?)+)", re.MULTILINE)
_ENUM_EXCLUDE_DIRS = {".obsidian", ".claude", ".git", "raw", "media", "bases", "node_modules"}


def extract_tags(content: str) -> List[str]:
    """Frontmatter `tags` — inline `[a, b]` or a `- ` block list. Empty list if absent. This is
    the piece that lets the corpus be sourced from the notes instead of INDEX.md's tag column."""
    m = _FM_BLOCK_RE.match(content)
    if not m:
        return []
    block = m.group(1)
    mi = _FM_TAGS_INLINE.search(block)
    if mi:
        return [t.strip() for t in mi.group(1).split(",") if t.strip()]
    mb = _FM_TAGS_BLOCK.search(block)
    if mb:
        items = [ln.strip()[1:].strip() for ln in mb.group(1).splitlines() if ln.strip().startswith("-")]
        return [i for i in items if i]
    return []


def enumerate_from_frontmatter(wiki_root: Path) -> List[Dict]:
    """Build the corpus from the notes themselves: every `.md` under wiki_root that carries a
    frontmatter `tags` field is an entry (name = filename stem, path = relative, tags + summary
    from frontmatter). No INDEX.md needed — tagless files (INDEX.md, log.md) and build/scratch
    dirs are excluded for free. This is why frontmatter had to become the single source (Phase 0)
    before this could replace the INDEX parser without losing tags."""
    entries: List[Dict] = []
    silent: List[str] = []
    for note in sorted(wiki_root.rglob("*.md")):
        rel = note.relative_to(wiki_root)
        if any(part in _ENUM_EXCLUDE_DIRS for part in rel.parts):
            continue
        try:
            text = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tags = extract_tags(text)
        if not tags:
            continue
        summary = extract_summary(text)
        if not summary:
            silent.append(rel.as_posix())
        entries.append({
            "name": note.stem,
            "path": rel.as_posix(),
            "tags": tags,
            "summary": summary,
        })

    # A tagged note without a summary is IN the corpus and effectively mute: half its
    # ranked document is empty, and the agent sees "(no summary)" where the reason to
    # open it should be. That is a silent failure with no error attached to it, so the
    # compiler is where it gets said out loud.
    if silent:
        shown = ", ".join(silent[:5]) + (f" (+{len(silent) - 5} more)" if len(silent) > 5 else "")
        print(
            f"[compile_wiki_index] WARNING: {len(silent)} tagged note(s) have no summary and "
            f"rank against an empty document: {shown}",
            file=sys.stderr,
        )
    return entries


def _existing_paths(output: Path) -> set:
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {e.get("path") for e in data if isinstance(e, dict)}


def needs_rebuild_glob(wiki_root: Path, output: Path, entries: List[Dict]) -> bool:
    """Frontmatter-mode gate: rebuild if output is missing, the note set changed (add/delete), or
    any note is newer than the output. No INDEX.md mtime to check."""
    out_mtime = _mtime_or_none(output)
    if out_mtime is None:
        return True
    if {e["path"] for e in entries} != _existing_paths(output):
        return True
    for e in entries:
        nm = _mtime_or_none(wiki_root / e["path"])
        if nm is not None and nm > out_mtime:
            return True
    return False


# ============================================================================
# Driver
# ============================================================================

def _config_dir() -> Path:
    cfg_env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg_env) if cfg_env else Path.home() / ".claude"


def _recall_dir(config_dir: Path) -> Path:
    return config_dir / "hooks" / "recall"


def default_target(config_dir: Path) -> Optional[Tuple[Path, Optional[Path], Path, str]]:
    """The default corpus: CLAUDE_WIKI_PATH -> index_compiled.json, sourced from note
    FRONTMATTER (no INDEX.md for this corpus). Returns None when the vault is not
    configured — an unset path must never fall back to the current directory."""
    configured = os.environ.get("CLAUDE_WIKI_PATH", DEFAULT_WIKI_PATH).strip()
    if not configured:
        return None
    output = _recall_dir(config_dir) / "index_compiled.json"
    return Path(configured), None, output, "frontmatter"


def namespace_targets(config_dir: Path) -> List[Tuple[Path, Optional[Path], Path, str]]:
    """One compile target per namespace in corpora.json. Namespaces still source from INDEX.md —
    e.g. a personal vault's corpus includes notes OUTSIDE its vault root (../personal/...), which INDEX.md
    enumerates as a membership list; globbing the root would miss them. Absent/invalid registry
    -> [] (the default corpus still builds)."""
    registry = _recall_dir(config_dir) / "corpora.json"
    try:
        reg = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(reg, dict):
        return []

    targets: List[Tuple[Path, Optional[Path], Path, str]] = []
    for ns, cfg in reg.items():
        wiki_path = (cfg or {}).get("wiki_path", "") if isinstance(cfg, dict) else ""
        if not ns or not wiki_path:
            continue
        wiki_root = Path(wiki_path)
        output = _recall_dir(config_dir) / f"index_compiled_{ns}.json"
        targets.append((wiki_root, wiki_root / "INDEX.md", output, "index"))
    return targets


def compile_target(wiki_root: Path, index_md: Optional[Path], output: Path, source: str) -> Optional[int]:
    """Compile one corpus -> output. `source` selects where the note list + tags come from:
    "frontmatter" globs the notes (INDEX-less, code wiki); "index" parses INDEX.md (namespaces).
    Returns entry count on rebuild, 0 if already up to date, None if nothing to compile / write failed."""
    if source == "frontmatter":
        entries = enumerate_from_frontmatter(wiki_root)
        if not entries:
            return None
        if not needs_rebuild_glob(wiki_root, output, entries):
            return 0
        compiled = entries  # already carry name/path/tags/summary
    else:
        if index_md is None or not index_md.exists():
            return None
        try:
            index_text = index_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        parsed = parse_index(index_text)
        if not parsed:
            return None
        if not needs_rebuild(wiki_root, index_md, output, parsed):
            return 0
        compiled = build(wiki_root, parsed)

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(compiled, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(output)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None

    return len(compiled)


def main() -> int:
    config_dir = _config_dir()
    default = default_target(config_dir)
    if default is None:
        print(
            "[compile_wiki_index] CLAUDE_WIKI_PATH is not set - no vault to compile. "
            "Point it at your Obsidian vault root to enable recall.",
            file=sys.stderr,
        )
    targets = ([default] if default else []) + namespace_targets(config_dir)
    for wiki_root, index_md, output, source in targets:
        count = compile_target(wiki_root, index_md, output, source)
        if count:
            print(
                f"[compile_wiki_index] Rebuilt {output} from {count} entries",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
