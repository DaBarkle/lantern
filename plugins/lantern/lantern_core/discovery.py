"""Discovery: read-only scouts find what is on this machine, up to the depth the user chose. Nothing is remembered
until the user accepts it; an accepted find becomes a capability card in memory plus a health check for the doctor.

Depth (a consent boundary, `lantern depth N`):
  1 conversations  memory of sessions only, no scouts
  2 workspace      git repos and dev tools (the default)
  3 home           containers, services, SSH hosts, well-known local service ports
  4 life           accounts the user connects on purpose (Claude connectors; nothing to scout locally)
"""
import csv
import io
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import memory
from .paths import ensure_data_dir

DEV_TOOLS = ["git", "gh", "uv", "python3", "node", "npm", "cargo", "go", "java", "dotnet", "code", "nvim", "vim", "tmux", "zsh", "fish",
             "docker", "podman", "kubectl", "terraform", "aws", "gcloud", "az", "brew", "distrobox", "winget"]
PLATFORM = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "linux")
# Where macOS keeps the user's launchd jobs (Apple's own live under /System and are not the user's). Domain is what
# `launchctl print` needs to ask after one: gui/<uid> for agents, system for daemons.
LAUNCHD_DIRS = {"~/Library/LaunchAgents": "gui", "/Library/LaunchAgents": "gui", "/Library/LaunchDaemons": "system"}
# A Claude session may run inside a container (the desktop app's distrobox, for one). The host is still the machine
# the user means, so commands that look at the host go through distrobox-host-exec when it is there.
IN_CONTAINER = Path("/run/.containerenv").exists() or Path("/.dockerenv").exists() or bool(os.environ.get("CONTAINER_ID"))
HOST_EXEC = shutil.which("distrobox-host-exec") or shutil.which("host-spawn") if IN_CONTAINER else None
HOST_ROOT = Path("/run/host") if IN_CONTAINER and Path("/run/host/etc").exists() else Path("/")
# Well-known local services. Only a TCP connect to localhost: nothing is sent.
PORTS = {32400: "Plex", 8123: "Home Assistant", 8443: "UniFi Network", 3001: "Uptime Kuma", 8096: "Jellyfin",
         8989: "Sonarr", 7878: "Radarr", 9696: "Prowlarr", 8080: "SABnzbd or another web app", 5055: "Overseerr/Jellyseerr",
         8181: "Tautulli", 8265: "Tdarr", 9000: "Portainer", 1883: "MQTT broker"}
SKIP_DIRS = {"node_modules", ".cache", ".local", ".var", "Library", "AppData", ".cargo", ".rustup", "go", ".npm"}
TIMEOUT = 10
DEPTHS = {1: "conversations: memory of sessions only, no scouts",
          2: "workspace (default): this machine, your git repos and dev tools",
          3: "home: also containers, services, installed apps, SSH hosts and local servers"}


def settings_path() -> Path:
    return ensure_data_dir() / "settings.json"


def settings() -> dict:
    try:
        return json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {"depth": 2}  # workspace by default: repos and dev tools are harmless to know and make Lantern useful at once


def save_settings(s: dict) -> None:
    settings_path().write_text(json.dumps(s, indent=2))


def _which(name: str) -> str | None:
    """Where a tool is, on this machine or on the host behind a container."""
    found = None if (HOST_EXEC and name in HOST_TOOLS) else shutil.which(name)
    if found or not HOST_EXEC:
        return found
    try:
        r = subprocess.run([HOST_EXEC, "sh", "-c", f"command -v {name}"], capture_output=True, text=True, timeout=TIMEOUT)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


HOST_TOOLS = {"systemctl", "podman", "docker", "flatpak", "virsh", "tailscale", "lspci", "wg", "zerotier-cli"}


REFUSED = re.compile(r"permission denied|not permitted|access denied|authorization|connect: no such file", re.I)
WALLS: list[dict] = []  # refusals met while scouting, kept as frontiers: a wall is a finding, not silence


def frontier(id_: str, name: str, why: str, unlocks: str, ask: str) -> dict:
    return {"id": f"frontier:{id_}", "name": name, "why": why, "unlocks": unlocks, "ask": ask}


