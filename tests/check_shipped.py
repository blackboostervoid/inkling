# @module: check_shipped
# @role: asserts that everything the install contract names is actually tracked by git
# @deps: git, AGENTS.md, settings.example.json
# @used-by: CI, and by hand before publishing
"""
Verify the PUBLISHED tree, not the working directory.

A file can sit on disk, pass every test that reads it from disk, and still be missing
from the repository a stranger clones. That happened here: a `**/agent-memory/` line
inherited from a private config repo silently excluded the researcher's protocol, the
acceptance suite copied it from disk and reported success, and the published install
could not work.

So this check asks a different question from every other test: not "does it work" but
"would it still be there after a clone". It reads the install contract in AGENTS.md,
takes every source path it names, and requires git to be tracking it.

    python3 tests/check_shipped.py
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Rows of the install table: | `source/path` | `CONFIG/destination` |
TABLE_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", re.MULTILINE)
# Paths the hook commands invoke, e.g. "$CLAUDE_CONFIG_DIR/hooks/recall/lens_init.py"
HOOK_PATH = re.compile(r"\$(?:\{)?CLAUDE_CONFIG_DIR(?:\})?/([A-Za-z0-9_./-]+)")


def tracked_files() -> set:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True)
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def is_shipped(path: str, tracked: set) -> bool:
    """A directory ships when at least one file under it is tracked."""
    path = path.rstrip("/")
    return path in tracked or any(f.startswith(path + "/") for f in tracked)


def check_install_table(tracked: set) -> list:
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    missing = []
    for source, destination in TABLE_ROW.findall(text):
        if not source.startswith(("skills/", "agents/", "hooks/", "scripts/", "agent-memory/")):
            continue  # a row of some other table
        if not is_shipped(source, tracked):
            missing.append(f"AGENTS.md installs `{source}` -> `{destination}`, but git does not track it")
    return missing


def check_hook_commands(tracked: set) -> list:
    settings = json.loads((REPO / "settings.example.json").read_text(encoding="utf-8"))
    missing = []
    for event, matchers in settings.get("hooks", {}).items():
        for matcher in matchers:
            for hook in matcher.get("hooks", []):
                for rel in HOOK_PATH.findall(hook.get("command", "")):
                    # Binaries are built at session start from tracked source; the source
                    # is what has to ship, so a missing .exe is expected and fine.
                    if rel.endswith(".exe"):
                        rel = Path(rel).parent.as_posix() + "/main.go"  # git stores forward slashes
                    if not is_shipped(rel, tracked):
                        missing.append(f"{event} hook runs `{rel}`, but git does not track it")
    return missing


def check_go_formatting(tracked: set) -> list:
    """gofmt the bytes GIT stores, not the bytes on disk.

    Those differ. A working copy on Windows holds CRLF while the repository holds LF,
    and gofmt reads the two differently: a local `gofmt -l` came back clean while CI,
    running on the checked-out LF form, refused the same file. Checking the blob is the
    only way for a local run to mean what the CI run means."""
    problems = []
    for rel in sorted(f for f in tracked if f.endswith(".go")):
        blob = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=REPO,
                              capture_output=True, check=False)
        if blob.returncode != 0:
            continue  # not committed yet; the next commit is what CI will judge
        with tempfile.NamedTemporaryFile(suffix=".go", delete=False) as tmp:
            tmp.write(blob.stdout)
            tmp_path = tmp.name
        try:
            out = subprocess.run(["gofmt", "-l", tmp_path], capture_output=True, text=True, check=False)
            if out.returncode != 0:
                problems.append(f"gofmt could not read `{rel}`: {out.stderr.strip()[:80]}")
            elif out.stdout.strip():
                problems.append(f"`{rel}` is not gofmt-clean as stored in git; run gofmt -w on it")
        except FileNotFoundError:
            return []  # no Go toolchain here; CI has one and will judge it
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    return problems


def check_referenced_docs(tracked: set) -> list:
    """Docs the contract tells the reader to open must exist in the clone too."""
    missing = []
    for name in ("VAULT_CONTRACT.md", "RECIPE_FORMAT.md", "README.md", "LICENSE",
                 "settings.example.json", "tests/acceptance.py"):
        if name not in tracked:
            missing.append(f"`{name}` is referenced as part of the repository but is not tracked")
    return missing


def main() -> int:
    print("=" * 68)
    print("  SHIPPED-TREE CHECK - what a clone would actually contain")
    print("=" * 68)
    tracked = tracked_files()
    print(f"[INFO] {len(tracked)} files tracked by git")

    problems = (check_install_table(tracked) + check_hook_commands(tracked)
                + check_referenced_docs(tracked) + check_go_formatting(tracked))
    if problems:
        print(f"[ERROR] {len(problems)} promise(s) the repository does not keep:")
        for p in problems:
            print(f"   - {p}")
        return 1

    print("[SUCCESS] every path the install contract names is in the repository")
    return 0


if __name__ == "__main__":
    sys.exit(main())
