---
description: Phone alerts when a watch trips while you are away (failed service, service down, disk short, backup missed or failed)
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" alerts` and say its line in plain words: whether alerts are on, which kinds buzz the phone, and when the watches last ran.
Then ask one structured question (AskUserQuestion) with options: keep as is, turn off, turn on, choose kinds (multi-select from failed, down, disk, backup). Apply the answer yourself with `alerts on`, `alerts off` or `alerts kinds a,b` and confirm in one line.
If the line says phone approvals are needed first, offer `approve setup` and show the two ntfy steps; never ask the user to run a command or edit a file.
If the last check is more than two hours old and no session was open, the hourly watch is not scheduled: offer `schedule --apply` (user-scope timer, read-only checks) and run it if they say yes.
