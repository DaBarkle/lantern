"""Stage: what Lantern is paying attention to right now, shared by every session.

An entry is one finding or note with a score. Scores fade (half-life one day) so stale notes sink, and every entry
expires, so nothing sits on Stage for weeks. A check that owns an entry republishes it while the problem lasts and
resolves it when the check passes. Lives in memory.db so all sessions see one board.
"""
import hashlib
import sqlite3
import time

HALF_LIFE_HOURS = 24
DEFAULT_DAYS = 7
TOP = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS stage (
  key TEXT PRIMARY KEY,        -- same key = same finding; republishing refreshes it
  source TEXT NOT NULL,        -- who published: doctor check, note, pack
  message TEXT NOT NULL,
  score REAL NOT NULL,         -- 1 (minor) .. 10 (act now)
  updated REAL NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_seen (session TEXT PRIMARY KEY, digest TEXT NOT NULL, at REAL NOT NULL);
"""


def _ready(db: sqlite3.Connection) -> sqlite3.Connection:
    db.executescript(SCHEMA)
    return db


def publish(db: sqlite3.Connection, key: str, source: str, message: str, score: float = 5.0,
            days: float = DEFAULT_DAYS, now: float | None = None) -> None:
    from .shield import redact  # a note must never carry a secret into every later prompt
    now = now or time.time()
    with _ready(db):
        db.execute("INSERT INTO stage VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                   "source=excluded.source, message=excluded.message, score=excluded.score, "
                   "updated=excluded.updated, expires=excluded.expires",
                   (key, source, redact(message)[:500], max(1.0, min(10.0, score)), now, now + days * 86400))


def resolve(db: sqlite3.Connection, key: str) -> bool:
    with _ready(db):
        return db.execute("DELETE FROM stage WHERE key=?", (key,)).rowcount > 0


def resolve_source(db: sqlite3.Connection, source: str, keep: set[str]) -> None:
    """Drop a publisher's entries it no longer reports, so a removed check never leaves a ghost alert."""
    with _ready(db):
        for (key,) in db.execute("SELECT key FROM stage WHERE source=?", (source,)).fetchall():
            if key not in keep:
                db.execute("DELETE FROM stage WHERE key=?", (key,))


def top(db: sqlite3.Connection, k: int = TOP, now: float | None = None) -> list[dict]:
    now = now or time.time()
    with _ready(db):
        db.execute("DELETE FROM stage WHERE expires <= ?", (now,))
    rows = db.execute("SELECT key, source, message, score, updated FROM stage").fetchall()
    out = [{"key": key, "source": src, "message": msg,
            "salience": score * 0.5 ** (max(0.0, now - upd) / 3600 / HALF_LIFE_HOURS)}
           for key, src, msg, score, upd in rows]
    out.sort(key=lambda e: e["salience"], reverse=True)
    return out[:k]


def render(entries: list[dict]) -> str:
    return "\n".join(f"- [{e['source']}] {e['message']}" for e in entries)


def delta(db: sqlite3.Connection, session: str, now: float | None = None) -> str | None:
    """The top entries if they changed since this session last saw them, else None."""
    entries = top(db, now=now)
    digest = hashlib.sha256("|".join(e["key"] + e["message"] for e in entries).encode()).hexdigest()[:16]
    row = db.execute("SELECT digest FROM stage_seen WHERE session=?", (session,)).fetchone()
    if row and row[0] == digest:
        return None
    with db:
        db.execute("INSERT OR REPLACE INTO stage_seen VALUES (?, ?, ?)", (session, digest, now or time.time()))
    if row is None and not entries:
        return None  # nothing on Stage and nothing was shown before
    return render(entries) if entries else "Stage is now clear."
