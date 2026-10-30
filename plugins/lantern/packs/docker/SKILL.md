---
name: lantern-docker
description: Check, diagnose and manage Docker or Podman containers on this machine. Use when the user mentions a container, compose file, image, or a service that runs in one.
---
# Containers

Lantern reads first, explains what it found as a table, and stages every change for the user's yes. The user never runs
a command: Lantern runs the read-only ones itself and asks before any that changes state.

## Find the engine and the owner

1. Engine: `docker` or `podman`. Rootless Podman runs per user; a container that is missing from `podman ps` may belong
   to another user or to root. Quadlet and `podman generate systemd` units show up as `container-<name>.service` or
   `<name>.service` in the user's systemd (`systemctl --user list-units 'container-*' '*.service' --type=service`).
2. Owner: read the container's labels. `com.docker.compose.project.working_dir` names the compose file; `PODMAN_SYSTEMD_UNIT`
   names the unit. Whoever owns it is what you act through: a compose stack is restarted with compose, a unit with
   systemd, never the bare container, or the manager fights you and the next boot undoes the fix.

## Read first, in this order

| Step | Read | What it rules out |
|---|---|---|
| State | `ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.RunningFor}}'` | Which containers are down, and which restarted recently (an uptime shorter than its neighbours is a restart, not luck) |
| Why it stopped | `inspect --format '{{.State.ExitCode}} {{.State.OOMKilled}} {{.RestartCount}} {{.State.Error}}'` | 137 with OOMKilled true is memory; 137 with false is an outside kill (health check, systemd stop, the user); 1 or 2 is the app itself; 125 to 127 is the engine failing to start it (bad mount, missing binary) |
| Health | `inspect --format '{{json .State.Health}}'` and `--format '{{.HostConfig.RestartPolicy.Name}}'` | A failing health check with restart always is a loop; read the health log's last output before the app log |
| Logs | `logs --tail 200 --timestamps <name>`; for a unit also `journalctl --user -u <unit> -n 100` | The first error after the last start, not the last line |
| Mounts and ports | `inspect --format '{{json .Mounts}}'` and `'{{json .NetworkSettings.Ports}}'` | A bind mount whose source is gone (unmounted disk, moved folder), a port already taken by another process, SELinux denials (`:z`/`:Z` missing on a Fedora-family host) |
| Space | `df -h` on every mount source and the engine's storage root (`info --format '{{.DockerRootDir}}'` or `{{.Store.GraphRoot}}`) | A full disk stops writes, image pulls and log rotation before anything else looks wrong |
| Images | `images --digests` for the running tag; `inspect --format '{{.Image}}'` against it | A container running an older image than the tag now points to (restart would upgrade it silently) |
| Resources | `stats --no-stream` | One container eating memory or CPU and starving the rest; the host's memory pressure |

Report the result as one table: name, owner (compose project or unit), status, restarts in 24 h, exit reason, health,
disk on its data mount. A restart count that differs from its neighbours is explained, never called harmless.

## Common findings and where they point

- Restart loop, exit 1 with a config error in the first log lines: the config file or an env variable, not the image.
- Restart loop, exit 137, OOMKilled: the memory limit or a leak. Read `stats` over a minute before raising a limit.
- Up but not answering: the port mapping (host port vs container port), the bind address (127.0.0.1 vs 0.0.0.0), the
  firewall, or a reverse proxy pointing at an old container IP after a recreate.
- Permission denied on a mount: the UID the process runs as versus the folder owner (`PUID`/`PGID` on LinuxServer
  images), rootless Podman's user namespace, or an SELinux label.
- Pull or build fails: disk space first, then registry auth, then DNS inside the engine.
- Clock or certificate errors inside a container: the host clock, then CA certificates in the image.

## Changing things

Write the plan out first as a short table (what, where, who it interrupts, how to undo), then ask with a structured
question. The user's own request for a restart counts as the yes for that restart only.

- Restart interrupts whoever uses the service; say who, and for a stack say which containers.
- Recreate (compose up -d, a unit restart after an image pull) applies a new image: say the version change.
- Stop and remove is destructive when the container holds state in its own writable layer; check mounts first.
- Edits to compose files, unit files, env files: show the diff, keep a dated copy next to the original.
- Logs and `stats` are safe to read any time; `exec` into a container is read-only only if the command is.
- Secrets in env files stay out of the chat: refer to them by name and run with `lantern secret run NAME -- ...`.

## Never without an explicit yes, every time

`rm`, `rmi`, `prune` of any kind, `down -v` or `--volumes`, `volume rm`, `system reset`: volumes and anonymous
volumes hold the data. The pack's guard asks the user before any of these runs, even inside a script.

After a change, confirm the outcome, not the exit code: the service answers, the health check passes, the logs are
clean for a minute, and the restart count stopped rising.
