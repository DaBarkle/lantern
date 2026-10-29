# /// script
# requires-python = ">=3.11"
# dependencies = ["fastembed>=0.7", "numpy>=1.26"]
# ///
"""Lantern memory server: keeps the embedding model warm so per-prompt semantic search takes milliseconds.

Started by the plugin monitor in every session; only the first one binds the port, the rest exit.
Listens on 127.0.0.1 only and answers only requests carrying the token from the user's data folder.
"""
import json
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from lantern_core import memory, semantic  # noqa: E402
from lantern_core.paths import ensure_data_dir  # noqa: E402

SYNC_SECONDS = 10
EMBED_BATCH = 64


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


def handle(conn, index, token):
    with conn:
        conn.settimeout(5)
        req = json.loads(conn.makefile("r", encoding="utf-8").readline() or "{}")
        if not secrets.compare_digest(str(req.get("token", "")), token):
            return
        if req.get("op") == "search":
            reply = {"hits": index.search(str(req.get("q", ""))[:2000], int(req.get("limit", 30)))}
        else:
            reply = {"ok": True}
        conn.sendall((json.dumps(reply) + "\n").encode())


def main() -> int:
    data = ensure_data_dir()
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
    index = Index(TextEmbedding(semantic.MODEL, cache_dir=str(data / "models")))

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
    while True:
        conn, _ = sock.accept()
        threading.Thread(target=handle, args=(conn, index, token), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