def _run(cmd: list[str]) -> str:
    if HOST_EXEC and (cmd[0] in HOST_TOOLS or not shutil.which(cmd[0])):
        cmd = [HOST_EXEC, *cmd]  # the container's own systemctl or podman cannot see the host; ask the host
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    tool = cmd[1] if HOST_EXEC and cmd[0] == HOST_EXEC else cmd[0]
    if r.returncode != 0 and REFUSED.search(r.stderr or "") and not any(w["id"] == f"frontier:wall:{tool}" for w in WALLS):
        WALLS.append(frontier(f"wall:{tool}", tool, f"`{tool}` refused this user: {r.stderr.strip().splitlines()[-1][:100]}",
                              f"seeing what {tool} manages", f"give this user access to {tool} (a group or a socket), or say to leave it"))
    return r.stdout


def scout_repos(root: Path | None = None, max_depth: int = 3) -> list[dict]:
    root = root or Path.home()
    out = []

    def walk(d: Path, depth: int):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        if any(e.name == ".git" for e in entries):
            out.append({"id": f"repo:{d}", "kind": "repo", "name": d.name, "detail": _repo_detail(d), "check": {"type": "path", "path": str(d)}})
            return  # a repo's subfolders are part of it
        if depth < max_depth:
            for e in entries:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP_DIRS \
                        and not any(w in e.name.lower() for w in ("archive", "backup", "old")):  # not live work
                    walk(Path(e.path), depth + 1)

    walk(root, 0)
    return out


