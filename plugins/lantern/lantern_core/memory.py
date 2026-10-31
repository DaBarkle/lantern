"""Memory store: one SQLite file with full-text search, ranked by relevance and gently by age."""
import json
import math
import os
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
CREATE TABLE IF NOT EXISTS uses (
  id INTEGER PRIMARY KEY,        -- items.id
  recalled INTEGER NOT NULL DEFAULT 0,  -- times recall brought it into a turn
  repeated INTEGER NOT NULL DEFAULT 0,  -- times the user had to say it again
  last REAL NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, content='items', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


SCHEMA_NAMES = {"items", "ledger", "uses", "items_fts", "items_ai", "items_ad"}
DAMAGED = ("malformed", "not a database", "file is encrypted", "disk image")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or ensure_data_dir() / "memory.db"
    try:
        db = _open(path)
    except sqlite3.DatabaseError as e:
        if not any(w in str(e).lower() for w in DAMAGED):
            raise  # locked or busy: the caller's own error handling says so; the file itself is fine
        heal(path, str(e))
        db = _open(path)
    return db


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=5)
    # WAL and the schema are set once per file. Re-running them on every open took the write lock each time, and four
    # sessions opening at once got "database is locked" (a lock taken while switching modes skips the busy timeout).
    for attempt in range(50):  # a brand-new file opened by several processes at once: one wins the switch, the rest wait
        try:
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")  # several sessions write at once
            if not SCHEMA_NAMES <= {r[0] for r in db.execute("SELECT name FROM sqlite_master")}:
                db.executescript(SCHEMA)
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 49:
                raise
            time.sleep(0.1)
    if "vec" not in {r[1] for r in db.execute("PRAGMA table_info(items)")}:
        db.execute("ALTER TABLE items ADD COLUMN vec BLOB")  # databases made before semantic search
    return db


