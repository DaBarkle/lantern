---
description: Store a fact or preference in Lantern's memory so every later session knows it
argument-hint: "<what to remember>"
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" remember $ARGUMENTS` and confirm in one line. If it refused because the text looks like a secret, say so and point to `lantern secret set`.
