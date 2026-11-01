# @module: rules_inputs
# @role: gather skill principles + CLAUDE.md section index for the rules-distiller mode (Layer 6 write side, rules variant)
# @deps: (stdlib only — zero third-party, runs on x64 and Windows ARM via `py`)
# @used-by: procedural-distiller (rules mode), procedural skill (rules mode)
# @doc: RECIPE_FORMAT.md#the-fields
"""
Read half of the rules-distiller MODE — the sibling of inputs.py for the rules variant of
Layer 6. Where inputs.py gathers BEHAVIORAL artifacts (sessions) to mine procedural recipes,
this gathers DECLARED-KNOWLEDGE artifacts (skills) to mine rule candidates for CLAUDE.md.

Two sources, both already-curated static .md (never a transcript):
  1. skills[]        — every SKILL.md under the skills dir: name, description, and only the
                       PRINCIPLE-BEARING sections of the body (headings matching
                       PRINCIPLE_HEADING). Full bodies are deliberately NOT collected — the
                       2+-skills evidence gate only needs where principles are declared, and
                       skipping how-to/examples keeps both the dry test and the Haiku pass cheap.
  2. claude_sections — the H2 section index of the target CLAUDE.md, so the distiller can check
                       "already covered" before proposing a rule.

Deterministic collection only — no LLM, no third-party deps. The distiller (Haiku in production,
or inline for a dry test) supplies the judgment. Invoke via `py`.
"""

import argparse
import glob
import os
import re
import sys
from pathlib import Path

# Section titles that tend to carry PROMOTABLE principles (not how-to, examples, or commands).
PRINCIPLE_HEADING = re.compile(
    r"principle|rule|anti-?pattern|best practice|discipline|guideline|convention|"
    r"do not|don'?t|always|never|design|invariant|constraint|gotcha|pitfall",
    re.IGNORECASE,
)

HEADING = re.compile(r"(?m)^(#{2,4})\s+(.*?)\s*$")
SECTION_SNIPPET = 700  # cap per principle-section so output stays bounded


# ============================================================
# CONFIG DIR (env first, script-relative fallback — never hardcoded)
# ============================================================
def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    # <config>/skills/procedural/scripts/rules_inputs.py -> parents[3] == <config>
    return Path(__file__).resolve().parents[3]


# ============================================================
# MARKDOWN HELPERS
# ============================================================
def frontmatter_field(text: str, field: str) -> str:
    in_fm = False
    for line in text.splitlines():
        if line.strip() == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if in_fm and line.startswith(field + ":"):
            val = line[len(field) + 1:].strip()
            return val.strip('"').strip("'")
    return ""


def split_sections(md: str) -> list:
    """Return [(title, body)] for every ## .. #### heading in order."""
    marks = list(HEADING.finditer(md))
    out = []
    for i, m in enumerate(marks):
        title = m.group(2).strip()
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(md)
        out.append((title, md[start:end].strip()))
    return out


# ============================================================
# SOURCE 1 — skills: name + description + principle sections
# ============================================================
def collect_skills(skills_dir: Path) -> list:
    skills = []
    for path in sorted(glob.glob(str(skills_dir / "*" / "SKILL.md"))):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        principle = {
            title: (body[:SECTION_SNIPPET] + (" ..." if len(body) > SECTION_SNIPPET else ""))
            for title, body in split_sections(text)
            if PRINCIPLE_HEADING.search(title) and body
        }
        skills.append({
            "name": frontmatter_field(text, "name") or Path(path).parent.name,
            "description": frontmatter_field(text, "description"),
            "principle_sections": principle,
        })
    return skills


# ============================================================
# SOURCE 2 — target CLAUDE.md H2 section index (for "already covered")
# ============================================================
def claude_section_index(claude_md: Path) -> list:
    if not claude_md.is_file():
        return []
    text = claude_md.read_text(encoding="utf-8", errors="replace")
    return [title for title, _ in split_sections(text) if HEADING]  # all headings, in order


# ============================================================
# BUNDLE
# ============================================================
def gather(cfg: Path = None) -> dict:
    cfg = cfg or config_dir()
    return {
        "skills": collect_skills(cfg / "skills"),
        "claude_sections": claude_section_index(cfg / "CLAUDE.md"),
        "config_dir": str(cfg),
    }


# ============================================================
# ENTRY POINT (standalone inspection / dry test)
# ============================================================
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="Gather skill principles + CLAUDE.md section index (rules mode)")
    parser.add_argument("--config-dir", default=None, help="Config dir (default: $CLAUDE_CONFIG_DIR or script-relative).")
    parser.add_argument("--titles-only", action="store_true", help="Print only principle-section titles, not bodies.")
    args = parser.parse_args()

    bundle = gather(Path(args.config_dir) if args.config_dir else None)
    skills_with = [s for s in bundle["skills"] if s["principle_sections"]]

    print(f"=== CONFIG DIR === {bundle['config_dir']}")
    print(f"=== SKILLS: {len(bundle['skills'])} scanned, {len(skills_with)} with principle sections ===\n")
    for s in skills_with:
        print(f"## {s['name']}")
        for title, body in s["principle_sections"].items():
            if args.titles_only:
                print(f"   - {title}")
            else:
                print(f"   - {title}: {body[:200].replace(chr(10), ' ')}")
        print()

    print(f"=== CLAUDE.md SECTION INDEX ({len(bundle['claude_sections'])}) ===")
    for title in bundle["claude_sections"]:
        print(f"   - {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
