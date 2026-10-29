---
name: tailscale
description: Read-only Tailscale diagnosis (is a node online, which IP or name it has, why two devices can't reach each other). Use when the user mentions Tailscale, the tailnet, MagicDNS or reaching a machine remotely.
---
# Tailscale (read-only)

This pack never changes Tailscale. Changing it can cut off the very connection the user is using to reach this machine.

Allowed, because they only read:
- `tailscale status` (peers, online state, exit node), `tailscale ip`, `tailscale netcheck` (relay and NAT), `tailscale ping <peer>`.
- `tailscale status --json` for detail; `tailscale version`.

Never run, even if it looks like the fix: `tailscale up`, `down`, `set`, `logout`, `login`, `serve`, `funnel`,
`cert`, `switch`, `file`, or anything that edits ACLs, exit nodes, routes, DNS or the service unit. Do not restart
`tailscaled`. Instead, explain the change, what it would affect, and let the user make it themselves.
