# @module: compile_procedural
# @role: procedural.md (recipe markdown) -> procedural_compiled.json (IndexEntry list for recall_scanner),
#        and the SessionStart report of recipes declared in that file that no reader can see
# @deps: recipe_blocks (sibling - the block format); stdlib only
# @used-by: SessionStart freshness check (settings.json), skills/procedural/scripts/lint_procedural.py
#           recompile() via subprocess, /procedural merge step
# @doc: RECIPE_FORMAT.md#what-the-agent-sees
"""
Compile the human-edited active recipe file into the JSON shape the Go recall_scanner
already consumes (IndexEntry: name, path, tags, summary).

Mirrors compile_wiki_index.py: markdown is the source of truth, this produces the fast
machine-read JSON. Runs OFF the hot path (on promote via /lint, or on a SessionStart
staleness check) — the scanner never parses markdown directly.

The block format and its reader live in recipe_blocks.py. Only `status: active` entries
compile. Matchable text = trigger + first Why line. Steps are intentionally NOT emitted:
the scanner shows trigger + why, then the agent opens procedural.md#<slug> to read the steps.

Zero third-party deps on purpose — this runs on hooks across two machines (x64 + Windows
ARM) synced via Dropbox; a missing pip package must never break it. Invoke via `py`.
"""

import argparse
import json
import sys
from pathlib import Path

from recipe_blocks import (duplicate_slugs, first_why_line, is_compilable, parse_blocks,
                           parse_frontmatter, parse_keywords, uncompiled_blocks, unreadable_text)


# ============================================================
# COMPILER — recipe markdown -> IndexEntry records
# ============================================================
def compile_recipes(md_text: str) -> list:
    entries = []
    for _, _, fm_text, body in parse_blocks(md_text):
        fm = parse_frontmatter(fm_text)
        if not is_compilable(fm):
            continue
        slug = fm["slug"].strip()
        trigger = fm["trigger"].strip()
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


def report_population(src: Path, md_text: str) -> int:
    """Say which declared recipes no reader can see, on STDOUT and with exit 0.

    `[!]` The exit code is 0 on purpose. This runs as a SessionStart hook, and there stdout reaches
    the model only on exit 0 - a non-zero exit shows stderr alone, so an [ERROR] printed before
    `return 1` would be the same silence this report exists to end. The line itself is the signal; nothing reads this script's exit code."""
    lost, _ = unreadable_text(md_text)
    skipped = uncompiled_blocks(md_text)
    dupes = duplicate_slugs(md_text)
    if md_text.strip() and not (lost or skipped or compile_recipes(md_text)):
        print(f"[WARNING] {src}: has content but produced 0 active recipes. Each recipe is a frontmatter "
              f"block between --- lines with slug, trigger, keywords and status: active, then numbered "
              f"steps and a Why: line.")
    if dupes:
        print(f"[WARNING] {src}: slug(s) carried by more than one block, and archive/promote reach "
              f"only the first: {', '.join(dupes)}")
    if skipped:
        print(f"[ERROR] {src}: {len(skipped)} block(s) parse but never compile: {', '.join(skipped)}.")
    if lost:
        print(f"[ERROR] {src}: {len(lost)} recipe(s) declared but unreadable, so never compiled, never "
              f"surfaced and not archivable by slug: {', '.join(lost)}. Each needs a `---` line, "
              f"`slug:` `trigger:` `keywords:` `status:` `source_sessions:` lines, a closing `---`, "
              f"then its steps.")
    return 0


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

    try:
        md_text = src.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[ERROR] Cannot read {src}: {exc}")
        return 1

    # Freshness gate (Layer 2): skip the work when the compiled JSON already
    # reflects the current source. mtime comparison makes a stale JSON impossible
    # after a SessionStart pass. The population check below still runs: a fresh JSON
    # compiled from an unreadable file is exactly the state that stayed silent for a month.
    if args.if_stale and out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        print(f"[INFO] {out.name} is fresh; skipping recompile.")
    else:
        entries = compile_recipes(md_text)
        out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[SUCCESS] Compiled {len(entries)} active recipe(s) -> {out}")
    return report_population(src, md_text)


if __name__ == "__main__":
    sys.exit(main())
