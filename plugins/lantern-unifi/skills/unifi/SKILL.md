---
name: unifi
description: Read UniFi network state (devices, clients, Wi-Fi, firewall, VLANs) and plan changes. Use when the user mentions UniFi, their gateway, access points, Wi-Fi, VLANs or a device that can't connect.
---
# UniFi

The Network application answers on port 8443 (self-hosted) or on the gateway itself (UniFi OS consoles, port 443, API
under `/proxy/network`). Use a local, read-only admin account where possible and store its credentials with
the user hands over in chat as `secret UNIFI_USER: <value>` and `secret UNIFI_PASS: <value>` (or `secret UNIFI_API_KEY: <value>`); Lantern stores them in the keychain before the model sees them, and commands get them as environment variables.

Read first:
- Devices: `/api/s/default/stat/device`; clients: `/api/s/default/stat/sta`; health: `/api/s/default/stat/health`.
- For "can't connect", check the client's last-seen AP, signal, VLAN and any firewall rule that matches it before suggesting changes.

Changing things:
- Network changes can cut off this machine, the user's remote access and every device in the house. Write the change out first (what, where, how to undo it) and ask for an explicit yes.
- Never change the network this machine or the user's remote access depends on in the same step as anything else.
- Firmware upgrades and device restarts drop connectivity: ask first, and prefer a time the user picks.
