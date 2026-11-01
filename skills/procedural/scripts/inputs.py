# @module: inputs
# @role: gather already-curated artifacts (HANDOFF Key Decisions + agent_digests + /plan spec) for the distiller
# @deps: (stdlib only — zero third-party, runs on both x64 and Windows ARM via `py`)
# @used-by: distill.py (write side of Layer 6 procedural memory)
# @doc: RECIPE_FORMAT.md#one-recipe
"""
The distiller's read half. Gathers the THREE already-curated inputs the distiller is
allowed to see — never the raw transcript. This is the central design move: the distiller
doesn't need to be smart, it combines artifacts each already passed a quality gate.

Inputs:
  1. HANDOFF Key Decisions  — the `### Key decisions` block of the latest session
                              (`## Session NN (latest)`) in <root>/.claude/HANDOFF.md
  2. agent_digests[]        — subagent summaries across <root>/.claude/code_index/*.json,
                              most-recent first (capped), each already 1-3 sentences
  3. /plan spec (optional)  — an explicit --spec path (passed by the step that finished the plan),
                              else the most-recently-modified documentation/to_do/**/plan_*.md

Pure file reading, no LLM, no third-party deps. `gather()` returns a bundle dict that
distill.py feeds to Haiku. `main()` prints a readable summary for standalone testing.
Invoke via `py`.
"""

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

MAX_DIGESTS = 20
KEY_DECISIONS_PATTERN = re.compile(
    r"(?im)^###\s*Key decisions.*?$(.*?)(?=^###\s|\Z)", re.DOTALL | re.MULTILINE
)


# ============================================================
# SOURCE 1 — HANDOFF latest-session Key Decisions
# ============================================================
def latest_key_decisions(handoff_path: Path) -> str:
    if not handoff_path.is_file():
        return ""
    text = handoff_path.read_text(encoding="utf-8", errors="replace")
    sections = re.split(r"(?m)^## ", text)
    latest = ""
    for sec in sections:
        first_line = sec.splitlines()[0] if sec.strip() else ""
        if "(latest)" in first_line:
            latest = sec
            break
    if not latest:  # fallback: first "Session ..." section
        for sec in sections:
            if sec.lstrip().lower().startswith("session"):
                latest = sec
                break
    if not latest:
        return ""
    match = KEY_DECISIONS_PATTERN.search(latest)
    return match.group(1).strip() if match else ""


# ============================================================
# SOURCE 2 — agent_digests across the code index
# ============================================================
def gather_digests(claude_dir: Path) -> list:
    entries = []
    for path in glob.glob(str(claude_dir / "code_index" / "*.json")):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries.extend(data.get("agent_digests") or [])
    entries.sort(key=lambda e: e.get("added_at", 0), reverse=True)
    return [
        f"[{e.get('by', '?')}] {e.get('summary', '').strip()} ({e.get('lines_cited', '')})"
        for e in entries[:MAX_DIGESTS]
    ]


# ============================================================
# SOURCE 3 — /plan spec (explicit or most-recent)
# ============================================================
def gather_spec(root: Path, spec_path: str = None) -> tuple:
    if spec_path:
        candidate = Path(spec_path)
    else:
        found = glob.glob(str(root / "documentation" / "to_do" / "**" / "plan_*.md"), recursive=True)
        if not found:
            return "", ""
        candidate = Path(max(found, key=os.path.getmtime))
    if not candidate.is_file():
        return "", ""
    return candidate.read_text(encoding="utf-8", errors="replace"), str(candidate)


# ============================================================
# BUNDLE
# ============================================================
def gather(root: str, spec_path: str = None) -> dict:
    root_path = Path(root)
    claude_dir = root_path / ".claude"
    spec_text, spec_used = gather_spec(root_path, spec_path)
    return {
        "key_decisions": latest_key_decisions(claude_dir / "HANDOFF.md"),
        "digests": gather_digests(claude_dir),
        "spec_path": spec_used,
        "spec": spec_text,
    }


# ============================================================
# ENTRY POINT (standalone inspection / testing)
# ============================================================
def main() -> int:
    # Standalone inspection prints curated content that can contain Unicode (Cyrillic,
    # arrows). Force UTF-8 so a cp1251 Windows console can't crash the tool.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="Gather curated distiller inputs (HANDOFF + digests + spec)")
    parser.add_argument("--root", default=str(Path.cwd()), help="Project root (default: cwd). Resolved dynamically.")
    parser.add_argument("--spec", default=None, help="Explicit /plan spec path (else most-recent in documentation/to_do/).")
    args = parser.parse_args()

    bundle = gather(args.root, args.spec)
    print("=== KEY DECISIONS (latest session) ===")
    print(bundle["key_decisions"] or "(none)")
    print(f"\n=== AGENT DIGESTS ({len(bundle['digests'])}) ===")
    for line in bundle["digests"]:
        print("  - " + line)
    print(f"\n=== /plan SPEC ===\n{bundle['spec_path'] or '(none)'}  ({len(bundle['spec'])} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
