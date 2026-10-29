"""Shield: raw secrets never go into a command, a file or a Stage note.

Detection is built in (no rules file that can go missing and switch it off). Commands that need a secret run through
`lantern secret run NAME -- <command>`, which puts the value in the command's environment, never in the prompt.
Values live in the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) via `keyring`.
"""
import json
import os
import re
import subprocess
import sys

from .paths import ensure_data_dir

PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # name = value, where the value is a long literal with letters and digits; $VAR, ${VAR} and calls don't match
    ("password or token", re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\w*[\"']?\s*[:=]\s*[\"']?"
        r"(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])([A-Za-z0-9+/_-]{16,})")),
]
SERVICE = "lantern"


def find(text: str) -> list[str]:
    return [name for name, rx in PATTERNS if rx.search(text or "")]


def redact(text: str) -> str:
    for name, rx in PATTERNS:
        text = rx.sub(f"[redacted {name}]", text)
    return text


def _tool_text(payload: dict) -> str:
    """Everything a tool call would write or run."""
    i = payload.get("tool_input") or {}
    parts = [i.get("command"), i.get("content"), i.get("new_string"), i.get("new_source")]
    parts += [e.get("new_string") for e in i.get("edits") or [] if isinstance(e, dict)]
    return "\n".join(p for p in parts if isinstance(p, str))


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def pre_tool_use(payload: dict) -> dict | None:
    try:
        hits = find(_tool_text(payload))
    except Exception as e:  # fail closed, and say why, instead of switching off silently
        return _deny(f"Lantern shield could not check this call ({e}); not running it.")
    if not hits:
        return None
    return _deny(f"Lantern shield: this {payload.get('tool_name', 'call')} contains what looks like a raw "
                 f"{', '.join(hits)}. Store it with `lantern secret set NAME` and run the command with "
                 "`lantern secret run NAME -- <command>`, which passes it as the $NAME environment variable.")


# --- secret broker: values in the OS keychain, names in a local index (keychains can't list) ---

def _names_file():
    return ensure_data_dir() / "secret-names.json"


def names() -> list[str]:
    try:
        return json.loads(_names_file().read_text())
    except (OSError, ValueError):
        return []


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def _reexec_with_keyring(argv: list[str]) -> int:
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lantern.py")
    return subprocess.call(["uv", "run", "--no-project", "--quiet", "--with", "keyring", script, *argv])


def cli(argv: list[str]) -> int:
    """lantern secret set NAME | run NAME[,NAME...] -- CMD... | list | delete NAME"""
    if argv[:1] == ["list"]:
        print("\n".join(names()))
        return 0
    kr = _keyring()
    if kr is None:
        return _reexec_with_keyring(["secret", *argv])
    if argv[:1] == ["set"] and len(argv) == 2:
        import getpass
        value = getpass.getpass(f"value for {argv[1]}: ") if sys.stdin.isatty() else sys.stdin.read().strip()
        kr.set_password(SERVICE, argv[1], value)
        _names_file().write_text(json.dumps(sorted(set(names()) | {argv[1]})))
        return 0
    if argv[:1] == ["delete"] and len(argv) == 2:
        try:
            kr.delete_password(SERVICE, argv[1])
        except kr.errors.PasswordDeleteError:
            pass
        _names_file().write_text(json.dumps([n for n in names() if n != argv[1]]))
        return 0
    if argv[:1] == ["run"] and "--" in argv:
        split = argv.index("--")
        env = dict(os.environ)
        for name in ",".join(argv[1:split]).split(","):
            value = kr.get_password(SERVICE, name)
            if value is None:
                print(f"lantern: no secret named {name!r}; add it with `lantern secret set {name}`", file=sys.stderr)
                return 1
            env[name] = value
        return subprocess.call(argv[split + 1:], env=env)
    print(cli.__doc__, file=sys.stderr)
    return 2
