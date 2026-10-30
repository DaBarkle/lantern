---
description: Set Lantern up on this machine in a short conversation (first run, or any time)
---
You are onboarding the user to Lantern. Do it as a short conversation, never as a settings lecture. Steps, in order, each confirmed with the user before the next:

1. Say what Lantern does in two sentences: it remembers your sessions and systems, brings the right context into every prompt, learns your rules the moment you state them, and checks itself daily.
2. Ask how much of this machine Lantern may look at, in plain words: just conversations (1), your projects and dev tools (2, the default), also containers, services and local servers (3), or also accounts you connect through Claude (4). Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" depth N`.
3. Run `... lantern.py discover` and show what was found in plain words, grouped (projects, tools, services). Ask if anything should be left out, then `... lantern.py discover --accept all` (or the numbers they chose). Mention any pack that adds skills for what was found and offer to install it with `claude plugin install <pack>@lantern`.
4. Ask whether they want approvals on their phone. If yes, run `... lantern.py approve setup`, tell them to install the ntfy app and subscribe to the topic shown, then send one test with `... lantern.py approve "Lantern test: tap Approve" --timeout 300` and confirm it came back approved.
5. Permissions: read the user's Claude Code permission settings with the Read tool (user settings and this project's settings). Explain in one paragraph what Lantern needs and already has by being a plugin: its hooks run automatically; its memory server runs in the background; nothing else. If the user has `deny` rules that block `uv` or the plugin folder, say which line and ask before suggesting a change; never edit settings yourself.
6. Run `... lantern.py doctor` and `... lantern.py status`. If every check is ok, say Lantern is ready and that nothing else needs doing: it learns and checks itself daily. If something is not ok, fix what you can and otherwise give one clear step.
7. Offer the always-on status line (one setting, shown in /lantern:status) and `/lantern:profile` to see what Lantern knows, as optional extras.
