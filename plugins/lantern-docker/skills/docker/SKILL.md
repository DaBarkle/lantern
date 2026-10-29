---
name: docker
description: Check, diagnose and manage Docker or Podman containers on this machine. Use when the user mentions a container, compose file, image, or a service that runs in one.
---
# Containers

Work out which engine is in use first: `docker` or `podman` (rootless Podman containers are often run by systemd user units named `container-<name>.service`).

Read before you act:
- `<engine> ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}'` for state, `<engine> logs --tail 100 <name>` for errors, `<engine> inspect <name>` for mounts, ports and restart policy.
- For compose stacks, find the compose file from the container's labels (`com.docker.compose.project.working_dir`) before editing anything.

Changing things:
- Restarting a container interrupts whoever uses it. Say what will restart and ask first, unless the user asked for that restart.
- Never `rm`, `prune`, `down -v` or delete volumes without an explicit yes: volumes hold the data.
- If a systemd unit manages the container, restart the unit, not the container, or systemd will fight you.
- Secrets in env files stay out of the chat: refer to them by name and run commands with `lantern secret run NAME -- ...`.

After a change, check the outcome (the service answers, logs are clean), not just that the command exited 0.
