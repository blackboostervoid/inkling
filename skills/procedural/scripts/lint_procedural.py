# @module: lint_procedural
# @role: review/promote/reject procedural candidates — the human curation gate (Layer 6)
# @deps: hooks/recall/recipe_blocks (the block format, by path); stdlib; runs compile_procedural.py via subprocess on promote
# @used-by: the /procedural skill (interactive driver)
# @doc: RECIPE_FORMAT.md#two-checks-before-a-recipe-is-done
"""
Mechanical core of the procedural curation gate. The thin /procedural skill drives this:
  list    — show every candidate with evidence (source-session count), conflict flags vs
            active recipes, and a recommended action (the machine pre-sort of the hybrid).
  promote — move a candidate block candidates -> procedural.md, flip status to active,
            recompile the matching JSON (Layer 1 freshness).
  reject  — move a candidate block candidates -> procedural_archive.md, status rejected.
  auto-promote — promote EVERY candidate that clears the evidence gate (>= --min-sessions
            source sessions) AND has no conflict with an active recipe, in one pass; recompile
            once; report loudly. Conflicting candidates are always left for the human. This is
            the human-on-the-loop path: the gate runs unattended, the human reviews post-hoc
            (and undoes via `archive`). Driven by SessionStart + /procedural batch.
  auto-archive — the symmetric OUTPUT side: retire every active recipe that fired >= --min-fires
            times but was never marked used (provably dead); recompile once; report loudly.
            Driven by the Stop hook (after procedural_feedback refreshes the stats).

Block surgery is done on raw text (extract by slug, flip the status line, move between
files) — NO full parse/serialize, so this never duplicates the distiller's recipe shape.
Merge is intentionally NOT automated (CONCEPT: resolution needs judgment) — the skill/model
edits the files, then re-runs `list`.

Promotion is automated (human-on-the-loop, not in-the-loop): any candidate past the
AUTO_PROMOTE_MIN_SESSIONS evidence gate with NO conflict is promoted unattended by
`auto-promote` (SessionStart + batch). The wall is no longer the human at the input —
it is (1) the conflict guard (same-situation candidates always wait for a human decision)
and (2) automated output-side retirement (`auto-archive` retires recipes that fired but were
never used). Input automation is safe only because the output side retires dead recipes; both
ends are now symmetric and unattended.

Zero third-party deps (runs across two machines). Invoke via `py`.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# One block grammar for every reader, in infrastructure: this file carried its own copy of the
# positional pattern, and one malformed block hid a month of recipes from the conflict check,
# auto-archive and `archive <slug>` at the same time as from the compiler.
PROCEDURAL_HOOKS = Path(__file__).resolve().parents[3] / "hooks" / "recall"
sys.path.insert(0, str(PROCEDURAL_HOOKS))
from recipe_blocks import (is_compilable, parse_blocks, parse_frontmatter,  # noqa: E402
                           span_hides, unreadable_text)

AUTO_PROMOTE_MIN_SESSIONS = 1     # auto-promote gate: >=N sessions + no conflict -> promote unattended (aggressive default: 1)
AUTO_ARCHIVE_MIN_FIRES = 3        # auto-archive gate: fired >=N times AND used==0 -> retire unattended (provably dead)
TRIGGER_JACCARD_CONFLICT = 0.6    # tokenized-trigger overlap that means "same situation"
KEYWORD_OVERLAP_CONFLICT = 0.5    # keyword set overlap that means "same domain"
TRIGGER_JACCARD_SAME = 0.85       # near-identical trigger = the SAME recipe seen again, not a rival


ACTIVE_HEADER = "# Procedural memory\n\nActive action-sequences. Compiled to JSON and surfaced as `[PROCEDURAL]`.\n"
ARCHIVE_HEADER = "# Procedural archive\n\nRejected or decayed recipes. Kept for possible re-promotion.\n"


# ============================================================
# LIGHT PARSE (read-only, for list + conflict) - the frontmatter reader is recipe_blocks'
# ============================================================
def parse_list_field(raw: str) -> list:
    return [x.strip() for x in raw.strip().strip("[]").split(",") if x.strip()]


def read_recipes(path: Path) -> list:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    out = []
    for _, _, fm_text, _ in parse_blocks(text):
        fm = parse_frontmatter(fm_text)
        if fm.get("slug"):
            out.append({
                "slug": fm["slug"],
                "trigger": fm.get("trigger", ""),
                "keywords": parse_list_field(fm.get("keywords", "")),
                "sessions": parse_list_field(fm.get("source_sessions", "")),
            })
    return out


# ============================================================
# CONFLICT DETECTION (candidate vs active recipes)
# ============================================================
def tokenize(text: str) -> set:
    return {t for t in re.split(r"\W+", text.lower(), flags=re.UNICODE) if t}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_ratio(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def find_conflicts(candidate: dict, active: list) -> list:
    hits = []
    cand_trigger = tokenize(candidate["trigger"])
    cand_kw = {k.lower() for k in candidate["keywords"]}
    for act in active:
        if jaccard(cand_trigger, tokenize(act["trigger"])) >= TRIGGER_JACCARD_CONFLICT:
            hits.append(act["slug"])
        elif overlap_ratio(cand_kw, {k.lower() for k in act["keywords"]}) >= KEYWORD_OVERLAP_CONFLICT:
            hits.append(act["slug"])
    return hits


# ============================================================
# BLOCK SURGERY (raw text — extract / move / flip status)
# ============================================================
def extract_block(text: str, slug: str) -> tuple:
    for start, end, fm_text, _ in parse_blocks(text):
        if parse_frontmatter(fm_text).get("slug") != slug:
            continue
        # `[!]` A block no reader parses sits inside the span above it, so moving this block would
        # carry that hidden recipe into the archive or the active file under this slug's status.
        hidden = span_hides(text, start, end)
        if hidden:
            print(f"[ERROR] '{slug}' spans text no reader parses ({', '.join(hidden)}); refusing to "
                  f"move it, because that text would move with it. Fix the hidden block's shape first.")
            return "", text
        return text[start:end].rstrip() + "\n", text[:start] + text[end:]
    return "", text


def unreadable_candidates(path: Path) -> list:
    """Candidate text in a shape no reader parses. It never promotes, and until 2026-09-14 nothing
    said so: sessions answering the Stop-hook ask wrote fields as bullets or under a heading, and
    those blocks sat in the file indefinitely while `list` reported no pending candidates."""
    if not path.is_file():
        return []
    lost, stray = unreadable_text(path.read_text(encoding="utf-8"))
    return lost + stray


def report_unreadable_candidates(path: Path, prefix: str) -> None:
    lost = unreadable_candidates(path)
    if lost:
        print(f"{prefix} {path.name} holds candidate text no reader parses, so it never promotes: "
              f"{', '.join(lost)}. Rewrite each as a `---` line, `slug:` `trigger:` `keywords:` "
              f"`status: candidate` `source_sessions:`, a closing `---`, then the steps - or merge it "
              f"into its target recipe by hand.")


def set_status(block: str, status: str) -> str:
    # count=1: the first `status:` line is the frontmatter's; any later one belongs to body text.
    return re.sub(r"(?im)^status:.*$", f"status: {status}", block, count=1)


def find_same_recipe(candidate: dict, active: list) -> str:
    """The active recipe this candidate is a RE-SIGHTING of, or ''.

    Distinct from find_conflicts on purpose. A 0.6 trigger overlap means "same
    situation" and may still be a rival recipe with different steps — a human calls
    that. A 0.85 overlap means the distiller produced the same recipe again, which is
    evidence, not a dispute: the situation recurred and the same sequence worked.
    """
    cand_trigger = tokenize(candidate["trigger"])
    for act in active:
        if jaccard(cand_trigger, tokenize(act["trigger"])) >= TRIGGER_JACCARD_SAME:
            return act["slug"]
    return ""


def merge_sessions(block: str, new_sessions: list) -> tuple:
    """Fold session ids into a block's source_sessions. Returns (block, added_count)."""
    match = re.search(r"(?m)^source_sessions:\s*(.*)$", block)
    if not match:
        return block, 0
    have = parse_list_field(match.group(1))
    added = [s for s in new_sessions if s not in have]
    if not added:
        return block, 0
    merged = ", ".join(have + added)
    return re.sub(r"(?m)^source_sessions:.*$", f"source_sessions: [{merged}]", block, count=1), len(added)