def heal(path: Path, why: str) -> None:
    """A damaged memory file is set aside (never deleted) and the last good copy the doctor made takes its place; with no
    copy, memory starts empty. Either way the next session says so once, in plain words."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        f = Path(f"{path}{suffix}")
        if f.exists():
            f.replace(f.with_name(f"{f.name}.damaged-{stamp}"))
    copy = path.with_name(path.name + ".bak")
    restored = ""
    if copy.exists():
        import shutil
        shutil.copyfile(copy, path)
        restored = time.strftime("%Y-%m-%d %H:%M", time.localtime(copy.stat().st_mtime))
    from .paths import write_atomic
    write_atomic(path.with_name("memory.healed"), json.dumps({"at": time.time(), "why": why[:200], "restored": restored,
                                                              "kept": f"{path.name}.damaged-{stamp}"}))


def backup(path: Path | None = None) -> str:
    """A daily copy of memory, made only from a file that passes its own check, so heal() has something good to restore."""
    path = path or ensure_data_dir() / "memory.db"
    db = connect(path)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return "memory did not pass its own check; yesterday's copy is kept"
        tmp = path.with_name(path.name + ".bak.tmp")
        out = sqlite3.connect(tmp)
        try:
            db.backup(out)
        finally:
            out.close()
        os.replace(tmp, path.with_name(path.name + ".bak"))
    finally:
        db.close()
    return f"memory copied for safety ({path.with_name(path.name + '.bak').stat().st_size / 1e6:.0f} MB)"


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


def used(db: sqlite3.Connection, ids: list[int], field: str = "recalled", now: float | None = None) -> None:
    """Count a use of each item: recall brought it into a turn, or the user had to say it again."""
    assert field in ("recalled", "repeated")
    with db:
        for i in ids:
            db.execute(f"INSERT INTO uses(id, {field}, last) VALUES (?, 1, ?) ON CONFLICT(id) DO UPDATE SET {field}={field}+1, last=excluded.last",
                       (i, now or time.time()))


def uses(db: sqlite3.Connection, ids: list[int]) -> dict[int, tuple[int, int]]:
    """{id: (recalled, repeated)} for the items that have been used."""
    if not ids:
        return {}
    return {r[0]: (r[1], r[2]) for r in db.execute(f"SELECT id, recalled, repeated FROM uses WHERE id IN ({','.join('?' * len(ids))})", ids)}


USE_DAYS = 7  # each recall keeps a note in the session index as if it were a week newer (capped), so used notes stay and unused ones age out
USE_CAP = 20


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


_STATE = re.compile(r"\b(?:stopped|paused|disabled|is down|turned off|broken|failing|pending|is running|enabled|on hold|waiting|blocked)\b", re.I)
STATE_FRESH_DAYS = 14
INDEX_LINE = 140


def rules(db: sqlite3.Connection, max_chars: int, kind: str = "rule", dated: bool = False, now: float | None = None) -> list[str]:
    """One line per item of this kind (its first line), newest first, up to max_chars. dated: lead with the month, so
    a session can tell a fresh fact from one that may have moved on."""
    out, used = [], 0
    # A long item is stored in chunks; its first chunk (lowest id) opens with the summary line.
    # Discovery cards are summarised elsewhere; the index is for what was learned and curated.
    rows = db.execute("SELECT items.id, text, created, coalesce(recalled, 0), coalesce(repeated, 0) FROM items LEFT JOIN uses ON uses.id = items.id "
                      "WHERE items.id IN (SELECT min(id) FROM items WHERE kind=? AND status='active' AND source NOT LIKE 'discovery:%' "
                      "GROUP BY source)", (kind,)).fetchall()
    # A rule the user had to repeat comes first; otherwise newest first, with a note that keeps being recalled counted as newer.
    rows.sort(key=lambda r: (r[4] > 0, r[2] + min(r[3], USE_CAP) * USE_DAYS * 86400), reverse=True)
    for _, text, created, _, repeated in rows:
        line = text.strip().splitlines()[0][:INDEX_LINE if dated else 300] if text.strip() else ""
        if repeated and line and kind == "rule":
            line += f" (the user has had to say this {repeated + 1} times)"
        if dated and line:
            # A note about a state ("SABnzbd is stopped") goes stale in days; a note about a design does not.
            stale = _STATE.search(line) and ((now or time.time()) - created) > STATE_FRESH_DAYS * 86400
            line = f"[{time.strftime('%Y-%m', time.localtime(created))}{', may have changed' if stale else ''}] {line}"
        if not line or used + len(line) > max_chars:
            continue
        out.append(line)
        used += len(line)
    return out


_FILLER = frozenset("first should could would please thing things something anything everything right wrong next last today "
                    "tonight check look know think want need help fix fixed broken stuck okay maybe really still again".split())


def rules_touching(db: sqlite3.Connection, prompt: str, limit: int = 2) -> list[str]:
    """Standing rules that share a distinctive word with the request ("restart", "plex"), so they are applied, not just loaded."""
    words = [w for w in terms(prompt) if len(w) >= 4 and w not in _FILLER]  # "first", "should" touch no rule
    if not words:
        return []
    match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
    rows = db.execute("SELECT items.text FROM items_fts JOIN items ON items.id = items_fts.rowid WHERE items_fts MATCH ? "
                      "AND items.kind='rule' AND items.status='active' ORDER BY bm25(items_fts) LIMIT ?", (match, limit)).fetchall()
    return [t.strip().splitlines()[0][:300] for (t,) in rows]


def learned_since(db: sqlite3.Connection, since: float, limit: int = 6) -> list[str]:
    """Rules, facts, patterns and profile lines curated or stated since a time, newest first (not discovery cards)."""
    rows = db.execute("SELECT kind, text FROM items WHERE kind IN ('rule','fact','pattern','profile','routine') AND status='active' "
                      "AND created > ? AND source NOT LIKE 'discovery:%' AND source != 'seeded' ORDER BY created DESC LIMIT ?", (since, limit))
    out = []
    for k, t in rows:
        if not t.strip():
            continue
        was = re.search(r"Replaces the earlier note: (.+)", t)
        out.append(f"[{k}, changed] {t.strip().splitlines()[0][:160]} (was: {was.group(1)[:120]})" if was else f"[{k}] {t.strip().splitlines()[0][:160]}")
    return out


def rule_items(db: sqlite3.Connection) -> list[tuple[int, str]]:
    """(id, summary line) of every active rule and fact, newest first."""
    rows = db.execute("SELECT id, text FROM items WHERE kind IN ('rule', 'fact') AND status='active' ORDER BY created DESC")
    return [(i, t.strip().splitlines()[0][:300]) for i, t in rows if t.strip()]


LEDGER_KEEP = 20


LOOPS_KEEP = 12  # open loops carried per session; the oldest fall off once a session holds more
RESUME_HOURS = 48
PROJECT_RESUME_DAYS = 14  # a session in the same folder is worth resuming for longer than one anywhere


def ledger_update(db: sqlite3.Connection, session: str, cwd: str, turn: dict, now: float | None = None) -> dict:
    """Fold one turn (from transcript.last_turn) into the session's ledger: what it set out to do, what it touched.
    Open loops are inherited from the previous session and kept until a reply addresses them, so a question asked
    on day 1 is still open on day 3 unless it was answered."""
    now = now or time.time()
    row = db.execute("SELECT data, started FROM ledger WHERE session=?", (session,)).fetchone()
    if row:
        d = json.loads(row[0])
    else:
        prior = previous_session(db, session, cwd, now)
        d = {"goal": turn["user"][:400], "files": [], "commands": [], "agents": [], "failed": 0, "turns": 0,
             "loops": list(prior.get("loops", [])) if prior else []}
    for k in ("files", "commands", "agents"):
        d[k] = list(dict.fromkeys(d[k] + turn.get(k, [])))[-LEDGER_KEEP:]
    d["failed"] += turn.get("failed", 0)
    d["turns"] += 1
    d["last"] = turn["user"][:400]
    d["reply"] = carry(turn.get("reply", ""), 600)  # the end carries the next steps; hedged sentences are kept whole
    reply = turn.get("reply", "")
    kept = [l for l in d.get("loops", []) if not addresses(reply, l)]  # done, re-asked or retired
    request = turn["user"] if len(terms(turn["user"])) >= 4 else d["goal"]  # "and now?" says less than what the session set out to do
    fresh = [anchor(l, request) for l in open_loops(reply) if not closed_later(reply, l)]
    d["loops"] = list(dict.fromkeys(kept + fresh))[-LOOPS_KEEP:]
    with db:
        # cwd stays the folder the session started in: a `cd` mid-session must not make the next session in this folder skip it
        db.execute("INSERT INTO ledger VALUES (?, ?, ?, ?, ?) ON CONFLICT(session) DO UPDATE SET "
                   "updated=excluded.updated, data=excluded.data", (session, cwd, row[1] if row else now, now, json.dumps(d)))
    return d


_SENT = r"(?:[^\n.!?]|\.(?! |$))"  # one sentence: a dot inside a path or version does not end it
_LOOP = re.compile(r"(?m)(?:^\s*(?:[-*\d.]+\s*)?|(?<=[.!?] )|(?<=[,;] ))(?:((?:(?:so |and |or |otherwise,? )?)(?:I (?:didn't|did not|couldn't|could not|haven't|have not|can't|cannot|won't|will not|can|could|propose)|"
                   r"Next,? I can|I'll|I will|I'd|Not (?:yet )?(?:done|checked)|Open question|Until you confirm|Still blocked|"
                   r"Shall I|Should I|Want me to|Would you like|Do you want|Let me know|Tell me (?:if|which|whether)|Say (?:the word|which|yes|go)|If you want me to|"
                   r"The (?:most useful )?next step|Still (?:waiting|open)|Waiting (?:on|for) you|type `secret)" + _SENT + r"{6,220}\??)|(" + _SENT + r"{6,220}\?)|("
                   + _SENT + r"{3,160}\b(?:looks|seems|appears|is|are) (?:stuck|frozen|wedged|stalled|hung|unexplained|still failing)\b" + _SENT + r"{0,120}))")


_HEDGE = re.compile(r"\b(?:guess\w*|couldn't|could not|can't (?:confirm|verify|tell|check|read)|cannot (?:confirm|verify|tell|check|read)|unverified|"
                    r"unconfirmed|not (?:sure|certain|verified|confirmed)|likely|probably|possibly|perhaps|may be|might be|I think|I suspect|"
                    r"seems|appears|assum\w+|was declined|didn't check|haven't checked)\b", re.I)
HEDGE_SENTENCE = 300


def carry(text: str, limit: int) -> str:
    """Shorten a reply to about limit characters without losing its hedges: the opening, every sentence that marks a claim
    as a guess or as unchecked, and the ending are kept whole, and the result says the guesses are guesses. A plain
    cut once dropped "It's a guess, because I couldn't read Plex's own error log", and the next day stated it as fact."""
    if len(text) <= limit:
        return text
    head, tail = text[:limit // 3], text[-(limit // 3):]
    hedged = [s.strip()[:HEDGE_SENTENCE] for s in re.split(r"(?<=[.!?])\s+|\n+", text) if _HEDGE.search(s)]
    middle = " ".join(s for s in hedged if s not in head and s not in tail)
    out = head + " [...] " + (middle + " [...] " if middle else "") + tail
    if hedged:
        out += " (hedged: the guesses above stay guesses; say what was not checked rather than stating a cause as fact)"
    return out


_POINTER = re.compile(r"\b(?:above|below|earlier|the (?:first|second|last|other) one|that one|this one|those|these|it|them|that)\b", re.I)


def anchor(loop: str, request: str) -> str:
    """A loop that points at something in its own reply ("withdraw the first warning above") is meaningless a day later:
    it carries the request it came from, so the next session knows what it was about."""
    if not request or (_content(loop) and not _POINTER.search(loop)):
        return loop
    return f"{loop} (from the request: \"{request.strip().splitlines()[0][:100]}\")"


def open_loops(reply: str, limit: int = 8) -> list[str]:
    """Promises, gaps, offers and questions the assistant stated in its reply ("I didn't check X", "Next, I can Y",
    "Should I Z?"), so the next session closes or re-asks them instead of repeating them."""
    return [(m.group(1) or m.group(2) or m.group(3)).strip().rstrip(".") for m in _LOOP.finditer(reply or "")][-limit:]


_GENERIC = frozenset("check checked checking whether already still would could should there which about their these those something because before haven didn "
                     "after right again since while where other first might maybe being doing going would wants want needs need".split())


def _content(text: str) -> set[str]:
    """The words a loop is really about: identifiers (paths, units, ports) and longer words that are not filler."""
    return {w for w in terms(text) if any(c in w for c in "./-_0123456789") or (len(w) >= 5 and w not in _GENERIC)}


_CLOSED_LATER = re.compile(r"Also checked|Checked after all|now (?:checked|confirmed|looked|verified)|I've now|I have now", re.I)


def closed_later(reply: str, loop: str) -> bool:
    """A gap stated early in a reply and answered further down the same reply ("Checked after all: ...") is not open."""
    later = reply[reply.find(loop) + len(loop):] if loop in reply else ""
    return bool(_CLOSED_LATER.search(later)) and bool(_content(loop) & _content(later))


def addresses(reply: str, loop: str) -> bool:
    """A reply addresses a loop when it reuses most of what the loop is about: the thing was done, re-asked or retired.
    One shared word ("firewall", "6006") is a mention, not a closure."""
    want = _content(loop)
    return len(want) >= 2 and len(want & _content(reply)) >= min(3, len(want))  # a one-word loop is retired by the cap, not by a mention


def previous_session(db: sqlite3.Connection, session: str, cwd: str, now: float | None = None) -> dict | None:
    """The session to resume from: the last one in this folder within two weeks, else the last one anywhere within two
    days."""
    now = now or time.time()
    prior = ledger_last(db, session, PROJECT_RESUME_DAYS * 86400, cwd=cwd, now=now) if cwd else None
    if prior and prior["cwd"] != cwd and now - prior["updated"] > RESUME_HOURS * 3600:
        prior = None  # another folder's session only counts while it is fresh
    return prior or ledger_last(db, session, RESUME_HOURS * 3600, now=now)


_CORRECTION = re.compile(r"(?:^|\n|(?<=[.!?] ))\W{0,4}(?:Correction|Retraction|I was wrong|That was wrong|That was incorrect|To correct (?:my|an) earlier)\b\W{0,3}(?:[^:\n]{0,40}:)?[\s*]*([^\n]{12,400})", re.I)


def corrections(reply: str) -> list[str]:
    """What the assistant took back in this reply, so the corrected version is what later sessions see."""
    return [m.group(1).strip().rstrip(".") for m in _CORRECTION.finditer(reply or "")][:3]


def record_corrections(db: sqlite3.Connection, session: str, reply: str, now: float | None = None) -> list[int]:
    """Store each correction as a dated fact and supersede the earlier notes it overlaps, so a claim retracted on day 2
    cannot come back as a fact on day 3."""
    now = now or time.time()
    ids = []
    for c in corrections(reply):
        words = {w for w in terms(c) if len(w) >= 4}
        for i, text in db.execute("SELECT id, text FROM items WHERE status='active' AND kind IN ('fact', 'pattern') AND source NOT LIKE 'correction:%'").fetchall():
            old = {w for w in terms(text) if len(w) >= 4}
            if old and len(words & old) >= max(2, 0.4 * min(len(words), len(old))):
                supersede(db, i)
        ids.append(add(db, "fact", f"correction:{session}", f"Correction ({time.strftime('%Y-%m-%d', time.localtime(now))}, supersedes an earlier note): {c}", now))
    return ids


def ledger_get(db: sqlite3.Connection, session: str) -> dict | None:
    row = db.execute("SELECT cwd, started, updated, data FROM ledger WHERE session=?", (session,)).fetchone()
    return {"cwd": row[0], "started": row[1], "updated": row[2], **json.loads(row[3])} if row else None


def ledger_last(db: sqlite3.Connection, exclude: str, within_s: float, cwd: str | None = None, now: float | None = None) -> dict | None:
    """The most recent other session's ledger, preferring one in the same folder, if it ended within within_s."""
    cut = (now or time.time()) - within_s
    for where, args in ((("AND cwd=?", (exclude, cut, cwd)),) if cwd else ()) + (("", (exclude, cut)),):
        row = db.execute(f"SELECT session FROM ledger WHERE session != ? AND updated > ? {where} ORDER BY updated DESC LIMIT 1",
                         args).fetchone()
        if row:
            return ledger_get(db, row[0])
    return None


def ledger_lines(d: dict) -> list[str]:
    out = [f"- Goal: {d['goal']}", f"- Last asked: {d['last']}"]
    if d.get("reply"):
        out.append(f"- Last reply ended: {d['reply']}")
    if d.get("loops"):
        out.append("- Left unfinished in that session (do each now, ask again once, or drop it with a reason; never repeat one unchanged "
                   "and never call it an 'open loop' to the user): " + "; ".join(d["loops"]))
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
