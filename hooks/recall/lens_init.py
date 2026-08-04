#!/usr/bin/env python3
"""
lens_init.py — idempotently creates lens.yaml at user and project level.
Runs as a SessionStart hook. Never overwrites existing files.
"""

import os
import sys
from pathlib import Path

USER_TEMPLATE = """\
# Recall lens — user-level defaults. Applies to all projects.
# Project override: <cwd>/.claude/lens.yaml (merged on top of this file)
#
# Levers:
#   boost:    lower match threshold by 1 for entries tagged with any listed tag
#   suppress: raise threshold by 3 for the named entry (effectively mutes it)
#   bridge:   if key appears in prompt, its tags expand the match pool

# boost:
#   - orchestrator
#   - akasha

# suppress:
#   - outdated-note-name

# bridge:
#   debugging: [silent-failure-patterns, log-reader-tricks]
"""

PROJECT_TEMPLATE = """\
# Recall lens — project-level override for: {project_name}
# Merged on top of user-level $CLAUDE_CONFIG_DIR/lens.yaml.
# Only specify what differs from user-level defaults.
#
# Levers:
#   boost:    lower match threshold by 1 for entries tagged with any listed tag
#   suppress: raise threshold by 3 for the named entry (effectively mutes it)
#   bridge:   if key appears in prompt, its tags expand the match pool

# boost:
#   - topic-tag

# suppress:
#   - irrelevant-note-name

# bridge:
#   key-concept: [related-tag-1, related-tag-2]
"""


def main():
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    cwd = Path.cwd()

    user_lens = config_dir / "lens.yaml"
    if not user_lens.exists():
        user_lens.write_text(USER_TEMPLATE, encoding="utf-8")
        print(f"[lens_init] Created user lens: {user_lens}", file=sys.stderr)

    project_claude = cwd / ".claude"
    if project_claude.is_dir():
        project_lens = project_claude / "lens.yaml"
        if not project_lens.exists():
            content = PROJECT_TEMPLATE.format(project_name=cwd.name)
            project_lens.write_text(content, encoding="utf-8")
            print(f"[lens_init] Created project lens: {project_lens}", file=sys.stderr)


if __name__ == "__main__":
    main()
