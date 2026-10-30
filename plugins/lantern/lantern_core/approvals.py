"""Approvals on your phone through ntfy, outbound-only: Lantern posts a question and polls for the answer, so nothing
listens on this machine and no VPN or open port is needed.

The topic name is the shared secret (long and random, made on first use). Each request carries a one-time nonce, so
an old tap can't answer a new question. Install the ntfy app and subscribe to the topic `lantern approve setup` shows.
"""
import json
import secrets
import time
import urllib.parse
import urllib.request

from .paths import ensure_data_dir

SERVER = "https://ntfy.sh"
POLL_SECONDS = 10  # ntfy.sh answers 429 to a poll every few seconds
RETRY_SECONDS = 30
APPROVED, DENIED, TIMED_OUT = 0, 1, 3


def ready() -> str | None:
    """Why a question cannot be asked yet: the topic only exists once setup showed it, so before that a question
    would wait on a phone that is not listening. None when approvals are set up."""
    if (ensure_data_dir() / "approvals.json").exists():
        return None
    return "phone approvals aren't set up yet: show the ntfy topic with `lantern approve setup`, subscribe in the app, then ask again"


def config() -> dict:
    path = ensure_data_dir() / "approvals.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        cfg = {"server": SERVER, "topic": "lantern-" + secrets.token_urlsafe(18)}
        path.write_text(json.dumps(cfg))
        try:
            path.chmod(0o600)
        except OSError:
            pass  # Windows: the per-user data folder already limits access
        return cfg


def _post(url: str, body: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


def _confirm(cfg: dict, approved: bool, question: str) -> None:
    """The tap itself shows nothing on the phone, so say that it landed."""
    try:
        _post(cfg["server"], {"topic": cfg["topic"], "title": "Approved" if approved else "Denied", "priority": 2,
                              "message": question[:200], "tags": ["white_check_mark" if approved else "x"]})
    except OSError:
        pass  # the decision stands; the confirmation is a courtesy


def _replies(cfg: dict, since: int) -> list[str]:
    topic = urllib.parse.quote(cfg["topic"] + "-reply")
    with urllib.request.urlopen(f"{cfg['server']}/{topic}/json?poll=1&since={since}", timeout=15) as r:
        return [json.loads(l).get("message", "") for l in r.read().decode().splitlines() if l.strip()]


def ask(question: str, timeout: float = 600, cfg: dict | None = None) -> int:
    cfg = cfg or config()
    nonce = secrets.token_hex(4)
    reply = f"{cfg['server']}/{cfg['topic']}-reply"
    since = int(time.time()) - 1
    _post(cfg["server"], {
        "topic": cfg["topic"], "title": "Lantern needs your OK", "message": question[:1000], "priority": 4,
        "actions": [{"action": "http", "label": "Approve", "url": reply, "method": "POST", "body": f"approve {nonce}", "clear": True},
                    {"action": "http", "label": "Deny", "url": reply, "method": "POST", "body": f"deny {nonce}", "clear": True}]})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            replies = _replies(cfg, since)
        except (OSError, ValueError):  # rate limit or a network blip: the answer will still be there next time
            time.sleep(min(RETRY_SECONDS, max(0.0, deadline - time.time())))
            continue
        for msg in replies:
            if msg in (f"approve {nonce}", f"deny {nonce}"):
                _confirm(cfg, msg.startswith("approve"), question)
                return APPROVED if msg.startswith("approve") else DENIED
        time.sleep(POLL_SECONDS)
    return TIMED_OUT


def cli(argv: list[str]) -> int:
    """lantern approve setup | lantern approve "<question>" [--timeout SECONDS]   exit 0 approved, 1 denied, 3 timed out"""
    if argv == ["setup"]:
        cfg = config()
        print(f"In the ntfy app, subscribe to topic {cfg['topic']} on {cfg['server']}.\nKeep the topic private: "
              "anyone who knows it can answer.")
        return 0
    if argv and not argv[0].startswith("-"):
        opts = dict(zip(argv[1::2], argv[2::2]))
        try:
            code = ask(argv[0], float(opts.get("--timeout", 600)))
        except OSError as e:
            print(f"could not reach the ntfy server ({e}); the phone was not asked. Is this machine online?")
            return TIMED_OUT
        print({APPROVED: "approved", DENIED: "denied", TIMED_OUT: "no answer"}[code])
        return code
    print(cli.__doc__)
    return 2
