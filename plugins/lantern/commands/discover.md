---
description: Find what is on this machine (up to the chosen depth) and choose what Lantern should remember
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" discover`. Show the user the finds as a short
checklist grouped by kind, and ask which to remember. Then run the same command with `--accept` and their picks
(`all` or numbers like `1,3`). Scouting is read-only; remembering only writes to Lantern's own memory.
