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
from .paths import ensure_data_dir

MARK = "lantern.py\" status"  # how Lantern recognises its own status line command


def settings_file() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def command() -> str:
    return f'uv run --no-project --quiet "{Path(__file__).resolve().parents[1] / "lantern.py"}" status'


def in_terminal() -> bool:
    """Only the terminal renders a status line; the desktop app, the web and the SDK start Claude Code as sdk-cli."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") == "cli"


def trial() -> bool:
    """A plugin loaded from a folder (`--plugin-dir`: Claude Code keeps its data under "lantern-inline") is a trial run
    or an eval; it must never point the user's real status line at a checkout under test."""
    return "lantern-inline" in ensure_data_dir().parts


def _write(path: Path, cfg: dict) -> None:
    if path.exists():  # one backup, the one before Lantern's first write; later writes keep it
        bak = path.with_name("settings.json.lantern-bak")
        if not bak.exists():
            bak.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("settings.json.lantern-tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure() -> tuple[str, str, bool]:
    """(ok|warn, message, changed). Installs once, repairs its own stale command, and leaves alone a status line the
    user removed or replaced afterwards: that was their choice."""
    if trial():
        return "ok", "status line left alone (plugin loaded from a folder)", False
    path = settings_file()
    try:
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
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
