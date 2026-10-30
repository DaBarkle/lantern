"""Shield: raw secrets never go into a command, a file or a Stage note.

Detection is built in (no rules file that can go missing and switch it off). Commands that need a secret run through
`lantern secret run NAME -- <command>`, which puts the value in the command's environment, never in the prompt.
Values live in the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) via `keyring`.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
                 f"{', '.join(hits)}. Ask the user for it with `lantern secret ask NAME` (show them its sentence, never syntax), "
                 "then run the command as `lantern secret run NAME -- <command>`, which passes it as the $NAME environment variable.")


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


def _links_file():
    return ensure_data_dir() / "secret-links.json"


def links() -> dict:
    """NAME -> a password-manager reference; the manager's own CLI resolves it when a command runs, nothing is copied."""
    try:
        return json.loads(_links_file().read_text())
    except (OSError, ValueError):
        return {}


# --- bridges: a reference per manager, read through that manager's CLI at run time (never an integration) ---
# scheme: (CLI, name, how a reference is written)
MANAGERS = {"op": ("op", "1Password", "op://vault/item/field"),
            "bw": ("bw", "Bitwarden", "bw://item[/password|username|totp|notes]"),
            "pass": ("pass", "pass", "pass://path/in/store"),
            "keepass": ("keepassxc-cli", "KeePassXC", "keepass:///path/to/db.kdbx/group/entry")}
# What a locked manager needs, held in the keychain like any other secret, so a session never types it.
UNLOCK = {"bw": "BW_SESSION", "keepass": "KEEPASSXC_PASSWORD"}


