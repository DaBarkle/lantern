---
description: Find and drop something Lantern remembers (a rule, fact or note)
argument-hint: "<words from the memory to drop>"
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" forget $ARGUMENTS`. Show the matches with their numbers and ask which to drop; then run `... lantern.py forget --id N` for each one the user picks. Never drop anything the user did not pick.
