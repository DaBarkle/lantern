"""Memory store: one SQLite file with full-text search, ranked by relevance and gently by age."""
import json
import math
import re
import sqlite3
import time
from pathlib import Path

from .paths import ensure_data_dir

CHUNK_CHARS = 1000
HALF_LIFE_DAYS = 90  # an item loses half its rank boost after this; old items still surface when they match well
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}")
_STOP = frozenset("the and for are but not you all can was were with this that from have has had what when where which who how why does did into about your they them then than there their our out also just like".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,          -- profile | rule | fact | note | doc | turn
  source TEXT NOT NULL,        -- session id, file path, or pack name
  text TEXT NOT NULL,
  created REAL NOT NULL,       -- unix seconds
  status TEXT NOT NULL DEFAULT 'active',  -- active | superseded
  vec BLOB                     -- float32 embedding, filled by the memory server
);
CREATE TABLE IF NOT EXISTS ledger (
  session TEXT PRIMARY KEY,
  cwd TEXT NOT NULL,
  started REAL NOT NULL,
  updated REAL NOT NULL,
  data TEXT NOT NULL             -- JSON: goal, last, files, commands, agents, failed, turns
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
    if "vec" not in {r[1] for r in db.execute("PRAGMA table_info(items)")}:
        db.execute("ALTER TABLE items ADD COLUMN vec BLOB")  # databases made before semantic search
    return db


def add(db: sqlite3.Connection, kind: str, source: str, text: str, created: float | None = None) -> int:
    with db:
        cur = db.execute("INSERT INTO items(kind, source, text, created) VALUES (?, ?, ?, ?)",
                         (kind, source, text.strip(), created or time.time()))
    return cur.lastrowid


def add_document(db: sqlite3.Connection, kind: str, source: str, text: str, created: float | None = None) -> list[int]:
    """Store long text as paragraph chunks, so each part gets its own match (the embedding model reads ~256 words)."""
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text.strip()):
        if cur and len(cur) + len(para) > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    title = text.strip().splitlines()[0][:120] if text.strip() else ""
    # Later chunks carry the document's first line so they still say what they are about.
    return [add(db, kind, source, c if i == 0 else f"{title}\n...\n{c}", created) for i, c in enumerate(chunks)]


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


