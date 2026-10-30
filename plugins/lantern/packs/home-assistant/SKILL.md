---
name: lantern-home-assistant
description: Check Home Assistant's health and read or change entities, automations and integrations through its REST API. Use when the user mentions Home Assistant, a smart-home device, automation or scene.
---
# Home Assistant

Lantern reads first and acts on the real home only with the user's yes. The user never runs a command.

## Find it and get in

- Usually port 8123; as a container (`homeassistant`), a VM or Home Assistant OS (then there is a Supervisor and
  add-ons too), or a Python venv unit. The config folder holds `configuration.yaml`, `.storage/`, `home-assistant.log`
  and `home-assistant_v2.db` (or an external recorder database).
- The REST API needs a long-lived access token. Ask the user to create one in the web UI (their profile, Security,
  Long-lived access tokens) and hand it over in chat as `secret HA_TOKEN: <value>`; Lantern stores it in the keychain
  before the model sees the message. Never ask for it as plain text, never show it. Run reads with
  `lantern secret run HA_TOKEN -- ...` and the `Authorization: Bearer` header.

## Read first, in this order

| Step | Read | What it tells you |
|---|---|---|
| Alive | `GET /api/` | `API running.` or nothing; nothing means the process, the port or the token |
| Version and setup | `GET /api/config` | version, location, unit system, loaded `components`, `config_dir`, `state` (RUNNING or not), `safe_mode` or `recovery_mode` |
| Errors | `GET /api/error_log` | the plain-text log since the last start; read the first ERROR after startup before the last one |
| Repairs | `.storage/repairs.issue_registry` or the Repairs panel the user can open | each issue names the integration and the fix it wants |
| Entities | `GET /api/states` | count, and every entity with state `unavailable` or `unknown`, grouped by integration (the entity id's domain and the `.storage/core.entity_registry` platform) |
| One entity | `GET /api/states/<entity_id>` | state, attributes, `last_changed`, `last_updated`; a sensor whose `last_updated` is hours old has lost its source |
| Automations | `GET /api/states` filtered to `automation.*` | `last_triggered` attribute and whether it is `on`; a trace lives in `.storage/trace.saved_traces` |
| Events and services | `GET /api/events`, `GET /api/services` | which integrations expose what; a missing service means its integration did not load |
| Recorder | size of `home-assistant_v2.db` and the `recorder` entries in the error log | a database over a few GB slows every history view and most startups; purge settings are in `configuration.yaml` |
| Host | disk, memory and the container or unit's restart count; for HA OS the Supervisor's `/supervisor/info` and `/host/info` through the Supervisor API from inside | a host at 90 % disk breaks the recorder and backups first |

Report as one table: version, state, safe or recovery mode, entities (total and unavailable), errors in the log, repairs
open, recorder size, last backup. Then a short list of unavailable entities grouped by integration.

## Common findings and where they point

- Many entities unavailable from one integration: that integration's device or hub is offline (a bridge, a Zigbee or
  Z-Wave stick, a cloud login that expired); the first log error from the integration names it.
- One device unavailable: battery, range, or it was re-paired and got a new id; the device page shows last seen.
- Automation did not run: it is turned off, its trigger never fired (check the trigger entity's history), or a
  condition was false; the trace shows which.
- Slow dashboard or startup: recorder database size, then a template sensor that updates every second, then the host.
- Safe mode or recovery mode after a restart: a YAML error; the log names the file and line.
- Login works locally but not remotely: the `http` block (`trusted_proxies`, `use_x_forwarded_for`) or the remote
  access setup (Nabu Casa, a reverse proxy, Tailscale), not the user's password.

## Changing things

Write the plan as a table (what, which entities, what happens in the house, how to undo), then ask with a structured
question. The user's own request for an action counts as the yes for that action only.

- Calling a service (`POST /api/services/<domain>/<service>`) acts on the real home: lights, switches, media, climate.
  Say exactly what will happen and ask first.
- Locks, alarm panels, garage doors, covers, valves, sirens and anything safety-related need an explicit yes every
  time, even when the user asked a moment ago.
- Prefer the UI or a YAML edit with a dated backup over bulk API changes; a reload (`POST /api/services/<domain>/reload`)
  beats a restart, and a restart drops every automation that is mid-run.
- Never edit `.storage/` files while Home Assistant is running; they are rewritten from memory on shutdown.
- Backups and database purges: say what is lost and ask first.

After a change, read the entity's state back and the error log; a 200 means the call was accepted, not that the device
did it.
