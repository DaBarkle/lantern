"""Discovery: read-only scouts find what is on this machine, up to the depth the user chose. Nothing is remembered
until the user accepts it; an accepted find becomes a capability card in memory plus a health check for the doctor.

Depth (a consent boundary, `lantern depth N`):
  1 conversations  memory of sessions only, no scouts
  2 workspace      git repos and dev tools (the default)
  3 home           containers, services, SSH hosts, well-known local service ports
  4 life           accounts the user connects on purpose (Claude connectors; nothing to scout locally)
"""
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

from . import memory
from .paths import ensure_data_dir

DEV_TOOLS = ["git", "gh", "uv", "python3", "node", "npm", "cargo", "go", "java", "dotnet", "code", "nvim", "docker", "podman"]
# Well-known local services. Only a TCP connect to localhost: nothing is sent.
PORTS = {32400: "Plex", 8123: "Home Assistant", 8443: "UniFi Network", 3001: "Uptime Kuma", 8096: "Jellyfin",
         8989: "Sonarr", 7878: "Radarr", 9696: "Prowlarr", 8080: "SABnzbd or another web app", 5055: "Overseerr/Jellyseerr",
         8181: "Tautulli", 8265: "Tdarr", 9000: "Portainer", 1883: "MQTT broker"}
SKIP_DIRS = {"node_modules", ".cache", ".local", ".var", "Library", "AppData", ".cargo", ".rustup", "go", ".npm"}
TIMEOUT = 10
DEPTHS = {1: "conversations: memory of sessions only, no scouts", 2: "workspace (default): git repos and dev tools",
          3: "home: containers, services, SSH hosts, well-known local service ports",
          4: "life: also accounts you connect on purpose through Claude connectors"}


def settings_path() -> Path:
    return ensure_data_dir() / "settings.json"


def settings() -> dict:
    try:
        return json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {"depth": 2}  # workspace by default: repos and dev tools are harmless to know and make Lantern useful at once


def save_settings(s: dict) -> None:
    settings_path().write_text(json.dumps(s, indent=2))


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def scout_repos(root: Path | None = None, max_depth: int = 3) -> list[dict]:
    root = root or Path.home()
    out = []

    def walk(d: Path, depth: int):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        if any(e.name == ".git" for e in entries):
            out.append({"id": f"repo:{d}", "kind": "repo", "name": d.name, "detail": str(d), "check": {"type": "path", "path": str(d)}})
            return  # a repo's subfolders are part of it
        if depth < max_depth:
            for e in entries:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP_DIRS \
                        and not any(w in e.name.lower() for w in ("archive", "backup", "old")):  # not live work
                    walk(Path(e.path), depth + 1)

    walk(root, 0)
    return out


def scout_tools() -> list[dict]:
    return [{"id": f"tool:{t}", "kind": "tool", "name": t, "detail": shutil.which(t), "check": {"type": "tool", "name": t}}
            for t in DEV_TOOLS if shutil.which(t)]


def scout_containers() -> list[dict]:
    out = []
    for engine in ("docker", "podman"):
        if not shutil.which(engine):
            continue
        for line in _run([engine, "ps", "--format", "{{.Names}}\t{{.Image}}"]).splitlines():
            if "\t" in line:
                name, image = line.split("\t", 1)
                out.append({"id": f"container:{engine}:{name}", "kind": "container", "name": name,
                            "detail": f"{engine} container from {image}", "check": {"type": "container", "engine": engine, "name": name}})
    return out


def scout_services() -> list[dict]:
    """Services the user added (user units, and system units outside the OS's own set)."""
    if not shutil.which("systemctl"):
        return []  # macOS launchd / Windows services: shortcut: not scouted yet, add when a user on them asks
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
    return [{"id": f"vpn:{t}", "kind": "vpn", "name": t, "detail": shutil.which(t), "check": None}
            for t in ("tailscale", "wg", "zerotier-cli") if shutil.which(t)]


SCOUTS = {2: [scout_repos, scout_tools], 3: [scout_containers, scout_services, scout_ssh_hosts, scout_ports, scout_vpn]}


def discover(depth: int) -> list[dict]:
    finds = []
    for level in range(2, min(depth, 3) + 1):
        for scout in SCOUTS[level]:
            finds += scout()
    return finds


def card(find: dict) -> str:
    return f"Capability on this machine: {find['name']} ({find['kind']}), {find['detail']}."


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
        return shutil.which(check["name"]) is not None
    if t == "container":
        return check["name"] in _run([check["engine"], "ps", "--format", "{{.Names}}"]).split()
    if t == "service":
        scope = ["--user"] if check["scope"] == "user" else []
        return _run(["systemctl", *scope, "is-active", check["name"]]).strip() == "active"
    return None


PACKS = {"docker": "lantern-docker", "podman": "lantern-docker", "tailscale": "lantern-tailscale", "Home Assistant": "lantern-home-assistant",
         "Plex": "lantern-media-server", "Jellyfin": "lantern-media-server", "Sonarr": "lantern-media-server", "Radarr": "lantern-media-server",
         "UniFi Network": "lantern-unifi"}


def auto(now: float | None = None) -> list[dict]:
    """Daily: scout within the depth the user chose, remember what is new, and say so on Stage (once). The depth is
    the consent boundary; within it nothing needs accepting by hand."""
    from . import memory, stage
    s = settings()
    known = s.get("capabilities", {})
    finds = [f for f in discover(s.get("depth", 2)) if f["id"] not in known]
    if not finds:
        return []
    accept(finds)
    packs = sorted({PACKS[f["name"]] for f in finds if f["name"] in PACKS})
    msg = "Found on this machine and remembered: " + ", ".join(f["name"] for f in finds[:12])
    if len(finds) > 12:
        msg += f" and {len(finds) - 12} more"
    if packs:
        msg += ". Packs that add skills for these: " + ", ".join(packs) + " (claude plugin install <pack>@lantern)"
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
            s["depth"] = int(argv[1])
            save_settings(s)
        print(f"depth {s['depth']}, {DEPTHS[s['depth']]}")
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
