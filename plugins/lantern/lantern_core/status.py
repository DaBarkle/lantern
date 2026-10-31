"""One plain line that tells the user Lantern is working: what it just recalled, what it learned, and whether anything
needs them. For Claude Code's status line (`lantern status`) and the /lantern:status command."""
import json
import re
import time

from . import memory, register, stage
from .paths import ensure_data_dir


def _last(name: str) -> dict:
    """The newest record, reading only the file's tail: the status line is asked for often."""
    try:
        with open(ensure_data_dir() / f"{name}.jsonl", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            lines = [l for l in f.read().decode("utf-8", errors="replace").splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError):
        return {}


def plain_finding(e: dict) -> str:
    """A finding as a non-technical reader would put it: what it means for them, the thing in their own words."""
    kind, _, rest = e["key"].removeprefix("insight/").partition("/")
    msg = e["message"]
    if kind == "disk":
        left = re.search(r"\((\d+ GB) of", msg)
        return "your drive is nearly full" + (f" ({left.group(1)} left)" if left else "")
    if kind == "failed":
        return register.say(f"{rest} stopped working")
    if kind == "down":
        return register.say(f"{msg.removesuffix(' is not running')} is not running")
    if kind == "repo-risk":
        return f"unsaved work in {msg.split(' (')[0]}"
    if kind == "backup":
        return "a backup did not run" if "MISSED" in msg else "the last backup failed"
    if kind == "own-job":
        return "Lantern's nightly learning missed a run; it is retrying by itself"
    return register.say(msg)


def line(now: float | None = None) -> str:
    now = now or time.time()
    db = memory.connect()
    try:
        total = db.execute("SELECT count(*) FROM items WHERE status='active' AND kind != 'turn'").fetchone()[0]
        learned = db.execute("SELECT count(*) FROM items WHERE status='active' AND kind IN ('rule','fact','pattern','profile','routine') "
                             "AND source NOT LIKE 'discovery:%' AND source != 'seeded' AND created > ?", (now - 86400,)).fetchone()[0]
        turns = db.execute("SELECT count(*) FROM items WHERE kind='turn' AND created > ?", (now - 86400,)).fetchone()[0]
        found = db.execute("SELECT count(*) FROM items WHERE status='active' AND source LIKE 'discovery:%' AND created > ?",
                           (now - 86400,)).fetchone()[0]
        plain = register.level() != "expert"
        findings = [e for e in stage.top(db, k=3, now=now) if e["salience"] >= (5 if plain else 4)]  # a system chore they cannot act on is not theirs
    finally:
        db.close()
    recall = _last("recall")
    parts = []
    if recall and now - recall.get("ts", 0) < 3600:
        n = len(recall.get("hits", []))
        parts.append(f"{n} note{'s' if n != 1 else ''} recalled last turn" if n else "nothing to recall last turn")
    if not plain:
        parts.append(f"{total:,} things known")  # a count of notes means nothing to a non-technical reader
    if turns:
        parts.append(f"remembered {turns} exchange{'s' if turns != 1 else ''} today")
    if learned:
        parts.append(f"learned {learned} today")
    if found:
        parts.append(f"found {found} new on this machine")
    if findings:
        parts.append("needs you: " + (plain_finding(findings[0]) if plain else findings[0]["message"])[:80])
    else:
        parts.append("all well")
    return "Lantern: " + " · ".join(parts)