def get(db: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    """Active items by id."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT id, kind, source, text, created FROM items WHERE status='active' AND id IN ({marks})", ids)
    return {r[0]: {"id": r[0], "kind": r[1], "source": r[2], "text": r[3], "created": r[4]} for r in rows}


def profile(db: sqlite3.Connection, max_chars: int) -> str:
    """The user's profile items, newest first, up to max_chars."""
    out, used = [], 0
    for (text,) in db.execute("SELECT text FROM items WHERE kind='profile' AND status='active' ORDER BY created DESC"):
        if used + len(text) > max_chars:
            break
        out.append(text)
        used += len(text)
    return "\n\n".join(out)


def rules(db: sqlite3.Connection, max_chars: int, kind: str = "rule", dated: bool = False) -> list[str]:
    """One line per item of this kind (its first line), newest first, up to max_chars. dated: lead with the month, so
    a session can tell a fresh fact from one that may have moved on."""
    out, used = [], 0
    # A long item is stored in chunks; its first chunk (lowest id) opens with the summary line.
    for text, created in db.execute("SELECT text, created FROM items WHERE id IN (SELECT min(id) FROM items WHERE kind=? "
                                    "AND status='active' GROUP BY source) ORDER BY created DESC", (kind,)):
        line = text.strip().splitlines()[0][:300] if text.strip() else ""
        if dated and line:
            line = f"[{time.strftime('%Y-%m', time.localtime(created))}] {line}"
        if not line or used + len(line) > max_chars:
            continue
        out.append(line)
        used += len(line)
    return out


def rule_items(db: sqlite3.Connection) -> list[tuple[int, str]]:
    """(id, summary line) of every active rule and fact, newest first."""
    rows = db.execute("SELECT id, text FROM items WHERE kind IN ('rule', 'fact') AND status='active' ORDER BY created DESC")
    return [(i, t.strip().splitlines()[0][:300]) for i, t in rows if t.strip()]


LEDGER_KEEP = 20


def ledger_update(db: sqlite3.Connection, session: str, cwd: str, turn: dict, now: float | None = None) -> dict:
    """Fold one turn (from transcript.last_turn) into the session's ledger: what it set out to do, what it touched."""
    now = now or time.time()
    row = db.execute("SELECT data, started FROM ledger WHERE session=?", (session,)).fetchone()
    d = json.loads(row[0]) if row else {"goal": turn["user"][:400], "files": [], "commands": [], "agents": [], "failed": 0, "turns": 0}
    for k in ("files", "commands", "agents"):
        d[k] = list(dict.fromkeys(d[k] + turn.get(k, [])))[-LEDGER_KEEP:]
    d["failed"] += turn.get("failed", 0)
    d["turns"] += 1
    d["last"] = turn["user"][:400]
    with db:
        db.execute("INSERT INTO ledger VALUES (?, ?, ?, ?, ?) ON CONFLICT(session) DO UPDATE SET cwd=excluded.cwd, "
                   "updated=excluded.updated, data=excluded.data", (session, cwd, row[1] if row else now, now, json.dumps(d)))
    return d


def ledger_get(db: sqlite3.Connection, session: str) -> dict | None:
    row = db.execute("SELECT cwd, started, updated, data FROM ledger WHERE session=?", (session,)).fetchone()
    return {"cwd": row[0], "started": row[1], "updated": row[2], **json.loads(row[3])} if row else None


def ledger_last(db: sqlite3.Connection, exclude: str, within_s: float, cwd: str | None = None) -> dict | None:
    """The most recent other session's ledger, preferring one in the same folder, if it ended within within_s."""
    cut = time.time() - within_s
    for where, args in ((("AND cwd=?", (exclude, cut, cwd)),) if cwd else ()) + (("", (exclude, cut)),):
        row = db.execute(f"SELECT session FROM ledger WHERE session != ? AND updated > ? {where} ORDER BY updated DESC LIMIT 1",
                         args).fetchone()
        if row:
            return ledger_get(db, row[0])
    return None


def ledger_lines(d: dict) -> list[str]:
    out = [f"- Goal: {d['goal']}", f"- Last asked: {d['last']}"]
    if d["files"]:
        out.append("- Files edited: " + ", ".join(d["files"]))
    if d["commands"]:
        out.append("- Commands run: " + "; ".join(f"`{c}`" for c in d["commands"][-8:]))
    if d["agents"]:
        out.append("- Agents used: " + "; ".join(d["agents"]))
    if d["failed"]:
        out.append(f"- Tool calls that failed: {d['failed']}")
    return out


def last_session(db: sqlite3.Connection, exclude: str, within_s: float, n: int = 2) -> list[dict]:
    """The last n turns of the most recent other session, if it ended within within_s seconds."""
    row = db.execute("SELECT source, max(created) FROM items WHERE kind='turn' AND source != ? AND source NOT LIKE '%:%' "
                     "GROUP BY source ORDER BY max(created) DESC LIMIT 1", (exclude,)).fetchone()
    if not row or row[1] is None or time.time() - row[1] > within_s:
        return []
    rows = db.execute("SELECT text, created FROM items WHERE kind='turn' AND source=? ORDER BY created DESC LIMIT ?", (row[0], n))
    return [{"text": t, "created": c} for t, c in reversed(rows.fetchall())]


_IDENT = re.compile(r"`([^`\n]{3,80})`|\b([A-Z]{2,}-\d+)\b|\b([0-9a-f]{7,12})\b|((?:~|/)[\w./-]{4,})|\b([\w.-]+\.(?:py|sh|md|json|ya?ml|toml|service|timer|conf))\b")


def session_turns(db: sqlite3.Connection, session: str) -> list[str]:
    return [t for (t,) in db.execute("SELECT text FROM items WHERE kind='turn' AND source=? ORDER BY created", (session,))]


def identifiers(texts: list[str], limit: int = 40) -> list[str]:
    """Paths, file names, ids, hashes and code spans named in these texts, most recent first, deduplicated."""
    seen, out = set(), []
    for text in reversed(texts):
        for m in _IDENT.finditer(text):
            tok = next(g for g in m.groups() if g)
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out[:limit]
