# @module: recipe_blocks
# @role: the recipe block format itself - how procedural.md and procedural_candidates.md split into
#        recipes, and which declared recipes no reader can see
# @deps: (stdlib only - no main, no file I/O: infrastructure every procedural reader imports)
# @used-by: hooks/recall/compile_procedural.py, skills/procedural/scripts/lint_procedural.py, skills/procedural/scripts/distill.py
# @doc: RECIPE_FORMAT.md#one-recipe
"""One grammar for every reader of the recipe files.

Three private copies of a positional regex - in the compiler, in lint and in the distiller - were
how one malformed block hid a month of recipes from all of them at once (measured 2026-09-14). The
format lives here, apart from any entry point, so a reader imports a format and not a hook.

    ---
    slug: <stable-id>
    trigger: <one-line situation>
    keywords: [a, b, c]
    status: active | candidate | retired | rejected
    source_sessions: [a, b]
    ---
    1. step
    Why: <causation; the first Why line feeds the matchable summary>
    Anti-pattern: <what not to do>
"""
import re

ACTIVE_STATUS = "active"
DELIMITER = re.compile(r"^---[ \t]*$", re.MULTILINE)
FRONTMATTER_LINE = re.compile(r"^[A-Za-z_][\w-]*:")
# Every way a slug or a candidate status has been written by hand, so a block no reader parses is
# still COUNTED: plain, indented, capitalised, spaced before the colon, under a heading, as a
# bullet, in bold. Code fences are skipped, so an example inside a recipe body is not a recipe.
SLUG_DECLARATION = re.compile(r"^[ \t>*#-]*\**slug\**[ \t]*:\**[ \t]*(\S*)",
                              re.MULTILINE | re.IGNORECASE)
CANDIDATE_FIELD = re.compile(r"^[ \t>*#-]*\**status\**[ \t]*:\**[ \t]*candidate\b",
                             re.MULTILINE | re.IGNORECASE)
FENCE = re.compile(r"^[ \t]*```", re.MULTILINE)
STRAY_FIELD_REACH = 8   # lines: a stray status this close below a lost slug is the same block
WHY_PATTERN = re.compile(r"^\s*Why:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


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


def is_compilable(fm: dict) -> bool:
    """Whether the compiler emits this block: a slug, a trigger, and the active status."""
    return bool(fm.get("slug", "").strip() and fm.get("trigger", "").strip()) \
        and fm.get("status", "").lower() == ACTIVE_STATUS


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_frontmatter(head: str) -> bool:
    """True when every non-blank line is `key: value` and one of them names a slug."""
    lines = [line for line in head.splitlines() if line.strip()]
    if not lines or not all(FRONTMATTER_LINE.match(line) for line in lines):
        return False
    return any(line.lower().startswith("slug:") and line.partition(":")[2].strip() for line in lines)


def parse_blocks(text: str) -> list:
    """-> [(start, end, frontmatter, body)] for every recipe block, in file order.

    `[!]` A `---` LINE OPENS A BLOCK BY WHAT FOLLOWS IT, NOT BY ITS POSITION. The pattern this
    replaced paired each `---` with the next one, so one block written without frontmatter flipped
    every later pair. Measured 2026-09-14: one project's file declared 43 recipes and compiled 14,
    because a hand merge on 2026-08-13 left a body with no frontmatter and every recipe
    auto-promote appended after it landed where no reader looked; another declared 10 and
    compiled 7. Now a delimiter opens a block only when the lines up to the next delimiter are
    `key: value` lines naming a slug; any other `---` stays inside the body it sits in.
    `start`/`end` span the whole block, so text surgery can cut it out by slice - and a block no
    reader parses sits INSIDE the span above it, which is why unreadable_text exists.
    """
    marks = list(DELIMITER.finditer(text))
    openers, i = [], 0
    while i + 1 < len(marks):
        head = text[marks[i].end() + 1:marks[i + 1].start()]
        if _is_frontmatter(head):
            openers.append((marks[i].start(), head, min(marks[i + 1].end() + 1, len(text))))
            i += 2
        else:
            i += 1
    blocks = []
    for k, (start, head, body_start) in enumerate(openers):
        end = openers[k + 1][0] if k + 1 < len(openers) else len(text)
        blocks.append((start, end, head, text[body_start:end]))
    return blocks


def _outside(spans: list, offset: int) -> bool:
    return not any(start <= offset < end for start, end in spans)


def unreadable_text(text: str) -> tuple:
    """-> (['slug@line', ...], ['line N', ...]): slug declarations and candidate status lines that
    sit outside every parsed frontmatter and outside code fences.

    The count that was missing for a month: "Compiled 14" printed every session beside a file of
    43, and nothing compared the two. A slug declared outside a readable block is a recipe that is
    never compiled, never surfaced and never archivable by name, so it is reported, not skipped.
    A stray `status: candidate` with no slug near it is a candidate written as bullets under a
    heading - the shape sessions answering the Stop-hook ask produced, which never promotes.
    """
    frontmatters = [(start, end - len(body)) for start, end, _, body in parse_blocks(text)]
    fences = [m.start() for m in FENCE.finditer(text)]
    fenced = list(zip(fences[0::2], fences[1::2] + [len(text)] * (len(fences) % 2)))

    def stray(match) -> bool:
        return _outside(frontmatters, match.start()) and _outside(fenced, match.start())

    lost_lines, lost = [], []
    for match in filter(stray, SLUG_DECLARATION.finditer(text)):
        line = line_of(text, match.start())
        lost_lines.append(line)
        lost.append(f"{match.group(1) or '<empty>'}@{line}")
    fields = [f"line {line_of(text, m.start())}" for m in filter(stray, CANDIDATE_FIELD.finditer(text))
              if not any(0 <= line_of(text, m.start()) - line <= STRAY_FIELD_REACH for line in lost_lines)]
    return lost, fields


def unparsed_slugs(text: str) -> list:
    """-> ['slug@line', ...] for every slug declared outside a readable block (see unreadable_text)."""
    return unreadable_text(text)[0]


def uncompiled_blocks(text: str) -> list:
    """-> ['slug (why)', ...] for parsed blocks the compiler skips: no trigger, or not active.
    Parsed is not compiled - a promotion that checks only the slug reports success for these."""
    out = []
    for _, _, head, _ in parse_blocks(text):
        fm = parse_frontmatter(head)
        if not fm.get("trigger", "").strip():
            out.append(f"{fm.get('slug', '')} (no trigger)")
        elif fm.get("status", "").lower() != ACTIVE_STATUS:
            out.append(f"{fm.get('slug', '')} (status '{fm.get('status', '')}')")
    return out


def duplicate_slugs(text: str) -> list:
    """-> slugs carried by more than one parsed block. Archive and promote act on the FIRST only."""
    seen, dupes = set(), []
    for _, _, head, _ in parse_blocks(text):
        slug = parse_frontmatter(head).get("slug", "")
        if slug in seen and slug not in dupes:
            dupes.append(slug)
        seen.add(slug)
    return dupes


def span_hides(text: str, start: int, end: int) -> list:
    """-> the unreadable items (slug@line / line N) that sit inside text[start:end].

    A block no reader parses becomes body text of the block above it, so moving that block by its
    span would carry a whole hidden recipe along: archiving a dead recipe once retired a live
    neighbour this way and the loss report went quiet."""
    first, last = line_of(text, start), line_of(text, end)
    lost, fields = unreadable_text(text)
    return [item for item in lost + fields if first <= int(re.search(r"(\d+)$", item).group(1)) < last]
