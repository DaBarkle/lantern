---
name: media-server
description: Diagnose a home media stack (Plex or Jellyfin, Sonarr, Radarr, Prowlarr, a download client, Overseerr/Jellyseerr, Tautulli, Tdarr). Use for playback problems, missing or stuck downloads, requests, or library issues.
---
# Media stack

Map the flow before diagnosing: request app (Overseerr/Jellyseerr) -> Sonarr (TV) or Radarr (movies) -> Prowlarr
(indexers) -> download client (SABnzbd, qBittorrent...) -> import into the library -> Plex/Jellyfin. Find where an item
stopped, then look only at that step.

Read first:
- Each *arr app has an API key in Settings, General. Store it with `lantern secret set SONARR_API_KEY` (and so on).
- Sonarr/Radarr: `/api/v3/queue` (stuck downloads and why), `/api/v3/history`, `/api/v3/health`.
- Prowlarr: `/api/v1/indexer` and `/api/v1/health`. Plex: `/status/sessions` (needs a Plex token) for what is playing and whether it transcodes.
- Playback stutter is often transcoding: image subtitles (PGS) and unsupported audio force it. Check the session's decision before blaming the network.

Changing things:
- Deleting from the queue or blocklisting a release changes what downloads next: say which items and ask first.
- Never delete media files or library folders without an explicit yes.
