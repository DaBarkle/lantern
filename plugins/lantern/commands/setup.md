---
description: Walk through Lantern's setup again (it already happens by itself in the first session)
---
Lantern set itself up in its first session here. Offer to revisit any of these, do each one yourself with `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" ...`, and explain what each is for in one plain sentence before doing it:
1. How much of the machine Lantern may look at: `depth 2` (projects, tools) or `depth 3` (also containers, services, apps, SSH hosts, local servers). Changing it rescans at once.
2. Secrets: a raw secret never enters a command or note. `secret set NAME` stores one in the OS keychain; `secret run NAME -- <command>` passes it as an environment variable.
3. Approvals on the phone: `approve setup` prints the ntfy topic to subscribe to; `approve "<question>" --timeout 300` sends one. The same topic carries one line per watch that trips while no session is open (`alerts` shows and changes this; `schedule --apply` installs the hourly check).
4. Packs for what was found are already on (they switch on with discovery); `lantern pack` lists them, `pack on NAME` adds one.
5. Who the user is and what to be careful about: `remember --profile "<text>"` and `remember --rule "<text>"`.
Finish with `doctor` and say in one line whether everything is fine. Never ask the user to run a command.