def scout_system() -> list[dict]:
    """One card for the machine itself: OS, CPU, memory, GPU. Read-only, from files and `lspci`."""
    import platform
    parts = []
    try:
        rel = dict(l.split("=", 1) for l in (HOST_ROOT / "etc/os-release").read_text().splitlines() if "=" in l)
        parts.append(rel.get("PRETTY_NAME", "").strip('"'))
    except OSError:
        parts.append(platform.platform(aliased=True, terse=True))  # "macOS-14.1", "Windows-11"
    try:
        cpu = next((l.split(":", 1)[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines() if l.startswith("model name")), "")
        mem = next((int(l.split()[1]) // (1024 * 1024) for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal")), 0)
        parts += [p for p in (cpu, f"{mem} GB RAM" if mem else "") if p]
    except OSError:
        pass
    gpu = [l.split(": ", 1)[-1] for l in _run(["lspci"]).splitlines() if "VGA" in l or "3D controller" in l]
    parts += [f"GPU {g[:60]}" for g in gpu[:2]]
    host = platform.node() or "this machine"
    return [{"id": "system", "kind": "machine", "name": host, "detail": ", ".join(p for p in parts if p), "check": None}]


def _repo_detail(d: Path) -> str:
    remote = _run(["git", "-C", str(d), "remote", "get-url", "origin"]).strip()
    last = _run(["git", "-C", str(d), "log", "-1", "--format=%cs"]).strip()
    return ", ".join(x for x in (str(d), f"origin {remote}" if remote else "no remote", f"last commit {last}" if last else "") if x)


def scout_apps() -> list[dict]:
    """Desktop apps the user installed (Flatpak, Homebrew casks, winget), by name."""
    out = []
    for line in _run(["flatpak", "list", "--app", "--columns=application,name"]).splitlines():
        if "\t" in line:
            app, name = line.split("\t", 1)
            out.append({"id": f"app:{app}", "kind": "app", "name": name.strip(), "detail": f"Flatpak {app}", "check": None})
    if PLATFORM == "mac":
        out += [{"id": f"app:{c}", "kind": "app", "name": c, "detail": "Homebrew cask", "check": None}
                for c in _run(["brew", "list", "--cask", "-1"]).split()]
    if PLATFORM == "windows":
        out += _winget_apps(_run(["winget", "list", "--disable-interactivity", "--accept-source-agreements"]))
    return out


def _winget_apps(table: str) -> list[dict]:
    """`winget list` prints a fixed-width table; the header gives the column offsets."""
    lines = table.splitlines()
    head = next((i for i, l in enumerate(lines) if l.startswith("Name") and " Id " in l), None)
    if head is None:
        return []
    cols = {c: lines[head].index(c) for c in ("Id", "Version", "Source") if c in lines[head]}
    out = []
    for l in lines[head + 2:]:  # header, then a dashed rule
        name, app = l[:cols["Id"]].strip(), l[cols["Id"]:cols.get("Version")].strip()
        # Rows without a source are programs winget merely sees in the registry (drivers, runtimes); the user's apps have one.
        if name and app and ("Source" not in cols or l[cols["Source"]:].strip()):
            out.append({"id": f"app:{app}", "kind": "app", "name": name, "detail": f"winget {app}", "check": None})
    return out


ALERT_WORDS = {"ntfy": "ntfy", "pushcut": "Pushcut", "gotify": "Gotify", "pushover": "Pushover", "telegram": "Telegram",
               "discord.com/api/webhooks": "Discord webhook", "hooks.slack.com": "Slack webhook"}


def scout_alerts() -> list[dict]:
    """Ways this machine already reaches the user's phone or chat, from unit files and configs (names only)."""
    found: dict[str, str] = {}
    for folder in (Path.home() / ".config" / "systemd" / "user", Path.home() / ".config"):
        for f in list(folder.glob("*.service"))[:300] + list(folder.glob("*/*.yaml"))[:200] + list(folder.glob("*/*.json"))[:200]:
            try:
                text = f.read_text(errors="replace")[:200000].lower()
            except OSError:
                continue
            for word, name in ALERT_WORDS.items():
                if word in text:
                    found.setdefault(name, f.name)
    return [{"id": f"alert:{name}", "kind": "alert-channel", "name": name, "detail": f"seen in {where}", "check": None}
            for name, where in found.items()]


def readme_snippets(limit: int = 2) -> list[str]:
    """The opening of the README of the most recently worked-on projects: what the user is building, in their words."""
    caps = settings().get("capabilities", {})
    repos = []
    for cid in caps:
        if cid.startswith("repo:"):
            d = Path(cid[5:])
            last = _run(["git", "-C", str(d), "log", "-1", "--format=%ct"]).strip()
            repos.append((int(last) if last.isdigit() else 0, d))
    out = []
    for _, d in sorted(repos, reverse=True)[:limit]:
        for name in ("README.md", "readme.md", "README"):
            try:
                text = (d / name).read_text(errors="replace")
            except OSError:
                continue
            body = " ".join(l.strip("# ").strip() for l in text.splitlines()[:12] if l.strip())[:400]
            if body:
                out.append(f"{d.name}: {body}")
            break
    return out


def expertise() -> str:
    """A reader level inferred from the machine: someone running services and many dev tools is an expert."""
    s = settings()
    if s.get("expertise"):
        return s["expertise"]
    caps = s.get("capabilities", {})
    kinds = [c.split(":", 1)[0] for c in caps]
    return "expert" if kinds.count("tool") >= 8 or kinds.count("service") + kinds.count("container") >= 3 else "general"


def scout_tools() -> list[dict]:
    out = []
    for t in DEV_TOOLS:
        where = _which(t)
        if where:
            out.append({"id": f"tool:{t}", "kind": "tool", "name": t, "detail": where, "check": {"type": "tool", "name": t}})
    return out


def _launchd_jobs() -> list[dict]:
    """The user's launchd agents and daemons from their plists (read-only): a job on a schedule is a timer, the rest are services."""
    out = []
    for folder, domain in LAUNCHD_DIRS.items():
        for f in sorted(Path(folder).expanduser().glob("*.plist"))[:300]:
            try:
                job = plistlib.loads(f.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            label = job.get("Label") or f.stem
            if label.startswith("com.apple."):
                continue
            what = "daemon" if domain == "system" else "agent"
            target = f"{domain}/{os.getuid()}/{label}" if domain == "gui" else f"system/{label}"
            if "StartCalendarInterval" in job or "StartInterval" in job:
                out.append({"id": f"timer:{label}", "kind": "timer", "name": label, "detail": f"launchd {what} on a schedule ({f})", "check": None})
            else:
                out.append({"id": f"service:{label}", "kind": "service", "name": label, "detail": f"launchd {what} ({f})",
                            "check": {"type": "launchd", "target": target}})
    return out


def _windows_tasks() -> list[dict]:
    rows = csv.reader(io.StringIO(_run(["schtasks", "/Query", "/FO", "CSV", "/NH"])))
    # Windows' own tasks live under \Microsoft; the user's are the rest.
    return [{"id": f"timer:{r[0]}", "kind": "timer", "name": r[0], "detail": f"Windows scheduled task ({r[2]})", "check": None}
            for r in rows if len(r) >= 3 and not r[0].startswith("\\Microsoft\\")]


def _windows_services() -> list[dict]:
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Service | Where-Object State -eq Running | Select-Object Name,PathName | ConvertTo-Json -Compress"])
    try:
        svcs = json.loads(raw or "[]")
    except ValueError:
        return []
    windir = os.environ.get("SystemRoot", r"C:\Windows").lower()
    return [{"id": f"service:{s['Name']}", "kind": "service", "name": s["Name"], "detail": f"Windows service ({s.get('PathName') or '?'})",
             "check": {"type": "winservice", "name": s["Name"]}}
            for s in ([svcs] if isinstance(svcs, dict) else svcs)
            if not (s.get("PathName") or "").strip('"').lower().startswith(windir)]  # the OS's own services run from %SystemRoot%


def scout_timers() -> list[dict]:
    """The user's own scheduled jobs (systemd user timers, scheduled launchd jobs, Windows scheduled tasks)."""
    if PLATFORM == "mac":
        return [j for j in _launchd_jobs() if j["kind"] == "timer"]
    if PLATFORM == "windows":
        return _windows_tasks()
    out = []
    for line in _run(["systemctl", "--user", "list-timers", "--all", "--no-legend", "--plain"]).splitlines():
        parts = line.split()
        if not parts:
            continue
        timer = parts[-2] if len(parts) >= 2 and parts[-2].endswith(".timer") else next((p for p in parts if p.endswith(".timer")), "")
        if timer:
            out.append({"id": f"timer:{timer}", "kind": "timer", "name": timer, "detail": "systemd user timer", "check": None})
    return out


def scout_vms() -> list[dict]:
    names = [n for n in _run(["virsh", "-c", "qemu:///system", "list", "--all", "--name"]).split() if n]
    return [{"id": f"vm:{n}", "kind": "vm", "name": n, "detail": "libvirt virtual machine", "check": {"type": "vm", "name": n}} for n in names]


SECRET_MANAGERS = {"op": "1Password CLI", "bw": "Bitwarden CLI", "pass": "pass (GPG store)", "gopass": "gopass",
                   "secret-tool": "the desktop keyring (Secret Service)", "keepassxc-cli": "KeePassXC", "vault": "HashiCorp Vault CLI"}


def scout_secrets() -> list[dict]:
    """Which password managers are here (presence only, nothing opened), so setup can say how secrets should flow."""
    return [{"id": f"secrets:{t}", "kind": "secret-manager", "name": name, "detail": f"{t} at {_which(t)}", "check": None}
            for t, name in SECRET_MANAGERS.items() if _which(t)]


def scout_containers() -> list[dict]:
    out = []
    for engine in ("docker", "podman"):
        if not _which(engine):
            continue
        for line in _run([engine, "ps", "--format", "{{.Names}}\t{{.Image}}"]).splitlines():
            if "\t" in line:
                name, image = line.split("\t", 1)
                out.append({"id": f"container:{engine}:{name}", "kind": "container", "name": name,
                            "detail": f"{engine} container from {image}", "check": {"type": "container", "engine": engine, "name": name}})
    return out


def scout_services() -> list[dict]:
    """Services the user added (user units, and system units outside the OS's own set)."""
    if PLATFORM == "mac":
        return [j for j in _launchd_jobs() if j["kind"] == "service"]
    if PLATFORM == "windows":
        return _windows_services()
    if not _which("systemctl"):
        return []
    out = []
    for scope in (["--user"], []):
        units = [l.split()[0] for l in _run(["systemctl", *scope, "list-units", "--type=service", "--state=running",
                                              "--no-legend", "--plain"]).splitlines() if l.split()]
        if not units:
            continue
        shown = _run(["systemctl", *scope, "show", "-p", "Id", "-p", "FragmentPath", *units]).split("\n\n")
        for block in shown:
            props = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
            unit, path = props.get("Id", ""), props.get("FragmentPath", "")
            # Units shipped by the OS live under /usr/lib; the user's own are in /etc or ~/.config.
            if unit and path and not path.startswith(("/usr/lib", "/lib", "/run/")):  # /run: transient app launches
                out.append({"id": f"service:{unit}", "kind": "service", "name": unit,
                            "detail": f"systemd {'user ' if scope else ''}service ({path})",
                            "check": {"type": "service", "scope": "user" if scope else "system", "name": unit}})
    return out


def scout_ssh_hosts() -> list[dict]:
    try:
        lines = (Path.home() / ".ssh" / "config").read_text().splitlines()
    except OSError:
        return []
    hosts = [h for l in lines if l.strip().lower().startswith("host ") for h in l.split()[1:] if "*" not in h and "?" not in h]
    return [{"id": f"ssh:{h}", "kind": "ssh-host", "name": h, "detail": "host in ~/.ssh/config", "check": None} for h in hosts]


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        socket.create_connection((host, port), timeout=0.3).close()
        return True
    except OSError:
        return False


def scout_ports() -> list[dict]:
    return [{"id": f"port:{p}", "kind": "local-service", "name": name, "detail": f"answering on localhost:{p}",
             "check": {"type": "port", "port": p}} for p, name in PORTS.items() if port_open(p)]


def scout_vpn() -> list[dict]:
    # Presence only: Lantern never runs or changes VPN tools during discovery.
    return [{"id": f"vpn:{t}", "kind": "vpn", "name": t, "detail": _which(t), "check": None}
            for t in ("tailscale", "wg", "zerotier-cli") if _which(t)]


SCOUTS = {2: [scout_system, scout_repos, scout_tools, scout_secrets, scout_alerts],
          3: [scout_containers, scout_vms, scout_services, scout_timers, scout_apps, scout_ssh_hosts, scout_ports, scout_vpn]}


def discover(depth: int) -> list[dict]:
    WALLS.clear()
    finds = []
    for level in range(2, min(depth, 3) + 1):
        for scout in SCOUTS[level]:
            finds += scout()
    return finds


def card(find: dict) -> str:
    if find["kind"] == "machine":
        return f"This machine, {find['name']}: {find['detail']}."
    return f"On this machine: {find['name']} ({find['kind']}), {find['detail']}."


def accept(finds: list[dict]) -> None:
    """Write capability cards to memory (replacing older cards for the same find) and register their checks."""
    s = settings()
    caps = s.setdefault("capabilities", {})
    db = memory.connect()
    try:
        for f in finds:
            source = f"discovery:{f['id']}"
            for (old,) in db.execute("SELECT id FROM items WHERE source=? AND status='active'", (source,)).fetchall():
                memory.supersede(db, old)
            memory.add(db, "fact", source, card(f))
            caps[f["id"]] = {"name": f["name"], "check": f["check"]}
    finally:
        db.close()
    save_settings(s)
    from . import packs
    packs.for_finds(finds)  # a pack switches on the moment its system is found: no install, no restart


def healthy(check: dict) -> bool | None:
    """True/False for a capability's health, None when it has no check."""
    if not check:
        return None
    t = check["type"]
    if t == "port":
        return port_open(check["port"])
    if t == "path":
        return Path(check["path"]).exists()
    if t == "tool":
        return _which(check["name"]) is not None
    if t == "vm":
        return check["name"] in _run(["virsh", "-c", "qemu:///system", "list", "--name"]).split()
    if t == "container":
        return check["name"] in _run([check["engine"], "ps", "--format", "{{.Names}}"]).split()
    if t == "service":
        scope = ["--user"] if check["scope"] == "user" else []
        return _run(["systemctl", *scope, "is-active", check["name"]]).strip() == "active"
    if t == "launchd":
        return "state = running" in _run(["launchctl", "print", check["target"]])
    if t == "winservice":
        return "RUNNING" in _run(["sc", "query", check["name"]])
    return None


KIND_LABELS = {"machine": "machine", "repo": "projects", "tool": "dev tools", "container": "containers", "vm": "virtual machines",
               "service": "services", "timer": "scheduled jobs", "app": "apps", "ssh-host": "SSH hosts", "local-service": "local servers", "vpn": "VPN",
               "secret-manager": "password managers", "alert-channel": "alert channels"}


def describe(finds: list[dict]) -> str:
    """The finds grouped by kind with names, for a reply that says what was found without digging."""
    groups: dict[str, list[str]] = {}
    for f in finds:
        groups.setdefault(KIND_LABELS.get(f["kind"], f["kind"]), []).append(f["name"])
    lines = []
    for label, names in groups.items():
        shown = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
        lines.append(f"  {label} ({len(names)}): {shown}")
    return "\n".join(lines)


def summary() -> str:
    """What is known about this machine, in one short paragraph for a session start."""
    caps = settings().get("capabilities", {})
    if not caps:
        return ""
    kinds: dict[str, list[str]] = {}
    for cid, c in caps.items():
        kinds.setdefault(cid.split(":", 1)[0], []).append(c["name"])
    labels = {"system": "machine", "repo": "projects", "tool": "dev tools", "container": "containers", "vm": "virtual machines",
              "service": "services", "timer": "scheduled jobs", "app": "apps", "ssh": "SSH hosts", "port": "local servers", "vpn": "VPN", "secrets": "password managers", "alert": "alert channels to the user"}
    parts = []
    for k, names in kinds.items():
        if k == "system":
            parts.insert(0, "machine " + ", ".join(names))
        else:
            shown = ", ".join(sorted(names)[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
            parts.append(f"{len(names)} {labels.get(k, k)} ({shown})")
    return "; ".join(parts)


# The local services that need a key before Lantern can do more than see them answer, and the name the key goes by.
API_SECRETS = {"Plex": "PLEX_TOKEN", "Home Assistant": "HASS_TOKEN", "UniFi Network": "UNIFI_PASSWORD", "Uptime Kuma": "UPTIME_KUMA_PASSWORD",
               "Jellyfin": "JELLYFIN_API_KEY", "Sonarr": "SONARR_API_KEY", "Radarr": "RADARR_API_KEY", "Prowlarr": "PROWLARR_API_KEY",
               "Overseerr/Jellyseerr": "OVERSEERR_API_KEY", "Tautulli": "TAUTULLI_API_KEY", "Portainer": "PORTAINER_API_KEY"}


def frontiers() -> list[dict]:
    """Where discovery stopped: each wall as a finding (what, why Lantern cannot pass it, what passing it would unlock,
    the one-line ask). A frontier closes by itself once what it needs is there (a key stored, a manager signed in, a
    wider look allowed), because this is recomputed, never remembered."""
    from . import shield, sshkey
    s = settings()
    out = []
    if s.get("depth", 2) < 3:
        out.append(frontier("depth", "the rest of this machine", "the look is limited to projects and tools (depth 2)",
                            "containers, services, VMs, SSH hosts and local servers", "say 'look wider' and Lantern scouts them, read-only"))
    if IN_CONTAINER and not HOST_EXEC:
        out.append(frontier("host", "the host behind this container", "this session runs in a container with no way to the host",
                            "the host's containers, services and tools", "start the session on the host, or allow distrobox-host-exec"))
    for m in shield.manager_status()["managers"]:
        if not m["ready"]:
            out.append(frontier(f"secrets:{m['tool']}", m["name"], m["why"], "reading linked items at run time, nothing pasted into chat", m["ask"]))
    held = set(shield.names()) | set(shield.links())
    for cid, c in s.get("capabilities", {}).items():
        if cid.startswith("ssh:") and c["name"] not in sshkey.grants():
            out.append(frontier(cid, c["name"], "Lantern has no key of its own on this host", "checking its services, disks and backups over SSH",
                                f"one tap on your phone and Lantern installs its own key on {c['name']} (nothing typed)"))
        need = API_SECRETS.get(c["name"]) if cid.startswith("port:") else None
        if need and need not in held:
            out.append(frontier(cid, c["name"], "it answers locally but Lantern has no API key for it", "watching its health, queue and errors",
                                f"ask for its API key in plain words (`lantern secret ask {need}` gives the sentence to show); "
                                "the pasted key goes to the keychain and never reaches the model"))
    return out + s.get("walls", [])


def refresh_frontiers() -> list[dict]:
    """Recompute and cache the frontiers (a session start and each discovery run), so the prompt hook reads a file, not CLIs."""
    fs = frontiers()
    save_settings({**settings(), "frontiers": fs})
    return fs


def frontier_block() -> str:
    fs = settings().get("frontiers") or []
    if not fs:
        return ""
    return ("Beyond Lantern's reach for now (in any service map or table show these greyed: in italics with '(not reachable yet)'; "
            "never nag: raise one only when a task or duty needs what is behind it, then in one line with its ask):\n"
            + "\n".join(f"- {f['name']}: {f['why']}; would unlock {f['unlocks']}. Ask: {f['ask']}" for f in fs))


def frontiers_touching(prompt: str) -> list[str]:
    """Frontiers whose subject the prompt names, so the ask is raised exactly when it is relevant."""
    words = set(memory.terms(prompt))
    return [f"{f['name']}: {f['why']}; one-line ask if the task needs it: {f['ask']}"
            for f in settings().get("frontiers") or [] if set(memory.terms(f["name"])) & words]


def auto(now: float | None = None) -> list[dict]:
    """Daily: scout within the depth the user chose, remember what is new, and say so on Stage (once). The depth is
    the consent boundary; within it nothing needs accepting by hand."""
    from . import memory, stage
    s = settings()
    known = s.get("capabilities", {})
    finds = [f for f in discover(min(3, s.get("depth", 2))) if f["id"] not in known]
    save_settings({**settings(), "walls": list(WALLS)})
    if not finds:
        refresh_frontiers()
        return []
    accept(finds)
    refresh_frontiers()
    msg = "Found on this machine and remembered: " + ", ".join(f["name"] for f in finds[:12])
    if len(finds) > 12:
        msg += f" and {len(finds) - 12} more"
    db = memory.connect()
    try:
        stage.publish(db, "discovery/new", "discovery", msg, score=3, days=2, now=now)
    finally:
        db.close()
    return finds


def cli(argv: list[str]) -> int:
    """lantern discover [--accept all|1,2,...]   lantern depth [1-4]"""
    s = settings()
    if argv[:1] == ["depth"]:
        if len(argv) == 2 and argv[1] in "1234":
            s["depth"] = min(3, int(argv[1]))
            save_settings(s)
            found = auto()  # a wider look takes effect at once, not tomorrow
            print(f"depth {s['depth']}, {DEPTHS[s['depth']]}; found and remembered {len(found)} new things")
            print(describe(found))
            return 0
        print(f"depth {min(3, s['depth'])}, {DEPTHS[min(3, s['depth'])]}")
        return 0
    depth = s.get("depth", 2)
    if depth < 2:
        print("Depth is 1 (conversations only), so there is nothing to scout. Raise it with `lantern depth 2` or `3`.")
        return 0
    known = s.get("capabilities", {})
    finds = [f for f in discover(depth) if f["id"] not in known]
    if "--accept" in argv:
        pick = argv[argv.index("--accept") + 1] if len(argv) > argv.index("--accept") + 1 else ""
        chosen = finds if pick == "all" else [finds[int(i) - 1] for i in pick.split(",") if i.isdigit() and 0 < int(i) <= len(finds)]
        accept(chosen)
        print(f"remembered {len(chosen)} capabilities")
        return 0
    if not finds:
        print("Nothing new found.")
    for n, f in enumerate(finds, 1):
        print(f"{n:3}. [{f['kind']}] {f['name']}: {f['detail']}")
    if finds:
        print("\nRemember these with `lantern discover --accept all` or pick numbers, e.g. `--accept 1,3`.")
    return 0
