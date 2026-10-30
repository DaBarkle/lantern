"""Where Lantern keeps its state, on any OS."""
import json
import os
import re
import sys
from pathlib import Path


PROFILE_FILE = ".lantern.json"  # {"profile": "work"} in a folder (or a parent) keeps that folder's memory apart


def profile(start: Path | None = None) -> str | None:
    """The profile named by the nearest .lantern.json at or above the working folder, if any."""
    d = (start or Path.cwd()).resolve()
    for folder in (d, *d.parents):
        f = folder / PROFILE_FILE
        if f.is_file():
            try:
                name = json.loads(f.read_text()).get("profile")
            except (OSError, ValueError):
                return None
            return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) else None
        if folder == Path.home():
            break
    return None


def data_dir() -> Path:
    # Inside Claude Code the plugin data dir survives updates; outside it (scheduler jobs) use the OS default.
    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        base = Path(os.environ["CLAUDE_PLUGIN_DATA"])
    elif os.environ.get("LANTERN_HOME"):
        base = Path(os.environ["LANTERN_HOME"])
    else:
        home = Path.home()
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "lantern"
        elif sys.platform == "darwin":
            base = home / "Library" / "Application Support" / "lantern"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "lantern"
    name = profile()
    return base / "profiles" / name if name else base


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
