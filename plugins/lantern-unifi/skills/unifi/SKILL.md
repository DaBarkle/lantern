---
name: unifi
description: Read UniFi network state (devices, clients, Wi-Fi, firewall, VLANs) and plan changes. Use when the user mentions UniFi, their gateway, access points, Wi-Fi, VLANs or a device that can't connect.
---
# UniFi

Lantern reads the network and plans changes; the user applies the risky ones themselves in the UniFi app, step by step
as Lantern writes them out. The user never runs a command.

## Find the controller and get in

- Self-hosted Network application: port 8443, API under `/api/`. UniFi OS console (Dream Machine, Cloud Key Gen2,
  Express): port 443 on the gateway, API under `/proxy/network/api/`, login at `/api/auth/login`.
- Credentials: a local admin with read-only role where possible. Ask the user to create it in the UniFi app and hand
  it over in chat as `secret UNIFI_USER: <value>` and `secret UNIFI_PASS: <value>`, or an API key from the console's
  Control Plane, Integrations as `secret UNIFI_API_KEY: <value>`. Lantern stores them in the keychain before the model
  sees them; commands get them as environment variables through `lantern secret run`. Never show them.
- Site name is usually `default`; `/api/self/sites` lists the rest.

## Read first, in this order

| Step | Read | What it tells you |
|---|---|---|
| Health | `/api/s/<site>/stat/health` | WAN, LAN and WLAN subsystems: status, gateway up, WAN IP, users and guests, latency and drops |
| Devices | `/api/s/<site>/stat/device` | every gateway, switch and AP: `state` (1 adopted and online), `uptime`, `version`, `upgradable`, `last_seen`, uplink, CPU, memory, temperature; radios with channel, width and tx power; port table with speed, PoE and errors |
| Clients | `/api/s/<site>/stat/sta` | connected clients: AP or switch port, `signal` and `rssi`, `tx_rate`, `rx_rate`, `channel`, `vlan`, `network`, `ip`, `last_seen`, `is_wired` |
| All known clients | `/api/s/<site>/rest/user` | clients that used to connect, with fixed IPs and names, for "it was fine yesterday" |
| Networks and VLANs | `/api/s/<site>/rest/networkconf` | subnets, VLAN ids, DHCP ranges, the network this machine is on |
| Wi-Fi | `/api/s/<site>/rest/wlanconf` | each SSID: bands, VLAN, security, band steering, minimum RSSI, client isolation, schedule |
| Firewall | `/api/s/<site>/rest/firewallrule` and `firewallgroup` | rules in order, and the groups they reference |
| Port forwards and routes | `/api/s/<site>/rest/portforward`, `rest/routing` | what reaches in from outside |
| Alarms and events | `/api/s/<site>/stat/alarm`, `stat/event` | disconnects, roams, DFS channel changes, failed upgrades, with time stamps |

Report as a table of devices (name, model, state, version, uptime, clients, uplink) and a short tree of the networks
with the SSIDs and VLANs on each. Tell the user which network this machine and their remote access are on; those are
the ones no change may touch in the same step as anything else.

## "It can't connect": read in this order

1. Is the client in `stat/sta` at all? If not, it never associated: wrong password, MAC filter, the SSID's band it
   cannot see (a 2.4 GHz only device on a 5 GHz only SSID), the SSID's schedule, or the AP is down.
2. If it is there: `signal` under about -70 dBm is weak; minimum RSSI on that SSID would be dropping it; a `tx_rate`
   far below its neighbours means a far AP or a wall.
3. Does it have an `ip` in the right `network`? No IP: DHCP on that VLAN (the range is full, or the VLAN is not
   tagged to that AP's uplink port). Wrong VLAN: the SSID's network setting or a switch port profile.
4. Can it reach what it wants? Firewall rules in order for that source network, client isolation on the SSID, and
   whether the target is on a network that blocks inter-VLAN traffic.
5. Events for that client's MAC: repeated disconnect and reconnect with the same AP is interference or power save;
   roaming every minute is band steering or two APs on overlapping channels.
6. If the symptom is "everything on Wi-Fi is slow": channel width, DFS events (the AP changed channel after radar),
   an AP with far more clients than the others, or a 100 Mb uplink on a gigabit switch port (port errors in the device
   port table).

## Changing things

Network changes can cut off this machine, the user's remote access and every device in the house. Lantern writes the
change out as a table (what, where in the UniFi app, what it affects, how to undo it) and asks with a structured
question; the user then makes it in the app, or gives an explicit yes for Lantern to apply it through the API.

- Never change the network, VLAN, SSID, firewall rule or port that this machine or the user's remote access depends
  on in the same step as anything else, and never without the undo written first.
- Firmware upgrades, device restarts, channel changes and provisioning drop connectivity for that device's clients: ask
  first and prefer a time the user picks.
- Firewall rules apply in order: show where the new rule lands and what sits above it.
- Removing a device from the controller (forget) wipes its config: explicit yes every time.

After a change, read `stat/health` and the client back; a saved setting is not a working one.
