# Changelog

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
