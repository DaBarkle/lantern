# /// script
# requires-python = ">=3.11"
# dependencies = ["fastembed>=0.7", "numpy>=1.26"]
# ///
"""Lantern memory server: keeps the embedding model warm so per-prompt semantic search takes milliseconds.

Started on demand (session start, a prompt that found it down, the daily doctor); only the first one binds the port,
the rest exit. Exits by itself after half an hour without a search, so a machine with no session running carries no
resident model. Listens on 127.0.0.1 only and answers only requests carrying the token from the user's data folder.
"""
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from lantern_core import memory, resources, semantic  # noqa: E402
from lantern_core.paths import ensure_data_dir  # noqa: E402

SYNC_SECONDS = 10
EMBED_BATCH = 64
IDLE_SECONDS = 1800  # no search for this long: free the memory; the next session or prompt starts a server again


def normalise(v):
    n = float(np.linalg.norm(v))
    return v / n if n else v


class Index:
    """All item vectors in one normalised matrix. shortcut: brute-force cosine, fine to ~200k items; use an ANN index beyond that."""

    def __init__(self, model):
        self.model = model
        self.ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, semantic.DIM), dtype=np.float32)
        self.lock = threading.Lock()

    def embed(self, texts):
        return [normalise(np.asarray(v, dtype=np.float32)) for v in self.model.embed(texts)]

    def sync(self, db) -> int:
        """Load rows that arrived with a vector (imports), then embed rows that have none. Returns rows embedded."""
        last = int(self.ids.max()) if len(self.ids) else 0
        rows = db.execute("SELECT id, vec FROM items WHERE id > ? AND vec IS NOT NULL ORDER BY id", (last,)).fetchall()
        self._append([i for i, _ in rows], [np.frombuffer(v, dtype=np.float32) for _, v in rows])
        # Loaded above only if they had a vector, so these are never added twice.
        todo = db.execute("SELECT id, text FROM items WHERE vec IS NULL LIMIT ?", (EMBED_BATCH,)).fetchall()
        if todo:
            vecs = self.embed([t for _, t in todo])
            with db:
                db.executemany("UPDATE items SET vec=? WHERE id=?", [(v.tobytes(), i) for (i, _), v in zip(todo, vecs)])
            self._append([i for i, _ in todo], vecs)
        return len(todo)

    def _append(self, ids, vecs):
        if not ids:
            return
        with self.lock:
            self.ids = np.concatenate([self.ids, np.array(ids, dtype=np.int64)])
            self.mat = np.vstack([self.mat, np.stack(vecs).astype(np.float32)])

    def search(self, query: str, limit: int) -> list[list]:
        q = self.embed([query])[0]
        with self.lock:
            if not len(self.ids):
                return []
            sims = self.mat @ q
            top = np.argsort(-sims)[:limit]
            return [[int(self.ids[i]), float(sims[i])] for i in top]


def handle(conn, index, token, used):
    with conn:
        conn.settimeout(5)
        req = json.loads(conn.makefile("r", encoding="utf-8").readline() or "{}")
        if not secrets.compare_digest(str(req.get("token", "")), token):
            return
        if req.get("op") == "search":
            used[0] = time.monotonic()
            reply = {"hits": index.search(str(req.get("q", ""))[:2000], int(req.get("limit", 30)))}
        else:
            reply = {"ok": True}
        conn.sendall((json.dumps(reply) + "\n").encode())


def main() -> int:
    data = ensure_data_dir()
    if not resources.can_spawn():
        return 0  # no room for a 300 MB model right now; recall stays keyword-only and the doctor retries later
    port = semantic.port_for(data)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":  # rebind straight after a restart; on Windows this flag would let two servers share
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return 0  # another session's server already has it
    sock.listen(16)
    token = semantic.write_token(data)

    from fastembed import TextEmbedding
    cache = data.parent.parent / "models" if data.parent.name == "profiles" else data / "models"  # one model for all profiles
    index = Index(TextEmbedding(semantic.MODEL, cache_dir=str(cache)))

    def sync_loop():
        db = memory.connect()  # sqlite connections belong to the thread that made them
        while True:
            try:
                if index.sync(db) == 0:
                    time.sleep(SYNC_SECONDS)
            except Exception as e:  # keep serving; the doctor reads this log
                print(f"lantern serve: sync failed: {e}", file=sys.stderr)
                time.sleep(SYNC_SECONDS)

    threading.Thread(target=sync_loop, daemon=True).start()
    used = [time.monotonic()]
    born = {"code": _code_stamp(), "wall": time.time()}

    def watchdog():
        # A server once ran 22 h at 1.2 GB after it should have gone idle: its clients had moved to a new port, and a
        # process that has run the model can hang on its way out. The watchdog decides from outside the accept loop
        # and ends the process outright.
        while True:
            time.sleep(WATCH_SECONDS)
            why = stale(time.monotonic() - used[0], time.time() - born["wall"], born["code"], _code_stamp(), port,
                        semantic.port_for(data), token, _read(semantic._token_path(data)))
            if why:
                print(f"lantern serve: exiting, {why}", file=sys.stderr, flush=True)
                os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()
    sock.settimeout(60)
    while True:
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(conn, index, token, used), daemon=True).start()


WATCH_SECONDS = 30
MAX_HOURS = 12  # even a busy server is replaced twice a day: a long-lived process is where leaks collect


def _code_stamp() -> float:
    return max(f.stat().st_mtime for f in [Path(__file__), *Path(__file__).parent.joinpath("lantern_core").glob("*.py")])


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def stale(idle_s: float, age_s: float, code_then: float, code_now: float, port: int, port_now: int, token: str, token_now: str) -> str | None:
    """Why this server should stop now, or None. Pure, so it is tested without a model."""
    if idle_s > IDLE_SECONDS:
        return f"no search for {idle_s / 60:.0f} min"
    if age_s > MAX_HOURS * 3600:
        return f"running {age_s / 3600:.0f} h; the next prompt starts a fresh one"
    if code_now > code_then:
        return "Lantern was updated; the next prompt starts a server on the new code"
    if port_now != port:
        return "clients now look for the server on another port"
    if token_now != token:
        return "another server took over this folder's key"
    return None


if __name__ == "__main__":
    sys.exit(main())
