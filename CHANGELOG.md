# Changelog

## 0.5.0 (2026-10-31)

Lantern learns from ordinary use and speaks plainly to someone who is not technical. Nothing to run, no simulations, no
extra model calls per turn.

- The nightly curate reads the whole day in up to four parts. Before, a heavy day lost everything but its newest part.
- A rule you have to say again is counted. It moves to the top of the rules and says how many times you said it.
- Recall counts which notes get used. The notes you keep needing stay in every session's index, and unused ones age out.
- Checks you keep asking for become routines. A session runs the read-only ones without being asked.
- A note that replaces an older one says so, so the next session tells you what changed instead of quietly reversing itself.
- One approval, never two: a yes given in the question tool covers the restart a rule guards. The container guard's dialog speaks to you.
- An unfinished item that points at its own reply ("the first warning above") keeps the request it came from.
- A Stop-hook follow-up reads as the reply's next paragraph, not as a correction of it.
- Search by meaning works on Fedora Atomic again: one memory server per data folder however its path is spelt, with a key per port.
- The plain-words check on replies fires again, and catches paths, unit names and "do it from Windows".
- The status line speaks plainly: what a finding means for you ("your drive is nearly full"), no note counts, no system chores you cannot act on.
- A returning session says in one plain sentence what Lantern picked up about you.
- "Any problems?" and "what should I be worried about?" are answered from the watches, like "is everything ok?".
- A failed nightly job reruns at the next session and clears once it succeeds. Old errors from replaced code and moved project folders stop raising warnings.

## 0.4.0 (2026-10-31)

Measured against simulated weeks on a real machine; the first session is stronger and the sessions after it are honest.

- Every hook entry point runs against a returning-session fixture in the test suite and in the doctor, so a crashing hook can never sit silently on a live install.
- Session start takes about a quarter of a second (podman's reclaimable figure is kept for an hour; failed units are read in parallel).
- Lantern's own scheduled jobs are checked: a failed nightly run is a finding, and a unit naming a bare `uv` is repointed by itself.
- A timer found overnight stays a new finding for three days, so the next session reports it.
- Stop-hook feedback is part of the turn, never a new one; the last reply's text is always captured; subagent reports are stored.

- First reply is text before any tool call; the wider look (containers, VMs, services, apps, SSH hosts, local servers) runs read-only in the first session without asking, and "look less" narrows it.
- Nothing outside Claude: the user is never shown a command or syntax. A key or password is offered as a choice (paste it next, use the password manager, or not now); the value pasted on its own goes to the keychain before the model sees it. A Plex token the machine already holds is found in Plex's own settings file and stored, not asked for.
- Plain words by default: the reader's register is learned from their own messages, their names for things are used, problems are named by their effect, and a carried guess stays a guess.
- Hands-off replies are caught by shape and answered from Lantern's own snapshot; health questions re-run the cheap checks first; a read-only check that was only offered is run.
- Since last session: computed in code with stable keys (new, changed, resolved with the reason, back again), including large new files; "what changed?" answers from that block; the first reply must name every item.
- Rules: a restart or stop of anything a rule names asks first and quotes the rule; corrections supersede the fact they correct; curated rules come only from the user's own lines.
- Open loops: offers and questions are carried as unfinished items and never re-asked verbatim; answers given through the question tool settle setup by themselves.
- Phone alerts between sessions for newly tripped watches (failed or down services, disk, missed backups), deduplicated, through the approvals channel; the hourly check schedules itself once the phone can be reached (systemd user timer, launchd agent, Task Scheduler, or the user's crontab), and is built for the host when Lantern runs inside a container.
- Status line set up by Lantern itself, keeping any status line the user had; where none renders, the same line shows under each reply. A plugin loaded from a folder never touches the user's settings.
- Packs ship inside the core plugin and switch on by discovery, with their guards; the separate `lantern-tailscale` and `lantern-media-server` plugins are kept only as compatibility shims (uninstall them; nothing is lost).
- Portability: hooks never block a prompt on an unfamiliar machine; nothing in the core assumes Fedora, systemd or a desktop keyring; discovery covers macOS and Windows.
- Memory server exits after half an hour idle and comes back on demand; the data-folder pointer is per plugin copy, so a checkout under test never redirects the installed one.
- Evals: replay on the user's own transcripts with a judge that says what was wrong; faithful simulated weeks driven like the desktop app, run in a bounded pool with a memory gate.

## Unreleased

- A Plex token the machine already holds (Preferences.xml under Plex's config folder on Linux, macOS, Windows, or a Plex container's `/config` volume) is found and stored in the keychain as `PLEX_TOKEN` instead of being asked for; the session is told it was found, never the value.
- Portability: a hook that crashes on an unfamiliar machine is logged and said once, never a blocked prompt; the memory server's stderr goes to `serve.log` and a server that did not come up is reported instead of restarted; free memory is read on macOS (`vm_stat`) and Windows too; uv and claude are found from `$UV` and the installers' folders when PATH is bare; tool output and free-text files are UTF-8 on every OS; the data folder is private (0700); a headless Linux box with no keyring gets a frontier pointing at the manager CLIs; offline approvals say so; guardrails and look-alikes no longer name Fedora unless the machine is one; project transcripts are found on Windows and under `CLAUDE_CONFIG_DIR`; Linux without a systemd user session (WSL, servers over SSH) schedules the jobs in the user's crontab. See docs/platforms.md.

## 0.3.0 (2026-10-31)

The first release that sets itself up, looks after itself, and knows your machine.

- Register: plain words by default; Lantern learns from your own messages whether you want paths and unit names, and uses your names for things ("the plex thing") in its status line and replies. A guess carried from one session to the next stays marked as a guess.

- First session: discovery, insights and a read-only survey run before the first reply; onboarding is a short conversation, never a settings page.
- Memory: ledger, same-turn rules, patterns, subagent capture, recall fused from keyword and meaning with recency; a correction becomes a rule as you type it.
- Stage: open findings in one place, with what changed since last session.
- Discovery on Linux, macOS and Windows: services, timers, containers, VMs, apps, SSH hosts, local servers, alert channels, password managers; frontiers for what it cannot reach yet.
- Shield: OS keychain store, manager bridges (`op://`, `bw://`, `pass://`, `keepass://`), `ssh grant HOST` with phone approval.
- Doctor: daily self-check and self-repair; resource guard so nothing starts on a machine short of memory.
- Memory server exits after half an hour idle and comes back on demand.
- Profiles: a folder's `.lantern.json` keeps its memory apart. The data-folder pointer is per plugin copy.
- Packs ship inside the core plugin and switch on by discovery: docker (asks before deleting), media server, Home Assistant, UniFi, Tailscale (read-only, guarded). The separate `lantern-tailscale` and `lantern-media-server` plugins are kept only as compatibility shims: if you had them installed, uninstall them; nothing is lost. They ship inside the core plugin and switch on when discovery finds their system or on `lantern pack on NAME`, in the running session: no install, no restart.
- Evals: replay on your own transcripts (`evals/replay.py`), faithful simulated week (`evals/week.py`).

## 0.2.0 (2026-10-30)

Rebuilt as a Claude Code plugin marketplace: core plugin with hooks, memory and doctor; five packs.