def _read(ref: str, kr) -> str | None:
    scheme, _, rest = ref.partition("://")
    env, stdin = dict(os.environ), None
    if scheme == "op":
        cmd = ["op", "read", ref]
    elif scheme == "bw":
        item, _, field = rest.partition("/")
        cmd = ["bw", "get", field or "password", item]
        env["BW_SESSION"] = kr.get_password(SERVICE, UNLOCK["bw"]) or env.get("BW_SESSION", "")
    elif scheme == "pass":
        cmd = ["pass", "show", rest]
    elif scheme == "keepass":
        db, sep, entry = rest.partition(".kdbx/")
        cmd = ["keepassxc-cli", "show", "-q", "-s", "-a", "Password", db + ".kdbx", entry]
        stdin = kr.get_password(SERVICE, UNLOCK["keepass"])
        if not sep or stdin is None:
            return None
    else:
        return None
    try:
        r = subprocess.run(cmd, input=stdin, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.splitlines()[0].strip() if r.returncode == 0 and r.stdout.strip() else None


def _ready(scheme: str, held: set[str]) -> tuple[bool, str, str]:
    """Whether a manager can answer now; if not, why and the one-line ask."""
    if scheme == "op":
        try:  # `account list` needs no unlock, so a session start never pops the 1Password prompt; `op read` does, once, when a command needs it
            r = subprocess.run(["op", "account", "list"], capture_output=True, text=True, timeout=10)
            ok = r.returncode == 0 and bool(r.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        return ok, "the 1Password CLI is installed but no account is added to it", \
            "add it once (`op account add`, or turn on the desktop app's CLI integration); Lantern then reads linked items at run time"
    if scheme == "bw":
        ok = UNLOCK["bw"] in held or bool(os.environ.get("BW_SESSION"))
        return ok, "the Bitwarden CLI is installed but its vault is locked", \
            "ask once for the session key from `bw unlock --raw` (`lantern secret ask BW_SESSION` gives the sentence to show)"
    if scheme == "pass":
        return (Path.home() / ".password-store").is_dir(), "pass is installed but has no store", "run `pass init <gpg-id>` once"
    return UNLOCK["keepass"] in held, "KeePassXC is installed but Lantern has no database password", \
        "ask once for the database password (`lantern secret ask KEEPASSXC_PASSWORD` gives the sentence to show)"


def manager_status() -> dict:
    """What can hold secrets here: each installed password manager (ready or not, and why) and the OS keychain."""
    held = set(names())
    managers = []
    for scheme, (tool, name, form) in MANAGERS.items():
        where = shutil.which(tool)
        if not where:
            continue
        ok, why, ask = _ready(scheme, held)
        managers.append({"scheme": scheme, "tool": tool, "name": name, "path": where, "form": form, "ready": ok,
                         "why": "" if ok else why, "ask": "" if ok else ask})
    return {"managers": managers, "keychain": _keyring() is not None or shutil.which("uv") is not None,
            "stored": names(), "linked": links()}


PASTE = re.compile(r"^\s*secret\s+([A-Za-z][A-Za-z0-9_]{0,63})\s*[:=]\s*(\S{4,})\s*$")
BARE = re.compile(r"^\s*(\S{8,512})\s*$")  # a value pasted on its own, once Lantern has asked for one
ASK_SECONDS = 600  # an ask is answered by the next message or not at all: a bare word ten minutes later is not a secret


def ask(name: str) -> str:
    """Record that Lantern is waiting for NAME and give the one plain sentence to show the user: no syntax to type."""
    (ensure_data_dir() / "secret-ask.json").write_text(json.dumps({"name": name, "at": time.time()}))
    what = name.replace("_", " ").lower()
    return f"Paste the {what} as your next message, on its own. It goes straight into your keychain and never reaches me."


def pending() -> str | None:
    try:
        a = json.loads((ensure_data_dir() / "secret-ask.json").read_text())
        return a["name"] if time.time() - a["at"] < ASK_SECONDS else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def paste(prompt: str) -> tuple[str | None, bool]:
    """A prompt of the form `secret NAME: value` is a hand-over: store the value in the keychain and return (NAME, stored),
    so the hook can stop the prompt before it reaches the model whether or not this session has a keychain. The value
    is never printed or logged."""
    m = PASTE.match(prompt)
    if m:
        name, value = m.group(1), m.group(2)
    elif pending() and BARE.match(prompt):  # the answer to an ask: the value alone, no syntax
        name, value = pending(), BARE.match(prompt).group(1)
    else:
        return None, False
    (ensure_data_dir() / "secret-ask.json").unlink(missing_ok=True)
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lantern.py")
    try:
        r = subprocess.run(["uv", "run", "--no-project", "--quiet", "--with", "keyring", script, "secret", "set", name],
                           input=value, capture_output=True, text=True, timeout=60, env={**os.environ, "LANTERN_HOME": str(ensure_data_dir())})
        return name, r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return name, False


def cli(argv: list[str]) -> int:
    """lantern secret ask NAME | set NAME | run NAME[,NAME...] -- CMD... | list | delete NAME | link NAME <manager reference> | status
    References: op://vault/item/field, bw://item[/field], pass://path, keepass:///db.kdbx/entry"""
    if argv[:1] == ["list"]:
        print("\n".join(sorted(set(names()) | set(links()))))
        return 0
    if argv[:1] == ["ask"] and len(argv) == 2 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", argv[1]):
        print(ask(argv[1]))
        return 0
    if argv[:1] == ["status"]:
        st = manager_status()
        print(json.dumps({**st, "stored": len(st["stored"]), "linked": len(st["linked"])}, indent=1))
        return 0
    if argv[:1] == ["link"] and len(argv) == 3 and argv[2].partition("://")[0] in MANAGERS:
        _links_file().write_text(json.dumps({**links(), argv[1]: argv[2]}))
        print(f"{argv[1]} will be read from {MANAGERS[argv[2].partition('://')[0]][1]} when a command needs it")
        return 0
    kr = _keyring()
    if kr is None:
        return _reexec_with_keyring(["secret", *argv])
    if argv[:1] == ["set"] and len(argv) == 2:
        import getpass
        value = getpass.getpass(f"value for {argv[1]}: ") if sys.stdin.isatty() else sys.stdin.read().strip()
        try:
            kr.set_password(SERVICE, argv[1], value)
        except kr.errors.KeyringError as e:  # a device-less session (cloud, no Secret Service) holds no machine secrets
            print(f"lantern: this session has no keychain ({e}); the secret was not stored anywhere. Hand it over in a "
                  "session on your own machine.", file=sys.stderr)
            return 1
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
            if value is None and name in links():
                value = _read(links()[name], kr)
                if value is None:
                    print(f"lantern: {name} is linked to {links()[name]} but that manager did not answer "
                          f"(`lantern secret status` says why)", file=sys.stderr)
                    return 1
            if value is None:
                print(f"lantern: no secret named {name!r}; hand it over in chat as `secret {name}: <value>`", file=sys.stderr)
                return 1
            env[name] = value
        return subprocess.call(argv[split + 1:], env=env)
    print(cli.__doc__, file=sys.stderr)
    return 2
