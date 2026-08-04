# @module: compile_procedural
# @role: procedural.md (recipe markdown) -> procedural_compiled.json (IndexEntry list for recall_scanner)
# @deps: (stdlib only — zero third-party, must run on both x64 and Windows ARM via `py`)
# @used-by: /lint procedural (last step), SessionStart freshness check
# @doc: lab/akasha_engine/6_procedural/CONCEPT.md
"""
Compile the human-edited active recipe file into the JSON shape the Go recall_scanner
already consumes (IndexEntry: name, path, tags, summary).

Mirrors compile_wiki_index.py: markdown is the source of truth, this produces the fast
machine-read JSON. Runs OFF the hot path (on promote via /lint, or on a SessionStart
staleness check) — the scanner never parses markdown directly.

Recipe block format in procedural.md (repeatable, one block per recipe):
    ---
    slug: <stable-id>
    trigger: <one-line situation>
    keywords: [a, b, c]
    status: active
    ---
    1. step
    2. step
    Why: <causation; the first Why line feeds the matchable summary>
    Anti-pattern: <what not to do>

Only `status: active` entries compile. Matchable text = trigger + first Why line.
Steps are intentionally NOT emitted: the scanner shows trigger + why, then the agent
opens procedural.md#<slug> to read the steps.

Zero third-party deps on purpose — this runs on hooks across two machines (x64 + Windows
ARM) synced via Dropbox; a missing pip package must never break it. Invoke via `py`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ACTIVE_STATUS = "active"
# One recipe block: opening ---, frontmatter, closing ---, then body up to the next
# block's opening --- (lookahead) or end of file.
BLOCK_PATTERN = re.compile(
    r"^---[ \t]*$\n(.*?)^---[ \t]*$\n(.*?)(?=^---[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)
WHY_PATTERN = re.compile(r"^\s*Why:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


# ============================================================
# PARSING (minimal frontmatter — no YAML dependency)
# ============================================================
def parse_frontmatter(text: str) -> dict:
    fm = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fm[key.strip().lower()] = value.strip()
    return fm


def parse_keywords(raw: str) -> list:
    inner = raw.strip().strip("[]")
    return [kw.strip().strip("\"'") for kw in inner.split(",") if kw.strip()]


def first_why_line(body: str) -> str:
    match = WHY_PATTERN.search(body)
    return match.group(1).strip() if match else ""


# ============================================================
# COMPILER — recipe markdown -> IndexEntry records
# ============================================================
def compile_recipes(md_text: str) -> list:
    entries = []
    for fm_text, body in BLOCK_PATTERN.findall(md_text):
        fm = parse_frontmatter(fm_text)
        if fm.get("status", "").lower() != ACTIVE_STATUS:
            continue
        slug = fm.get("slug", "").strip()
        trigger = fm.get("trigger", "").strip()
        if not slug or not trigger:
            continue
        why = first_why_line(body)
        # Carry the evidence base into the signal itself. A recipe distilled from ONE
        # session is a hypothesis that happened to work; one seen across several is a
        # pattern. The reader has to weigh those differently and cannot tell them apart
        # from the trigger text, so the count leads the line rather than hiding in the file.
        support = len(parse_keywords(fm.get("source_sessions", "")))
        weight = "worked once" if support <= 1 else f"{support} sessions"
        summary = f"[{weight}] {trigger} - {why}" if why else f"[{weight}] {trigger}"
        entries.append({
            "name": slug,
            "path": f"procedural.md#{slug}",
            "tags": parse_keywords(fm.get("keywords", "")),
            "summary": summary,
        })
    return entries


# ============================================================
# ENTRY POINT (error handling at the edge)
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Compile procedural.md -> procedural_compiled.json")
    parser.add_argument(
        "--dir",
        default=str(Path.cwd() / ".claude"),
        help="Directory holding procedural.md (default: <cwd>/.claude). Resolved dynamically — no hardcoded paths.",
    )
    parser.add_argument(
        "--if-stale",
        action="store_true",
        help="Recompile only if procedural.md is newer than the compiled JSON (SessionStart freshness check). "
             "Without it, always recompile (used by /lint after a promote).",
    )
    args = parser.parse_args()

    claude_dir = Path(args.dir)
    src = claude_dir / "procedural.md"
    out = claude_dir / "procedural_compiled.json"

    # Procedural memory is opt-in per project: no procedural.md means this project
    # doesn't use it. Write nothing (no litter) — the scanner treats a missing
    # compiled file as "no procedural corpus".
    if not src.is_file():
        print(f"[INFO] No procedural.md at {src}; nothing to compile.")
        return 0

    # Freshness gate (Layer 2): skip the work when the compiled JSON already
    # reflects the current source. mtime comparison makes a stale JSON impossible
    # after a SessionStart pass.
    if args.if_stale and out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        print(f"[INFO] {out.name} is fresh; skipping recompile.")
        return 0

    try:
        md_text = src.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[ERROR] Cannot read {src}: {exc}")
        return 1

    entries = compile_recipes(md_text)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    # A file with content that yields no recipes is almost always a format mistake,
    # and reporting it as a plain success is how a hand-written procedural.md stays
    # silently invisible for weeks. Say what was expected instead.
    if not entries and md_text.strip():
        print(
            f"[WARNING] {src.name} has content but produced 0 active recipes. Each recipe is a "
            "frontmatter block delimited by --- lines carrying slug / trigger / keywords / "
            "status: active, followed by numbered steps and a 'Why:' line. Only status: active "
            "compiles.",
            file=sys.stderr,
        )

    print(f"[SUCCESS] Compiled {len(entries)} active recipe(s) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
