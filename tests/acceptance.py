# @module: acceptance
# @role: proves an inkling install actually works, end to end, in a throwaway sandbox
# @deps: the repo itself; a Go toolchain for the hook binaries
# @used-by: run by hand after installing, and before trusting a change to the rails
"""
Acceptance run for inkling.

Builds a complete throwaway installation - config dir, vault, project - from THIS
repository, then exercises every joint of both rails and reports one line per check.
Nothing outside the temporary directory is touched, and your real vault is never read.

    python3 tests/acceptance.py          # run, then delete the sandbox
    python3 tests/acceptance.py --keep   # leave the sandbox in place to poke at it

A check that raises is a FAILED check, never a skipped one: the whole point is that a
broken joint cannot pass quietly.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

results = []


def check(name, passed, evidence):
    results.append((name, passed, evidence))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"         {evidence}")


class Sandbox:
    """A complete inkling installation in a temporary directory."""

    def __init__(self, root: Path):
        self.root = root
        self.config = root / "config"
        self.vault = root / "vault"
        self.project = root / "project"
        self.transcript = root / "transcript.jsonl"

    def build(self):
        for d in (self.vault / "patterns", self.vault / "raw", self.project / ".claude"):
            d.mkdir(parents=True, exist_ok=True)
        self.config.mkdir(parents=True, exist_ok=True)
        for item in ("hooks", "skills", "agents", "agent-memory", "scripts"):
            source = REPO / item
            if source.is_dir():
                shutil.copytree(source, self.config / item)
        self._write_notes()
        self._write_recipe()

    def _write_notes(self):
        (self.vault / "patterns" / "circuit-breaker.md").write_text(
            "---\n"
            "type: pattern\n"
            "tags: [circuit-breaker, retry, resilience, timeout, outage]\n"
            "summary: >\n"
            "  A retry without a circuit breaker turns one slow dependency into a full outage:\n"
            "  every caller piles on while the callee is already saturated. Break the circuit\n"
            "  after N consecutive failures and probe with a single request.\n"
            "---\n\n# Circuit breaker\n\nThe body is never read by retrieval.\n",
            encoding="utf-8")
        (self.vault / "patterns" / "yaml-booleans.md").write_text(
            "---\n"
            "type: discovery\n"
            "tags: [yaml, config, parsing, gotcha]\n"
            "summary: PyYAML defaults to YAML 1.1, so off/on/yes/no parse as booleans unless quoted.\n"
            "---\n\n# YAML booleans\n",
            encoding="utf-8")
        # Staging material: tagged, yet must stay out of the corpus because it lives in raw/.
        (self.vault / "raw" / "unprocessed.md").write_text(
            "---\ntags: [should, never, be, indexed]\nsummary: staging material\n---\n\nraw\n",
            encoding="utf-8")

    def _write_recipe(self):
        (self.project / ".claude" / "procedural.md").write_text(
            "# Procedural memory\n\n"
            "---\n"
            "slug: rebuild-index-after-editing-notes\n"
            "trigger: an edited note does not show up in recall\n"
            "keywords: [index, rebuild, search, notes, recall]\n"
            "status: active\n"
            "---\n"
            "1. Edit the note.\n"
            "2. Run the index compiler.\n"
            "3. Confirm the entry count changed.\n"
            "Why: an edited note stays invisible until the index is recompiled, so the symptom\n"
            "looks like a retrieval bug when it is a stale build.\n",
            encoding="utf-8")

    # -------------------------------------------------------------- plumbing

    def env(self, **overrides):
        e = dict(os.environ)
        e["CLAUDE_CONFIG_DIR"] = str(self.config)
        e["CLAUDE_WIKI_PATH"] = str(self.vault) + "/"
        e["PYTHONIOENCODING"] = "utf-8"
        for key, value in overrides.items():
            if value is None:
                e.pop(key, None)
            else:
                e[key] = value
        return e

    def run(self, cmd, stdin=None, **envkw):
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=str(self.project),
                              env=self.env(**envkw))

    def hook_input(self, prompt):
        return json.dumps({"prompt": prompt, "session_id": "acceptance-0001",
                           "transcript_path": str(self.transcript), "cwd": str(self.project)})

    @staticmethod
    def context_of(result):
        out = result.stdout.strip()
        if not out:
            return ""
        try:
            return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
        except ValueError:
            return out

    def bin(self, rail):
        return self.config / "hooks" / rail / ("recall_scanner.exe" if rail == "recall"
                                               else "capture_scanner.exe")

    def script(self, *parts):
        return str(self.config.joinpath(*parts))


# -------------------------------------------------------------------- checks

def check_build(sb):
    r = subprocess.run(["bash", str(sb.config / "hooks" / "build_hooks.sh")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=sb.env())
    ok = sb.bin("recall").is_file() and sb.bin("capture").is_file()
    check("binaries build from source alone", ok,
          f"recall={sb.bin('recall').is_file()} capture={sb.bin('capture').is_file()} rc={r.returncode}"
          + ("" if ok else " - is a Go toolchain installed?"))


def check_compile(sb):
    sb.run([PYTHON, sb.script("hooks", "recall", "compile_wiki_index.py")])
    index = sb.config / "hooks" / "recall" / "index_compiled.json"
    entries = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else []
    names = sorted(e["name"] for e in entries)
    check("index compiles from frontmatter, raw/ excluded",
          names == ["circuit-breaker", "yaml-booleans"], f"entries={names}")


def check_recall_fires(sb):
    ctx = sb.context_of(sb.run([str(sb.bin("recall"))],
                               stdin=sb.hook_input("we keep hammering a slow service with retry logic")))
    check("recall fires on a tagged prompt",
          "[RECALL]" in ctx and "circuit-breaker" in ctx,
          (ctx.strip().splitlines() or ["<silence>"])[-1][:120])


def check_recall_silent(sb):
    ctx = sb.context_of(sb.run([str(sb.bin("recall"))],
                               stdin=sb.hook_input("what is the weather in Lisbon tomorrow")))
    check("recall stays silent on an unrelated prompt", "[RECALL]" not in ctx,
          f"emitted {len(ctx)} chars")


def check_procedural(sb):
    sb.run([PYTHON, sb.script("hooks", "recall", "compile_procedural.py"),
            "--dir", str(sb.project / ".claude")])
    ctx = sb.context_of(sb.run([str(sb.bin("recall"))],
                               stdin=sb.hook_input("how do I rebuild the search index after editing notes")))
    check("procedural rail fires from the same binary", "[PROCEDURAL]" in ctx,
          (ctx.strip().splitlines() or ["<silence>"])[-1][:120])


def check_recipe_format_warning(sb):
    """A file with content and no valid recipe must SAY so - silence here is how a
    hand-written procedural.md stays invisible for weeks."""
    recipe = sb.project / ".claude" / "procedural.md"
    good = recipe.read_text(encoding="utf-8")
    recipe.write_text("# Procedural memory\n\n## a recipe in the wrong shape\nsteps\n", encoding="utf-8")
    r = sb.run([PYTHON, sb.script("hooks", "recall", "compile_procedural.py"),
                "--dir", str(sb.project / ".claude")])
    recipe.write_text(good, encoding="utf-8")
    sb.run([PYTHON, sb.script("hooks", "recall", "compile_procedural.py"),
            "--dir", str(sb.project / ".claude")])
    check("a malformed recipe file warns instead of passing quietly",
          "0 active recipes" in r.stderr, r.stderr.strip()[:120] or "<no warning>")


def check_rail_broken(sb):
    index = sb.config / "hooks" / "recall" / "index_compiled.json"
    good = index.read_text(encoding="utf-8")
    index.write_text("{ this is not json", encoding="utf-8")
    ctx = sb.context_of(sb.run([str(sb.bin("recall"))],
                               stdin=sb.hook_input("retry logic and circuit breakers")))
    index.write_text(good, encoding="utf-8")
    check("a broken corpus speaks instead of going quiet", "[RAIL-BROKEN]" in ctx,
          (ctx.strip().splitlines() or ["<silence>"])[0][:120])


def check_unconfigured(sb):
    r = sb.run([PYTHON, sb.script("hooks", "recall", "compile_wiki_index.py")], CLAUDE_WIKI_PATH=None)
    check("unconfigured vault says so and exits clean",
          r.returncode == 0 and "CLAUDE_WIKI_PATH is not set" in r.stderr,
          f"rc={r.returncode} {r.stderr.strip()[:90]}")


def check_capture(sb):
    ctx = sb.context_of(sb.run([str(sb.bin("capture"))],
                               stdin=sb.hook_input("we decided to go with the circuit breaker instead of more retries")))
    check("capture stages a durable moment", "[CAPTURE]" in ctx,
          (ctx.strip().splitlines() or ["<silence>"])[0][:120])


def check_evidence(sb):
    sb.transcript.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "[RECALL-USED] circuit-breaker - named the failure mode before we retried harder"}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    sb.run([PYTHON, sb.script("hooks", "recall", "recall_evidence.py")],
           stdin=json.dumps({"session_id": "acceptance-0001",
                             "transcript_path": str(sb.transcript), "cwd": str(sb.project)}))
    note = (sb.vault / "patterns" / "circuit-breaker.md").read_text(encoding="utf-8")
    grew = len(note.splitlines()) > 12
    check("evidence is written back into the note it helped", grew,
          "note gained an applied-log section" if grew else "note unchanged")


def _usage_line(tokens, model):
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "model": model,
        "content": [{"type": "text", "text": "work"}],
        "usage": {"input_tokens": 1000, "cache_read_input_tokens": tokens - 1000,
                  "cache_creation_input_tokens": 0}}}, ensure_ascii=False)


def _context_run(sb, tokens, model):
    sb.transcript.write_text(_usage_line(tokens, model) + "\n", encoding="utf-8")
    return sb.context_of(sb.run([PYTHON, sb.script("hooks", "capture", "capture_inbox.py"),
                                 "--mode=context"], stdin=sb.hook_input("continue")))


def _clear_context_cooldown(sb):
    for marker in (sb.config / "hooks" / "capture").glob("capture_ctx_*.json"):
        marker.unlink()


def check_context_quiet_below(sb):
    _clear_context_cooldown(sb)
    ctx = _context_run(sb, 40_000, "claude-sonnet-5")
    check("context arm stays quiet below the threshold", "[CAPTURE-DUE]" not in ctx,
          f"emitted {len(ctx)} chars at 40k of 200k")


def check_context_fires(sb):
    _clear_context_cooldown(sb)
    ctx = _context_run(sb, 170_000, "claude-sonnet-5")
    fired = "[CAPTURE-DUE]" in ctx and "85%" in ctx and "in this order" in ctx
    check("context arm fires with a percentage AND a decision rule", fired,
          (ctx.splitlines() or ["<silence>"])[0][:120])


def check_context_once(sb):
    """Second prompt in the same session must not repeat the warning."""
    ctx = _context_run(sb, 180_000, "claude-sonnet-5")
    check("context arm warns once per session", "[CAPTURE-DUE]" not in ctx,
          f"repeat emitted {len(ctx)} chars")


def check_context_window_from_model(sb):
    """The regression test for a real incident: 170k tokens is 85% of a 200k window and
    17% of a 1M one. Reading the window off the model id is what keeps a big-window
    session from being warned - and nagged - at a sixth of its capacity."""
    _clear_context_cooldown(sb)
    big = _context_run(sb, 170_000, "some-future-model[1m]")
    quiet_on_1m = "[CAPTURE-DUE]" not in big
    _clear_context_cooldown(sb)
    small = _context_run(sb, 170_000, "claude-sonnet-5")
    loud_on_200k = "[CAPTURE-DUE]" in small
    check("the same token count fires at 200k and stays quiet at 1M",
          quiet_on_1m and loud_on_200k,
          f"1M model quiet={quiet_on_1m}; 200k model warned={loud_on_200k}")


def check_context_unknown_window(sb):
    """An unrecognised model must not get a confident percentage. Prose gets judged;
    a figure gets used, so a made-up figure is worse than an admitted gap."""
    _clear_context_cooldown(sb)
    ctx = _context_run(sb, 170_000, "unlabelled-model-x")
    honest = "assumed" in ctx and "%" not in ctx
    check("an assumed window is declared instead of a made-up percentage", honest,
          (ctx.splitlines() or ["<silence>"])[0][:130])


def check_lens(sb):
    sb.run([PYTHON, sb.script("hooks", "recall", "lens_init.py")])
    user = (sb.config / "lens.yaml").is_file()
    project = (sb.project / ".claude" / "lens.yaml").is_file()
    check("lens files are seeded at both levels", user or project,
          f"user={user} project={project}")


CHECKS = (check_build, check_compile, check_recall_fires, check_recall_silent,
          check_procedural, check_recipe_format_warning, check_rail_broken,
          check_unconfigured, check_capture, check_context_quiet_below,
          check_context_fires, check_context_once, check_context_window_from_model,
          check_context_unknown_window,
          check_evidence, check_lens)


def main() -> int:
    parser = argparse.ArgumentParser(description="Acceptance run for an inkling install")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox directory")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="inkling-acceptance-"))
    sandbox = Sandbox(root)
    print("=" * 68)
    print("  INKLING ACCEPTANCE RUN")
    print("=" * 68)
    print(f"[INFO] sandbox at {root}")
    try:
        sandbox.build()
        for fn in CHECKS:
            try:
                fn(sandbox)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
        passed = sum(1 for _, ok, _ in results if ok)
        print("=" * 68)
        print(f"  {passed}/{len(results)} checks passed")
        print("=" * 68)
        return 0 if passed == len(results) else 1
    finally:
        if args.keep:
            print(f"[INFO] sandbox kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
