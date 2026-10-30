---
name: lantern-media-server
description: Diagnose a home media stack (Plex or Jellyfin, Sonarr, Radarr, Lidarr, Prowlarr, a download client, Overseerr/Jellyseerr, Tautulli, Tdarr, Bazarr). Use for playback problems, missing or stuck downloads, requests, or library issues.
---
# Media stack

Lantern reads first and only touches the step that is actually broken. The user never runs a command and never pastes a
key: Lantern reads keys from the apps' own config files and passes them by reference.

## Map the pipeline before diagnosing

```
request (Overseerr / Jellyseerr)
  -> Sonarr (TV) / Radarr (movies) / Lidarr (music)
       -> Prowlarr (indexers, synced into the *arr apps)
       -> download client (SABnzbd, NZBGet, qBittorrent, Transmission, Deluge)
            -> completed download folder
                 -> *arr import (hardlink or copy) into the library folder
                      -> Plex / Jellyfin library scan -> playback (direct play, direct stream or transcode)
```

Find which hop the item stopped at, then read only that hop. Show the user the pipeline as this tree with a mark on the
broken hop, and a table of the apps found: app, where it runs (container, unit, host), port, version, answers.

## Keys and hosts, by reference only

| App | Key lives in | Port |
|---|---|---|
| Sonarr, Radarr, Lidarr, Prowlarr, Bazarr | `config.xml`, `<ApiKey>` | 8989, 7878, 8686, 9696, 6767 |
| Plex | `Preferences.xml`, `PlexOnlineToken` | 32400 |
| Jellyfin | Dashboard, API keys (ask the user to create one in the web UI and hand it over as `secret JELLYFIN_API_KEY: <value>`) | 8096 |
| Overseerr / Jellyseerr | `settings.json` (`main.apiKey`, plus the Sonarr and Radarr hosts it was told about) | 5055 |
| SABnzbd | `sabnzbd.ini`, `api_key` | 8080 |
| qBittorrent | web UI login, cookie session | 8080 |
| Tautulli | `config.ini`, `api_key` | 8181 |
| Tdarr | no key by default | 8265 |

Container config folders are the `/config` bind mount; read the path from the container's mounts. Never show a key,
never ask the user to read one out, and never write one into a note or memory.

## Read first, in this order

1. **Space** on every library, download and transcode path, as the containers see them (`df -h` on each mount source).
   A full disk is the first thing that breaks a media stack and it breaks every hop at once.
2. **Each app's health**: `/api/v3/health` (Sonarr, Radarr, Lidarr), `/api/v1/health` (Prowlarr, Bazarr). Each
   warning names its cause: indexer unavailable, download client unreachable, remote path mapping, import failing,
   root folder missing, update available.
3. **The queue**: `/api/v3/queue?includeUnknownMovieItems=true` or `...UnknownSeriesItems`. For every item read
   `status`, `trackedDownloadStatus`, `trackedDownloadState` and `statusMessages`: `importPending` or `importBlocked`
   with a message is a path or permission problem on the import hop, `failedPending` is the download client, `warning`
   with "no files found eligible" is usually a sample, an archive, or a wrong root folder.
4. **History and events**: `/api/v3/history?pageSize=50` for the last grabs and imports; the app's own
   `logs/*.txt` in its config folder for the first error after the last start.
5. **Indexers**: Prowlarr `/api/v1/indexer` and `/api/v1/indexerstats`; an indexer disabled by rate limit or auth
   failure explains "nothing found" across every app at once.
6. **Download client**: queue and history from the client itself (SABnzbd `api?mode=queue` and `mode=history`,
   qBittorrent `/api/v2/torrents/info`): stalled, missing files, incomplete, paused by ratio or by disk space.
7. **Import path**: the download client's completed folder as the *arr app sees it must be the same filesystem as
   the library for hardlinks; different mounts mean a copy, and a copy to a full or read-only mount is the usual
   silent stall. Remote path mappings in Settings, Download Clients translate one into the other.
8. **Requests**: Overseerr `/api/v1/request?filter=pending` and `/api/v1/settings/sonarr` and `/radarr`: does each
   target still answer at the host and port Overseerr was given, and does its default root folder and profile exist?
9. **Server and playback**: Plex `/status/sessions` and `/identity`; Jellyfin `/Sessions` and `/System/Info`.
   For each session read `TranscodeSession` (Plex) or `TranscodingInfo` (Jellyfin): video, audio or subtitle
   transcode, throttled, and the reason. Image subtitles (PGS, VOBSUB) and unsupported audio force transcodes;
   check this before blaming the network.
10. **Restarts**: restart count and last start per container; a container that restarted when the others did not
    gets its journal read for the reason.
11. **Transcoding backlog**: Tdarr's failed and queued counts; Bazarr's wanted list growing means subtitle
    providers are failing.

## Common findings and where they point

- Request approved, nothing downloads: Overseerr's target host, then Sonarr/Radarr search (indexers), then the
  quality profile or minimum availability (a movie not yet released never grabs).
- Download finished, not in the library: import hop. The *arr app's queue message says why: path mapping, permissions
  (PUID/PGID versus folder owner), hardlink across mounts, name parse failure, or the item is still seeding and the
  client reports it as incomplete.
- In the library, not in Plex/Jellyfin: library scan (manual scan on the folder), the server's own mount of the same
  folder (a different path inside its container), naming the server cannot match.
- Plays on one device, buffers on another: the session's transcode reason; a client that cannot direct play forces
  a CPU transcode, and a server without hardware transcoding falls behind on the second stream.
- Everything slow at once: disk space, then a disk at 100 % utilisation (a Tdarr or scan pass), then memory.

## Changing things

Write the plan as a table (what, which items, who it affects, how to undo), then ask with a structured question.

- Removing from the queue or blocklisting a release changes what downloads next: name the items and ask first.
- Triggering a search, a rescan or an import is safe and usually the fix; say which items and do it on the yes.
- Editing root folders, path mappings or quality profiles changes future imports for the whole app: show the before
  and after.
- Restarting an app drops active playback (server) or running imports (*arr); say who is affected.

Never delete media files, library folders, download history or the apps' databases without an explicit yes, every
time. After a change, check the item moved to the next hop, not that the request returned 200.
