# @module: secrets_reader
# @role: the one reader for every secret this machine holds - here, the embedding API key - living
#        as `NAME=value` lines under `<config_dir>/tokens/`, a directory you gitignore whole so
#        nothing in it can ever sync anywhere
#
# NAMED `secrets_reader`, NOT `secrets`, ON PURPOSE
#   `secrets` is a Python standard-library module. Callers insert this directory at the FRONT of
#   the search path, so a file named secrets.py here would shadow the stdlib one for every module
#   in the process - a failure that appears far from its cause and long after the rename.
# @deps: none (stdlib only - this is imported by hooks on the hot path)
# @used-by: hooks/recall/semantic.py
#
# WHY THIS FILE EXISTS
#   Three modules read the same key and each carried its own copy of the reading logic, so they
#   drifted. Measured 2026-08-07: two of them read the config directory and the third read a path
#   relative to its own location - which, after that code moved, resolved to a file that exists
#   nowhere. Nothing reported it, because the machine it was written on had the key in an
#   environment variable and preferred its local model anyway. The machine where the key is the
#   ONLY embedding path would have hit it.
#
# WHY THE ENVIRONMENT IS NOT CONSULTED
#   An environment variable silently outranks a file and is invisible when you go looking for it.
#   That is precisely why replacing the key twice appeared to change nothing: the file was edited
#   and the stale variable kept winning. One place, readable with a text editor, is the whole
#   design. `tokens/` is where it lives.

import os
from pathlib import Path
from typing import Optional

TOKENS_DIRNAME = "tokens"


def config_dir() -> Path:
    """The Claude config directory, from the environment, else the conventional home location."""
    declared = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(declared) if declared else (Path.home() / ".claude")


def tokens_dir() -> Path:
    return config_dir() / TOKENS_DIRNAME


def read_secret(filename: str, name: str) -> Optional[str]:
    """Read `name=` from `<config_dir>/tokens/<filename>`. Missing file or key returns None."""
    path = tokens_dir() / filename
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def voyage_key() -> Optional[str]:
    """The Voyage embedding key, from `tokens/voyage.env` and nowhere else."""
    return read_secret("voyage.env", "VOYAGE_API_KEY")


def missing_key_message(what: str) -> str:
    """One wording for 'the key is not here', naming the file that has to exist."""
    return ("%s is not set: expected a line in %s. Keep that directory gitignored - it is "
            "machine-local on purpose, so a git pull can never bring the key with it."
            % (what, tokens_dir() / "voyage.env"))


if __name__ == "__main__":
    print("[secrets] tokens dir: %s" % tokens_dir())
    print("  %-28s %s" % ("voyage.env", "present" if voyage_key() else "ABSENT"))
    if os.environ.get("VOYAGE_API_KEY"):
        print("  [WARNING] VOYAGE_API_KEY is set in the environment - nothing reads it any "
              "more, but it will confuse whoever looks. Clear it.")
