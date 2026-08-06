#!/usr/bin/env bash
# Build the inkling MCP server locally if missing or stale.
# Called from SessionStart so each machine builds for its own arch and node version, exactly like
# build_hooks.sh does for the Go binaries. The config dir syncs SOURCES between machines; dist/ and
# node_modules/ are gitignored and rebuilt here, because carrying a build across two architectures
# is how a working tree starts serving a binary nobody on this machine compiled.
#
# Skips silently when node is absent. The MCP then fails to start, which the client shows as a
# disconnected server - a visible, correct signal. A missing toolchain is a setup error, not a
# runtime one, and papering over it here would hide it.

set -uo pipefail

MCP_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp"
[[ -d "$MCP_DIR" ]] || exit 0

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "[build_mcp] node/npm not found - skipping build" >&2
  exit 0
fi

cd "$MCP_DIR" || exit 0

if [[ ! -d node_modules ]]; then
  echo "[build_mcp] installing dependencies" >&2
  npm install --silent --no-audit --no-fund >/dev/null 2>&1 || {
    echo "[build_mcp] npm install failed" >&2
    exit 0
  }
fi

# Stale check by mtime, same rule build_hooks.sh applies to .go vs .exe: any source newer than the
# built entry point means the build is behind. Cheap, and it makes editing a .ts file enough.
needs_build=0
if [[ ! -f dist/index.js ]]; then
  needs_build=1
else
  for src in src/*.ts; do
    [[ -f "$src" ]] || continue
    [[ "$src" -nt dist/index.js ]] && needs_build=1 && break
  done
fi

if [[ "$needs_build" -eq 1 ]]; then
  echo "[build_mcp] rebuilding" >&2
  npm run build --silent >/dev/null 2>&1 || echo "[build_mcp] build FAILED - the server will not start" >&2
fi

exit 0
