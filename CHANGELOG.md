# Changelog

## 0.3.0 (2026-10-31)

The first release that sets itself up, looks after itself, and knows your machine.

- First session: discovery, insights and a read-only survey run before the first reply; onboarding is a short conversation, never a settings page.
- Memory: ledger, same-turn rules, patterns, subagent capture, recall fused from keyword and meaning with recency; a correction becomes a rule as you type it.
- Stage: open findings in one place, with what changed since last session.
- Discovery on Linux, macOS and Windows: services, timers, containers, VMs, apps, SSH hosts, local servers, alert channels, password managers; frontiers for what it cannot reach yet.
- Shield: OS keychain store, manager bridges (`op://`, `bw://`, `pass://`, `keepass://`), `ssh grant HOST` with phone approval.
- Doctor: daily self-check and self-repair; resource guard so nothing starts on a machine short of memory.
- Memory server exits after half an hour idle and comes back on demand.
- Profiles: a folder's `.lantern.json` keeps its memory apart. The data-folder pointer is per plugin copy.
- Packs: docker (asks before deleting), media server, Home Assistant, UniFi, Tailscale (read-only, guarded).
- Evals: replay on your own transcripts (`evals/replay.py`), faithful simulated week (`evals/week.py`).

## 0.2.0 (2026-10-30)

Rebuilt as a Claude Code plugin marketplace: core plugin with hooks, memory and doctor; five packs.
