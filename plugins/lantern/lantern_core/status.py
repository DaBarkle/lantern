"""One plain line that tells the user Lantern is working: what it just recalled, what it learned, and whether anything
needs them. For Claude Code's status line (`lantern status`) and the /lantern:status command."""
import json
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


def line(now: float | None = None) -> str:
    now = now or time.time()
    db = memory.connect()
    try:
        total = db.execute("SELECT count(*) FROM items WHERE status='active' AND kind != 'turn'").fetchone()[0]
        learned = db.execute("SELECT count(*) FROM items WHERE status='active' AND kind IN ('rule','fact','pattern','profile') "
                             "AND source NOT LIKE 'discovery:%' AND created > ?", (now - 86400,)).fetchone()[0]
        turns = db.execute("SELECT count(*) FROM items WHERE kind='turn' AND created > ?", (now - 86400,)).fetchone()[0]
        found = db.execute("SELECT count(*) FROM items WHERE status='active' AND source LIKE 'discovery:%' AND created > ?",
                           (now - 86400,)).fetchone()[0]
        findings = [e for e in stage.top(db, k=3, now=now) if e["salience"] >= 4]
    finally:
        db.close()
    recall = _last("recall")
    parts = []
    if recall and now - recall.get("ts", 0) < 3600:
        n = len(recall.get("hits", []))
        parts.append(f"{n} note{'s' if n != 1 else ''} recalled last turn" if n else "nothing to recall last turn")
    parts.append(f"{total:,} things known")
    if turns:
        parts.append(f"remembered {turns} exchange{'s' if turns != 1 else ''} today")
    if learned:
        parts.append(f"learned {learned} today")
    if found:
        parts.append(f"found {found} new on this machine")
    if findings:
        parts.append("needs you: " + register.say(findings[0]["message"])[:80])
    else:
        parts.append("all well")
    return "Lantern: " + " · ".join(parts)
