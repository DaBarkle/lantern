---
name: home-assistant
description: Check Home Assistant's health and read or change entities, automations and integrations through its REST API. Use when the user mentions Home Assistant, a smart-home device, automation or scene.
---
# Home Assistant

Home Assistant usually answers on port 8123. The REST API needs a long-lived access token: ask the user to create one
(Profile, Security) and hand it over in chat as `secret HA_TOKEN: <value>`; Lantern stores it in the keychain before the model sees the message. Never ask for it as plain text.

Read first:
- `lantern secret run HA_TOKEN -- sh -c 'curl -s -H "Authorization: Bearer $HA_TOKEN" http://HOST:8123/api/'` answers `API running.` when healthy.
- `/api/states` lists entities; `/api/states/<entity_id>` reads one; `/api/error_log` shows recent errors.

Changing things:
- Calling a service (`/api/services/<domain>/<service>`) acts on the real home: lights, locks, heating. Say exactly what will happen and ask first, unless the user asked for that action.
- Locks, alarms, garage doors and anything safety-related always need an explicit yes, every time.
- Prefer editing automations in the UI or YAML with a backup over bulk API changes.
