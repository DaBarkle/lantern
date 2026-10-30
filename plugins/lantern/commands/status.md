---
description: Is Lantern working? One plain line, and what to do if not
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" status`. Give the user that line in plain words. If it says "needs you", also run `... lantern.py doctor` and explain the fix for each problem in one or two sentences each, as steps they can follow without knowing how Lantern works inside.
To keep this line always visible, the user can add it to Claude Code's status line in settings: `"statusLine": {"type": "command", "command": "uv run --no-project --quiet <plugin root>/lantern.py status"}`.
