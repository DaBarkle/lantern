---
name: media-server
description: Diagnose a home media stack (Plex or Jellyfin, Sonarr, Radarr, Prowlarr, a download client, Overseerr/Jellyseerr, Tautulli, Tdarr). Use for playback problems, missing or stuck downloads, requests, or library issues.
---
# Media stack

Map the flow before diagnosing: request app (Overseerr/Jellyseerr) -> Sonarr (TV) or Radarr (movies) -> Prowlarr
(indexers) -> download client (SABnzbd, qBittorrent...) -> import into the library -> Plex/Jellyfin. Find where an item
stopped, then look only at that step.

Read first:
- Each *arr app has an API key in Settings, General. Read it from the app's own config first (`config.xml` ApiKey for the *arr apps, `Preferences.xml` PlexOnlineToken for Plex, Overseerr's `settings.json` for its Sonarr and Radarr hosts) and pass it by reference; never ask the user for a value, and never show one.
- Sonarr/Radarr: `/api/v3/queue` (stuck downloads and why), `/api/v3/history`, `/api/v3/health`.
- Prowlarr: `/api/v1/indexer` and `/api/v1/health`. Plex: `/status/sessions` (needs a Plex token) for what is playing and whether it transcodes.
- Playback stutter is often transcoding: image subtitles (PGS) and unsupported audio force it. Check the session's decision before blaming the network.

Health means more than a port answering. Before calling the stack healthy, check and report:
- free space on every media and transcode path (`df -h` on the mounts the containers use); this is the first thing that breaks a media stack
- restart counts and last start time per container (`podman inspect --format '{{.RestartCount}} {{.State.StartedAt}}'`), and the journal reason for any restart in the last day; an uptime that differs from its neighbours is explained, never called harmless
- Tdarr's failed-transcode count, Overseerr's pending requests and whether its Sonarr/Radarr targets answer
- the request pipeline end to end: if Overseerr exists but no *arr app is reachable, say so and read Overseerr's settings for where they run

Changing things:
- Deleting from the queue or blocklisting a release changes what downloads next: say which items and ask first.
- Never delete media files or library folders without an explicit yes.
