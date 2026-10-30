---
name: lantern-tailscale
description: Read-only Tailscale diagnosis (is a node online, which IP or name it has, why two devices can't reach each other). Use when the user mentions Tailscale, the tailnet, MagicDNS or reaching a machine remotely.
---
# Tailscale (read-only)

This pack never changes Tailscale. Changing it can cut off the very connection the user is using to reach this machine.
The pack's guard blocks every changing command; reads pass. The user never runs a command: when a change is needed,
Lantern writes it out and the user makes it in the Tailscale admin console or the app.

## Allowed, because they only read

- `tailscale status` (peers, online state, exit node in use) and `tailscale status --json` for detail: `Self`, each
  peer's `Online`, `LastSeen`, `CurAddr` (direct address) or `Relay` (DERP region), `KeyExpiry`, `Tags`, `OS`,
  `PrimaryRoutes`, `ExitNodeOption`.
- `tailscale ip` (this node's addresses), `tailscale version`, `tailscale whois <ip>` (who a tailnet address belongs to).
- `tailscale netcheck` (UDP, NAT type, nearest DERP and latency, port mapping), `tailscale ping <peer>` (direct or
  relayed, and how long it took to go direct), `tailscale dns status`.
- Read-only looks at the daemon: `systemctl status tailscaled`, `journalctl -u tailscaled -n 100`.

## Read first, in this order

| Step | Read | What it tells you |
|---|---|---|
| This node | `status --json` `Self` and `BackendState` | `Running` or `NeedsLogin` or `Stopped`; the node's name, tailnet, IPs, key expiry |
| The peer | the peer's entry in `status --json` | `Online` false with an old `LastSeen` is the other machine, not the path; key expired shows as `Expired` |
| The path | `ping <peer>` | direct (address and port) or via a DERP relay and which; a relay that never goes direct means NAT on one side |
| NAT and relay | `netcheck` on this node | `UDP: false` means UDP is blocked here (direct impossible); `MappingVariesByDestIP: true` is a hard NAT; `PortMapping` says whether UPnP, NAT-PMP or PCP is available; the nearest DERP and its latency |
| Names | `dns status` and `status` names | MagicDNS on or off, the tailnet suffix, the search domains; a name that does not resolve is DNS, an IP that does not answer is the path or the peer |
| Routes and exit | `Self.PrimaryRoutes`, peers' `PrimaryRoutes`, `ExitNodeStatus` | a subnet router advertising but not approved, an exit node selected that is offline |
| Daemon | `systemctl status tailscaled`, the last 100 journal lines | restarts, login URLs (the node needs a re-auth), "no network" lines |
| Interface and firewall | `ip addr show tailscale0`, firewall rules that mention 41641 or the tailscale0 interface | the interface exists with the 100.x address; a local firewall that drops inbound on it |

Report as a table: this node (name, IPs, state, key expiry, DERP), the peer (name, online, last seen, OS, key expiry),
the path (direct or relay, latency), DNS. Then the finding in one sentence and the fix written out for the user.

## Common findings and where they point

- Peer offline with last seen hours ago: the other machine is asleep, off, or its Tailscale stopped; nothing here fixes
  it. Say which machine and when it was last seen.
- `NeedsLogin` or a login URL in the journal: the node's key expired or it was logged out. The user opens the URL
  the status shows, or re-authenticates in the admin console; Lantern never logs the node in or out itself.
- Key expiry within days: the admin console, the machine's menu, "Disable key expiry" if the user wants it to stay.
- Direct ping fails, relay works, slow: hard NAT or blocked UDP on one side; the fix is on the router (UPnP or a
  static UDP 41641 forward) or a peer relay, and it is the user's to make.
- Name does not resolve: MagicDNS off on this node or the tailnet; or the OS resolver overrides it (`resolv.conf`
  managed by something else); the fix is in the admin console's DNS page or the OS, written out for the user.
- Can reach the node but not the thing behind it: a subnet route advertised but not approved (admin console,
  Machines, Edit route settings), or an ACL that does not allow this user to that port; read the ACL only if the user
  shows it.
- Everything down on this node: `tailscaled` not running; Lantern reports it and the user brings it back themselves
  (the Tailscale app or the service), because a restart from here can cut the session.

## Never run, even if it looks like the fix

`tailscale up`, `down`, `set`, `logout`, `login`, `serve`, `funnel`, `cert`, `switch`, `file`, `update`, `configure`,
or anything that edits ACLs, exit nodes, routes, DNS or the service unit. Do not start, stop or restart `tailscaled`.
Instead, explain the change, what it would affect (including this session), and let the user make it themselves.
