"""Where Lantern keeps its state, on any OS."""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path


def write_atomic(path: Path, text: str, private: bool = False) -> None:
    """Write a whole file or nothing: a reader in another session never sees half of it, and a full disk leaves the old
    copy whole. (In-place writes let a torn read of settings.json come back empty and the next save wipe it.)"""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if private and sys.platform != "win32":
            tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


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


def _os_default() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "lantern"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "lantern"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "lantern"


def _pointer() -> Path:
    """Where this plugin copy's hooks note the data folder Claude Code gave them, so `lantern ...` commands run from a
    shell (which has no CLAUDE_PLUGIN_DATA) use the same folder as the session. One file per copy of the plugin: a
    worktree under test or a second install never redirects another copy's commands."""
    root = Path(__file__).resolve().parents[1]
    return _os_default() / "data-dirs" / hashlib.sha1(str(root).encode()).hexdigest()[:12]


def data_dir() -> Path:
    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        base = Path(os.environ["CLAUDE_PLUGIN_DATA"])
        try:
            if not (_pointer().exists() and _pointer().read_text().strip() == str(base)):
                _pointer().parent.mkdir(parents=True, exist_ok=True)
                _pointer().write_text(str(base))
        except OSError:
            pass
    elif os.environ.get("LANTERN_HOME"):
        base = Path(os.environ["LANTERN_HOME"])
    else:
        try:
            base = Path(_pointer().read_text().strip())
        except OSError:
            base = _os_default()
    name = profile()
    return base / "profiles" / name if name else base


def ensure_data_dir() -> Path:
    d = data_dir()
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            try:
                d.chmod(0o700)  # memory holds the user's conversations: on a shared machine other accounts must not read it
            except OSError:
                pass
    return d


def uv() -> str:
    """The uv that runs Lantern. `uv run` names itself in $UV, so a child started from a scheduler's bare PATH or a GUI
    app's minimal PATH still finds it; the bare name is the last resort."""
    return os.environ.get("UV") or shutil.which("uv") or "uv"