def merge_sessions_in_file(text: str, slug: str, new_sessions: list) -> tuple:
    """In-place fold, so a reinforced recipe keeps its position in the file."""
    for start, end, fm_text, _ in parse_blocks(text):
        if parse_frontmatter(fm_text).get("slug") != slug:
            continue
        merged, added = merge_sessions(text[start:end], new_sessions)
        return (text[:start] + merged + text[end:], added) if added else (text, 0)
    return text, 0


def append_block(path: Path, block: str, header: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.is_file() else header
    path.write_text(existing.rstrip() + "\n\n" + block.strip() + "\n", encoding="utf-8")


# ============================================================
# RECOMPILE (Layer 1 — after a promote, force-rebuild the active JSON)
# ============================================================
def recompile(claude_dir: Path) -> None:
    # Found beside the grammar this file imports, not through CLAUDE_CONFIG_DIR: two lookups can
    # point at two trees when a check swaps the config dir.
    compiler = PROCEDURAL_HOOKS / "compile_procedural.py"
    if not compiler.is_file():
        print(f"[WARNING] compiler not found at {compiler}; SessionStart will refresh instead.")
        return
    result = subprocess.run(
        ["py", str(compiler), "--dir", str(claude_dir)],
        capture_output=True, text=True,
    )
    print(result.stdout.strip() or result.stderr.strip())


# ============================================================
# COMMANDS
# ============================================================
def cmd_list(claude_dir: Path) -> int:
    report_unreadable_candidates(claude_dir / "procedural_candidates.md", "[WARNING]")
    candidates = read_recipes(claude_dir / "procedural_candidates.md")
    active = read_recipes(claude_dir / "procedural.md")
    if not candidates:
        print("[INFO] No pending candidates.")
        return 0
    print(f"{len(candidates)} candidate(s) pending review:\n")
    for c in candidates:
        evidence = len(c["sessions"])
        conflicts = find_conflicts(c, active)
        if conflicts:
            rec = f"REVIEW (conflicts with: {', '.join(conflicts)}) - auto-promote skips this; human decides"
        elif evidence >= AUTO_PROMOTE_MIN_SESSIONS:
            rec = "AUTO-PROMOTES on next SessionStart/batch (no conflict, evidence >= gate)"
        else:
            rec = f"hold ({evidence}/{AUTO_PROMOTE_MIN_SESSIONS} sessions)"
        print(f"  [{c['slug']}]")
        print(f"      trigger:   {c['trigger']}")
        print(f"      evidence:  {evidence} session(s): {', '.join(c['sessions']) or '-'}")
        print(f"      conflicts: {', '.join(conflicts) or 'none'}")
        print(f"      -> {rec}\n")
    return 0


def cmd_move(claude_dir: Path, slug: str, to_active: bool) -> int:
    cand_path = claude_dir / "procedural_candidates.md"
    if not cand_path.is_file():
        print(f"[ERROR] No candidates file at {cand_path}")
        return 1
    text = cand_path.read_text(encoding="utf-8")
    block, remaining = extract_block(text, slug)
    if not block:
        print(f"[ERROR] No candidate with slug '{slug}'.")
        return 1
    if to_active:
        if not promote_block(claude_dir, slug, block, remaining):
            print(f"[ERROR] '{slug}' would not compile back from procedural.md (a missing trigger, a "
                  f"status the flip did not reach, or a broken block above it). Both files restored; "
                  f"fix the shape by hand.")
            return 1
        print(f"[SUCCESS] Promoted '{slug}' -> procedural.md")
        recompile(claude_dir)
    else:
        cand_path.write_text(remaining.rstrip() + "\n", encoding="utf-8")
        append_block(claude_dir / "procedural_archive.md", set_status(block, "rejected"), ARCHIVE_HEADER)
        print(f"[SUCCESS] Rejected '{slug}' -> procedural_archive.md")
    return 0


def is_readable(path: Path, slug: str) -> bool:
    """Whether the ACTIVE file would compile a recipe with this slug. Parsed is not enough: a block
    with no trigger, or a status the flip did not reach, parses and never surfaces."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(parse_frontmatter(head).get("slug") == slug and is_compilable(parse_frontmatter(head))
               for _, _, head, _ in parse_blocks(text))


def promote_block(claude_dir: Path, slug: str, block: str, remaining: str) -> bool:
    """Move one candidate block into procedural.md, and put both files back when it does not compile
    there. Until 2026-09-14 the candidate was removed first and never restored, so a failed promotion
    left `list` answering "No pending candidates" over a recipe nobody would ever see."""
    cand_path, active_path = claude_dir / "procedural_candidates.md", claude_dir / "procedural.md"
    cand_before = cand_path.read_text(encoding="utf-8")
    active_before = active_path.read_text(encoding="utf-8") if active_path.is_file() else None
    cand_path.write_text(remaining.rstrip() + "\n", encoding="utf-8")
    append_block(active_path, set_status(block, "active"), ACTIVE_HEADER)
    if is_readable(active_path, slug):
        return True
    cand_path.write_text(cand_before, encoding="utf-8")
    if active_before is None:
        active_path.unlink()
    else:
        active_path.write_text(active_before, encoding="utf-8")
    return False


def cmd_auto_promote(claude_dir: Path, min_sessions: int) -> int:
    report_unreadable_candidates(claude_dir / "procedural_candidates.md", "[PROCEDURAL-AUTO]")
    candidates = read_recipes(claude_dir / "procedural_candidates.md")
    if not candidates:
        return 0  # fast no-op: nothing pending (the common SessionStart case)
    active = read_recipes(claude_dir / "procedural.md")
    cand_path = claude_dir / "procedural_candidates.md"
    active_path = claude_dir / "procedural.md"
    promoted = []
    reinforced = []
    unreadable = []
    for c in candidates:
        # A re-sighting is EVIDENCE, not a rival. Before the conflict check: if the
        # distiller produced essentially the same recipe again, fold its session id into
        # the active one and drop the candidate. Without this the second sighting lands
        # as a conflict, the active recipe's support never grows, and every recipe in the
        # corpus reads "worked once" forever — measured 28/28 on 2026-07-28.
        same = find_same_recipe(c, active)
        if same:
            text, added = merge_sessions_in_file(active_path.read_text(encoding="utf-8"),
                                                 same, c["sessions"])
            if added:
                active_path.write_text(text, encoding="utf-8")
                reinforced.append(same)
            _, remaining = extract_block(cand_path.read_text(encoding="utf-8"), c["slug"])
            cand_path.write_text(remaining.rstrip() + "\n", encoding="utf-8")
            continue
        # Conflict check runs against active PLUS already-promoted-this-pass, so two
        # near-identical candidates in one batch can't both auto-land — the second
        # collides with the first and is left for the human.
        if find_conflicts(c, active) or len(c["sessions"]) < min_sessions:
            continue
        block, remaining = extract_block(cand_path.read_text(encoding="utf-8"), c["slug"])
        if not block:
            continue
        if not promote_block(claude_dir, c["slug"], block, remaining):
            unreadable.append(c["slug"])
            continue
        active.append(c)
        promoted.append(c["slug"])
    if unreadable:
        print(f"[PROCEDURAL-AUTO] [ERROR] not promoted, because procedural.md would not compile them "
              f"back: {', '.join(unreadable)}. Both files restored; fix each block's shape by hand.")
    if promoted or reinforced:
        recompile(claude_dir)
    if promoted:
        print(f"[PROCEDURAL-AUTO] Promoted {len(promoted)} recipe(s) to active: {', '.join(promoted)}. "
              f"Now surfaced as [PROCEDURAL]. Review/undo any via /procedural archive <slug>.")
    if reinforced:
        print(f"[PROCEDURAL-AUTO] Reinforced {len(reinforced)} recipe(s) with a repeat sighting: "
              f"{', '.join(sorted(set(reinforced)))}. Their support tag rises at the next "
              f"corpus build — a recipe seen twice is a pattern, not a one-off.")
    return 0


def cmd_auto_archive(claude_dir: Path, min_fires: int) -> int:
    # Output side of the loop: retire provably-dead active recipes (surfaced >= min_fires
    # times by recall, never once marked [PROCEDURAL-USED]). Conservative on purpose —
    # a recipe that WAS used and then went quiet is left for the human (it may come back).
    active = read_recipes(claude_dir / "procedural.md")
    if not active:
        return 0
    stats = read_stats(claude_dir)
    active_path = claude_dir / "procedural.md"
    archived = []
    for r in active:
        st = stats.get(r["slug"], {})
        if len(st.get("fired_sessions", [])) < min_fires or len(st.get("used_sessions", [])) > 0:
            continue
        block, remaining = extract_block(active_path.read_text(encoding="utf-8"), r["slug"])
        if not block:
            continue
        active_path.write_text(remaining.rstrip() + "\n", encoding="utf-8")
        append_block(claude_dir / "procedural_archive.md", set_status(block, "retired"), ARCHIVE_HEADER)
        archived.append(r["slug"])
    if archived:
        recompile(claude_dir)
        print(f"[PROCEDURAL-AUTO] Archived {len(archived)} dead recipe(s) (fired >= {min_fires}, never used): "
              f"{', '.join(archived)}. Kept in procedural_archive.md.")
    return 0


def cmd_archive(claude_dir: Path, slug: str) -> int:
    active_path = claude_dir / "procedural.md"
    if not active_path.is_file():
        print(f"[ERROR] No active recipes file at {active_path}")
        return 1
    block, remaining = extract_block(active_path.read_text(encoding="utf-8"), slug)
    if not block:
        print(f"[ERROR] No active recipe with slug '{slug}'.")
        return 1
    active_path.write_text(remaining.rstrip() + "\n", encoding="utf-8")
    append_block(claude_dir / "procedural_archive.md", set_status(block, "retired"), ARCHIVE_HEADER)
    print(f"[SUCCESS] Archived '{slug}' -> procedural_archive.md")
    recompile(claude_dir)
    return 0


# ============================================================
# CONFIDENCE / DECAY (read-only view of procedural_stats.json)
# ============================================================
def read_stats(claude_dir: Path) -> dict:
    path = claude_dir / "procedural_stats.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def recommend(entry: dict) -> str:
    fired = len(entry.get("fired_sessions", []))
    used = len(entry.get("used_sessions", []))
    conf = entry.get("confidence", 0.0)
    if used >= 2 and conf >= 0.3:
        return "strong - keep"
    if fired >= 3 and used == 0:
        return "STALE - recommend archive (fired, never used)"
    if fired >= 2 and conf < 0.15:
        return "decayed - consider archive"
    return "watch"


def cmd_stats(claude_dir: Path) -> int:
    active = read_recipes(claude_dir / "procedural.md")
    stats = read_stats(claude_dir)
    if not active:
        print("[INFO] No active recipes.")
        return 0
    print(f"{len(active)} active recipe(s) - confidence/decay (observe-only, never auto-tuned):\n")
    for r in active:
        st = stats.get(r["slug"], {})
        fired = len(st.get("fired_sessions", []))
        used = len(st.get("used_sessions", []))
        print(f"  [{r['slug']}]")
        print(f"      fired: {fired}  used: {used}  confidence: {st.get('confidence', 0.0)}  last_used: {st.get('last_used', '-')}")
        print(f"      -> {recommend(st)}\n")
    return 0


# ============================================================
# ENTRY POINT
# ============================================================
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="Procedural candidate curation gate (list/promote/reject)")
    parser.add_argument("--dir", default=str(Path.cwd() / ".claude"), help="Project .claude dir (default: <cwd>/.claude).")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="Show pending candidates with evidence + conflicts + recommendation.")
    p_promote = sub.add_parser("promote", help="Promote a candidate to active and recompile.")
    p_promote.add_argument("slug")
    p_reject = sub.add_parser("reject", help="Reject a candidate to the archive.")
    p_reject.add_argument("slug")
    p_auto = sub.add_parser("auto-promote", help="Promote all non-conflicting candidates past the evidence gate, unattended.")
    p_auto.add_argument("--min-sessions", type=int, default=AUTO_PROMOTE_MIN_SESSIONS,
                        help=f"Min source sessions to auto-promote (default: {AUTO_PROMOTE_MIN_SESSIONS}).")
    p_autoarch = sub.add_parser("auto-archive", help="Retire provably-dead active recipes (fired >= N, never used), unattended.")
    p_autoarch.add_argument("--min-fires", type=int, default=AUTO_ARCHIVE_MIN_FIRES,
                            help=f"Min fires with zero use before auto-archive (default: {AUTO_ARCHIVE_MIN_FIRES}).")
    sub.add_parser("stats", help="Show active recipes with fire/use confidence + archive recommendation.")
    p_archive = sub.add_parser("archive", help="Archive an ACTIVE recipe (procedural.md -> archive) and recompile.")
    p_archive.add_argument("slug")
    args = parser.parse_args()

    claude_dir = Path(args.dir)
    if args.action == "list":
        return cmd_list(claude_dir)
    if args.action == "promote":
        return cmd_move(claude_dir, args.slug, to_active=True)
    if args.action == "reject":
        return cmd_move(claude_dir, args.slug, to_active=False)
    if args.action == "auto-promote":
        return cmd_auto_promote(claude_dir, args.min_sessions)
    if args.action == "auto-archive":
        return cmd_auto_archive(claude_dir, args.min_fires)
    if args.action == "stats":
        return cmd_stats(claude_dir)
    if args.action == "archive":
        return cmd_archive(claude_dir, args.slug)
    return 1


if __name__ == "__main__":
    sys.exit(main())
