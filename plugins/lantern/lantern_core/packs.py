"""Packs: skills for the systems a machine runs (containers, media server, Home Assistant, UniFi, Tailscale), shipped
inside the core plugin and switched on without an install or a restart.

A pack's SKILL.md lives in packs/<name>/. Switching a pack on copies it to the user's skills folder, which Claude Code
watches during a session, so the skill appears in the listing within the same session. Its guard (packs/<name>/guard.py)
runs from the core PreToolUse hook while the pack is on. Discovery switches a pack on the moment its system is found;
`lantern pack on NAME` does it on the user's yes; `pack off` removes the copy again.
"""
import importlib.util
import os
import time
from pathlib import Path

from . import discovery

ROOT = Path(__file__).resolve().parents[1] / "packs"
# Which find switches which pack on (the find's name, as discovery reports it).
TRIGGERS = {"docker": "docker", "podman": "docker", "tailscale": "tailscale", "Home Assistant": "home-assistant",
            "Plex": "media-server", "Jellyfin": "media-server", "Sonarr": "media-server", "Radarr": "media-server",
            "UniFi Network": "unifi"}


def names() -> list[str]:
    return sorted(p.name for p in ROOT.iterdir() if (p / "SKILL.md").exists())


def skills_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "skills"


def active() -> dict:
    return discovery.settings().get("packs") or {}


def _copy(name: str) -> Path:
    return skills_dir() / f"lantern-{name}" / "SKILL.md"


def sync() -> list[str]:
    """Make every active pack's skill copy current (a plugin update changes the source); returns what was rewritten."""
    skills_dir().mkdir(parents=True, exist_ok=True)  # exists before any skill lands in it, so the session's watcher sees the folder
    fixed = []
    for name in active():
        src = ROOT / name / "SKILL.md"
        dst = _copy(name)
        if src.exists() and (not dst.exists() or dst.read_bytes() != src.read_bytes()):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            fixed.append(name)
    return fixed


def activate(name: str, via: str) -> bool:
    """On, with its skill in the listing; False when it is already on or no such pack exists."""
    if name not in names() or name in active():
        return False
    s = discovery.settings()
    s.setdefault("packs", {})[name] = {"since": time.time(), "via": via}
    discovery.save_settings(s)
    sync()
    return True


def deactivate(name: str) -> bool:
    s = discovery.settings()
    if name not in (s.get("packs") or {}):
        return False
    del s["packs"][name]
    discovery.save_settings(s)
    dst = _copy(name)
    if dst.exists():
        dst.unlink()
        dst.parent.rmdir()
    return True


def for_finds(finds: list[dict]) -> list[str]:
    """Switch on every pack whose system is among the finds."""
    return [p for f in finds if (p := TRIGGERS.get(f["name"])) and activate(p, via=f["name"])]


def _guard(name: str):
    path = ROOT / name / "guard.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"lantern_pack_{name}_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pre_tool_use(payload: dict) -> dict | None:
    """The active packs' guards, in one hook: the first that objects decides (deny beats ask)."""
    command = (payload.get("tool_input") or {}).get("command", "")
    if payload.get("tool_name") != "Bash" or not command:
        return None
    verdicts = []
    for name in active():
        g = _guard(name)
        reason = g.check(command) if g else None
        if reason:
            verdicts.append((g.DECISION, reason))
    if not verdicts:
        return None
    decision, reason = min(verdicts, key=lambda v: v[0] != "deny")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}


def cli(argv: list[str]) -> int:
    """lantern pack            lantern pack on NAME | off NAME"""
    if len(argv) == 2 and argv[0] in ("on", "off"):
        if argv[1] not in names():
            print(f"no pack named {argv[1]}; packs: {', '.join(names())}")
            return 1
        changed = activate(argv[1], via="user") if argv[0] == "on" else deactivate(argv[1])
        print(f"pack {argv[1]} {argv[0]}" + ("" if changed else " already") + ("; its skill is in this session's listing now" if changed and argv[0] == "on" else ""))
        return 0
    on = active()
    for name in names():
        state = f"on since {time.strftime('%Y-%m-%d', time.localtime(on[name]['since']))} (" + ("you asked" if on[name]['via'] == "user" else on[name]['via'] + " found") + ")" if name in on else "off"
        print(f"{name:16} {state}")
    return 0
