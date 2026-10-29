---
description: Choose how much of this machine Lantern may look at (1 conversations, 2 workspace, 3 home, 4 life)
argument-hint: "[1-4]"
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" depth $ARGUMENTS` and tell the user the result in one line.
If no level was given, show the four levels from the output and ask which one they want. Each level is a consent
boundary: never raise it without the user choosing.
