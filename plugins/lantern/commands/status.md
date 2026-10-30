---
description: Is Lantern working? One plain line, and what to do if not
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" status`. Give the user that line in plain words. If it says "needs you", also run `... lantern.py doctor` and explain the fix for each problem in one or two sentences each, as steps they can follow without knowing how Lantern works inside.
This line is already on screen every turn: Lantern put it in Claude Code's status line (in a terminal) or under each reply (elsewhere) by itself; nothing to set up. If the doctor says the status line is not set up, give its reason in plain words.
