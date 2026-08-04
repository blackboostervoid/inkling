#!/usr/bin/env bash
# Build all Go hook binaries locally if missing or stale.
# Called from SessionStart so each machine gets a binary built for its own arch.
# Skips silently if Go is not installed — hook calls will then fail loudly,
# which is the right signal (missing toolchain is a setup error, not a runtime one).

set -uo pipefail

REPO_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
HOOKS_DIR="$REPO_DIR/hooks"

if ! command -v go >/dev/null 2>&1; then
  echo "[build_hooks] go toolchain not found — skipping rebuild" >&2
  exit 0
fi

rebuilt=0
for main_go in "$HOOKS_DIR"/*/main.go; do
  [[ -f "$main_go" ]] || continue
  hook_dir="$(dirname "$main_go")"
  go_mod="$hook_dir/go.mod"
  [[ -f "$go_mod" ]] || continue
  # Exe name comes from `module <name>` in go.mod, NOT the directory name
  # (e.g. dir "recall" but module "recall_scanner").
  hook_name="$(awk '/^module / {print $2; exit}' "$go_mod")"
  [[ -n "$hook_name" ]] || continue
  # The .exe suffix is used on EVERY platform on purpose. Hook commands live in
  # settings.json as plain strings with no way to branch per OS, so one uniform
  # binary name is what keeps a single config working on Windows, Linux and Mac.
  exe="$hook_dir/${hook_name}.exe"

  # Rebuild if exe missing, or any .go file is newer than the exe.
  needs_build=0
  if [[ ! -f "$exe" ]]; then
    needs_build=1
  else
    while IFS= read -r -d '' src; do
      if [[ "$src" -nt "$exe" ]]; then
        needs_build=1
        break
      fi
    done < <(find "$hook_dir" -maxdepth 1 -name '*.go' -print0)
  fi

  if [[ "$needs_build" -eq 1 ]]; then
    (cd "$hook_dir" && go build -o "${hook_name}.exe" . 2>&1) && {
      echo "[build_hooks] rebuilt: $hook_name"
      rebuilt=$((rebuilt + 1))
    } || {
      echo "[build_hooks] FAILED: $hook_name" >&2
    }
  fi
done

exit 0
