---
description: Check that every part of Lantern is doing its job, and show anything on Stage
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" doctor`, then `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" stage list`.
Report problems first, in plain words, with the fix for each. If everything is ok, say so in one line.
