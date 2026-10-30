"""One plain line that tells the user Lantern is working: what it just recalled, what it learned, and whether anything
needs them. For Claude Code's status line (`lantern status`) and the /lantern:status command."""
import json
import time

from . import memory, stage
from .paths import ensure_data_dir


def _last(name: str) -> dict:
    try:
        lines = (ensure_data_dir() / f"{name}.jsonl").read_text().splitlines()
        return json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError):
        return {}


def line(now: float | None = None) -> str:
    now = now or time.time()
    db = memory.connect()
    try:
        total = db.execute("SELECT count(*) FROM items WHERE status='active' AND kind != 'turn'").fetchone()[0]
        learned = db.execute("SELECT count(*) FROM items WHERE status='active' AND kind IN ('rule','fact','pattern','profile') "
                             "AND created > ?", (now - 86400,)).fetchone()[0]
        findings = [e for e in stage.top(db, k=3, now=now) if e["salience"] >= 4]
    finally:
        db.close()
    recall = _last("recall")
    parts = []
    if recall and now - recall.get("ts", 0) < 3600:
        n = len(recall.get("hits", []))
        parts.append(f"{n} note{'s' if n != 1 else ''} recalled last turn" if n else "nothing to recall last turn")
    parts.append(f"{total:,} things known")
    if learned:
        parts.append(f"learned {learned} today")
    if findings:
        parts.append("needs you: " + findings[0]["message"][:80])
    else:
        parts.append("all well")
    return "Lantern: " + " · ".join(parts)
