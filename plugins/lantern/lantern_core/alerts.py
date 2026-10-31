"""Phone alerts: one ntfy line when a watch newly trips while no session is open, one line when findings clear.

Rides on the approvals topic, so there is nothing extra to set up; until phone approvals are ready nothing is sent.
A session start marks the open findings as heard without sending (the user is reading them in the session), so the
phone only learns what tripped while they were away. alerts.json remembers what the phone has heard.
"""
import json
import subprocess
import time
from pathlib import Path

from . import approvals, discovery, schedule, stage
from .paths import data_dir, ensure_data_dir, write_atomic

KINDS = ("failed", "down", "disk", "backup")  # worth a buzz; project work does not change while the user is away
MAX_LINES = 5  # per run; further new findings fold into one line


def settings() -> dict:
    s = discovery.settings().get("alerts") or {}
    return {"on": s.get("on", True), "kinds": s.get("kinds") or list(KINDS)}


def _state() -> dict:
    try:
        return json.loads((ensure_data_dir() / "alerts.json").read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    write_atomic(ensure_data_dir() / "alerts.json", json.dumps(st))


def open_findings(db) -> dict[str, str]:
    kinds = set(settings()["kinds"])
    return {e["key"]: e["message"] for e in stage.top(db, k=200)
            if e["source"] == "insight" and e["key"].split("/")[1] in kinds}


def _short(msg: str) -> str:
    return msg.split(";")[0].split(" (")[0][:120]


def run(db, send: bool = True, now: float | None = None) -> list[str]:
    """Send the phone what is new since it last heard, then what cleared. Returns the lines sent. A line that could not
    be posted stays unsent, so the next run tries again."""
    st = _state()
    kinds = set(settings()["kinds"])
    heard = {k: v for k, v in st.get("heard", {}).items() if k.split("/")[1] in kinds}  # a kind switched off is not "resolved"
    current = open_findings(db)
    st["ran"] = now or time.time()
    lines: list[str] = []
    if not send or not settings()["on"] or approvals.ready() is not None:
        st["heard"] = current  # nothing to send: what is open now counts as heard, so a later switch-on is not a flood
        _save(st)
        return lines
    try:
        cfg = approvals.config()
    except OSError:
        return lines  # the channel's file is unreadable: nothing sent, nothing marked heard; the doctor says why
    new = [k for k in current if k not in heard]
    gone = [k for k in heard if k not in current]
    try:
        for k in new[:MAX_LINES]:
            _post(cfg, current[k])
            heard[k] = current[k]
            lines.append(current[k])
        if len(new) > MAX_LINES:
            rest = f"and {len(new) - MAX_LINES} more new findings; they are on Stage in your next session"
            _post(cfg, rest)
            lines.append(rest)
            heard.update({k: current[k] for k in new[MAX_LINES:]})
        if gone:
            line = "resolved: " + "; ".join(_short(heard[k]) for k in gone)
            _post(cfg, line)
            lines.append(line)
            for k in gone:
                del heard[k]
    except OSError:
        pass  # offline: whatever was posted is heard; the rest goes next run
    st["heard"] = heard
    _save(st)
    return lines


def _post(cfg: dict, message: str) -> None:
    approvals._post(cfg["server"], {"topic": cfg["topic"], "title": "Lantern watch", "message": message[:1000], "priority": 3})


def ensure_scheduled(entry: Path) -> bool:
    """Install the hourly watch timer (user scope, read-only checks) once phone approvals exist, once per plugin copy,
    so nobody has to run `schedule --apply`. True when it was installed just now."""
    s = discovery.settings()
    if approvals.ready() is not None or not settings()["on"] or s.get("watch_scheduled") == str(entry):
        return False
    try:
        schedule.apply(schedule.plan("watch", None, entry, data=data_dir()))
    except (OSError, subprocess.CalledProcessError):
        return False  # no scheduler reachable (a container, say): session starts and the nightly doctor still check
    discovery.save_settings({**discovery.settings(), "watch_scheduled": str(entry)})
    return True


def status() -> str:
    s = settings()
    at = _state().get("ran")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at else "never"
    channel = ("sends to the phone" if approvals.ready() is None else "the phone is not linked yet (if the user wants alerts, run "
               "`approve setup` yourself and show its two steps in plain words; never ask the user to run or type anything)")
    timer = "hourly check scheduled" if discovery.settings().get("watch_scheduled") else "hourly check not scheduled here (session starts and the nightly doctor still check)"
    return (f"phone alerts {'on' if s['on'] else 'off'} for newly tripped watches ({', '.join(s['kinds'])}); "
            f"{channel}; {timer}; last watch check {when}")


def cli(argv: list[str]) -> int:
    """lantern alerts | alerts on | alerts off | alerts kinds failed,down,disk,backup"""
    s = discovery.settings()
    if argv in (["on"], ["off"]):
        s["alerts"] = {**settings(), "on": argv == ["on"]}
    elif argv[:1] == ["kinds"] and len(argv) == 2:
        kinds = [k for k in argv[1].split(",") if k in KINDS]
        if not kinds:
            print(f"kinds are any of {', '.join(KINDS)}")
            return 2
        s["alerts"] = {**settings(), "kinds": kinds}
    elif argv:
        print(cli.__doc__)
        return 2
    if argv:
        discovery.save_settings(s)
    print(status())
    return 0
