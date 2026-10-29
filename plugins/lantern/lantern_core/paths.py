"""Where Lantern keeps its state, on any OS."""
import os
import sys
from pathlib import Path


def data_dir() -> Path:
    # Inside Claude Code the plugin data dir survives updates; outside it (scheduler jobs) use the OS default.
    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        return Path(os.environ["CLAUDE_PLUGIN_DATA"])
    if os.environ.get("LANTERN_HOME"):
        return Path(os.environ["LANTERN_HOME"])
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "lantern"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "lantern"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "lantern"


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
