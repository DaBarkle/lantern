"""Claude Code hook entry points. Each reads the event JSON on stdin and may print a JSON reply."""
import json
import sys

from . import __version__
from .paths import ensure_data_dir


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def session_start(payload: dict) -> dict | None:
    d = ensure_data_dir()
    return _context("SessionStart", f"Lantern {__version__} is active. Its data folder is {d}.")


HANDLERS = {"session-start": session_start}


def run(name: str, stdin=sys.stdin, stdout=sys.stdout) -> int:
    handler = HANDLERS.get(name)
    if handler is None:
        print(f"lantern: unknown hook {name!r}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    reply = handler(payload)
    if reply:
        stdout.write(json.dumps(reply))
    return 0
