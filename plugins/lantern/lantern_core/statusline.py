"""Lantern's one-line signal on every turn, with nothing for the user to set up.

In a terminal it is Claude Code's status line: Lantern merges a `statusLine` entry into ~/.claude/settings.json
(backup first, other keys untouched, done once). A status line the user already had keeps running, printed above
Lantern's line. Where no status line renders (the desktop app, the web, the SDK) the Stop hook shows the same line.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import discovery, status

MARK = "lantern.py\" status"  # how Lantern recognises its own status line command


def settings_file() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def command() -> str:
    return f'uv run --no-project --quiet "{Path(__file__).resolve().parents[1] / "lantern.py"}" status'


def in_terminal() -> bool:
    """Only the terminal renders a status line; the desktop app, the web and the SDK start Claude Code as sdk-cli."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") == "cli"


def _write(path: Path, cfg: dict) -> None:
    if path.exists():
        path.with_name(f"settings.json.{time.strftime('%Y%m%d-%H%M%S')}.lantern-bak").write_text(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("settings.json.lantern-tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)


def ensure() -> tuple[str, str, bool]:
    """(ok|warn, message, changed). Installs once, repairs its own stale command, and leaves alone a status line the
    user removed or replaced afterwards: that was their choice."""
    path = settings_file()
    try:
        cfg = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as e:
        return "warn", f"the status line is not set up: {path} could not be read ({e})", False
    if not isinstance(cfg, dict):
        return "warn", f"the status line is not set up: {path} is not a settings object", False
    cur = cfg.get("statusLine") if isinstance(cfg.get("statusLine"), dict) else None
    mine = command()
    s = discovery.settings()
    if cur and MARK in str(cur.get("command", "")):
        if cur["command"] == mine:
            return "ok", "status line is Lantern's", False
        cfg["statusLine"] = {**cur, "command": mine}  # the plugin moved (an update): point at the new copy
    elif s.get("statusline"):
        return "ok", "status line was changed by you; Lantern leaves it", False
    else:
        previous = cur.get("command") if cur and cur.get("type", "command") == "command" else None
        cfg["statusLine"] = {**(cur or {}), "type": "command", "command": mine}
        discovery.save_settings({**s, "statusline": {"installed_at": time.time(), "previous": previous}})
    try:
        _write(path, cfg)
    except OSError as e:
        return "warn", f"the status line could not be written to {path}: {e}", False
    previous = (discovery.settings().get("statusline") or {}).get("previous")
    return "ok", "status line installed" + (f" (your own, {previous!r}, still runs above it)" if previous else ""), True


def cli(stdin=sys.stdin, stdout=sys.stdout) -> int:
    """`lantern status`: the line, after the output of any status line the user had before Lantern's."""
    raw = "" if stdin.isatty() else stdin.read()
    previous = (discovery.settings().get("statusline") or {}).get("previous")
    if previous:
        try:
            out = subprocess.run(previous, shell=True, input=raw, capture_output=True, text=True, timeout=5).stdout
            if out.strip():
                stdout.write(out if out.endswith("\n") else out + "\n")
        except (OSError, subprocess.TimeoutExpired):
            pass
    stdout.write(status.line() + "\n")
    return 0


def signal(payload: dict) -> dict | None:
    """Stop hook, synchronous: where no status line renders, the same line is shown under the reply."""
    return None if in_terminal() else {"systemMessage": status.line()}
