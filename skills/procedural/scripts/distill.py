# @module: distill
# @role: orchestrate the write side of Layer 6 - gather inputs -> Haiku -> candidate recipes
# @deps: inputs (sibling); anthropic (optional, lazy - graceful no-op if absent)
# @used-by: /handoff, /plan-implement (verifier OK), /debug success, weekly batch
# @doc: lab/akasha_engine/6_procedural/CONCEPT.md#the-distiller--write-side
"""
The distiller. Reads the three curated artifacts (via inputs.gather), asks Haiku to
extract repeatable recipes, and writes/merges them as CANDIDATES into
<claude_dir>/procedural_candidates.md. It NEVER writes the active procedural.md - promotion
is a separate stage: non-conflicting candidates are auto-promoted (SessionStart + /procedural
batch), conflicting ones are reviewed by the user via /procedural.

Haiku is the single LLM call in the pipeline. It degrades gracefully: if the anthropic
SDK or ANTHROPIC_API_KEY is absent (e.g. on the ARM laptop without deps), the distiller
skips silently and the user can hand-author candidates - the rest of Layer 6 is unaffected.
`--from-json <file>` bypasses Haiku entirely (testing, or feeding pre-made JSON).

Haiku returns JSON {session_id, procedurals:[{title, steps[], why, anti_pattern?, keywords[]}]};
this module converts each procedural into the markdown candidate block that
compile_procedural.py understands, deduping by slug (a repeat slug just adds this session
to source_sessions[] - the triple-source evidence counter).

Invoke via `py`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import inputs  # sibling module

HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 3000
CANDIDATES_HEADER = (
    "# Procedural candidates\n\n"
    "Auto-distilled drafts. Non-conflicting ones auto-promote (SessionStart + /procedural batch);\n"
    "conflicting ones wait here for review via /procedural.\n"
)
BLOCK_PATTERN = re.compile(
    r"^---[ \t]*$\n(.*?)^---[ \t]*$\n(.*?)(?=^---[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)


# ============================================================
# RECIPE <-> MARKDOWN BLOCK
# ============================================================
_CYR_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
SLUG_MAX_LEN = 48


def slugify(title: str) -> str:
    # Transliterate non-Latin -> latin so a title in any script yields a typeable ASCII slug
    # (recipes are promoted by slug on the command line). Drop any other non-ascii, then
    # cap the length at a word boundary so titles do not produce a monster slug.
    lowered = "".join(_CYR_LAT.get(ch, ch) for ch in title.strip().lower())
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if len(slug) > SLUG_MAX_LEN:
        slug = slug[:SLUG_MAX_LEN].rsplit("-", 1)[0] or slug[:SLUG_MAX_LEN]
    return slug or "recipe"


def proc_to_recipe(proc: dict, session_id: str) -> dict:
    steps = [str(s).strip() for s in proc.get("steps", []) if str(s).strip()]
    body_lines = [f"{i}. {step}" for i, step in enumerate(steps, 1)]
    if proc.get("why"):
        body_lines.append(f"\nWhy: {proc['why'].strip()}")
    if proc.get("anti_pattern"):
        body_lines.append(f"Anti-pattern: {proc['anti_pattern'].strip()}")
    # The weekly batch supplies explicit source_sessions (one token per session a recipe
    # recurred in) - that cross-session count is its whole value. The per-session /handoff
    # path omits the field and falls back to the single current session id.
    sources = [str(s).strip() for s in proc.get("source_sessions", []) if str(s).strip()]
    return {
        "slug": str(proc.get("slug", "")).strip() or slugify(proc.get("title", "")),
        "trigger": proc.get("title", "").strip(),
        "keywords": [str(k).strip() for k in proc.get("keywords", []) if str(k).strip()],
        "status": "candidate",
        "source_sessions": sources or [session_id],
        "body": "\n".join(body_lines).strip(),
    }


def serialize_recipe(r: dict) -> str:
    return (
        "---\n"
        f"slug: {r['slug']}\n"
        f"trigger: {r['trigger']}\n"
        f"keywords: [{', '.join(r['keywords'])}]\n"
        f"status: {r['status']}\n"
        f"source_sessions: [{', '.join(r['source_sessions'])}]\n"
        "---\n"
        f"{r['body']}\n"
    )


def _parse_list(raw: str) -> list:
    return [x.strip() for x in raw.strip().strip("[]").split(",") if x.strip()]


def parse_candidates(text: str) -> list:
    recipes = []
    for fm_text, body in BLOCK_PATTERN.findall(text):
        fm = {}
        for line in fm_text.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip().lower()] = val.strip()
        slug = fm.get("slug", "").strip()
        if not slug:
            continue
        recipes.append({
            "slug": slug,
            "trigger": fm.get("trigger", "").strip(),
            "keywords": _parse_list(fm.get("keywords", "")),
            "status": fm.get("status", "candidate").strip(),
            "source_sessions": _parse_list(fm.get("source_sessions", "")),
            "body": body.strip(),
        })
    return recipes


# ============================================================
# MERGE (dedup by slug; repeat slug -> add session to evidence counter)
# ============================================================
def merge(existing: list, new: list) -> tuple:
    by_slug = {r["slug"]: r for r in existing}
    order = [r["slug"] for r in existing]
    added, bumped = 0, 0
    for r in new:
        if r["slug"] in by_slug:
            sessions = by_slug[r["slug"]]["source_sessions"]
            for sid in r["source_sessions"]:
                if sid not in sessions:
                    sessions.append(sid)
                    bumped += 1
        else:
            by_slug[r["slug"]] = r
            order.append(r["slug"])
            added += 1
    return [by_slug[s] for s in order], added, bumped


# ============================================================
# HAIKU CALL (single LLM dependency; graceful fallback)
# ============================================================
def call_haiku(prompt_text: str) -> dict:
    try:
        import anthropic
    except ImportError:
        print("[INFO] anthropic SDK not installed - distillation skipped; add candidates manually.")
        return None
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[INFO] ANTHROPIC_API_KEY not set - distillation skipped; add candidates manually.")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt_text}],
        )
        text = resp.content[0].text
    except anthropic.AnthropicError as exc:
        print(f"[ERROR] Haiku call failed: {exc}; distillation skipped.")
        return None
    return parse_haiku_json(text)


def parse_haiku_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        print("[ERROR] Haiku returned no JSON object; distillation skipped.")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Haiku JSON parse failed: {exc}; distillation skipped.")
        return None


def build_prompt(bundle: dict, session_id: str, prompt_path: Path) -> str:
    template = prompt_path.read_text(encoding="utf-8")
    digests = "\n".join(bundle["digests"]) or "(none)"
    return (
        template
        .replace("{{key_decisions}}", bundle["key_decisions"] or "(none)")
        .replace("{{digests}}", digests)
        .replace("{{spec}}", bundle["spec"] or "(none)")
        .replace("{{session_id}}", session_id)
    )


# ============================================================
# ENTRY POINT
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Distill curated artifacts into candidate procedurals")
    parser.add_argument("--root", default=str(Path.cwd()), help="Project root (default: cwd).")
    parser.add_argument("--session-id", required=True, help="Current session id (stamped into source_sessions).")
    parser.add_argument("--spec", default=None, help="Explicit /plan spec path (else most-recent in documentation/to_do/).")
    parser.add_argument("--from-json", default=None, help="Read Haiku-format JSON from this file instead of calling Haiku.")
    args = parser.parse_args()

    claude_dir = Path(args.root) / ".claude"
    candidates_path = claude_dir / "procedural_candidates.md"
    prompt_path = Path(__file__).resolve().parent / "prompt.md"

    if args.from_json:
        try:
            result = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[ERROR] Cannot read --from-json: {exc}")
            return 1
    else:
        bundle = inputs.gather(args.root, args.spec)
        if not (bundle["key_decisions"] or bundle["digests"] or bundle["spec"]):
            print("[INFO] No curated inputs (HANDOFF/digests/spec) - nothing to distill.")
            return 0
        result = call_haiku(build_prompt(bundle, args.session_id, prompt_path))

    if result is None:
        return 0  # graceful skip already reported
    procedurals = result.get("procedurals") or []
    if not procedurals:
        print("[INFO] Haiku found no procedurals (NO_PROCEDURALS) - nothing written.")
        return 0

    new_recipes = [proc_to_recipe(p, args.session_id) for p in procedurals]
    existing = parse_candidates(candidates_path.read_text(encoding="utf-8")) if candidates_path.is_file() else []
    merged, added, bumped = merge(existing, new_recipes)

    out = CANDIDATES_HEADER + "\n" + "\n".join(serialize_recipe(r) for r in merged)
    claude_dir.mkdir(parents=True, exist_ok=True)
    candidates_path.write_text(out, encoding="utf-8")
    print(f"[SUCCESS] Candidates: +{added} new, {bumped} evidence-bump -> {candidates_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
