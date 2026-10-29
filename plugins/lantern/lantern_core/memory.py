"""Memory store: one SQLite file with full-text search, ranked by relevance and gently by age."""
import math
import re
import sqlite3
import time
from pathlib import Path

from .paths import ensure_data_dir

HALF_LIFE_DAYS = 90  # an item loses half its rank boost after this; old items still surface when they match well
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}")
_STOP = frozenset("the and for are but not you all can was were with this that from have has had what when where which who how why does did into about your they them then than there their our out also just like".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,          -- turn | note | fact | tool
  source TEXT NOT NULL,        -- session id, file path, or pack name
  text TEXT NOT NULL,
  created REAL NOT NULL,       -- unix seconds
  status TEXT NOT NULL DEFAULT 'active'  -- active | superseded
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, content='items', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or ensure_data_dir() / "memory.db", timeout=5)
    db.execute("PRAGMA journal_mode=WAL")  # several sessions write at once
    db.executescript(SCHEMA)
    return db


def add(db: sqlite3.Connection, kind: str, source: str, text: str, created: float | None = None) -> int:
    with db:
        cur = db.execute("INSERT INTO items(kind, source, text, created) VALUES (?, ?, ?, ?)",
                         (kind, source, text.strip(), created or time.time()))
    return cur.lastrowid


def supersede(db: sqlite3.Connection, item_id: int) -> None:
    with db:
        db.execute("UPDATE items SET status='superseded' WHERE id=?", (item_id,))


def terms(text: str) -> list[str]:
    seen, out = set(), []
    for w in _WORD.findall(text.lower()):
        if w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def search(db: sqlite3.Connection, query: str, limit: int = 5, now: float | None = None) -> list[dict]:
    words = terms(query)
    if not words:
        return []
    match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
    rows = db.execute(
        "SELECT items.id, items.kind, items.source, items.text, items.created, bm25(items_fts) FROM items_fts "
        "JOIN items ON items.id = items_fts.rowid "
        "WHERE items_fts MATCH ? AND items.status = 'active' ORDER BY bm25(items_fts) LIMIT ?",
        (match, limit * 10),
    ).fetchall()
    now = now or time.time()
    hits = []
    for rid, kind, source, text, created, bm in rows:
        age_days = max(0.0, (now - created) / 86400)
        relevance = -bm  # bm25() is lower-is-better
        score = relevance * (0.5 + 0.5 * math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS))
        hits.append({"id": rid, "kind": kind, "source": source, "text": text, "created": created, "score": score})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]
