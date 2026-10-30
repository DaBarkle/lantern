# Platforms

What Lantern needs, what it does on each kind of machine, and how much of that has been seen on real hardware.
Audit of 2026-10-31 over `plugins/lantern` (core, hooks, packs). CI runs the unit tests on Ubuntu, macOS and Windows
runners; everything marked *mocked* has only been exercised that way.

## The one requirement

[uv](https://docs.astral.sh/uv/) on PATH. The hooks are `uv run --no-project lantern.py ...`; uv fetches Python 3.11
when the machine has none (network needed once). Without uv no hook runs and Claude Code reports each as failed; there
is nothing Lantern can do about its own interpreter being absent, so the README says it first.

## Status by concern

Works = does the job; degrades = keeps going with a plain message in the session; breaks = a prompt is blocked or a
traceback is shown. After this audit nothing known breaks except the missing-uv case above.

| Concern | Linux (systemd desktop) | Linux without systemd user session (servers over SSH, WSL, Alpine, containers) | macOS | Windows | Where verified |
|---|---|---|---|---|---|
| Hooks and memory (SQLite FTS5) | works | works | works | works | Linux live; macOS and Windows CI |
| A hook crashing on an unforeseen platform | degrades: logged to `errors.jsonl`, one line in the session per hour, doctor check `hook-errors` | same | same | same | mocked |
| Data folder | `$XDG_DATA_HOME/lantern`, mode 0700 | same | `~/Library/Application Support/lantern` | `%APPDATA%\lantern` | Linux live; defaults mocked |
| `CLAUDE_CONFIG_DIR` | honoured for skills, status line, project transcripts | same | same | same | mocked |
| Paths with spaces, Windows separators | works (argv arrays, quoted status-line command, `pathlib`) | | | works | mocked |
| Discovery: services, timers, failed units, backups | systemd | silent: `systemctl` errors are swallowed, no finds | launchd agents/daemons | services via PowerShell, tasks via `schtasks` | Linux live; mac/win parsers mocked |
| Discovery: apps | Flatpak | none | Homebrew casks | winget (English column headers only; other locales yield nothing) | Linux live; others mocked |
| Discovery: containers | docker, podman | same | same | same | Linux live |
| Tool output decoding | UTF-8 with replacement | same | same | same (console code page no longer matters) | mocked |
| Scheduled jobs | systemd user timers | degrades: `ensure_scheduled` falls back to session-start and nightly checks; status says "not scheduled here" | launchd | Task Scheduler | Linux live; others mocked, no cron fallback yet |
| Jobs started from a session | detached `uv run` (uv found via `$UV`, then PATH) | same | same | `DETACHED_PROCESS` | Linux live |
| Memory server (fastembed, onnxruntime) | works; exits after 30 min idle | works | works on Intel and Apple silicon | works on x64; ARM64 Windows likely no wheel | Linux live; others untested |
| Memory server cannot start (no wheel, reserved port, no network for the model download) | degrades: stderr in `serve.log`, doctor says "did not come up ... recall is keyword-only" | same | same | same | mocked |
| Low RAM | background work waits below 1.5 GB free (`/proc/meminfo`) | same | `vm_stat` | `GlobalMemoryStatusEx` | Linux live; mac parser mocked; Windows untested |
| Secrets: keychain | Secret Service via `keyring` | degrades: no session bus means no keychain; frontier says so and points at `pass`, `bw`, `op`, `keepassxc-cli`; a pasted secret is refused with a message, never stored elsewhere | Keychain | Credential Manager | Linux live; headless mocked |
| Secrets: manager bridges | `op`, `bw`, `pass`, `keepassxc-cli` CLIs | same | same | same (`pass` rare) | Linux live |
| Phone approvals and alerts (ntfy) | outbound HTTPS only | same | same | same | Linux live |
| Offline | approvals and alerts say "could not reach the ntfy server"; curation and survey fail and retry next day; memory server cannot fetch its model the first time | same | same | same | partly mocked |
| No sudo | never used; refusals become frontiers | same | same | n/a | Linux live |
| Container or cloud session | host reached through `distrobox-host-exec` when present, otherwise a frontier | | | | Linux live (distrobox) |
| Immutable OS specifics | ostree rule and the `/var/home` look-alike only when `/run/ostree-booted` exists; guardrail text is generic | | | | mocked |
| Non-English locale | fine (systemd, git porcelain and launchctl are not localised) | | fine | winget parse yields nothing; `sc query` fine | reasoning, not tested |
| Multiple users on one machine | data folder 0700, one memory-server port per data folder, token file 0600 | same | same | per-user `%APPDATA%` | Linux live |
| ARM | fine (pure Python plus onnxruntime aarch64 wheels) | | Apple silicon fine | see memory server | untested |

## What remains

- No cron fallback where systemd is absent (WSL, servers over SSH): the hourly watch then runs only at session start and
  from the nightly doctor if that is scheduled some other way.
- winget's table parser needs English headers.
- No failed-service or backup-job watch on macOS and Windows yet (launchd and Task Scheduler expose it; nothing reads it).
- Nothing in this table has run on a real Mac or Windows machine; CI runners cover the unit tests only.
