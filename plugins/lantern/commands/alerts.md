---
description: Phone alerts when a watch trips while you are away (failed service, service down, disk short, backup missed or failed)
---
Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" alerts` and say its line in plain words: whether alerts are on, which kinds buzz the phone, and when the watches last ran.
Then ask one structured question (AskUserQuestion) with options: keep as is, turn off, turn on, choose kinds (multi-select from failed, down, disk, backup). Apply the answer yourself with `alerts on`, `alerts off` or `alerts kinds a,b` and confirm in one line.
If the line says phone approvals are needed first, offer `approve setup` and show the two ntfy steps; never ask the user to run a command or edit a file.
The hourly check installs itself (user-scope timer, read-only checks) once phone approvals exist; if the line says it is not scheduled here, the OS scheduler could not be reached from this session and `schedule --apply` from a shell on the machine itself installs it.
